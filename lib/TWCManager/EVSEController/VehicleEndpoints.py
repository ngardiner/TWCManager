"""
EVSEController listing a VehicleEndpoint for each car TeslaAPI places at home.

Registered only when TeslaAPI is loaded; pure-Gen2 and BLE-only installs keep
the per-slave path. Endpoints are cached by VIN so per-endpoint state (the
last queued rate) survives between distributor passes.
"""

from typing import Dict, Tuple

from TWCManager.EVSEController.EVSEController import EVSEController
from TWCManager.EVSEInstance.VehicleEndpoint import VehicleEndpoint
from TWCManager.Vehicle.Registry.VehicleRegistry import isAnonKey


def _unknownKey(key) -> bool:
    # A point that cannot name its car: no key, or an anonymous plug session
    return key is None or isAnonKey(key)


class VehicleEndpoints(EVSEController):
    name = "VehicleEndpoints"

    # A TWC drawing at least this is charging a car: the threshold at
    # which TWCSlave flags isCharging and queries the VIN.
    POWER_FLOWING_AMPS = 1.0

    def __init__(self, master):
        self.master = master
        self._cache: Dict[str, VehicleEndpoint] = {}

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_carapi(self):
        carapi = self.master.getModuleByName("TeslaAPI")
        if carapi is None:
            return None
        try:
            if not carapi.enabled():
                return None
        except Exception:
            return None
        return carapi

    def _blindTWCs(self) -> Tuple[bool, bool]:
        """How far an unnamed car on a local charging point may reach.

        Returns (blind, unknown). A slave names its car when its vehicleKey
        is a VIN (currentVIN, or lastVIN while it reports a plugged state),
        so a stopped car does not hide the others. unknown is True when some
        slave's key is None or anonymous; blind when such a slave reports a
        plugged state (non-00) or draws power, so it might be serving any
        car. An occupied Gen3 that can't name its car (anonymous, or no
        key at all) could be serving any account car, so it blinds too. A
        failed scan fails closed.
        """
        blind = False
        unknown = False
        try:
            for slave in self.master.getSlaveTWCs():
                if not _unknownKey(getattr(slave, "vehicleKey", None)):
                    continue
                unknown = True
                if (
                    slave.reportedState != 0x00
                    or slave.reportedAmpsActual >= self.POWER_FLOWING_AMPS
                ):
                    blind = True
            gen3 = self.master.getModuleByName("Gen3TWCs")
            if gen3 is not None:
                for unit in gen3.devices():
                    if unit.occupied is True and _unknownKey(unit.vehicleKey):
                        blind = True
        except Exception:
            return True, True
        return blind, unknown

    @staticmethod
    def _telemetryPluggedIn(vehicle):
        # A live telemetry feed (TeslaMate) says the cable is plugged in.
        # Live is the telemetryChargeLimit criterion: syncSource is the
        # telemetry module, which reverts to "TeslaAPI" after SYNC_TIMEOUT.
        return vehicle.syncSource != "TeslaAPI" and vehicle.value("pluggedIn") is True

    # ------------------------------------------------------------------
    # EVSEController interface
    # ------------------------------------------------------------------

    @property
    def allEVSEs(self) -> list:
        """A VehicleEndpoint for each registry car TeslaAPI places at home.

        While a charging point that can't name its car is plugged in or draws
        power, it might be serving any of them, so none is listed: an
        endpoint would get its own power share and its own rate commands. A
        Gen2 idle at 00 may still hide a plugged-in car, so while any point
        can't name its car, a car live telemetry says is plugged in is not
        listed either. A car on a named point is listed; the distributor
        merges it with that point. Read-only endpoints are listed too (the
        distributor skips them). Dropping an endpoint strands nothing: stops
        go to master.stopCarsCharging by VIN.
        """
        if self._get_carapi() is None:
            return self._keep({})

        blind, unknown = self._blindTWCs()
        if blind:
            return self._keep({})

        listed = {}
        for vehicle, car in self.master.vehicleRegistry.attached("TeslaAPI"):
            if not car.atHome:
                continue
            if unknown and self._telemetryPluggedIn(vehicle):
                continue
            listed[vehicle.vin] = vehicle
        return self._keep(listed)

    def _keep(self, listed) -> list:
        # Drop departed (or now hidden) cars; add newly listed ones
        cache = {}
        for vin, vehicle in listed.items():
            endpoint = self._cache.get(vin)
            if endpoint is None or endpoint.vehicle is not vehicle:
                endpoint = VehicleEndpoint(
                    vehicle, self.master.providerOrder, self.master
                )
            cache[vin] = endpoint
        self._cache = cache
        return list(cache.values())

    @property
    def maxPower(self) -> float:
        # No controller-level limit: each endpoint reports its car's own
        return float("inf")
