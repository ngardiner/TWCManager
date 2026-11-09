import hashlib
import json
import math
import time

from TWCManager.EVSEInstance.Gen2TWC import Gen2TWC
from TWCManager.EVSEInstance.Gen3TWC import Gen3TWC
from TWCManager.Vehicle.Presence import presence
from TWCManager.Vehicle.Reconcile.Belief import known, newerReading
from TWCManager.Vehicle.Reconcile.Desired import AMPS
from TWCManager.Vehicle.Reconcile.Outcome import RULES
from TWCManager.Vehicle.Reconcile.Rules import satisfied
from TWCManager.Vehicle.Registry.VIN import vehicleRecordKey

# Service start; folded into every fingerprint so a restart invalidates pages
STARTED = time.time()


def fingerprint(*shape):
    # Short digest of the page structure; changes when the shape or the service start changes
    raw = json.dumps([STARTED] + list(shape), sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def jsonSafe(obj):
    # Copy of obj that json.dumps(allow_nan=False) always accepts
    if isinstance(obj, dict):
        return {str(k): jsonSafe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonSafe(v) for v in obj]
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if obj is None or isinstance(obj, (bool, int, str)):
        return obj
    return str(obj)


def slaveTotals(master) -> dict:
    # The "total" row of /api/getSlaveTWCs
    totals = {
        "carsCharging": 0,
        "lastAmpsOffered": 0,
        "lifetimekWh": 0,
        "maxAmps": 0,
        "reportedAmpsActual": 0,
    }
    for slaveTWC in master.getSlaveTWCs():
        totals["carsCharging"] += slaveTWC.isCharging
        totals["lastAmpsOffered"] += slaveTWC.lastAmpsOffered
        totals["lifetimekWh"] += slaveTWC.lifetimekWh
        totals["maxAmps"] += slaveTWC.maxAmps
        totals["reportedAmpsActual"] += slaveTWC.reportedAmpsActual

    # A car on a charger no TWC reports is offered its share as its own
    # charge rate (its desired amps), which no slave carries; a car on a
    # TWC is already in that TWC's offer
    for point in master.chargingPoints():
        if point.localEndpoints:
            continue
        for endpoint in point.vehicleEndpoints:
            d = master.desired.get(endpoint.vehicle.key, AMPS)
            totals["lastAmpsOffered"] += d.value if d else 0

    return {
        "carsCharging": totals["carsCharging"],
        "lastAmpsOffered": round(totals["lastAmpsOffered"], 2),
        "lifetimekWh": totals["lifetimekWh"],
        "maxAmps": totals["maxAmps"],
        "reportedAmpsActual": round(totals["reportedAmpsActual"], 2),
        "TWCID": "total",
    }


def _member(point, kind):
    return next((e for e in point.endpoints if isinstance(e, kind)), None)


def _gen2View(twc) -> dict:
    return {
        "twcid": twc.twcid,
        "state": twc.reportedState,
        "version": twc.protocolVersion,
        "lifetimekWh": twc.lifetimekWh,
        "maxAmps": float(twc.maxAmps),
        "lastHeardAt": twc.timeLastRx,
        "currentVIN": twc.currentVIN,
        "lastVIN": twc.lastVIN,
        "chargingSince": twc.lastChargingStart if twc.lastChargingStart > 0 else None,
    }


def _gen3View(unit) -> dict:
    # From the cached vitals only; a field the vitals lack is None, and so is
    # every vitals field after a failed fetch (the cache keeps the old ones)
    vitals = unit.vitals if unit.isReachable else {}
    connected = vitals.get("vehicle_connected")
    return {
        "id": unit.ID,
        "reachable": unit.isReachable,
        "state": vitals.get("evse_state"),
        "vehicleConnected": None if connected is None else bool(connected),
        "sessionEnergyWh": vitals.get("session_energy_wh"),
    }


def _pointView(master, point) -> dict:
    # One charging point from cached state only: no read, no fetch
    car = master.vehicleRegistry.get(point.key)
    gen2, gen3 = _member(point, Gen2TWC), _member(point, Gen3TWC)
    if gen2 is not None:
        offered, drawn = gen2.lastAmpsOffered, gen2.reportedAmpsActual
    elif gen3 is not None:
        offered = gen3.convertWattsToAmps(getattr(gen3, "lastTarget", 0) or 0)
        drawn = gen3.currentAmps
    else:
        offered = 0
        for e in point.vehicleEndpoints:
            d = master.desired.get(e.vehicle.key, AMPS)
            offered += d.value if d else 0
        drawn = (car.value("actualCurrent") if car else None) or 0
    volts = point.currentVoltage
    return {
        "id": point.ID,
        "controllers": point.controllers,
        "hasTWC": bool(point.localEndpoints),
        "vin": car.vin if car else None,
        "name": car.name if car else "",
        "anonymous": car.isAnonymous if car else False,
        "appearance": car.appearance() if car else {},
        "ampsOffered": float(offered),
        "ampsDrawn": float(drawn),
        "volts": list(volts),
        "minAmps": float(point.convertWattsToAmps(point.minPower, volts)),
        "maxAmps": float(point.convertWattsToAmps(point.maxPower, volts)),
        "soc": car.value("batteryLevel") if car else None,
        "chargeLimit": car.value("chargeLimit") if car else None,
        "gen2": _gen2View(gen2) if gen2 is not None else None,
        "gen3": _gen3View(gen3) if gen3 is not None else None,
    }


def pointsTotal(points) -> dict:
    # The total row over the listed points; lifetime kWh only Gen2 TWCs report
    return {
        "ampsOffered": round(sum((p["ampsOffered"] for p in points), 0.0), 2),
        "ampsDrawn": round(sum((p["ampsDrawn"] for p in points), 0.0), 2),
        "maxAmps": round(
            sum(
                (p["gen2"]["maxAmps"] if p["gen2"] else p["maxAmps"] for p in points),
                0.0,
            ),
            2,
        ),
        "lifetimekWh": sum(p["gen2"]["lifetimekWh"] for p in points if p["gen2"]),
        "carsCharging": sum(1 for p in points if p["ampsDrawn"] > 0),
    }


def chargingPointsView(master) -> dict:
    # /api/getChargingPoints: every charging point, for the home pages
    points = [_pointView(master, p) for p in master.chargingPoints()]
    return {
        "fingerprint": pageFingerprint(master, "home"),
        "now": time.time(),
        "points": points,
        "total": pointsTotal(points),
    }


def _settingsShape(master):
    # Vehicle group and charge history names, read under the settings lock
    with master.settingsLock:
        groups = sorted(master.settings.get("VehicleGroups", {}))
        history = master.settings.get("Vehicles", {})
        historyKeys = sorted(history)
        # A list in VIN order, so the page addresses rows by position: a
        # settings key may hold a "." that a hook path cannot
        rows = [
            {
                "key": k,
                "chargeSessions": history[k].get("chargeSessions", 0),
                "totalkWh": history[k].get("totalkWh", 0),
            }
            for k in historyKeys
        ]
    return groups, historyKeys, rows


def pageFingerprint(master, page, vin=None) -> str:
    # The fingerprint a live page carries; equals its API response's
    if page == "home":
        return fingerprint("home")
    if page == "vehicles":
        keys = [v.key for v in master.vehicleRegistry.vehicles()]
        groups, historyKeys, _ = _settingsShape(master)
        fleet, telemetry = spendShown(master)
        return fingerprint("vehicles", keys, groups, historyKeys, fleet or telemetry)
    if page == "vehicle":
        vin = str(vin or "")
        groups, _, _ = _settingsShape(master)
        exists = master.vehicleRegistry.get(vin) is not None
        return fingerprint("vehicle", vin.upper(), exists, groups)
    if page == "policy":
        names = [p.get("name") for p in master.getModuleByName("Policy").charge_policy]
        return fingerprint("policy", names)
    raise ValueError("no live page " + repr(page))


def spendShown(master):
    # (fleet, telemetry): Fleet API spend only with a Fleet token, telemetry
    # only with its module
    api = master.getModuleByName("TeslaAPI")
    fleet = bool(api and getattr(api.fleetSlot, "accessToken", ""))
    return fleet, master.getModuleByName("FleetTelemetryMQTT") is not None


def providerClaims(master, vehicle) -> list:
    # One entry per provider claim, in claim order; whether it can command
    # the car and, when it cannot, why (if known)
    api = master.getModuleByName("TeslaAPI")
    # The account listing claims the car for both APIs; without its token an
    # API can neither read nor command it
    held = {"OwnerAPI": "ownerTokenHeld", "FleetAPI": "fleetTokenHeld"}
    out = []
    for name, claim in vehicle.claims.items():
        if api and name in held and not getattr(api, held[name])():
            continue
        can = bool(claim.commands)
        why = None
        if not can and api and name == "OwnerAPI":
            why = api.ownerReadOnlyReason(vehicle.attachment("TeslaAPI"))
        elif not can and api and name == "FleetAPI":
            why = api.fleetReadOnlyReason()
        out.append(
            {
                "provider": name,
                "status": claim.status,
                "commands": can,
                "readOnlyReason": why or None,
            }
        )
    return out


def pendingCommandData(master, vehicle) -> list:
    # Each desire the car has not reached: what is in flight, what the
    # reconciler said, and the last outcome. Reads only: never creates state
    state, rows = master.desired.peek(vehicle.key), []
    for aspect, d in list(state.desired.items()) if state else []:
        rec = state.records.get(aspect)
        if rec is None:
            continue
        sent, last = rec.inFlight, rec.lastOutcome
        if not sent and (
            satisfied(aspect, d.value, known(vehicle, aspect, d.value))
            or (
                # Settled, the reconciler waits for a reading saying otherwise
                rec.settledAt is not None
                and not newerReading(vehicle, aspect, rec.settledAt)
            )
        ):
            continue
        rows.append(
            {
                "aspect": aspect,
                "value": d.value,
                "inFlight": (
                    {"provider": sent.provider, "since": sent.since} if sent else None
                ),
                "said": master.reconciler.blockedReason(vehicle.key, aspect),
                "lastOutcome": (
                    {
                        "kind": last.kind,
                        "detail": last.detail.replace(RULES, ""),
                        "provider": rec.lastProvider,
                        "at": rec.lastOutcomeAt,
                    }
                    if last
                    else None
                ),
            }
        )
    return rows


def vehicleEntry(master, vehicle, fleet, telemetry, now=None) -> dict:
    # One vehicle from cached state only
    now = time.time() if now is None else now
    row = vehicle.summary()
    limit = master.heldLimits.get(vehicle.vin) if vehicle.vin else None
    return {
        "key": vehicle.key,
        "vin": row["vin"],
        "name": row["name"],
        "anonymous": row["anonymous"],
        "point": row["point"],
        "appearance": row["appearance"],
        "presence": presence(master, vehicle),
        "watch": (
            master.watch.status(vehicle)
            if master.watch
            else {"watched": False, "why": "off"}
        ),
        "providers": providerClaims(master, vehicle),
        "lastSeenAt": now - row["lastSeenAge"],
        "lastSeenSource": row["lastSeenSource"],
        "heldLimit": (
            {"held": limit.held, "outside": limit.outside}
            if limit is not None
            else None
        ),
        "spend": (
            master.fleetSpend.car(vehicle.vin)
            if (fleet or telemetry) and not row["anonymous"]
            else None
        ),
    }


def vehiclesView(master) -> dict:
    # /api/getVehicles: every vehicle, for the Vehicles page
    now = time.time()
    fleet, telemetry = spendShown(master)
    vehicles = master.vehicleRegistry.vehicles()
    groups, historyKeys, history = _settingsShape(master)
    return {
        "fingerprint": fingerprint(
            "vehicles", [v.key for v in vehicles], groups, historyKeys, fleet or telemetry
        ),
        "now": now,
        "budget": master.watchBudget.tokens(),
        "spend": {
            "fleet": fleet,
            "telemetry": telemetry,
            "month": master.fleetSpend.month() if fleet or telemetry else None,
        },
        "vehicles": [vehicleEntry(master, v, fleet, telemetry, now) for v in vehicles],
        "history": history,
    }


def vehicleView(master, vin):
    # /api/getVehicle/<VIN>: one vehicle, for its details page; None if unknown
    vehicle = master.vehicleRegistry.get(vin)
    if vehicle is None:
        return None
    now = time.time()
    fleet, telemetry = spendShown(master)
    ble = master.getModuleByName("TeslaBLE")
    with master.settingsLock:
        history = master.settings.get("Vehicles", {})
        rec = history.get(vehicleRecordKey(history, vehicle.vin or vin), {})
        sessions = rec.get("chargeSessions", 0)
    return {
        "fingerprint": pageFingerprint(master, "vehicle", vin),
        "now": now,
        "budget": master.watchBudget.tokens(),
        "spend": {"fleet": fleet, "telemetry": telemetry},
        "vehicle": dict(
            vehicleEntry(master, vehicle, fleet, telemetry, now),
            pending=pendingCommandData(master, vehicle),
            ble=ble.bleStatus(vehicle.vin) if ble and vehicle.vin else None,
            chargeSessions=sessions,
        ),
    }
