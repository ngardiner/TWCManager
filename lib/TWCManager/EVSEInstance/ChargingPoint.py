"""
A charging point: the endpoints that serve one car, grouped by vehicle key.

A TWC and the car it serves are one point however many endpoints can see the
car, so the distributor budgets the car once. A point without a local member
sends the target to its vehicle endpoints. A Gen2 TWC with a car splits the
rate by chargeRateControl (carriers); any other local member (a Gen3 unit,
whose target feeds one shared headroom) carries the whole target.
"""

from typing import List, Tuple

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.Vehicle.Reconcile.Desired import AMPS
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
        # The TWC's own report decides when there is one: the registry's
        # chargingState can be stale for a car that has finished
        return any(e.wantsToCharge for e in self._preferLocal())

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

    def carriers(self) -> Tuple[bool, bool]:
        # (the TWC carries the rate, the car carries it), by chargeRateControl
        mode = int(self.master.settings.get("chargeRateControl", 1))
        return {2: (False, True), 3: (True, True)}.get(mode, (True, False))

    def setTargetPower(self, watts: float) -> None:
        local = self.localEndpoints[0] if self.localEndpoints else None
        cars = [e for e in self.vehicleEndpoints if not e.isReadOnly]
        layered = hasattr(local, "pointOwnsAmps")
        if layered:
            # One writer per cycle: the heartbeat writes no car rate now
            local.pointOwnsAmps = bool(cars)
        if local is None or not (layered and cars):
            for e in [local] if local else cars:
                e.setTargetPower(watts)
            return
        if not watts:
            # A stop is the charging aspect; a pending lift still goes
            local.setTargetPower(0)
            for e in cars:
                e.setTargetPower(0)
            return
        twc, car = self.carriers()
        amps = self.convertWattsToAmps(watts, self.currentVoltage)
        floor = local.layerFloorAmps()
        if twc and (not car or amps >= floor):
            local.setTargetPower(watts)
            for e in cars:
                # The TWC carries: the car keeps no rate of ours (M4)
                e.setTargetPower(0)
                k = e.requestedAmps()
                # Once per session; in mode 3 only for a car that would cap
                # below the offer (undershoot is safe; unknown is left alone)
                if not local.vehicleRateRaised and (
                    not car or k is not None and k < int(amps)
                ):
                    self.master.desired.set(
                        e.vehicleKey, AMPS, local.wiringMaxAmps, "lift", lift=True
                    )
                    local.vehicleRateRaised = True
            return
        if twc:
            # Below the floor: the next return to range may lift again
            local._resetVehicleRateRaised()
        # The car carries: the TWC offers its maximum (mode 2) or holds the
        # heartbeat's handoff floor (mode 3 below range)
        local.setTargetPower(local.convertAmpsToWatts(floor) if twc else local.maxPower)
        for e in cars:
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
