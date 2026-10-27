# Which cars a decision applies to, and the charge-limit decisions that
# write desired state: the policy's limit, a plug-in and a departure.
# Presence is Presence's; a car with no provider able to carry the
# capability gets no desired state.
from typing import List, Optional

from .. import Presence
from ..Providers import Capability as C
from .Desired import CHARGE_LIMIT, RELEASE

# The policy's limit before its first pass, which decides for every car
UNDECIDED = object()


def presentVehicles(master, capability) -> List:
    order = master.providerOrder
    return [
        v
        for v in master.vehicleRegistry.vehicles()
        if (not v.isAnonymous or capability in (C.START_CHARGING, C.STOP_CHARGING))
        and Presence.presence(master, v) != Presence.NOT_HOME
        and order.candidates(v, capability)
    ]


def targetsFor(master, vin: Optional[str], capability=C.STOP_CHARGING) -> List:
    # A targeted command names its car, present or not
    if vin:
        vehicle = master.vehicleRegistry.get(vin)
        return [vehicle] if vehicle is not None else []
    return presentVehicles(master, capability)


def _apply(master, vehicle, limit, decision, restore=False) -> None:
    # The policy's limit, or RELEASE; a departure's restore of the outside
    # limit. Either wants nothing once we hold no limit
    if limit is None and vehicle.vin not in master.heldLimits.holding():
        master.desired.clear(vehicle.key, CHARGE_LIMIT)
    elif restore:
        held = master.heldLimits.get(vehicle.vin)
        master.desired.set(
            vehicle.key, CHARGE_LIMIT, held.outside, "restore", decision, restore=True
        )
    else:
        value = RELEASE if limit is None else limit
        master.desired.set(vehicle.key, CHARGE_LIMIT, value, "policy", decision)


def applyPolicyLimit(master, limit: Optional[int], decision: bool) -> int:
    # Every car that may be here; applicable decides at send. A car seen
    # leaving gets its restore instead
    touched = 0
    for vehicle in presentVehicles(master, C.SET_CHARGE_LIMIT):
        if vehicle.vin and Presence.departed(master, vehicle) is None:
            _apply(master, vehicle, limit, decision)
            touched += 1
    _restoreDeparted(master)
    return touched


def onArrival(master, vehicle) -> None:
    # A plug-in here is a decision for this car alone: it ends an adopted
    # value. The plug is checked now, not taken from the event
    if master.policyLimit is UNDECIDED:
        return
    if vehicle.vin and Presence.pluggedHere(master, vehicle):
        if master.providerOrder.candidates(vehicle, C.SET_CHARGE_LIMIT):
            _apply(master, vehicle, master.policyLimit, decision=True)


def _restoreDeparted(master) -> None:
    # A held car gone or leaving gets its normal limit back. Evidence older
    # than our last limit write cannot show a departure
    for vin in master.heldLimits.holding():
        vehicle = master.vehicleRegistry.get(vin)
        if vehicle is None:
            continue
        at = Presence.departed(master, vehicle)
        write = vehicle.lastWrite("chargeLimit")
        # <= on purpose: a retained MQTT position redelivered after our write
        # is stamped with that write's time, and is not a new fix
        if at is None or write is not None and at <= write.at:
            continue
        _apply(master, vehicle, None, True, restore=True)
