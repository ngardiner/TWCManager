"""
A Gen2 TWC slave on RS-485 as a charging endpoint: TWCSlave's protocol
state, read as the distributor's endpoint.
"""

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.TWCSlave import TWCSlave
from TWCManager.Vehicle.Registry.VehicleRegistry import ANON_PREFIX


class Gen2TWC(EVSEInstance, TWCSlave):
    # Heartbeat states in which a car is plugged in
    PLUGGED_STATES = (0x01, 0x03, 0x04, 0x05, 0x08, 0x09)
    isReadOnly = False
    isLocal = True
    controllers = ["Gen2TWCs"]
    _isCharging = 0

    @property
    def ID(self) -> str:
        return "Gen2TWC-" + self.twcid

    @property
    def twcid(self) -> str:
        return "%02X%02X" % (self.TWCID[0], self.TWCID[1])

    @property
    def isCharging(self) -> bool:
        # The serial loop writes 0/1; /api/getSlaveTWCs publishes a bool
        return bool(self._isCharging)

    @isCharging.setter
    def isCharging(self, value):
        self._isCharging = value

    @property
    def vehicleKey(self):
        reg = self.master.vehicleRegistry
        anonKey = ANON_PREFIX + self.twcid
        if self.currentVIN and (
            reg.get(self.currentVIN.upper()) is not None
            or reg.isPending(self.twcid)
            or reg.get(anonKey) is None
        ):
            return self.currentVIN.upper()
        if reg.get(anonKey) is not None:
            # Also for a VIN the registry rejected: the session stays anonymous
            return anonKey
        if self.lastVIN and self.reportedState in self.PLUGGED_STATES:
            # The car last confirmed here has shown no unplug evidence
            return self.lastVIN.upper()
        return None

    @property
    def occupied(self):
        if (
            self.currentVIN
            or self.reportedAmpsActual >= 1.0
            or self.reportedState in self.PLUGGED_STATES
        ):
            return True
        # A Gen2 reports 00 for a sleeping plugged car, so 00 is not "empty"
        return None

    @property
    def wantsToCharge(self) -> bool:
        """True when a vehicle is plugged in and willing to accept power.

        TWC heartbeat: reportedAmpsMax is non-zero once the vehicle has
        communicated its maximum charge rate (i.e. it's plugged in and awake).
        """
        return self.reportedAmpsMax > 0 or self.reportedAmpsActual > 0

    @property
    def currentVoltage(self) -> list:
        """Per-phase voltages [A, B, C].  Falls back to configured default when
        the TWC has not yet reported voltage measurements."""
        phases = [self.voltsPhaseA, self.voltsPhaseB, self.voltsPhaseC]
        if not any(phases):
            default_v = self.configConfig.get("defaultVoltage", 240)
            count = self.configConfig.get("numberOfPhases", 1)
            phases = [default_v if i < count else 0 for i in range(3)]
        return phases

    def _rangeOverride(self, key):
        # controller.Gen2TWCs.ranges.<twcid>.<key>, when a positive number
        controller = self.master.config.get("controller.Gen2TWCs", {})
        ranges = controller.get("ranges", {}) if isinstance(controller, dict) else {}
        override = ranges.get(self.twcid, {}) if isinstance(ranges, dict) else {}
        value = override.get(key) if isinstance(override, dict) else None
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if value > 0:
                return value
        return None

    def minAmpsToOffer(self):
        # The per-TWC minAmps override replaces minAmpsPerTWC for this TWC,
        # never below what the TWC can start a car at. offerFor zeroes
        # any offer below this while no car charges.
        override = self._rangeOverride("minAmps")
        if override is None:
            return TWCSlave.minAmpsToOffer(self)
        return max(override, self.minAmpsTWCSupports)

    def layerFloorAmps(self):
        # Mode 3 hands the rate to the car below this: offerFor's
        # literal 6 A handoff, never below the TWC's own minimum
        return max(6, self.minAmpsToOffer())

    @property
    def minAmps(self) -> float:
        # What offerFor will really offer, so an idle TWC's minimum
        # reaches the wire
        return self.minAmpsToOffer()

    @property
    def maxOfferAmps(self) -> float:
        # TWCSlave.maxAmps is the linkready maximum; this is what we offer
        override = self._rangeOverride("maxAmps")
        if override is not None:
            return override
        return min(self.wiringMaxAmps, self.maxAmps or self.wiringMaxAmps)

    @property
    def minPower(self) -> float:
        return self.convertAmpsToWatts(self.minAmps, self.currentVoltage)

    @property
    def maxPower(self) -> float:
        return self.convertAmpsToWatts(self.maxOfferAmps, self.currentVoltage)

    def setTargetPower(self, watts: float) -> None:
        # The distributor's target in amps; the control tick makes the
        # offer from it (TWCSlave.offerFor) and decides start and stop
        self.pointAmps = self.convertWattsToAmps(watts, self.currentVoltage)
