"""
EVSEController for Tesla Wall Connector Generation 3 devices.

Gen3 TWCs do not use the RS485 TWC master/slave protocol.  Instead, each
unit acts as a Modbus RTU *client* that polls a Neurio energy meter to
determine how much headroom is available on the electrical circuit and
self-regulates its charge current accordingly.

TWCManager exploits this by running a fake Neurio Modbus RTU *server* on
the same serial bus.  When the controller wants a Gen3 TWC to charge at
``W`` watts, it computes a synthetic "house load" of
``fuse_watts - W`` and writes that into the Neurio holding-register map.
The TWC reads that load, subtracts it from its configured fuse capacity,
and charges at (up to) the resulting headroom - so the charger ends up at
the target power without any proprietary command protocol.

Protocol references (no code copied):
  - Klangen82/tesla-wall-connector-control (Modbus register map)
  - LucaTNT Gist 4adf01a7252386559070023612efa117 (FP32 encoding, register addresses)

Configuration (under ``controller.Gen3TWCs`` in config.json)::

    "controller.Gen3TWCs": {
        "enabled": true,
        "port": "/dev/ttyUSB1",
        "baudrate": 115200,
        "stopbits": 1,
        "bytesize": 8,
        "parity": "N",
        "devices": [
            {
                "ip": "192.168.1.100",
                "fuseAmps": 48,
                "phases": 1
            }
        ]
    }

The ``devices`` list enumerates every Gen3 TWC on the bus.  Each entry
maps to a ``Gen3TWC`` EVSEInstance.  If a device is unreachable (no vitals
response) it is omitted from ``allEVSEs`` for that polling cycle.

Multiple Gen3 devices sharing the same Neurio bus see the same fake house
load.  In a single-device deployment this is exact.  In multi-device
deployments the distributor should allocate the *total* Gen3 target to the
sum of all units; since each unit self-limits by the same headroom, they
naturally share it equally (assuming identical fuse ratings).
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
from typing import TYPE_CHECKING

from TWCManager.EVSEController.EVSEController import EVSEController

if TYPE_CHECKING:
    from TWCManager.EVSEInstance.Gen3TWC import Gen3TWC as Gen3TWCType

logger = logging.getLogger(__name__.rpartition(".")[2])


# ---------------------------------------------------------------------------
# Neurio register map constants
# ---------------------------------------------------------------------------

# Holding-register base addresses (0-based, i.e. subtract 1 from the
# datasheet address when using pymodbus address arguments).
#
# Power registers (IEEE 754 FP32, big-endian pair)
_REG_CT1_POWER = 0x88  # 136
_REG_CT2_POWER = 0x8A  # 138
_REG_CT3_POWER = 0x8C  # 140
_REG_TOTAL_POWER = 0x90  # 144

# Current registers (IEEE 754 FP32, big-endian pair)
_REG_CT1_CURRENT = 0xF4  # 244
_REG_CT2_CURRENT = 0xF6  # 246
_REG_CT3_CURRENT = 0xF8  # 248
_REG_TOTAL_CURRENT = 0xFC  # 252

# Number of holding registers to allocate (covers all addresses we serve)
_NUM_REGISTERS = 0x100  # 256 - covers all Neurio addresses

# Identity string registers (ASCII, 2 chars per register, 0-padded)
_IDENTITY_MAP: list[tuple[int, str]] = [
    (1, "ENC"),  # manufacturer   (regs 1-10)
    (11, "WBI1001-CCDC"),  # model          (regs 11-16)
    (29, "PWR"),  # (regs 29-31)
    (32, "4.2.9.9"),  # firmware ver   (regs 32-38)
    (47, "000000000"),  # serial         (regs 47-55)
]

# Registers that should read as 0xFFFF (Neurio "not present" sentinel)
_FFFF_RANGES: list[tuple[int, int]] = [
    (17, 20),  # regs 17-20
    (28, 28),  # reg  28
    (39, 46),  # regs 39-46
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fp32_pair(value: float) -> tuple[int, int]:
    """Encode a float as two big-endian 16-bit Modbus registers (IEEE 754)."""
    raw = struct.pack(">f", float(value))
    return (raw[0] << 8) | raw[1], (raw[2] << 8) | raw[3]


def _str_to_regs(s: str) -> list[int]:
    """Encode an ASCII string as a list of 16-bit registers (2 chars each)."""
    # Pad to even length
    if len(s) % 2:
        s += "\x00"
    regs = []
    for i in range(0, len(s), 2):
        regs.append((ord(s[i]) << 8) | ord(s[i + 1]))
    return regs


def _build_static_registers() -> list[int]:
    """Return the 256-register static base image for a Neurio meter."""
    regs: list[int] = [0] * _NUM_REGISTERS

    # Identity strings
    for base_addr, text in _IDENTITY_MAP:
        encoded = _str_to_regs(text)
        for idx, val in enumerate(encoded):
            if base_addr + idx < _NUM_REGISTERS:
                regs[base_addr + idx] = val

    # 0xFFFF sentinel blocks
    for start, end in _FFFF_RANGES:
        for addr in range(start, end + 1):
            if addr < _NUM_REGISTERS:
                regs[addr] = 0xFFFF

    return regs


# ---------------------------------------------------------------------------
# Gen3TWCs EVSEController
# ---------------------------------------------------------------------------


class Gen3TWCs(EVSEController):
    """EVSEController for Gen3 Tesla Wall Connectors via Neurio Modbus emulation."""

    name = "Gen3TWCs"

    def __init__(self, master):
        self.master = master
        config = master.config.get("controller.Gen3TWCs", {})

        self._port: str = config.get("port", "/dev/ttyUSB1")
        self._baudrate: int = int(config.get("baudrate", 115200))
        self._stopbits: int = int(config.get("stopbits", 1))
        self._bytesize: int = int(config.get("bytesize", 8))
        self._parity: str = config.get("parity", "N")
        self._devices: list = config.get("devices", [])

        # Shared register image - mutated by setHouseWatts(), read by the
        # Modbus action callback.  Protected by _lock.
        self._lock = threading.Lock()
        self._registers: list[int] = _build_static_registers()

        # Per-EVSEInstance cache: ip -> Gen3TWC
        self._evse_cache: dict = {}

        # Background thread running the asyncio Modbus server
        self._server_thread: threading.Thread | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None

        self._start_server()

    # ------------------------------------------------------------------
    # Modbus server internals
    # ------------------------------------------------------------------

    def setHouseWatts(
        self, watts: float, phases: int = 1, voltage: float = 240.0
    ) -> None:
        """Update the synthetic house-load registers served to Gen3 TWCs.

        The Gen3 TWC reads this value and charges at
        ``fuse_capacity - house_watts``.  Call this from ``Gen3TWC.setTargetPower``
        with ``house_watts = fuse_watts - target_watts``.

        Args:
            watts:   Total synthetic house load in watts.
            phases:  Number of active phases (1 or 3); distributes watts evenly.
            voltage: Per-phase voltage (used to compute per-CT current values).
        """
        phase_watts = watts / max(phases, 1)
        phase_amps = phase_watts / max(voltage, 1.0)
        total_current = phase_amps * phases

        with self._lock:
            r = self._registers

            def _wr(addr: int, val: float) -> None:
                hi, lo = _fp32_pair(val)
                r[addr] = hi
                r[addr + 1] = lo

            _wr(_REG_CT1_POWER, phase_watts if phases >= 1 else 0.0)
            _wr(_REG_CT2_POWER, phase_watts if phases >= 2 else 0.0)
            _wr(_REG_CT3_POWER, phase_watts if phases >= 3 else 0.0)
            _wr(_REG_TOTAL_POWER, watts)

            _wr(_REG_CT1_CURRENT, phase_amps if phases >= 1 else 0.0)
            _wr(_REG_CT2_CURRENT, phase_amps if phases >= 2 else 0.0)
            _wr(_REG_CT3_CURRENT, phase_amps if phases >= 3 else 0.0)
            _wr(_REG_TOTAL_CURRENT, total_current)

        logger.debug(
            "Gen3TWCs Neurio registers updated: %.0f W (%.1f A/phase x %d phase(s))",
            watts,
            phase_amps,
            phases,
        )

    def _make_action(self):
        """Return the async action callback for pymodbus SimDevice.

        The action is invoked by pymodbus on every register read.  It copies
        the current register image into the mutable ``current_registers`` list
        so the TWC always receives up-to-date values.
        """

        async def _action(
            function_code: int,
            start_address: int,
            address: int,
            count: int,
            current_registers: list,
            set_values,
        ) -> None:
            with self._lock:
                snapshot = self._registers[:]
            for i in range(count):
                reg_addr = address + i
                if 0 <= reg_addr < len(snapshot):
                    current_registers[i] = snapshot[reg_addr]

        return _action

    def _start_server(self) -> None:
        """Launch the Modbus RTU server in a daemon background thread."""
        try:
            from pymodbus.simulator import SimData, SimDevice, DataType
            from pymodbus.server import ModbusSerialServer
            from pymodbus.framer import FramerType
        except ImportError as exc:
            logger.error(
                "Gen3TWCs: pymodbus is not installed - Gen3 TWC support unavailable (%s)",
                exc,
            )
            return

        action = self._make_action()
        device = SimDevice(
            id=1,
            simdata=[
                SimData(
                    address=0,
                    count=_NUM_REGISTERS,
                    values=0,
                    datatype=DataType.REGISTERS,
                )
            ],
            action=action,
        )

        async def _serve():
            try:
                server = ModbusSerialServer(
                    device,
                    framer=FramerType.RTU,
                    port=self._port,
                    baudrate=self._baudrate,
                    stopbits=self._stopbits,
                    bytesize=self._bytesize,
                    parity=self._parity,
                    timeout=1,
                )
                await server.serve_forever()
            except Exception as exc:
                logger.error("Gen3TWCs Modbus server error: %s", exc)

        def _thread_main():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            self._server_loop = loop
            try:
                loop.run_until_complete(_serve())
            finally:
                loop.close()

        self._server_thread = threading.Thread(
            target=_thread_main,
            name="Gen3TWCs-modbus",
            daemon=True,
        )
        self._server_thread.start()
        logger.debug(
            "Gen3TWCs: Neurio Modbus RTU server started on %s @ %d baud",
            self._port,
            self._baudrate,
        )

    # ------------------------------------------------------------------
    # EVSEController interface
    # ------------------------------------------------------------------

    @property
    def allEVSEs(self) -> list:
        """Return a Gen3TWC EVSEInstance for each configured device."""
        from TWCManager.EVSEInstance.Gen3TWC import Gen3TWC

        evses = []
        for device_cfg in self._devices:
            ip = device_cfg.get("ip", "")
            if not ip:
                continue
            if ip not in self._evse_cache:
                self._evse_cache[ip] = Gen3TWC(device_cfg, self, self.master)
            evse = self._evse_cache[ip]
            if evse.isReachable:
                evses.append(evse)
        return evses

    @property
    def maxPower(self) -> float:
        """Maximum power: sum of all configured device fuse capacities."""
        config_cfg = self.master.config.get("config", {})
        voltage = config_cfg.get("defaultVoltage", 240)
        total = 0.0
        for dev in self._devices:
            fuse = dev.get("fuseAmps", 48)
            phases = dev.get("phases", 1)
            total += fuse * voltage * phases
        return total if total > 0 else float("inf")

    def stop(self) -> None:
        """Signal the background server thread to stop."""
        if self._server_loop and self._server_loop.is_running():
            self._server_loop.call_soon_threadsafe(self._server_loop.stop)
