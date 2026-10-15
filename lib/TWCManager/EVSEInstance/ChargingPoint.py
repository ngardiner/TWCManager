"""
A charging point: the endpoints that serve one car, grouped by vehicle key.

A TWC and the car it serves are one point however many endpoints can see the
car, so the distributor budgets the car once. A point with a local (TWC)
member gives the whole target to it and TWCSlave applies chargeRateControl as
before; the car's own endpoint gets no amps command. A point without a local
member sends the target to its vehicle endpoints.
"""

from typing import List

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.Vehicle.Registry.VehicleRegistry import ANON_PREFIX


class ChargingPoint(EVSEInstance):
    def __init__(self, master, key: str, endpoints: List[EVSEInstance]):
        self.master = master
        self.key = key
        self.endpoints = list(endpoints)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @property
    def localEndpoints(self) -> list:
        return [e for e in self.endpoints if e.isLocal]

    @property
    def vehicleEndpoints(self) -> list:
        return [e for e in self.endpoints if not e.isLocal]

    def _preferLocal(self) -> list:
        return self.localEndpoints or self.endpoints

    # ------------------------------------------------------------------
    # EVSEInstance: identity
    # ------------------------------------------------------------------

    @property
    def ID(self) -> str:
        return "Point-" + self.key

    # ------------------------------------------------------------------
    # EVSEInstance: capabilities
    # ------------------------------------------------------------------

    @property
    def isReadOnly(self) -> bool:
        return all(e.isReadOnly for e in self.endpoints)

    @property
    def isLocal(self) -> bool:
        return any(e.isLocal for e in self.endpoints)

    # ------------------------------------------------------------------
    # EVSEInstance: state
    # ------------------------------------------------------------------

    @property
    def isCharging(self) -> bool:
        return any(e.isCharging for e in self.endpoints)

    @property
    def wantsToCharge(self) -> bool:
        return any(e.wantsToCharge for e in self.endpoints)

    @property
    def occupied(self):
        states = [e.occupied for e in self.endpoints]
        if any(s is True for s in states):
            return True
        if states and all(s is False for s in states):
            return False
        return None

    @property
    def currentAmps(self) -> float:
        consider = self._preferLocal()
        if not consider:
            return 0.0
        return sum(e.currentAmps for e in consider) / len(consider)

    @property
    def currentVoltage(self) -> list:
        consider = self._preferLocal()
        if not consider:
            return [0, 0, 0]
        columns = list(zip(*[e.currentVoltage for e in consider]))
        if not columns:
            return [0, 0, 0]
        return [sum(col) / len(col) for col in columns]

    @property
    def currentPower(self) -> float:
        consider = self._preferLocal()
        if not consider:
            return 0.0
        return sum(e.currentPower for e in consider) / len(consider)

    # ------------------------------------------------------------------
    # EVSEInstance: power limits
    # ------------------------------------------------------------------

    @property
    def minPower(self) -> float:
        # Every member must be able to take the offer
        return max(e.minPower for e in self.endpoints)

    @property
    def maxPower(self) -> float:
        # The binding member limits the point
        return min(e.maxPower for e in self.endpoints)

    # ------------------------------------------------------------------
    # EVSEInstance: optional properties
    # ------------------------------------------------------------------

    @property
    def currentVIN(self) -> str:
        for e in self.endpoints:
            if e.currentVIN:
                return e.currentVIN.upper()
        return ""

    @property
    def vehicleKey(self):
        return self.key

    @property
    def controllers(self) -> list:
        seen = []
        for e in self.endpoints:
            for c in e.controllers:
                if c not in seen:
                    seen.append(c)
        return seen

    # ------------------------------------------------------------------
    # EVSEInstance: control
    # ------------------------------------------------------------------

    def setTargetPower(self, watts: float) -> None:
        # The TWC carries the whole target and its heartbeat applies
        # chargeRateControl; a vehicle member then gets no amps command
        targets = self.localEndpoints or [e for e in self.endpoints if not e.isReadOnly]
        for e in targets:
            e.setTargetPower(watts)

    def startCharging(self) -> None:
        for e in self.endpoints:
            e.startCharging()

    def stopCharging(self) -> None:
        # No Phase 2 caller. chargeStopMode 1 prefers the car's own stop
        if int(self.master.settings.get("chargeStopMode", 1)) == 1:
            targets = [e for e in self.endpoints if not e.isLocal and not e.isReadOnly]
        else:
            targets = self.localEndpoints
        for e in targets or self.endpoints:
            e.stopCharging()


def _groupKey(key):
    # VINs group case-insensitively; anonymous keys are kept as issued
    if key.startswith(ANON_PREFIX):
        return key
    return key.upper()


def groupEndpoints(master, endpoints: List[EVSEInstance]) -> List[ChargingPoint]:
    """Group endpoints that serve the same car into one charging point.

    Endpoints sharing a vehicleKey form one point keyed by it; an endpoint
    with no vehicleKey is its own point keyed by its ID. Points keep the
    order of their first endpoint.

    A point holds at most one local endpoint: every local member gets the
    whole target, so a second TWC claiming the same car (a stale lastVIN)
    is its own point keyed by its ID and budgeted separately.
    """
    groups = {}
    for e in endpoints:
        key = e.vehicleKey
        key = e.ID if key is None else _groupKey(key)
        if e.isLocal and any(m.isLocal for m in groups.get(key, ())):
            key = e.ID
        groups.setdefault(key, []).append(e)
    return [ChargingPoint(master, key, members) for key, members in groups.items()]
