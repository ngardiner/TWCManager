"""
A registry vehicle whose charge rate a vehicle provider sets, as a charging
endpoint.

The endpoint reads only the registry's latest values and never calls a
provider: the distributor runs on the control tick, which must not wait on
a car, so a rate is desired state, which the reconciler delivers through the
provider order (the local path first; a paid provider only where no local
one covers the rate). The endpoint holds no state of its own.
"""

from typing import List, Tuple

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.Vehicle.Presence import pluggedHere, telemetrySaysPlugged
from TWCManager.Vehicle.Providers.Capability import SET_AMPS
from TWCManager.Vehicle.Providers.Provider import vehicleMaxAmps
from TWCManager.Vehicle.Reconcile.Desired import AMPS
from TWCManager.Vehicle.Reconcile.Belief import POWER_FLOWING_AMPS, known
from TWCManager.Vehicle.Registry.VehicleRegistry import isAnonKey


class VehicleEndpoint(EVSEInstance):
    # Tesla vehicles accept as low as 5 A (car-side minimum)
    MIN_AMPS = 5
    isLocal = False
    controllers = ["VehicleEndpoints"]

    def __init__(self, vehicle, order, master):
        self.vehicle = vehicle
        self.order = order
        self.master = master
        self.configConfig = master.config.get("config", {})

    @property
    def ID(self) -> str:
        return "Vehicle-%s" % self.vehicle.vin

    def _candidates(self):
        return self.order.candidates(self.vehicle, SET_AMPS)

    @property
    def isReadOnly(self) -> bool:
        # No provider can set this car's amps (an Owner-only token on a car
        # that needs signed commands, ble_only without BLE), so the
        # distributor must not budget power for it
        return not self._candidates()

    @property
    def wantsToCharge(self) -> bool:
        # "Stopped" is a paused session: the car takes power if offered
        return self.vehicle.value("chargingState") in ("Charging", "Stopped")

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
    def minPower(self) -> float:
        return self.convertAmpsToWatts(self.MIN_AMPS, self.currentVoltage)

    @property
    def maxPower(self) -> float:
        return self.convertAmpsToWatts(
            vehicleMaxAmps(self.vehicle, self.configConfig), self.currentVoltage
        )

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


def _unknownKey(key) -> bool:
    # A point that cannot name its car: no key, or an anonymous plug session
    return key is None or isAnonKey(key)


def _blindTWCs(master) -> Tuple[bool, bool]:
    """How far an unnamed car on a local charging point may reach.

    Returns (blind, unknown). A slave names its car when its vehicleKey
    is a VIN (currentVIN, or its last confirmed car while it reports a plug),
    so a stopped car does not hide the others. unknown is True when some
    slave's key is None or anonymous; blind when such a slave reports a
    plugged state (non-00) or draws power, so it might be serving any
    car. An occupied Gen3 that can't name its car (anonymous, or no
    key at all) could be serving any account car, so it blinds too. So
    does an open Gen2 link with no slave linked (at startup, or after a
    slave times out); a closed link controls no TWC. A failed scan fails
    closed.
    """
    blind = False
    unknown = False
    try:
        slaves = master.getSlaveTWCs()
        if not slaves and master.busUp():
            blind = unknown = True
        for slave in slaves:
            if not _unknownKey(getattr(slave, "vehicleKey", None)):
                continue
            unknown = True
            if (
                slave.reportedState != 0x00
                or slave.reportedAmpsActual >= POWER_FLOWING_AMPS
            ):
                blind = True
        gen3 = master.getModuleByName("Gen3TWCs")
        if gen3 is not None:
            for unit in gen3.devices():
                if unit.occupied is True and _unknownKey(unit.vehicleKey):
                    blind = True
    except Exception:
        return True, True
    return blind, unknown


def vehicleEndpoints(master) -> List[VehicleEndpoint]:
    """A VehicleEndpoint for each car plugged in here with a provider that
    can set its rate, whichever module knows it.

    While a charging point that can't name its car is plugged in or draws
    power, it might be serving any of them, so none is listed: an
    endpoint would get its own power share and its own rate commands. A
    Gen2 idle at 00 may still hide a plugged-in car, so while any point
    can't name its car, a car live telemetry says is plugged in is not
    listed either. A car on a named point is listed; the distributor
    merges it with that point. Dropping an endpoint strands nothing: stops
    go to master.stopCarsCharging by VIN.
    """
    blind, unknown = _blindTWCs(master)
    if blind:
        return []
    order = master.providerOrder
    return [
        VehicleEndpoint(v, order, master)
        for v in master.vehicleRegistry.vehicles()
        if not v.isAnonymous
        and pluggedHere(master, v)
        and order.candidates(v, SET_AMPS)
        and not (unknown and telemetrySaysPlugged(master, v))
    ]
