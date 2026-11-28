# How many phases a car draws on, from what it reports. A three-phase
# charging point serving a single-phase car delivers one phase's watts per
# amp, not three: pricing it at the point's own phases overstates its load
# and understates the amps a watts target asks for
from typing import Optional

from .Presence import leftAt

# A car drawing less tells too little apart: a 120 V car at 5 A shows the
# same rounded kilowatt as one at twice that
_MIN_AMPS = 5


def carPhases(vehicle) -> Optional[int]:
    """1 or 3, the phases the car draws on now, or None when unknown.

    From its power over its voltage and current where it reports all three
    while drawing: charger_phases has been seen reading 2 for a car drawing
    three. Otherwise its reported count, but 2 only as unknown. Only values
    read since the car last left count: a count read elsewhere says nothing
    about the point it is on now.
    """
    if vehicle is None:
        return None
    since = leftAt(vehicle)

    def fresh(field):
        obs = vehicle.latest(field)
        return obs.value if obs is not None and obs.at > since else None

    kw, volts, amps = (fresh(f) for f in ("chargerPower", "voltage", "actualCurrent"))
    try:
        if kw and volts and volts >= 90 and amps and amps >= _MIN_AMPS:
            ratio = kw * 1000 / (volts * amps)
            if ratio < 1.5:
                return 1
            if ratio > 2.5:
                return 3
    except (TypeError, ValueError):
        pass
    phases = fresh("phases")
    return phases if phases in (1, 3) else None


def maskPhases(voltage: list, phases: Optional[int]) -> list:
    # The point's per-phase voltages, kept on its first `phases` live phases
    # and zero on the rest; unknown phases change nothing, nor do more phases
    # than the point has
    if not phases:
        return voltage
    out, kept = [], 0
    for v in voltage:
        live = v > 0 and kept < phases
        kept += live
        out.append(v if live else 0)
    return out
