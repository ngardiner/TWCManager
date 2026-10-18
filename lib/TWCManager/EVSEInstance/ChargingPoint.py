"""
A charging point: the endpoints that serve one car, grouped by vehicle key.

A TWC and the car it serves are one point however many endpoints can see the
car, so the distributor budgets the car once. A point without a local member
sends the target to its vehicle endpoints. A Gen2 TWC splits the rate with
its car by chargeRateControl (carriers): with car members, or the car the
TWC names; any other local member (a Gen3 unit, whose target feeds one
shared headroom) carries the whole target.
"""

from typing import List, Tuple

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance
from TWCManager.Vehicle.Reconcile.Belief import known
from TWCManager.Vehicle.Reconcile.Desired import AMPS
from TWCManager.Vehicle.Reconcile.Rules import satisfied
from TWCManager.Vehicle.Registry.VehicleRegistry import canonicalKey, isAnonKey


class ChargingPoint(EVSEInstance):
    def __init__(self, master, key: str, endpoints: List[EVSEInstance]):
        self.master = master
        self.key = key
        self.endpoints = list(endpoints)

    @property
    def localEndpoints(self) -> list:
        return [e for e in self.endpoints if e.isLocal]

    @property
    def vehicleEndpoints(self) -> list:
        return [e for e in self.endpoints if not e.isLocal]

    @property
    def ID(self) -> str:
        return "Point-" + self.key

    @property
    def isReadOnly(self) -> bool:
        return all(e.isReadOnly for e in self.endpoints)

    @property
    def wantsToCharge(self) -> bool:
        # The TWC's own report decides when there is one: the registry's
        # chargingState can be stale for a car that has finished
        return any(e.wantsToCharge for e in self.localEndpoints or self.endpoints)

    @property
    def currentVoltage(self) -> list:
        consider = self.localEndpoints or self.endpoints
        columns = list(zip(*[e.currentVoltage for e in consider]))
        if not columns:
            return [0, 0, 0]
        return [sum(col) / len(col) for col in columns]

    def _atPointVoltage(self, limit, pick) -> float:
        # Members are compared in amps and priced at the point's voltage.
        # Each prices itself at its own: a TWC at what it measures, a car
        # that is not charging at the configured default. In watts, a volt
        # of difference would put a car's cap under the TWC's equal minimum
        # and flip the offer between nothing and that minimum.
        amps = (
            self.convertWattsToAmps(limit(e), e.currentVoltage) for e in self.endpoints
        )
        return self.convertAmpsToWatts(pick(amps), self.currentVoltage)

    @property
    def minPower(self) -> float:
        # Every member must be able to take the offer
        return self._atPointVoltage(lambda e: e.minPower, max)

    @property
    def maxPower(self) -> float:
        # The binding member limits the point
        return self._atPointVoltage(lambda e: e.maxPower, min)

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

    def carriers(self) -> Tuple[bool, bool]:
        # (the TWC carries the rate, the car carries it), by chargeRateControl
        mode = int(self.master.settings.get("chargeRateControl", 1))
        return {2: (False, True), 3: (True, True)}.get(mode, (True, False))

    def _carKeys(self) -> List[str]:
        # The cars this point rates: its car members; else the Gen2 TWC's
        # named car, or the unnamed cars its anonymous session fans out to
        cars = [e.vehicleKey for e in self.vehicleEndpoints if not e.isReadOnly]
        local = self.localEndpoints[0] if self.localEndpoints else None
        key = getattr(local, "vehicleKey", None)
        if cars or key is None:
            return cars
        if isAnonKey(key):
            return [v.key for v in anonymousCars(self.master, local)]
        return [key]

    def confirmed(self) -> bool:
        # Every car carrying our rate believes it: a read, or a delivered write
        keys = self._carKeys()
        reg, desired = self.master.vehicleRegistry, self.master.desired
        for k in keys:
            d, v = desired.get(k, AMPS), reg.get(k)
            if d is None or v is None or not satisfied(AMPS, d.value, known(v, AMPS)):
                return False
        return bool(keys)

    def setTargetPower(self, watts: float) -> None:
        local = self.localEndpoints[0] if self.localEndpoints else None
        cars = [e for e in self.vehicleEndpoints if not e.isReadOnly]
        if not hasattr(local, "pointAmps"):
            # No Gen2 TWC to layer with: the local member (a Gen3 unit)
            # carries the whole target, or with none the cars do
            for e in [local] if local else cars:
                e.setTargetPower(watts)
            return
        # A car member's rate goes through its endpoint; a car only the TWC
        # names gets the "heartbeat" rate
        desired, members = self.master.desired, {e.vehicleKey for e in cars}
        mine = [k for k in self._carKeys() if k not in members]

        def withdraw(keys):
            # Only our own rate: never a lift, nor a car endpoint's
            for k in keys:
                d = desired.get(k, AMPS)
                if d is not None and d.by == "heartbeat":
                    desired.clearIf(k, AMPS, d.value)

        def own(amps, by, lift=False):
            # A car that left the set keeps no rate of ours
            withdraw([k for k in local._lastKeys if k not in mine])
            local._lastKeys = mine
            for k in mine:
                desired.set(k, AMPS, int(amps), by, lift=lift)

        twc, car = self.carriers()
        if not watts:
            # A stop is the charging aspect; a pending lift still goes
            local.setTargetPower(0)
            for e in cars:
                e.setTargetPower(0)
            if car:
                withdraw(local._lastKeys)
            return
        amps = self.convertWattsToAmps(watts, self.currentVoltage)
        floor = local.layerFloorAmps()
        if twc and (not car or amps >= floor):
            local.setTargetPower(watts)
            for e in cars:
                # The TWC carries: the car keeps no rate of ours
                e.setTargetPower(0)
            if local.vehicleRateRaised:
                return
            # Once per session; in mode 3 only for a car member that would
            # cap below the offer (undershoot is safe; unknown is left
            # alone), a car only the TWC names always
            lifts = [e.vehicleKey for e in cars if not car or self._caps(e, amps)]
            for k in lifts:
                desired.set(k, AMPS, local.wiringMaxAmps, "lift", lift=True)
            own(local.wiringMaxAmps, "lift", lift=True)
            if lifts or mine:
                local.liftedSession = local.plugSessions
            return
        # The car carries: the next return to the TWC may lift again. With
        # a car member the TWC holds the target, never below the handoff
        # floor; offerFor lifts it to its maximum once the car's rate is
        # confirmed
        local.liftedSession = None
        local.setTargetPower(
            local.convertAmpsToWatts(max(floor, amps)) if cars else watts
        )
        for e in cars:
            e.setTargetPower(watts)
        own(amps, "heartbeat")

    @staticmethod
    def _caps(member, amps) -> bool:
        # The car's own request is known to sit below the offer
        k = member.requestedAmps()
        return k is not None and k < int(amps)


def anonymousCars(master, slave) -> list:
    # The cars an anonymous session rates, paid once: the plugged-in
    # cars, else vehicles[0]
    plugged, cars = slave.pluggedCars()
    if plugged:
        return plugged
    # The first car the Tesla API listed (registry (sorted VIN) order)
    listed = [v for v, _ in master.vehicleRegistry.attached("TeslaAPI")]
    return listed[:1] if listed and listed[0] in cars else []


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
        key = e.ID if key is None else canonicalKey(key)
        if e.isLocal and any(m.isLocal for m in groups.get(key, ())):
            key = e.ID
        groups.setdefault(key, []).append(e)
    return [ChargingPoint(master, key, members) for key, members in groups.items()]
