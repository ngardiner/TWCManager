"""
EVSEInstance implementation for Tesla vehicles managed via the Fleet API.

Wraps a TeslaAPI vehicle object and exposes it through the unified
EVSEInstance interface so the power distribution algorithm can control
API-connected vehicles the same way it controls local RS485 TWC slaves.

Ported from ngardiner/TWCManager#483 (MikeBishop); adapted for current main.
"""

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance


class TeslaAPIEVSE(EVSEInstance):
    """A Tesla vehicle accessible via the Fleet API.

    Unlike Gen2TWC (which is a physical charging station), TeslaAPIEVSE
    represents the vehicle itself.  Charge rate control goes through the
    Tesla cloud API rather than the RS485 heartbeat protocol.
    """

    def __init__(self, vehicle, controller, master):
        """
        Args:
            vehicle: TeslaAPI.CarApiVehicle instance.
            controller: The TeslaAPIController that owns this EVSE.
            master: TWCMaster instance (for config and background tasks).
        """
        self.vehicle = vehicle
        self.controller = controller
        self.master = master
        self.configConfig = master.config.get("config", {})

    # ------------------------------------------------------------------
    # EVSEInstance: identity
    # ------------------------------------------------------------------

    @property
    def ID(self) -> str:
        return "TeslaAPI-%s" % self.vehicle.VIN

    # ------------------------------------------------------------------
    # EVSEInstance: capabilities
    # ------------------------------------------------------------------

    @property
    def isReadOnly(self) -> bool:
        return False

    @property
    def isLocal(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # EVSEInstance: state
    # ------------------------------------------------------------------

    @property
    def isCharging(self) -> bool:
        return self.vehicle.chargingState == "Charging"

    @property
    def wantsToCharge(self) -> bool:
        """True when the vehicle is plugged in and not yet full.

        "Stopped" means the charge session is paused (e.g. below the
        scheduled start time or waiting for sufficient solar power) — the
        vehicle is willing to accept power if offered.
        """
        return self.vehicle.chargingState in ("Charging", "Stopped")

    @property
    def currentAmps(self) -> float:
        return self.vehicle.actualCurrent

    @property
    def currentVoltage(self) -> list:
        """Per-phase voltage [A, B, C].

        Falls back to the configured default when the API reports an
        implausibly low value (< 90 V) — this happens when the vehicle is
        asleep or before the first vehicle_data poll completes.
        """
        voltage = self.vehicle.voltage
        if voltage < 90:
            voltage = self.configConfig.get("defaultVoltage", 240)
        phases = self.vehicle.phases or self.configConfig.get("numberOfPhases", 1)
        return [
            voltage,
            voltage if phases > 1 else 0,
            voltage if phases > 2 else 0,
        ]

    @property
    def currentPower(self) -> float:
        return self.convertAmpsToWatts(self.currentAmps, self.currentVoltage)

    # ------------------------------------------------------------------
    # EVSEInstance: power limits
    # ------------------------------------------------------------------

    @property
    def minPower(self) -> float:
        # Tesla vehicles accept as low as 5 A (car-side minimum)
        return self.convertAmpsToWatts(5, self.currentVoltage)

    @property
    def maxPower(self) -> float:
        return self.convertAmpsToWatts(self.vehicle.availableCurrent, self.currentVoltage)

    # ------------------------------------------------------------------
    # EVSEInstance: optional properties
    # ------------------------------------------------------------------

    @property
    def currentVIN(self) -> str:
        return self.vehicle.VIN

    @property
    def controllers(self) -> list:
        return ["TeslaAPIController"]

    # ------------------------------------------------------------------
    # EVSEInstance: control
    # ------------------------------------------------------------------

    def setTargetPower(self, watts: float) -> None:
        amps = self.convertWattsToAmps(watts, self.currentVoltage)
        carapi = self.master.getModuleByName("TeslaAPI")
        if carapi:
            carapi.setChargeRate(amps, vehicle=self.vehicle)

    def startCharging(self) -> None:
        self.master.startCarsCharging(self.vehicle.VIN)

    def stopCharging(self) -> None:
        self.master.stopCarsCharging(self.vehicle.VIN)
