# Which cars a decision applies to, and the charge-limit decisions that
# write desired state: the policy's limit, a plug-in and a departure.
#
# Presence, until Phase 5 replaces it: a car on a charging point; else the
# Tesla API's home check on the latest known position (free, any source);
# with none, a car we hold a limit on (main's wasAtHome); else Home
# Assistant's at_home; else, for a car only BLE knows, its BLE key (today's
# broadcast reach). A car with no provider able to carry the capability gets
# no desired state.
from typing import List, Optional

from ..Providers.Capability import (
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
)
from .Desired import CHARGE_LIMIT


def _located(vehicle) -> Optional[bool]:
    # The Tesla API's home check on the latest position; None if unknown
    api = vehicle.attachment("TeslaAPI")
    return getattr(api, "locate", lambda: None)()


def _present(master, vehicle) -> bool:
    if vehicle.isAnonymous:
        # Only on a point, and only while plugged in
        return True
    if vehicle.value("twcVin"):
        return True
    api = vehicle.attachment("TeslaAPI")
    if api is not None:
        home = _located(vehicle)
        if home is not None:
            return home
        held = master.heldLimits.get(vehicle.vin)
        if api.atHome or held is not None and held.held is not None:
            return True
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
        and _present(master, v)
        and order.candidates(v, capability)
    ]


def broadcastVehicles(master, capability) -> List:
    # Main's unaddressed car_api_charge: every car on the Tesla API's list at
    # home (or not yet located), every VIN BLE knows, and every Home
    # Assistant car at home that a TWC reports by VIN
    def reached(v):
        api = v.attachment("TeslaAPI")
        home = api is not None and (
            _located(v) or api.atHome or getattr(api, "lat", 0) == 10000
        )
        ha = v.attachment("HomeAssistant")
        haHome = ha is not None and ha.at_home and master.getTWCbyVIN(v.vin)
        return home or bool(haHome) or "TeslaBLE" in v.claims

    order = master.providerOrder
    return [
        v
        for v in master.vehicleRegistry.vehicles()
        if not v.isAnonymous and reached(v) and order.candidates(v, capability)
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
    for vin in master.heldLimits.holding():
        # Departure with no edge: a held car its free position places away
        # (main located each wasAtHome car on every apply)
        vehicle = master.vehicleRegistry.get(vin)
        if vehicle and not vehicle.value("twcVin") and _located(vehicle) is False:
            _restore(master, vehicle, decision=True)
    return touched


def onArrival(master) -> None:
    # A plug-in is a decision: it ends an adopted value
    applyPolicyLimit(master, master.policyLimit, decision=True)


def onDeparture(master) -> None:
    # Background thread only. A car off its point that the Tesla API knows
    # is located first, unless live telemetry already has (a paid
    # vehicle_data that never wakes; only for cars we hold a limit on): an
    # unplug alone is not a departure
    for vin in master.heldLimits.holding():
        vehicle = master.vehicleRegistry.get(vin)
        if vehicle is None:
            continue
        api = vehicle.attachment("TeslaAPI")
        if api is not None and not vehicle.value("twcVin"):
            if api.syncSource == "TeslaAPI":
                api.update_location(0)
        if not _present(master, vehicle):
            _restore(master, vehicle, decision=True)
