"""
A Tesla Wall Connector Generation 3 as a charging endpoint.

Gen3 TWCs are self-contained units with their own Wi-Fi connectivity and a
local HTTP API.  They do not participate in the TWC RS485 master/slave
protocol.  Instead, they self-regulate charge current by polling a Neurio
Modbus energy meter to measure available headroom on the circuit.

TWCManager controls a Gen3 by manipulating a *fake* Neurio server
(Gen3TWCs) to present a synthetic house load that creates
exactly the headroom we want the TWC to charge at.

State readback uses the Gen3 HTTP vitals endpoint::

    GET http://<ip>/api/1/vitals

which returns a JSON object including:
    vehicle_connected   bool
    vehicle_current_a   float
    grid_v              float
    evse_state          str  ("charging", "not_charging", ...)

The fetch runs on the background thread only (``refreshVitals``, queued by
``Gen3TWCs.allEVSEs`` when the cache is stale); every property reads the
cache, so the serial loop never waits on HTTP.  Vitals carry no VIN: a
connect begins an anonymous plug session in the vehicle registry, and
``_correlate`` names the car when the evidence allows.  The energy site's
live_status may name it (``cloudVin``, set by the controller's
WallConnectorVinReader), matched by the unit's DIN: the ``din`` device key,
else ``<part_number>--<serial_number>`` from ``GET /api/1/version``
(unverified on hardware).

Protocol references (no code copied):
  - Klangen82/tesla-wall-connector-control (vitals API format)
  - LucaTNT Gist 4adf01a7252386559070023612efa117 (Neurio control approach)
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Optional

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.Vehicle.Presence import HOME, presence

if TYPE_CHECKING:
    from TWCManager.EVSEController.Gen3TWCs import Gen3TWCs

logger = logging.getLogger(__name__.rpartition(".")[2])

# How many seconds a failed vitals fetch is cached before retry
_VITALS_CACHE_TTL = 10.0
# How many seconds a successful vitals response is cached
_VITALS_SUCCESS_TTL = 5.0
# How many seconds a failed /api/1/version fetch waits before a retry
_VERSION_RETRY_SECONDS = 600.0


class Gen3TWC(EVSEInstance):
    """A Gen3 TWC controlled indirectly via Neurio Modbus emulation.

    Power level is set by writing synthetic house-load values to the fake
    Neurio server.  Actual charge state is read back from the Gen3 HTTP
    vitals API.
    """

    # Seconds within which a car's plug-in observation correlates it
    CORRELATE_WINDOW = 120.0
    # Vitals carry no VIN (currentVIN stays "")
    isReadOnly = False
    isLocal = True
    controllers = ["Gen3TWCs"]

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
        # vehicle_connected from the last successful fetch: plug edges
        self._connected: bool = False

        # The unit's own target in watts (the controller aggregates)
        self.lastTarget: float = 0.0
        # Set by the energy-site reader when the cloud names the car here
        self.cloudVin: Optional[str] = None
        # The /api/1/version answer; {} once fetched without a part number
        self._version: Optional[dict] = None
        # time.monotonic() before which a failed version fetch is not retried
        self._versionRetryAt: float = 0.0

    # ------------------------------------------------------------------
    # Vitals: the cache, and the background-thread fetch
    # ------------------------------------------------------------------

    @property
    def vitals(self) -> dict:
        """The cached vitals; never fetches."""
        return self._vitals

    def vitalsStale(self) -> bool:
        """True when the cache is older than its TTL (failures back off)."""
        ttl = _VITALS_SUCCESS_TTL if self._vitals_ok else _VITALS_CACHE_TTL
        return time.monotonic() - self._vitals_ts >= ttl

    def refreshVitals(self) -> bool:
        """Fetch the vitals over HTTP.  Background thread only."""
        now = time.monotonic()
        try:
            import urllib.request
            import json

            url = f"http://{self._ip}/api/1/vitals"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            if not isinstance(data, dict):
                raise ValueError("vitals is not an object")
        except Exception as exc:
            logger.debug("Gen3TWC %s vitals fetch failed: %s", self._ip, exc)
            self._vitals_ok = False
            self._vitals_ts = now  # backoff
            return False

        # A new dict, so a reader on another thread sees old or new whole
        self._vitals = data
        self._vitals_ts = now
        self._vitals_ok = True

        connected = bool(data.get("vehicle_connected", False))
        rising = connected and not self._connected
        falling = self._connected and not connected
        try:
            self._plugEdges(rising, falling, connected)
            # Only once the edge is handled: a failure is retried next fetch
            self._connected = connected
        except Exception as exc:
            logger.warning("Gen3TWC %s: registry update failed: %s", self._ip, exc)
        self.controller.noteVitals(self, rising)
        return True

    @property
    def din(self) -> Optional[str]:
        """The unit's DIN as the energy site lists it: the ``din`` device
        key, else ``<part_number>--<serial_number>`` from the version
        (an unverified guess at the format), else None."""
        configured = self.device_cfg.get("din")
        if isinstance(configured, str) and configured.strip():
            return configured.strip()
        version = self._version or {}
        part, serial = version.get("part_number"), version.get("serial_number")
        if part and serial:
            return f"{part}--{serial}"
        return None

    def refreshVersion(self) -> bool:
        """Fetch ``/api/1/version`` once.  Background thread only.  A
        failure leaves ``_version`` None and is retried after
        _VERSION_RETRY_SECONDS; a 404 is final (``{}``: no DIN)."""
        now = time.monotonic()
        if now < self._versionRetryAt:
            return False
        import urllib.error
        import urllib.request
        import json

        try:
            url = f"http://{self._ip}/api/1/version"
            with urllib.request.urlopen(url, timeout=3) as resp:
                data = json.loads(resp.read().decode())
            if not isinstance(data, dict):
                raise ValueError("version is not an object")
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Firmware without the endpoint: never ask again
                self._version = {}
                return True
            logger.debug("Gen3TWC %s version fetch failed: %s", self._ip, exc)
            self._versionRetryAt = now + _VERSION_RETRY_SECONDS
            return False
        except Exception as exc:
            logger.debug("Gen3TWC %s version fetch failed: %s", self._ip, exc)
            self._versionRetryAt = now + _VERSION_RETRY_SECONDS
            return False
        part, serial = data.get("part_number"), data.get("serial_number")
        if isinstance(part, str) and part and isinstance(serial, str) and serial:
            self._version = {"part_number": part, "serial_number": serial}
        else:
            self._version = {}
        return True

    def _registry(self):
        return getattr(self.master, "vehicleRegistry", None)

    def _plugEdges(self, rising: bool, falling: bool, connected: bool) -> None:
        registry = self._registry()
        if registry is None:
            return
        if rising:
            current = registry.vehicleAt(self.pointId)
            # A retried edge whose earlier attempt already named the car
            # (and then raised) must not open a second, orphan session
            if current is None or current.isAnonymous:
                registry.beginPlugSession(self.pointId)
                self._correlate()
        elif falling:
            self.cloudVin = None
            # Before the session ends, while the car is still at the point:
            # this unit's own pluggedIn=True would otherwise keep it a fresh
            # correlation candidate for the next car
            vehicle = registry.vehicleAt(self.pointId)
            reader = getattr(self.controller, "vinReader", None)
            if reader is not None:
                # live_status may still name this car when the next connects
                reader.noteDisconnect(self, None if vehicle is None else vehicle.key)
            if vehicle is not None:
                vehicle.observe("pluggedIn", False, "Gen3TWC")
            registry.endPlugSession(self.pointId)
        if connected:
            vehicle = registry.vehicleAt(self.pointId)
            if vehicle is not None:
                vehicle.observe("pluggedIn", True, "Gen3TWC")
                vehicle.observe("actualCurrent", self.currentAmps, "Gen3TWC")
                vehicle.observe("voltage", self.currentVoltage[0], "Gen3TWC")

    def _correlate(self) -> Optional[str]:
        """Name the car that just connected, or leave the session anonymous.

        The cloud VIN wins, and replaces a plug-in guess once the registry
        confirms it; otherwise exactly one known car whose plug-in was
        observed within CORRELATE_WINDOW, that is on no other point and is
        not known to be away, is this one (provisionally: the cloud may
        still correct it).
        """
        registry = self._registry()
        if registry is None:
            return None
        if self.cloudVin:
            current = registry.vehicleAt(self.pointId)
            guessKey = None if current is None or current.isAnonymous else current.key
            status = registry.observeTwcVin(self.pointId, self.cloudVin)
            if (
                status == "confirmed"
                and guessKey is not None
                and guessKey != self.cloudVin.upper()
            ):
                # The guess was wrong: it is no longer at this point, nor
                # plugged in as far as this unit knows
                guess = registry.get(guessKey)
                if guess is not None:
                    guess.observe("twcVin", None, "TWC", seen=False)
                    guess.observe("pluggedIn", False, "Gen3TWC")
                logger.info(
                    "Gen3TWC %s: the energy site names %s here, not %s",
                    self._ip,
                    self.cloudVin.upper(),
                    guessKey,
                )
            return self.cloudVin
        now = registry.now()
        candidates = []
        for vehicle in registry.vehicles():
            if vehicle.isAnonymous:
                continue
            obs = vehicle.latest("pluggedIn")
            if obs is None or obs.value is not True:
                continue
            if now - obs.at > self.CORRELATE_WINDOW:
                continue
            if vehicle.value("twcVin") is not None:
                continue
            api = vehicle.attachment("TeslaAPI")
            if api is not None and presence(self.master, vehicle) != HOME:
                continue
            candidates.append(vehicle.vin)
        if len(candidates) == 1:
            registry.observeTwcVin(self.pointId, candidates[0])
            return candidates[0]
        logger.info(
            "Gen3TWC %s: car connected, %d candidates; left anonymous",
            self._ip,
            len(candidates),
        )
        return None

    @property
    def isReachable(self) -> bool:
        """True when the last vitals fetch succeeded (cached)."""
        return self._vitals_ok

    @property
    def ID(self) -> str:
        return f"Gen3TWC-{self._ip}"

    @property
    def pointId(self) -> str:
        return self.ID

    @property
    def fuseAmps(self) -> float:
        return self._fuse_amps

    @property
    def phases(self) -> int:
        return self._phases

    @property
    def occupied(self):
        if not self._vitals_ok:
            return None
        return bool(self.vitals.get("vehicle_connected", False))

    @property
    def vehicleKey(self):
        registry = self._registry()
        vehicle = None if registry is None else registry.vehicleAt(self.pointId)
        return None if vehicle is None else vehicle.key

    @property
    def wantsToCharge(self) -> bool:
        """True when a vehicle is plugged in (connected) and not fully charged."""
        v = self.vitals
        connected = v.get("vehicle_connected", False)
        state = str(v.get("evse_state", "")).lower()
        # "not_charging" can mean plugged-in-but-waiting; "charging" = active.
        # Exclude states that indicate completed / unplugged.
        not_done = state not in ("", "not_connected", "complete")
        return bool(connected) and not_done

    @property
    def currentAmps(self) -> float:
        v = self.vitals
        return float(v.get("vehicle_current_a", 0.0))

    @property
    def currentVoltage(self) -> list:
        """Per-phase voltages [A, B, C]."""
        v = self.vitals
        voltage = float(v.get("grid_v", 0.0))
        if voltage < 90:
            voltage = float(self.configConfig.get("defaultVoltage", 240))
        return [
            voltage,
            voltage if self._phases > 1 else 0.0,
            voltage if self._phases > 2 else 0.0,
        ]

    @property
    def minPower(self) -> float:
        # Gen3 minimum pilot is 6 A (IEC 61851)
        return self.convertAmpsToWatts(6, self.currentVoltage)

    @property
    def maxPower(self) -> float:
        return self.convertAmpsToWatts(self._fuse_amps, self.currentVoltage)

    def setTargetPower(self, watts: float) -> None:
        """Record this unit's own target; the controller turns every unit's
        target into the one shared synthetic house load.

        Args:
            watts: Desired charge power in watts.
        """
        self.lastTarget = watts
        self.controller.setUnitTarget(self.ID, watts)
