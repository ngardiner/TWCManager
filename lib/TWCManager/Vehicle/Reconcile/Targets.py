# Which cars a decision applies to, and the charge-limit decisions that
# write desired state: the policy's limit, a plug-in and a departure.
#
# Presence, until Phase 5 replaces it: a car on a charging point; else the
# Tesla API's atHome; else Home Assistant's at_home; else, for a car only BLE
# knows, its BLE key (today's broadcast reach). A car with no provider able
# to carry the capability gets no desired state.
from typing import List, Optional

from ..Providers.Capability import (
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
)
from .Desired import CHARGE_LIMIT


def _present(vehicle) -> bool:
    if vehicle.isAnonymous:
        # Only on a point, and only while plugged in
        return True
    if vehicle.value("twcVin"):
        return True
    api = vehicle.attachment("TeslaAPI")
    if api is not None:
        return bool(api.atHome)
    ha = vehicle.attachment("HomeAssistant")
    if ha is not None:
        return bool(ha.at_home)
    return "TeslaBLE" in vehicle.claims


def presentVehicles(master, capability) -> List:
    order = master.providerOrder
    return [
        v
        for v in master.vehicleRegistry.vehicles()
        if (not v.isAnonymous or capability in (START_CHARGING, STOP_CHARGING))
        and _present(v)
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


def applyPolicyLimit(master, limit: Optional[int], decision: bool) -> int:
    touched = 0
    for vehicle in presentVehicles(master, SET_CHARGE_LIMIT):
        if not vehicle.vin:
            continue
        if limit is None:
            _restore(master, vehicle, decision)
        else:
            master.desired.set(vehicle.key, CHARGE_LIMIT, limit, "policy", decision)
        touched += 1
    return touched


def onArrival(master) -> None:
    # A plug-in is a decision: it ends an adopted value
    applyPolicyLimit(master, master.policyLimit, decision=True)


def onDeparture(master) -> None:
    for vin in master.heldLimits.holding():
        vehicle = master.vehicleRegistry.get(vin)
        if vehicle is not None and not _present(vehicle):
            _restore(master, vehicle, decision=True)
