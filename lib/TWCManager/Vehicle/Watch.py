# The watch: keeps cars that no TWC names and no live feed covers observed,
# asking the free sleep-state question on a clock set by each car's
# situation. An asleep car is neither charging nor moving, so that answer
# alone carries most of the time; the costlier charge read runs when
# something changes, or on a clock while the car charges. The control
# tick only decides what is due; the asking runs on the reconciler's workers
import logging
import threading
import time
from typing import Optional

from .Presence import HOME, NEVER, _ble, asleep, decide
from .Presence import AWAY_ASLEEP_SECS, HOME_ASLEEP_SECS, covered, presence
from .Presence import twcVinAt
from .Providers.Capability import READ_CHARGE, SET_AMPS
from .Reconcile.Belief import standIn
from .WatchBudget import EVENT, ROUTINE

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Watch")

# Each awake or asleep situation is also its cadence setting's stem
HOME_ASLEEP, HOME_AWAKE, HOME_CHARGING, AWAY_ASLEEP, AWAY_AWAKE, UNKNOWN = (
    "homeAsleep homeAwake homeCharging awayAsleep awayAwake unknown".split()
)
DEFAULTS = dict(homeAsleepSecs=HOME_ASLEEP_SECS, awayAsleepSecs=AWAY_ASLEEP_SECS)
DEFAULTS.update(homeAwakeSecs=240)
DEFAULTS.update(awayAwakeSecs=300, chargingReadSecs=300, cloudChargingReadSecs=900)
DEFAULTS.update(sleepCourtesySecs=1200)
DEFAULTS.update(monthlyBudgetUSD=3.0, enabled=True)
# A charging car is awake; a car of unknown state is asked as an awake one
# away: until it answers, nothing says it cannot move
_CADENCE_OF = {HOME_CHARGING: HOME_AWAKE, UNKNOWN: AWAY_AWAKE}
NO_SOURCE, NAMED = "no status source", "named by TWC "
MISS, AWAKE = "no answer", (HOME_AWAKE, HOME_CHARGING, AWAY_AWAKE)
# A failed read asked for, retried as its ask was: courtesy-bounded or not
RETRY = ("failed, asking again later", "failed, asking again")
KINDS = ("status", "read")


def settingsOf(config) -> dict:
    return dict(DEFAULTS, **(config.get("watch") or {}))


def notWatched(master, v) -> Optional[str]:
    # Why the car is left alone, or None. A TWC naming it reports its changes
    # on the heartbeat; a live report-on-change feed reports them itself
    if v.isAnonymous:
        return "anonymous"
    if not settingsOf(master.config)["enabled"]:
        return "disabled"
    if twcVinAt(master, v) > NEVER:
        return NAMED + str(v.value("twcVin"))
    if covered(master, v):
        return "covered by " + v.syncSource
    return None


def radio(master):
    # The BLE module, whose pings ask first
    return _ble(master)


def cloudOnly(master, v) -> bool:
    # Watched with no radio to hear it come and go: the Tesla API's status
    # checks alone see it, and serve a held command's wake too
    api = v.attachment("TeslaAPI") is not None
    return api and radio(master) is None and notWatched(master, v) is None


def situation(master, v, where=None) -> str:
    # where: the car's presence, when the caller has it
    slept, state = asleep(v), v.value("state")
    if slept is None and state is None:
        return UNKNOWN
    home = (where or presence(master, v)) == HOME
    # A feed or the cloud says offline for a sleeping car
    if slept or state == "offline":
        return HOME_ASLEEP if home else AWAY_ASLEEP
    if home and v.value("chargingState") == "Charging":
        return HOME_CHARGING
    return HOME_AWAKE if home else AWAY_AWAKE


