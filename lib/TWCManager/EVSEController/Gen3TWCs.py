"""
The controller for Tesla Wall Connector Generation 3 devices.

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
maps to a ``Gen3TWC`` endpoint.  If a device is unreachable (no vitals
response) it is omitted from ``allEVSEs`` until a fetch succeeds.  The
vitals are fetched on the background thread (task ``gen3Vitals``, queued
here when a unit's cache is stale), never on the caller's thread.

Every Gen3 on the bus reads the same fake house load ``H`` and charges at
up to ``h_i = fuse_i - H``.  The distributor addresses each unit
separately, exactly as Gen2 slaves; this controller records each unit's own
target and water-fills one ``H`` so that the units' total draw matches the
sum of the targets (``solve``).  A unit whose car draws less than its
headroom frees that share for its siblings.  A unit with a zero target
cannot be stopped while a sibling holds one: it may still draw up to ``h``.
"""

from __future__ import annotations

import asyncio
import logging
import struct
import threading
from typing import TYPE_CHECKING, Dict, List, Optional

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


class Gen3TWCs:
    """Gen3 Tesla Wall Connectors via Neurio Modbus emulation.

    Registered as module type "EVSEController" under the name "Gen3TWCs".
    """

    name = "Gen3TWCs"
    # Republish only when the headroom moves by more than this (or the
    # targets change)
    DAMP_AMPS = 1.0
    # Every targeted unit keeps at least the minimum pilot
    MIN_AMPS = 6.0

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

        # Per-unit cache: ip -> Gen3TWC, built once here so every
        # thread sees the same instances
        from TWCManager.EVSEInstance.Gen3TWC import Gen3TWC

        self._evse_cache: dict = {}
        for device_cfg in self._devices:
            ip = device_cfg.get("ip", "")
            if ip and ip not in self._evse_cache:
                self._evse_cache[ip] = Gen3TWC(device_cfg, self, master)

        # Names a Gen3's car from the energy site's live_status (free)
        from TWCManager.Vehicle.Providers.WallConnectorVins import (
            WallConnectorVinReader,
        )

        self.vinReader = WallConnectorVinReader(
            master, getattr(master, "providerOrder", None)
        )

        # Each unit's own target in watts, and what was last published.
        # Guards the solve and the publish so two threads never publish out
        # of order; taken before _lock, never inside it.
        self._targetLock = threading.RLock()
        self._unitTargets: Dict[str, float] = {}
        self._publishedLoad: Optional[float] = None
        self._publishedTargets: Dict[str, float] = {}

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
        ``fuse_capacity - house_watts``.  ``republish`` calls this with the
        load ``solve`` chose for every unit's target.

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

    # ------------------------------------------------------------------
    # Per-unit targets and the one shared house load
    # ------------------------------------------------------------------

    def setUnitTarget(self, unitId: str, watts: float) -> None:
        """Record one unit's target and republish the shared house load."""
        with self._targetLock:
            self._unitTargets[unitId] = max(0.0, float(watts))
            self.republish()

    def noteVitals(self, unit, rising: bool) -> None:
        """Called by a unit after each successful vitals fetch (background
        thread, no lock held): closes the loop on the measured draw at the
        vitals cadence, then asks the energy site which car is connected
        while a session is anonymous."""
        self.republish()
        if unit._version is None and not unit.device_cfg.get("din"):
            unit.refreshVersion()
        if rising:
            self.vinReader.noteConnect(unit)
        self.vinReader.apply(self.devices())

    def _supply(self):
        """(phases, voltage) of the first device, which the published
        value uses."""
        devices = self.devices()
        if not devices:
            return 1, float(
                self.master.config.get("config", {}).get("defaultVoltage", 240)
            )
        first = devices[0]
        voltage = next(
            (v for v in first.currentVoltage if v > 0),
            float(self.master.config.get("config", {}).get("defaultVoltage", 240)),
        )
        return first.phases, voltage

    def _maxFuse(self) -> float:
        return max((d.fuseAmps for d in self.devices()), default=0.0)

    def _servedLoad(self) -> float:
        with self._targetLock:
            published = self._publishedLoad
        return self._maxFuse() if published is None else published

    def demandAmps(self, unit) -> float:
        """What a unit would draw from a headroom: its measured current when
        its car is holding below the published headroom, else unbounded."""
        if not unit.isReachable:
            return float("inf")
        vitals = unit.vitals
        try:
            measured = float(vitals.get("vehicle_current_a", 0.0))
        except (TypeError, ValueError):
            return float("inf")
        h = unit.fuseAmps - self._servedLoad()
        charging = str(vitals.get("evse_state", "")).lower() == "charging"
        if charging and 1.0 <= measured < h - self.DAMP_AMPS:
            return measured
        return float("inf")

    def solve(self) -> float:
        """The house load in amps whose headroom serves every target.

        Water-filling: the largest H at which the units with a target, each
        drawing min(demand, fuse - H), together draw the sum of the targets,
        clamped so every targeted unit keeps MIN_AMPS.  Units with no target
        are left out: they cannot be stopped through the shared value.
        """
        with self._targetLock:
            targets = dict(self._unitTargets)
            devices = self.devices()
            maxFuse = self._maxFuse()
            _, voltage = self._supply()
            active = []
            for unit in devices:
                watts = targets.get(unit.ID, 0.0)
                if watts > 0:
                    amps = watts / (voltage * max(unit.phases, 1))
                    active.append((unit.fuseAmps, self.demandAmps(unit), amps))
            if not active:
                return round(maxFuse, 2)
            wanted = sum(a for _, _, a in active)

            def served(load, demands):
                return sum(
                    min(d, max(0.0, f - load)) for (f, _, _), d in zip(active, demands)
                )

            demands = [d for _, d, _ in active]
            if served(0.0, demands) < wanted:
                # Measured draws below the targets even at the full fuse are
                # cars still ramping, not a binding cap: solve as if every
                # unit were unconstrained, so one unit gets its own target
                demands = [float("inf")] * len(active)
            lo, hi = 0.0, maxFuse
            if served(lo, demands) >= wanted:
                for _ in range(20):
                    mid = (lo + hi) / 2
                    if served(mid, demands) >= wanted:
                        lo = mid
                    else:
                        hi = mid
            load = min(round(lo, 2), min(f for f, _, _ in active) - self.MIN_AMPS)
            return round(max(0.0, load), 2)

    def republish(self) -> None:
        """Recompute the shared house load and publish it when the headroom
        moved by more than DAMP_AMPS or the targets changed."""
        with self._targetLock:
            if not self.devices():
                return
            load = self.solve()
            targets = {k: v for k, v in self._unitTargets.items() if v > 0}
            if (
                self._publishedLoad is not None
                and targets == self._publishedTargets
                and abs(load - self._publishedLoad) <= self.DAMP_AMPS
            ):
                return
            phases, voltage = self._supply()
            self._publishedLoad = load
            self._publishedTargets = targets
            logger.debug("Gen3TWCs: targets %s -> house load %.2f A", targets, load)
            # _lock is taken inside, after _targetLock: the order is fixed
            # load is amps per phase; setHouseWatts spreads watts over phases
            self.setHouseWatts(
                watts=load * voltage * phases, phases=phases, voltage=voltage
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
                "Gen3TWCs: Gen3 TWC support needs pymodbus 3.8 or later "
                "(Python 3.9 or later) - unavailable (%s)",
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

    def devices(self) -> List["Gen3TWCType"]:
        """Every configured unit, reachable or not."""
        return list(self._evse_cache.values())

    @property
    def allEVSEs(self) -> list:
        """The units whose last vitals fetch succeeded.  A stale cache queues
        one background fetch; nothing here does I/O."""
        devices = self.devices()
        if any(d.vitalsStale() for d in devices):
            self.master.queue_background_task({"cmd": "gen3Vitals"})
        return [d for d in devices if d.isReachable]

    def refreshVitals(self) -> None:
        """Fetch every stale unit's vitals.  Background thread only."""
        for device in self.devices():
            if device.vitalsStale():
                device.refreshVitals()

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
