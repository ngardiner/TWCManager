# Which car is on a Gen3 TWC, from the energy site: the free live_status
# lists each Wall Connector on the account by DIN, with the VIN of the car
# connected to it. Asked on a Gen3 connect edge, then once a minute while a
# session is anonymous or holds only a plug-in guess. A cloud VIN outranks
# the guess.
#
# Unverified on hardware: that live_status carries wall_connectors with a
# vin while a car is connected, and that a unit's DIN is
# <part_number>--<serial_number> from /api/1/version (the din device key
# overrides it).
#
# Background thread only (Gen3TWCs.noteVitals); never under a lock.
import logging
import time
import traceback
from typing import Dict

from ..Registry.VehicleRegistry import isAnonKey
from .Capability import READ_WALL_CONNECTOR_VINS

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Providers")


class WallConnectorVinReader:
    POLL_SECONDS = 60.0
    # After this many polls in a row name no car, poll this often instead
    SLOW_AFTER = 10
    SLOW_POLL_SECONDS = 600.0
    # A connect edge asks at once, but never twice inside this
    EDGE_MIN_GAP = 30.0

    def __init__(self, master, order, clock=time.time):
        self.master = master
        self.order = order
        self.clock = clock
        self.lastQuery = float("-inf")
        self._connectEdge = False
        # Unit IDs whose no-match INFO this session has logged
        self._unmatchedLogged = set()
        self._noProviderLogged = False
        # Polls in a row that named no car; an edge or a named car resets it
        self._unanswered = 0
        # Per unit ID: clock() of the last connect edge, and the VIN of the
        # session that last ended there. live_status may still report the
        # departed car (lag unverified), so that VIN is not taken until a
        # poll at least POLL_SECONDS after the edge.
        self._edgeAt: Dict[str, float] = {}
        self._endedVin: Dict[str, str] = {}

    @staticmethod
    def _waiting(unit) -> bool:
        # Occupied, and not yet named by the cloud: anonymous, a plug-in
        # guess, or a cloud VIN still pending confirmation
        if unit.occupied is not True:
            return False
        key = unit.vehicleKey
        if not isinstance(key, str):
            return False
        return unit.cloudVin is None or key.upper() != unit.cloudVin.upper()

    def noteConnect(self, unit=None) -> None:
        self._connectEdge = True
        self._unanswered = 0
        if unit is not None:
            self._edgeAt[unit.ID] = self.clock()

    def noteDisconnect(self, unit, vin) -> None:
        # vin: the leaving session's VIN, or None when it was anonymous
        if isinstance(vin, str) and not isAnonKey(vin):
            self._endedVin[unit.ID] = vin.upper()
        else:
            self._endedVin.pop(unit.ID, None)

    def interval(self) -> float:
        if self._unanswered >= self.SLOW_AFTER:
            return self.SLOW_POLL_SECONDS
        return self.POLL_SECONDS

    def due(self, units) -> bool:
        if not any(self._waiting(u) for u in units):
            return False
        since = self.clock() - self.lastQuery
        edge = self._connectEdge and since >= self.EDGE_MIN_GAP
        return edge or since >= self.interval()

    def _stale(self, unit, vin) -> bool:
        # The departed car's VIN, asked too soon after the next car's edge
        ended = self._endedVin.get(unit.ID)
        if ended is None or str(vin).upper() != ended:
            return False
        edgeAt = self._edgeAt.get(unit.ID)
        return edgeAt is not None and self.clock() - edgeAt < self.POLL_SECONDS

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
            vin = None
            if din is not None and din.upper() in cloud:
                vin = cloud[din.upper()]
            elif din is None and len(units) == 1 and len(cloud) == 1:
                vin = next(iter(cloud.values()))
            if vin is not None:
                if self._stale(unit, vin):
                    # Still waiting: the next poll asks again
                    continue
                self._endedVin.pop(unit.ID, None)
                found[unit] = vin
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
            # A cloud or registry hiccup never stops the vitals loop. The
            # frames are logged, never the message: it can carry a token
            logger.debug(
                "Wall Connector VINs: %s\n%s",
                type(e).__name__,
                "".join(traceback.format_tb(e.__traceback__)),
            )

    def _apply(self, units) -> None:
        for unit in units:
            if not self._waiting(unit):
                self._unmatchedLogged.discard(unit.ID)
        if not self.due(units):
            return
        self.lastQuery = self.clock()
        self._connectEdge = False
        vins = self.query(units)
        found = self.match(units, vins)
        for unit, vin in found.items():
            unit.cloudVin = vin
            unit._correlate()
        if any(not self._waiting(u) for u in found):
            self._unanswered = 0
        else:
            self._unanswered += 1
