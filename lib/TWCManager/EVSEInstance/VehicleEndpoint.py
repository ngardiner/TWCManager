"""
EVSEInstance for a registry vehicle whose charge rate a vehicle provider sets.

The endpoint reads only the registry's latest values and never calls a
provider: the distributor runs on the serial thread, so a rate is desired
state, which the reconciler delivers through the provider order (the local
path first; a paid provider only where no local one covers the rate).
"""

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.Vehicle.Providers.Capability import SET_AMPS
from TWCManager.Vehicle.Providers.Provider import vehicleMaxAmps
from TWCManager.Vehicle.Reconcile.Desired import AMPS
from TWCManager.Vehicle.Reconcile.Reconciler import known


class VehicleEndpoint(EVSEInstance):
    # Tesla vehicles accept as low as 5 A (car-side minimum)
    MIN_AMPS = 5

    def __init__(self, vehicle, order, master):
        self.vehicle = vehicle
        self.order = order
        self.master = master
        self.configConfig = master.config.get("config", {})

    # ------------------------------------------------------------------
    # EVSEInstance: identity
    # ------------------------------------------------------------------

    @property
    def ID(self) -> str:
        return "Vehicle-%s" % self.vehicle.vin

    # ------------------------------------------------------------------
    # EVSEInstance: capabilities
    # ------------------------------------------------------------------

    def _candidates(self):
        return self.order.candidates(self.vehicle, SET_AMPS)

    @property
    def provider(self) -> str:
        candidates = self._candidates()
        return candidates[0].name if candidates else ""

    @property
    def isReadOnly(self) -> bool:
        # No provider can set this car's amps (an Owner-only token, ble_only
        # without BLE), so the distributor must not budget power for it
        return not self._candidates()

    @property
    def isLocal(self) -> bool:
        return False

    # ------------------------------------------------------------------
    # EVSEInstance: state
    # ------------------------------------------------------------------

    @property
    def isCharging(self) -> bool:
        return self.vehicle.value("chargingState") == "Charging"

    @property
    def wantsToCharge(self) -> bool:
        # "Stopped" is a paused session: the car takes power if offered
        return self.vehicle.value("chargingState") in ("Charging", "Stopped")

    @property
    def currentAmps(self) -> float:
        return self.vehicle.value("actualCurrent", 0) or 0

    @property
    def currentVoltage(self) -> list:
        """Per-phase voltage [A, B, C].

        Falls back to the configured default when the car reports an
        implausibly low value (< 90 V), as it does asleep or before its first
        charge-state read.
        """
        voltage = self.vehicle.value("voltage", 0) or 0
        if voltage < 90:
            voltage = self.configConfig.get("defaultVoltage", 240)
        phases = self.vehicle.value("phases") or self.configConfig.get(
            "numberOfPhases", 1
        )
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
        return self.convertAmpsToWatts(self.MIN_AMPS, self.currentVoltage)

    @property
    def maxPower(self) -> float:
        return self.convertAmpsToWatts(
            vehicleMaxAmps(self.vehicle, self.configConfig), self.currentVoltage
        )

    # ------------------------------------------------------------------
    # EVSEInstance: optional properties
    # ------------------------------------------------------------------

    @property
    def currentVIN(self) -> str:
        return self.vehicle.vin

    @property
    def vehicleKey(self):
        return self.vehicle.key

    @property
    def occupied(self):
        if self.wantsToCharge or self.vehicle.value("pluggedIn") is True:
            return True
        return None

    @property
    def controllers(self) -> list:
        return ["VehicleEndpoints"]

    # ------------------------------------------------------------------
    # EVSEInstance: control
    # ------------------------------------------------------------------

    def requestedAmps(self):
        # The car's own current request we believe (a read or our write)
        obs = known(self.vehicle, AMPS)
        return None if obs is None else obs.value

    def setTargetPower(self, watts: float) -> None:
        desired, key = self.master.desired, self.vehicle.key
        if not watts:
            # A stop is a stop command; vehicle amps are never 0. A pending
            # lift still goes
            d = desired.get(key, AMPS)
            if d is not None and not d.lift:
                desired.clearIf(key, AMPS, d.value)
            return
        amps = max(1, int(self.convertWattsToAmps(watts, self.currentVoltage)))
        # The reconciler sends it once; a newer reading that disagrees resends
        desired.set(key, AMPS, amps, "distributor")

    def startCharging(self) -> None:
        self.master.startCarsCharging(self.vehicle.vin)

    def stopCharging(self) -> None:
        self.master.stopCarsCharging(self.vehicle.vin)
