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

    # ------------------------------------------------------------------
    # EVSEController interface
    # ------------------------------------------------------------------

    @property
    def allEVSEs(self) -> list:
        """Return TeslaAPIEVSE instances for each vehicle currently at home."""
        carapi = self._get_carapi()
        if carapi is None:
            return []

        vehicles_at_home = [v for v in carapi.getCarApiVehicles() if v.atHome]
        current_vins = {v.VIN for v in vehicles_at_home}

        # Remove departed vehicles from cache
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
