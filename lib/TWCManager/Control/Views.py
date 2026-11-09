import hashlib
import json
import math
import time

from TWCManager.EVSEInstance.Gen2TWC import Gen2TWC
from TWCManager.EVSEInstance.Gen3TWC import Gen3TWC
from TWCManager.Vehicle.Reconcile.Desired import AMPS

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
        "fingerprint": fingerprint("home"),
        "now": time.time(),
        "points": points,
        "total": pointsTotal(points),
    }
