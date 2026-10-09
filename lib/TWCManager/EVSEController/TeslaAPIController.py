"""
EVSEController for Tesla vehicles managed via the Fleet API.

Discovers vehicles at home through the TeslaAPI module and exposes them as
TeslaAPIEVSE instances.

Ported from ngardiner/TWCManager#483 (MikeBishop); adapted for current main.
Bug fixed: allEVSEs now caches instances by VIN rather than creating new
TeslaAPIEVSE objects on every access (original PR recreated objects each call,
losing any state accumulated on the instance).
"""

from TWCManager.EVSEController.EVSEController import EVSEController
from TWCManager.EVSEInstance.TeslaAPIEVSE import TeslaAPIEVSE


class TeslaAPIController(EVSEController):
    """Controller for Tesla vehicles accessible via the Fleet API.

    Enabled automatically when the TeslaAPI vehicle module is loaded and at
    least one configured vehicle is at home.
    """

    name = "TeslaAPIController"

    def __init__(self, master):
        self.master = master
        # VIN → TeslaAPIEVSE cache.  Avoids the PR bug of creating a new
        # object on every allEVSEs access, which would discard accumulated
        # per-instance state (e.g. timing guards).
        self._evse_cache: dict = {}

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

    # A TWC drawing at least this is charging a car: the threshold at
    # which TWCSlave flags isCharging and queries the VIN.
    POWER_FLOWING_AMPS = 1.0

    def _twcServedVINs(self):
        """VINs a local TWC serves, and how far an unnamed car may reach.

        Returns (vins, blind, unknown): vins are the cars TWCs report
        (currentVIN, or the plug session's VIN); unknown is True when some
        TWC can't name its car; blind is True when such a TWC reports a
        plugged state (non-00) or draws power, so it might be serving any
        of them. A failed scan fails closed (blind).
        """
        vins = set()
        blind = False
        unknown = False
        try:
            for slave in self.master.getSlaveTWCs():
                current = slave.currentVIN
                # plugVIN and NON_TESLA are absent on older TWCSlave.
                plug = getattr(slave, "plugVIN", None)
                if plug is not None and plug == getattr(slave, "NON_TESLA", None):
                    plug = None
                if current:
                    vins.add(current)
                if plug:
                    vins.add(plug)
                if not current and not plug:
                    unknown = True
                    if (
                        slave.reportedState != 0x00
                        or slave.reportedAmpsActual >= self.POWER_FLOWING_AMPS
                    ):
                        blind = True
        except Exception:
            return set(), True, True
        return vins, blind, unknown

    @staticmethod
    def _telemetryPluggedIn(vehicle):
        # A live telemetry feed (TeslaMate) says the cable is plugged in.
        # Live is the telemetryChargeLimit criterion: syncSource is the
        # telemetry module, which reverts to "TeslaAPI" after syncTimeout.
        return (
            getattr(vehicle, "syncSource", "TeslaAPI") != "TeslaAPI"
            and getattr(vehicle, "pluggedIn", None) is True
        )

    # ------------------------------------------------------------------
    # EVSEController interface
    # ------------------------------------------------------------------

    @property
    def allEVSEs(self) -> list:
        """Return TeslaAPIEVSE instances for each vehicle at home that no
        local TWC serves.

        A car on a local TWC is controlled through the TWC; an EVSE here
        would get its own power share and send paid Fleet API charge-rate
        commands. While a TWC that can't name its car is plugged in or draws
        power, it might be any of them, so none is listed. A TWC idle at 00
        may still hide a plugged-in car (Gen2 reports 00 for one), so while
        any TWC can't name its car, a car live telemetry says is plugged in
        is not listed either. Dropping an EVSE strands nothing:
        it holds no state, and stops go to master.stopCarsCharging by VIN.
        """
        carapi = self._get_carapi()
        if carapi is None:
            return []

        served, blind, unknown = self._twcServedVINs()
        if blind:
            vehicles_at_home = []
        else:
            vehicles_at_home = [
                v
                for v in carapi.getCarApiVehicles()
                if v.atHome
                and v.VIN not in served
                and not (unknown and self._telemetryPluggedIn(v))
            ]
        current_vins = {v.VIN for v in vehicles_at_home}

        # Remove departed (or now TWC-served) vehicles from cache
        for vin in list(self._evse_cache):
            if vin not in current_vins:
                del self._evse_cache[vin]

        # Add newly arrived vehicles to cache
        for vehicle in vehicles_at_home:
            if vehicle.VIN not in self._evse_cache:
                self._evse_cache[vehicle.VIN] = TeslaAPIEVSE(vehicle, self, self.master)

        return list(self._evse_cache.values())

    @property
    def maxPower(self) -> float:
        # No hard limit at the controller level — individual EVSEs report their
        # own maximums based on the vehicle's pilot current.
        return float("inf")
