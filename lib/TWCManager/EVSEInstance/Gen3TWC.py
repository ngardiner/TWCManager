"""
EVSEInstance implementation for Tesla Wall Connector Generation 3 devices.

Gen3 TWCs are self-contained units with their own Wi-Fi connectivity and a
local HTTP API.  They do not participate in the TWC RS485 master/slave
protocol.  Instead, they self-regulate charge current by polling a Neurio
Modbus energy meter to measure available headroom on the circuit.

TWCManager controls a Gen3 by manipulating a *fake* Neurio server
(Gen3TWCs EVSEController) to present a synthetic house load that creates
exactly the headroom we want the TWC to charge at.

State readback uses the Gen3 HTTP vitals endpoint::

    GET http://<ip>/api/1/vitals

which returns a JSON object including:
    vehicle_connected   bool
    vehicle_current_a   float
    grid_v              float
    evse_state          str  ("charging", "not_charging", ...)

Protocol references (no code copied):
  - Klangen82/tesla-wall-connector-control (vitals API format)
  - LucaTNT Gist 4adf01a7252386559070023612efa117 (Neurio control approach)
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance

if TYPE_CHECKING:
    from TWCManager.EVSEController.Gen3TWCs import Gen3TWCs

logger = logging.getLogger(__name__.rpartition(".")[2])

# How many seconds a failed vitals fetch is cached before retry
_VITALS_CACHE_TTL = 10.0
# How many seconds a successful vitals response is cached
_VITALS_SUCCESS_TTL = 5.0


class Gen3TWC(EVSEInstance):
    """A Gen3 TWC controlled indirectly via Neurio Modbus emulation.

    Power level is set by writing synthetic house-load values to the fake
    Neurio server.  Actual charge state is read back from the Gen3 HTTP
    vitals API.
    """

    def __init__(self, device_cfg: dict, controller: "Gen3TWCs", master):
        """
        Args:
            device_cfg:  Per-device config dict (``ip``, ``fuseAmps``, ``phases``).
            controller:  The Gen3TWCs controller that owns the Modbus server.
            master:      TWCMaster instance for config/settings.
        """
        self.device_cfg = device_cfg
        self.controller = controller
        self.master = master
        self.configConfig = master.config.get("config", {})

        self._ip: str = device_cfg.get("ip", "")
        self._fuse_amps: float = float(device_cfg.get("fuseAmps", 48))
        self._phases: int = int(device_cfg.get("phases", 1))

        # Vitals cache
        self._vitals: dict = {}
        self._vitals_ts: float = 0.0
        self._vitals_ok: bool = False

    # ------------------------------------------------------------------
    # Vitals fetch helper
    # ------------------------------------------------------------------

    def _fetch_vitals(self) -> dict:
        """Return the cached vitals dict, refreshing if stale."""
        now = time.monotonic()
        ttl = _VITALS_SUCCESS_TTL if self._vitals_ok else _VITALS_CACHE_TTL
        if now - self._vitals_ts < ttl and self._vitals:
            return self._vitals

        try:
            import urllib.request
            import json

            url = f"http://{self._ip}/api/1/vitals"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            self._vitals = data
            self._vitals_ts = now
            self._vitals_ok = True
        except Exception as exc:
            logger.debug("Gen3TWC %s vitals fetch failed: %s", self._ip, exc)
            self._vitals_ok = False
            self._vitals_ts = now  # backoff

        return self._vitals

    @property
    def isReachable(self) -> bool:
        """True when the Gen3 vitals endpoint responded successfully recently."""
        self._fetch_vitals()
        return self._vitals_ok

    # ------------------------------------------------------------------
    # EVSEInstance: identity
    # ------------------------------------------------------------------

    @property
    def ID(self) -> str:
        return f"Gen3TWC-{self._ip}"

    # ------------------------------------------------------------------
    # EVSEInstance: capabilities
    # ------------------------------------------------------------------

    @property
    def isReadOnly(self) -> bool:
        return False

    @property
    def isLocal(self) -> bool:
        # Gen3 TWCs are on-premise but not RS485 local; treat as non-local so
        # MergedEVSE prefers the Gen2 RS485 view when both exist.
        return False

    # ------------------------------------------------------------------
    # EVSEInstance: state
    # ------------------------------------------------------------------

    @property
    def isCharging(self) -> bool:
        v = self._fetch_vitals()
        state = v.get("evse_state", "")
        return str(state).lower() == "charging"

    @property
    def wantsToCharge(self) -> bool:
        """True when a vehicle is plugged in (connected) and not fully charged."""
        v = self._fetch_vitals()
        connected = v.get("vehicle_connected", False)
        state = str(v.get("evse_state", "")).lower()
        # "not_charging" can mean plugged-in-but-waiting; "charging" = active.
        # Exclude states that indicate completed / unplugged.
        not_done = state not in ("", "not_connected", "complete")
        return bool(connected) and not_done

    @property
    def currentAmps(self) -> float:
        v = self._fetch_vitals()
        return float(v.get("vehicle_current_a", 0.0))

    @property
    def currentVoltage(self) -> list:
        """Per-phase voltages [A, B, C]."""
        v = self._fetch_vitals()
        voltage = float(v.get("grid_v", 0.0))
        if voltage < 90:
            voltage = float(self.configConfig.get("defaultVoltage", 240))
        return [
            voltage,
            voltage if self._phases > 1 else 0.0,
            voltage if self._phases > 2 else 0.0,
        ]

    @property
    def currentPower(self) -> float:
        return self.convertAmpsToWatts(self.currentAmps, self.currentVoltage)

    # ------------------------------------------------------------------
    # EVSEInstance: power limits
    # ------------------------------------------------------------------

    @property
    def minPower(self) -> float:
        # Gen3 minimum pilot is 6 A (IEC 61851)
        return self.convertAmpsToWatts(6, self.currentVoltage)

    @property
    def maxPower(self) -> float:
        return self.convertAmpsToWatts(self._fuse_amps, self.currentVoltage)

    # ------------------------------------------------------------------
    # EVSEInstance: optional properties
    # ------------------------------------------------------------------

    @property
    def currentVIN(self) -> str:
        # Gen3 vitals do not expose VIN; return empty string.
        return ""

    @property
    def controllers(self) -> list:
        return ["Gen3TWCs"]

    # ------------------------------------------------------------------
    # EVSEInstance: control
    # ------------------------------------------------------------------

    def setTargetPower(self, watts: float) -> None:
        """Set the Gen3 charge rate by writing a synthetic house load.

        The Neurio emulator serves: house_watts = fuse_watts - target_watts.
        The Gen3 TWC reads that house load, subtracts it from its configured
        fuse capacity, and charges at the resulting headroom.

        Args:
            watts: Desired charge power in watts.
        """
        voltages = self.currentVoltage
        voltage = next(
            (v for v in voltages if v > 0), self.configConfig.get("defaultVoltage", 240)
        )

        fuse_watts = self._fuse_amps * voltage * self._phases
        house_watts = max(0.0, fuse_watts - watts)

        logger.debug(
            "Gen3TWC %s: target=%.0f W -> house_load=%.0f W (fuse=%.0f W)",
            self._ip,
            watts,
            house_watts,
            fuse_watts,
        )

        self.controller.setHouseWatts(
            watts=house_watts,
            phases=self._phases,
            voltage=voltage,
        )

    def startCharging(self) -> None:
        """Charging starts automatically when the TWC sees available headroom."""

    def stopCharging(self) -> None:
        """Stop charging by zeroing the headroom (present full fuse as house load)."""
        voltages = self.currentVoltage
        voltage = next(
            (v for v in voltages if v > 0), self.configConfig.get("defaultVoltage", 240)
        )
        fuse_watts = self._fuse_amps * voltage * self._phases
        self.controller.setHouseWatts(
            watts=fuse_watts,
            phases=self._phases,
            voltage=voltage,
        )
        logger.debug(
            "Gen3TWC %s: stopCharging -> house_load=%.0f W", self._ip, fuse_watts
        )