class Watch:
    def __init__(self, master, clock=time.time):
        self.master, self._clock = master, clock
        self._settings = settingsOf(master.config)
        # A leaf: never held across a registry or provider call, or a submit
        self._lock = threading.Lock()
        # (vin, job kind) queued or running, and its last answer and time,
        # which with the car's situation now sets the next; vin -> what the
        # last pass saw; a read asked for: (budget class, whether the
        # courtesy window yields to it)
        self._inflight, self._last, self._seen, self._want = set(), {}, {}, {}
        self._started = None

    def _cadence(self, state) -> float:
        return float(self._settings[_CADENCE_OF.get(state, state) + "Secs"])

    def _source(self, v, ble):
        # (worker, the check): the radio first; with a radio the cloud is
        # never asked
        getModule = self.master.getModuleByName
        if ble is not None:
            return "ble", lambda: ble.ping(v.vin, scan=False)
        api = v.attachment("TeslaAPI") is not None and getModule("TeslaAPI")
        if api:
            return "cloud", lambda: api.onlineState(v) is not None
        ha, hv = getModule("HomeAssistant"), v.attachment("HomeAssistant")
        if ha is not None and hv is not None:
            return "cloud", lambda: hv.refresh_from_home_assistant(ha)
        return None

    def readNow(self, vin: str, reason: str, cls=EVENT) -> None:
        logger.debug("Read of %s asked: %s", vin, reason)
        with self._lock:
            self._ask(vin, cls, True)

    def _ask(self, vin, cls, exempt) -> None:
        # Under the lock; an event outranks a routine ask
        was = self._want.get(vin, (cls, False))
        self._want[vin] = (EVENT if EVENT in (cls, was[0]) else cls, exempt or was[1])

    def pass_(self, now: Optional[float] = None) -> None:
        now = self._clock() if now is None else now
        for v in self.master.vehicleRegistry.vehicles():
            try:
                if not v.isAnonymous:
                    self._visit(v, now)
            except Exception as e:
                logger.warning("Watching %s raised %s", v.vin, type(e).__name__)
        self._started = self._started or now

    def _visit(self, v, now) -> None:
        vin, (where, _, since) = v.vin, decide(self.master, v)
        state, door = situation(self.master, v, where), v.value("chargePortOpen")
        seen = (state, where, v.markAt("plugIn"), door)
        why = notWatched(self.master, v)
        named, ble = bool(why) and why.startswith(NAMED), radio(self.master)
        with self._lock:
            # What a car was before a restart is no news: the first pass
            # only learns it. A car first seen later has arrived
            fresh = seen if not self._started else (None, None, NEVER, None)
            was, self._seen[vin] = self._seen.get(vin, fresh), seen
            busy = {kind for key, kind in self._inflight if key == vin}
            (_, at), (tried, lastRead) = (
                self._last.get((vin, kind), ("", NEVER)) for kind in KINDS
            )
            # An arrival by any route, a wake, a plug-in (one a read made by
            # reporting it is already read), or an awake car's door opening,
            # which only a car in BLE range, so here, reports
            woke = was[0] in (HOME_ASLEEP, AWAY_ASLEEP) and seen[0] in AWAKE
            if woke or where == HOME and was[1] != HOME and since > lastRead:
                self._ask(vin, EVENT, False)
            if seen[2] > max(was[2], lastRead) or door and not was[3]:
                self._ask(vin, EVENT, True)
            if why and not named:
                # Covered or not watched: an old ask must not fire later
                self._want.pop(vin, None)
            want = self._want.get(vin)
        if "read" not in busy and (why is None or named):
            self._read(v, seen[0], named, want, tried, lastRead, now)
        if "status" not in busy and now >= at + self._cadence(seen[0]):
            # Coverage counts for the check only when it is due: a feed
            # blip between checks changes nothing
            source = None if why else self._source(v, ble)
            if source is None:
                self._settle(vin, "status", now)
            else:
                self._submit(v, "status", source[1], source[0])

    def _read(self, v, state, named, want, tried, lastRead, now) -> None:
        # The charge read due now, if any. A TWC-named car is touched only for
        # its plug-in, on request, and while its TWC reports current
        if state in (HOME_ASLEEP, AWAY_ASLEEP):
            # Never woken for a read: an arrival waits for the wake
            return
        if tried in RETRY and now >= lastRead + self._settings["chargingReadSecs"]:
            # A failed read asked for, once more, a charging read's cadence on
            want = want or (EVENT, bool(RETRY.index(tried)))
        recent = now < lastRead + self._settings["sleepCourtesySecs"]
        if want and not want[1] and (named or state == HOME_AWAKE and recent):
            self._take(v.vin, want)
            want = None
        flowing = getattr(standIn(v), "value", None) == "Charging"
        if not want and not (flowing if named else state == HOME_CHARGING):
            return
        order = self.master.providerOrder
        found = order.candidates(v, READ_CHARGE)
        local = [p for p in found if p.local]
        if not found or state == UNKNOWN and local and not named:
            # Over the radio only once the ping says it is awake; a cloud
            # read asks the free online question first by itself
            return
        found = local or found[:1]
        price = found[0].cost(READ_CHARGE).price or 0.0
        secs = self._settings["cloudChargingReadSecs" if price else "chargingReadSecs"]
        # Nothing can act on the amps of a car no provider can set. A paid
        # read buys the battery level, worth it only where a policy reads it:
        # the car's sleep says when its charge ended
        policy = self.master.getModuleByName("Policy")
        due = now >= lastRead + secs and (not price or policy and policy.readsBattery())
        if not want and not (due and order.candidates(v, SET_AMPS)):
            return
        self._take(v.vin, want)
        cls = want[0] if want else ROUTINE
        miss = RETRY[want[1]] if cls == EVENT and tried not in RETRY else MISS
        # A cloud read pays from the budget first; a refusal waits its cadence
        worker = "ble" if found[0].local else "cloud"
        if worker == "cloud" and not self.master.watchBudget.spend(price, cls):
            logger.debug("No budget for a %s read of %s", cls, v.vin)
            self._settle(v.vin, "read", now)
            return
        # The cascade itself: None when the read failed, not the old value
        read = lambda: order.read(v, "chargingState", False, found) is not None
        self._submit(v, "read", read, worker, miss)

    def _take(self, vin, want) -> None:
        # Drops the ask acted on, not one made since
        with self._lock:
            if want is not None and self._want.get(vin) == want:
                del self._want[vin]

    def _submit(self, v, kind, run, worker, miss=MISS) -> None:
        # Its completion stamps it with the answer, or miss, or why it never
        # ran, as does a refused submit: nothing can leave the car in flight
        vin = v.vin
        with self._lock:
            self._inflight.add((vin, kind))

        def done(result):
            # A check that answered says the sleep state it learned
            if result is True:
                result = v.value("state") or "answered"
            self._settle(vin, kind, answer=result if isinstance(result, str) else miss)

        if not self.master.reconciler.submitJob(vin, kind, run, worker, done):
            self._settle(vin, kind)

    def _settle(self, vin, kind, at=None, answer="") -> None:
        at = self._clock() if at is None else at
        with self._lock:
            self._inflight.discard((vin, kind))
            self._last[(vin, kind)] = (answer, at)

    def status(self, v) -> dict:
        ble = radio(self.master)
        with self._lock:
            last, at = self._last.get((v.vin, "status"), ("", None))
            seen = self._seen.get(v.vin)
        source = self._source(v, ble)
        via = (source or (None,))[0]
        why = notWatched(self.master, v) or ("" if via else NO_SOURCE)
        nextAt = None
        if at is not None and seen is not None:
            nextAt = at + self._cadence(seen[0])
        watched = why in ("", NO_SOURCE)
        return dict(
            watched=watched, why=why, via=via, last=last, lastAt=at, nextAt=nextAt
        )
