"""
EVSEInstance implementation for Tesla Wall Connector Generation 2 slave devices.

Extends TWCSlave with the unified EVSEInstance interface so that Gen2 TWCs can
participate in the controller-agnostic power distribution algorithm.

Ported from ngardiner/TWCManager#483 (MikeBishop); adapted for current main.
"""

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.TWCSlave import TWCSlave
from TWCManager.Vehicle.Registry.VehicleRegistry import ANON_PREFIX


class Gen2TWC(EVSEInstance, TWCSlave):
    """A Gen2 TWC slave device managed over RS485.

    This class inherits all RS485 protocol behaviour from TWCSlave and adds
    the EVSEInstance interface on top, allowing the controller-agnostic power
    distribution loop to treat Gen2 devices the same as any other EVSE.
    """

    # Heartbeat states in which a car is plugged in
    PLUGGED_STATES = (0x01, 0x03, 0x04, 0x05, 0x08, 0x09)

    # Set by ChargingPoint.setTargetPower each pass: the point has a car
    # member, which owns the car's amps; the heartbeat then writes none
    pointOwnsAmps = False

    def __init__(self, TWCID, maxAmps, config, master):
        # Backing stores for properties that shadow TWCSlave class attributes.
        # These must be initialised before TWCSlave.__init__ runs so that any
        # property setter triggered during super().__init__ has somewhere to write.
        self._gen2_isCharging = 0
        self._gen2_currentVIN = ""

        # Per-EVSE amps target set by distributeEVSEPower() (Phase 4).
        # When not None, send_master_heartbeat() uses this value directly
        # instead of recomputing the per-car fair share.
        self._evseTargetAmps = None

        TWCSlave.__init__(self, TWCID, maxAmps, config, master)

    # ------------------------------------------------------------------
    # EVSEInstance: identity
    # ------------------------------------------------------------------

    @property
    def ID(self) -> str:
        return "Gen2TWC-%02X%02X" % (self.TWCID[0], self.TWCID[1])

    # ------------------------------------------------------------------
    # EVSEInstance: capabilities
    # ------------------------------------------------------------------

    @property
    def twcid(self) -> str:
        return "%02X%02X" % (self.TWCID[0], self.TWCID[1])

    @property
    def isReadOnly(self) -> bool:
        return False

    @property
    def isLocal(self) -> bool:
        return True

    # ------------------------------------------------------------------
    # EVSEInstance: state
    #
    # isCharging and currentVIN are class-level attributes on TWCSlave that
    # are mutated via plain assignment throughout the codebase.  We expose
    # them as properties with setters so existing code keeps working while
    # the EVSEInstance interface can read them polymorphically.
    # ------------------------------------------------------------------

    @property
    def isCharging(self) -> bool:
        return bool(self._gen2_isCharging)

    @isCharging.setter
    def isCharging(self, value):
        self._gen2_isCharging = value

    @property
    def currentVIN(self) -> str:
        return self._gen2_currentVIN

    @currentVIN.setter
    def currentVIN(self, value):
        self._gen2_currentVIN = value

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
    def currentAmps(self) -> float:
        return self.reportedAmpsActual

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

    @property
    def currentPower(self) -> float:
        return self.convertAmpsToWatts(self.reportedAmpsActual, self.currentVoltage)

    # ------------------------------------------------------------------
    # EVSEInstance: amps range
    # ------------------------------------------------------------------

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
        # never below what the TWC can start a car at. The heartbeat zeroes
        # any offer below this while no car charges.
        override = self._rangeOverride("minAmps")
        if override is None:
            return TWCSlave.minAmpsToOffer(self)
        return max(override, self.minAmpsTWCSupports)

    def layerFloorAmps(self):
        # Mode 3 hands the rate to the car below this: the heartbeat's
        # literal 6 A handoff, never below the TWC's own minimum
        return max(6, self.minAmpsToOffer())

    @property
    def minAmps(self) -> float:
        # What the heartbeat will really send, so an idle TWC's minimum
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

    # ------------------------------------------------------------------
    # EVSEInstance: controller membership
    # ------------------------------------------------------------------

    @property
    def controllers(self) -> list:
        return ["Gen2TWCs"]

    # ------------------------------------------------------------------
    # EVSEInstance: control (start and stop are the heartbeat's)
    # ------------------------------------------------------------------

    def setTargetPower(self, watts: float) -> None:
        """Convert watts to amps and store as the centralized amps target.

        Sets ``_evseTargetAmps`` which send_master_heartbeat() picks up in
        place of the per-car fair-share calculation, giving the centralized
        distributor full control over this slave's charge rate.
        """
        amps = self.convertWattsToAmps(watts, self.currentVoltage)
        self._evseTargetAmps = amps
        self.lastAmpsDesired = amps  # kept for compatibility / logging
