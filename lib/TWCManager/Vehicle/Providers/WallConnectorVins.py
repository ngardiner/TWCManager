# Which car is on a Gen3 TWC, from the energy site: the free live_status
# lists each Wall Connector on the account by DIN, with the VIN of the car
# connected to it. Asked on a Gen3 connect edge and then once a minute while
# a Gen3 session is anonymous; never once the session has a VIN.
#
# Unverified on hardware: that live_status carries wall_connectors with a
# vin while a car is connected (the tesla-fleet-api npm types say so), and
# that a unit's DIN is <part_number>--<serial_number> from its local
# /api/1/version (the din device key overrides it).
#
# Background thread only (Gen3TWCs.noteVitals); never under a lock. The
# reader's own state is touched from that one thread.
import logging
import time
from typing import Dict, List

from ..Registry.VehicleRegistry import ANON_PREFIX
from .Capability import READ_WALL_CONNECTOR_VINS

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Providers")


def _anonymous(key) -> bool:
    return isinstance(key, str) and key.startswith(ANON_PREFIX)


class WallConnectorVinReader:
    POLL_SECONDS = 60.0

    def __init__(self, master, order, clock=time.time):
        self.master = master
        self.order = order
        self.clock = clock
        self.lastQuery = float("-inf")
        self._connectEdge = False
        # Per provider name: clock() of each call inside its rate window
        self._calls: Dict[str, List[float]] = {}
        # Unit IDs whose no-match INFO this session has logged
        self._unmatchedLogged = set()
        self._noProviderLogged = False

    @staticmethod
    def _waiting(unit) -> bool:
        return unit.occupied is True and _anonymous(unit.vehicleKey)

    def noteConnect(self) -> None:
        self._connectEdge = True

    def due(self, units) -> bool:
        if not any(self._waiting(u) for u in units):
            return False
        return self._connectEdge or self.clock() - self.lastQuery >= self.POLL_SECONDS

    def _allowed(self, provider) -> bool:
        limit = provider.declarations()[READ_WALL_CONNECTOR_VINS].rateLimit
        if limit is None:
            return True
        now = self.clock()
        calls = [
            t for t in self._calls.get(provider.name, []) if now - t < limit.perSeconds
        ]
        self._calls[provider.name] = calls
        return len(calls) < limit.calls

    def query(self, units) -> Dict[str, str]:
        candidates = list(self.order.candidates(None, READ_WALL_CONNECTOR_VINS))
        if not candidates:
            if not self._noProviderLogged:
                logger.info(
                    "Wall Connector VINs: no Tesla API token can read energy "
                    "sites (energy_device_data); Gen3 cars stay anonymous "
                    "unless one known car just plugged in"
                )
                self._noProviderLogged = True
            return {}
        self._noProviderLogged = False
        for provider in candidates:
            if not self._allowed(provider):
                continue
            self._calls.setdefault(provider.name, []).append(self.clock())
            vins = provider.wallConnectorVins()
            if vins is not None:
                return vins
        return {}

    def match(self, units, vins) -> Dict[object, str]:
        """{unit: vin} for each waiting unit the cloud names."""
        found = {}
        cloud = {str(din).upper(): vin for din, vin in vins.items()}
        for unit in units:
            if not self._waiting(unit):
                continue
            din = unit.din
            if din is not None and din.upper() in cloud:
                found[unit] = cloud[din.upper()]
                continue
            if din is None and len(units) == 1 and len(cloud) == 1:
                found[unit] = next(iter(cloud.values()))
                continue
            # A known DIN that matches nothing is a different connector (or
            # the DIN guess is wrong: the din device key fixes that)
            if unit.ID not in self._unmatchedLogged:
                self._unmatchedLogged.add(unit.ID)
                logger.info(
                    "Wall Connector VINs: no DIN match for %s (%d cloud connectors)",
                    unit.ID,
                    len(cloud),
                )
        return found

    def apply(self, units) -> None:
        try:
            self._apply(list(units))
        except Exception as e:
            # A cloud or registry hiccup never stops the vitals loop
            logger.debug("Wall Connector VINs: %s", type(e).__name__, exc_info=True)

    def _apply(self, units) -> None:
        for unit in units:
            if not self._waiting(unit):
                self._unmatchedLogged.discard(unit.ID)
        if not self.due(units):
            return
        self.lastQuery = self.clock()
        self._connectEdge = False
        vins = self.query(units)
        for unit, vin in self.match(units, vins).items():
            unit.cloudVin = vin
            unit._correlate()
