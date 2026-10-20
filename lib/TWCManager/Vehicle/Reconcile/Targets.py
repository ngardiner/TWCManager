# Which cars a decision applies to, and the charge-limit decisions that
# write desired state: the policy's limit, a plug-in and a departure.
# Presence is Presence's; a car with no provider able to carry the
# capability gets no desired state.
from typing import List, Optional

from .. import Presence
from ..Providers.Capability import (
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
)
from ..Registry.Fields import READ_MAX_AGE
from .Desired import CHARGE_LIMIT


def presentVehicles(master, capability) -> List:
    order = master.providerOrder
    return [
        v
        for v in master.vehicleRegistry.vehicles()
        if (not v.isAnonymous or capability in (START_CHARGING, STOP_CHARGING))
        and Presence.homeForLimits(master, v)
        and order.candidates(v, capability)
    ]


def targetsFor(master, vin: Optional[str], capability=STOP_CHARGING) -> List:
    # A targeted command names its car, present or not
    if vin:
        vehicle = master.vehicleRegistry.get(vin)
        return [vehicle] if vehicle is not None else []
    return presentVehicles(master, capability)


def _restore(master, vehicle, decision) -> bool:
    # Put back the outside limit while we hold one; else want nothing
    held = master.heldLimits.get(vehicle.vin)
    if held is None or held.held is None:
        master.desired.clear(vehicle.key, CHARGE_LIMIT)
        return False
    master.desired.set(
        vehicle.key, CHARGE_LIMIT, held.outside, "restore", decision, restore=True
    )
    return True


def _apply(master, vehicle, limit, decision) -> None:
    if limit is None:
        _restore(master, vehicle, decision)
    else:
        master.desired.set(vehicle.key, CHARGE_LIMIT, limit, "policy", decision)


def applyPolicyLimit(master, limit: Optional[int], decision: bool) -> int:
    # Every car that may be here; applicable decides at send. A car seen
    # leaving gets its restore instead
    touched = 0
    for vehicle in presentVehicles(master, SET_CHARGE_LIMIT):
        if vehicle.vin and Presence.departed(master, vehicle) is None:
            _apply(master, vehicle, limit, decision)
            touched += 1
    _restoreDeparted(master, read=False)
    return touched


def onArrival(master, vehicle) -> None:
    # A plug-in here is a decision for this car alone: it ends an adopted
    # value. The plug is checked now, not taken from the event
    if vehicle.vin and Presence.pluggedHere(master, vehicle):
        if master.providerOrder.candidates(vehicle, SET_CHARGE_LIMIT):
            _apply(master, vehicle, master.policyLimit, decision=True)


def onDeparture(master) -> None:
    # Background thread only: an unplug alone is not a departure
    _restoreDeparted(master, read=True)


def _restoreDeparted(master, read) -> None:
    # A held car gone or leaving gets its normal limit back. read: a listed
    # car off its point with no live telemetry is read first (never woken).
    # Evidence older than our last limit write cannot show a departure
    for vin in master.heldLimits.holding():
        vehicle = master.vehicleRegistry.get(vin)
        if vehicle is None:
            continue
        api = vehicle.attachment("TeslaAPI")
        if read and api is not None and vehicle.syncSource == "TeslaAPI":
            if Presence.twcVinAt(master, vehicle) == Presence.NEVER:
                vehicle.read("chargingState", READ_MAX_AGE)
        at = Presence.departed(master, vehicle)
        write = vehicle.lastWrite("chargeLimit")
        # <= on purpose: a retained MQTT position redelivered after our write
        # is stamped with that write's time, and is not a new fix
        if at is None or write is not None and at <= write.at:
            continue
        _restore(master, vehicle, decision=True)
