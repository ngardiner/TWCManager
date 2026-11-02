# The watch: keeps cars that no TWC names and no live feed covers observed,
# asking the free sleep-state question on a clock set by each car's
# situation. An asleep car is neither charging nor moving, so that answer
# alone carries most of the time; the costlier charge read runs when
# something changes, or on a clock while the car charges or only the cloud
# sees it. The control tick only decides what is due; the asking runs on
# the reconciler's workers. A car only the cloud reads is never paid for its
# charge: its free snapshot is polled while a charge may have started or
# ended, and its sessions rebuilt from them
import logging
import math
import threading
import time
from collections import namedtuple
from typing import Optional

from .ChargeLedger import CHARGING, ChargeLedger, Snapshot
from .Presence import HOME, NEVER, _ble, _fix, _FT_PER_DEGREE, asleep, decide, lastLife
from .Presence import AWAY_ASLEEP_SECS, HOME_ASLEEP_SECS, NOT_HOME, covered, presence
from .Presence import twcVinAt
from .Providers.Capability import READ_CHARGE, SET_AMPS
from .Reconcile.Belief import twcChargingState
from .WatchBudget import EVENT, ROUTINE

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Watch")

# Each awake or asleep situation is also its cadence setting's stem
HOME_ASLEEP, HOME_AWAKE, HOME_CHARGING, AWAY_ASLEEP, AWAY_AWAKE, UNKNOWN = (
    "homeAsleep homeAwake homeCharging awayAsleep awayAwake unknown".split()
)
DEFAULTS = dict(homeAsleepSecs=HOME_ASLEEP_SECS, awayAsleepSecs=AWAY_ASLEEP_SECS)
DEFAULTS.update(homeAwakeSecs=240)
DEFAULTS.update(awayAwakeSecs=300, chargingReadSecs=300, cloudChargingReadSecs=900)
DEFAULTS.update(scanAwaySecs=150, sleepCourtesySecs=1200, returnKph=120)
DEFAULTS.update(monthlyBudgetUSD=3.0, enabled=True, emsTrigger=True, emsNoiseWatts=300)
DEFAULTS.update(pollSecs=60, pollGiveUpSecs=600)
# A charging car is awake; a car of unknown state is asked as an awake one
# away: until it answers, nothing says it cannot move
_CADENCE_OF = {HOME_CHARGING: HOME_AWAKE, UNKNOWN: AWAY_AWAKE}
NO_SOURCE, NAMED, COVERED = "no status source", "named by TWC ", "covered by "
MISS, AWAKE = "no answer", (HOME_AWAKE, HOME_CHARGING, AWAY_AWAKE)
# A failed read asked for, retried as its ask was: courtesy-bounded or not
RETRY_COURTEOUS, RETRY_EXEMPT = "failed, asking again later", "failed, asking again"
RETRY, TWICE = (RETRY_COURTEOUS, RETRY_EXEMPT), "no answer twice"
# A read asked for: its budget class, and whether the courtesy window yields
Ask = namedtuple("Ask", "cls exempt")
KINDS = ("status", "read", "scan", "poll")
CACHED, ASLEEP = "FleetAPICached", (HOME_ASLEEP, AWAY_ASLEEP)
# A load step this soon before a session's first snapshot is its start
STEP_SECS = 600


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
        return COVERED + v.syncSource
    return None


def radio(master):
    # The BLE module while its pings can run
    ble = _ble(master)
    return ble if getattr(ble, "radioUp", bool)() else None


def cloudOnly(master, v) -> bool:
    # Watched with no radio to hear it come and go: the Tesla API's status
    # checks alone see it, and serve a held command's wake too
    api = v.attachment("TeslaAPI") is not None
    return api and radio(master) is None and notWatched(master, v) is None


def fleetOnly(master, v, deaf=False) -> bool:
    # Watched, with its charge read only through the cloud, and paid for: no
    # radio, or (deaf) one that missed it twice where no scan could settle
    # it. An Owner token's read is free and live, and is served no
    # snapshot. The tokens held decide it, not a slot's pause, so a session
    # outlives a pause
    api = v.attachment("TeslaAPI") is not None and notWatched(master, v) is None
    if not (api if deaf else cloudOnly(master, v)):
        return False
    carapi = master.getModuleByName("TeslaAPI")
    return bool(carapi and carapi.fleetTokenHeld() and not carapi.ownerTokenHeld())


def snapshot(v, cutoff) -> Optional[Snapshot]:
    # The cached snapshot's own readings, once one clears cutoff: a field it
    # left out carries an older stamp, and counts as missing
    got = {
        f: v.reported(f).get(CACHED)
        for f in ("chargingState", "chargeEnergyAdded", "actualCurrent", "voltage", "phases")
    }
    state = got.pop("chargingState")
    if state is None or state.at < cutoff:
        return None
    of = {f: o.value if o is not None and o.at == state.at else None for f, o in got.items()}
    watts = (of["actualCurrent"] or 0) * (of["voltage"] or 0) * (of["phases"] or 1)
    return Snapshot(state.at, state.value, of["chargeEnergyAdded"], watts)


def evidence(v) -> float:
    # When the car's charge was last read or commanded
    seen = [v.latest(f) for f in ("chargingState", "actualCurrent")]
    seen += [v.lastWrite(f) for f in ("chargingState", "chargeCurrentRequest")]
    return max((o.at for o in seen if o is not None), default=NEVER)


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


def scans(ble) -> bool:
    return getattr(ble, "canScan", bool)()


def lost(v, where) -> bool:
    # Away, and the radio's two misses said so
    return where != HOME and v.value("bleReach") is False


class LoadStep:
    # A rise or fall of at least the threshold, reported once the next sample
    # holds it: a spike that falls back says nothing
    def __init__(self, thresholdWatts: float):
        self.threshold, self._base, self._pending = thresholdWatts, None, None

    def sample(self, watts: float) -> Optional[str]:
        # The first sample is its own base, and a NaN reading stays NaN
        moved = watts - (watts if self._base is None else self._base)
        step = None if abs(moved) < self.threshold else "up" if moved > 0 else "down"
        confirmed = step == self._pending
        # A step reported is no longer pending; a new one waits for the next
        self._pending = None if confirmed else step
        if step is None or confirmed:
            self._base = watts
        return step if confirmed else None


class Watch:
    def __init__(self, master, clock=time.time):
        self.master, self._clock = master, clock
        self._settings = settingsOf(master.config)
        # The least a car draws, or the meter's noise; the step's state is the
        # background-tasks thread's alone, where (vin, step) -> its last read
        volts, phases = master.getVoltageMeasurement()
        minAmpsPerPhase = 5 if phases < 3 else 1
        least = max(self._settings["emsNoiseWatts"], minAmpsPerPhase * volts * phases)
        self._step, self._triggered = LoadStep(least), {}
        # A leaf: never held across a registry or provider call, or a submit
        self._lock = threading.Lock()
        # (vin, job kind) queued or running, and its last answer and time,
        # which with the car's situation now sets the next; vin -> what the
        # last pass saw; vin -> the read asked for, an Ask
        self._inflight, self._last, self._seen, self._want = set(), {}, {}, {}
        # Cars at home the radio stopped hearing, already read for it; and
        # those whose read for where they are has not yet run
        self._unheard, self._locate = set(), set()
        # The last load sample's time, and its time if its believed charging
        # was above the meter's consumption
        self._started, self._sampleAt, self._overAt = None, None, None
        # vin -> (since, the latest load step since): a charge may have
        # changed since then
        self._suspect, self.ledger = {}, ChargeLedger(clock)
        # vin -> the evidence a poll gave up on: a belief it could not settle
        # is suspected again only once something newer is known; vin -> the
        # load step an asleep car's next online check answers for
        self._spent, self._waking = {}, {}

    def _cadence(self, state) -> float:
        return float(self._settings[_CADENCE_OF.get(state, state) + "Secs"])

    def _source(self, v, where, ble, last=""):
        # (worker, the check), given the last check's answer. The radio first,
        # unless it lost the car; with a radio the cloud is never asked, as a
        # lost car is listened for. A car in range answers asleep or awake, so
        # a second miss is settled by the ping's scan whatever the car last
        # said; where none can run, away by the miss itself, at home by a read
        getModule, again = self.master.getModuleByName, last in (MISS, TWICE)
        useApi = v.attachment("TeslaAPI") is not None and ble is None
        api, deaf = useApi and getModule("TeslaAPI"), again and not scans(ble)
        if ble is not None and not lost(v, where):

            def ping():
                answered = ble.ping(v.vin, scan=again)
                if deaf and not answered and where != HOME:
                    v.observe("bleReach", False, "TeslaBLE", None, False)
                elif deaf and not answered:
                    # Asleep, its wake asks; charging, its charge is read or
                    # polled. Once until it answers: a car gone or out of
                    # reach stays so
                    awake = situation(self.master, v, where) == HOME_AWAKE
                    with self._lock:
                        ask = awake and v.vin not in self._unheard
                        self._unheard |= {v.vin} if ask else set()
                        self._locate |= {v.vin} if ask else set()
                    if ask:
                        self.readNow(v.vin, "two unanswered pings", exempt=False)
                    return TWICE
                elif answered:
                    with self._lock:
                        self._unheard.discard(v.vin)
                        self._locate.discard(v.vin)
                return answered

            return "ble", ping
        if api:
            return "cloud", lambda: api.onlineState(v) is not None
        ha, hv = getModule("HomeAssistant"), v.attachment("HomeAssistant")
        if ha is not None and hv is not None:
            return "cloud", lambda: hv.refresh_from_home_assistant(ha)
        return None

    def readNow(self, vin: str, reason: str, cls=EVENT, exempt=True) -> None:
        logger.debug("Read of %s asked: %s", vin, reason)
        with self._lock:
            self._ask(vin, cls, exempt)

    def _ask(self, vin, cls, exempt) -> None:
        # Under the lock; an event outranks a routine ask
        was = self._want.get(vin, Ask(cls, False))
        cls = EVENT if EVENT in (cls, was.cls) else cls
        self._want[vin] = Ask(cls, exempt or was.exempt)

    def _watched(self):
        for v in self.master.vehicleRegistry.vehicles():
            if notWatched(self.master, v) is None:
                where = presence(self.master, v)
                yield v, where, situation(self.master, v, where)

    def wantsEMS(self) -> bool:
        # The consumption step needs the meter read on every policy pass,
        # green policy or not, but only while a watched car could be the
        # one starting or stopping: otherwise nothing reads it for the step
        s = self._settings
        return bool(
            s["enabled"]
            and s["emsTrigger"]
            and self.master.getModulesByType("EMS")
            and any(True for _ in self._watched())
        )

    def believedAmps(self, v) -> float:
        # What the car draws, as far as is known. Until a reading newer than
        # a command arrives, the higher of that reading and the commanded
        # rate: a lowered rate or a stop keeps the old draw, and an accepted
        # start counts at its rate before any reading shows it
        if v.isAnonymous or twcVinAt(self.master, v) > NEVER:
            return 0
        where = situation(self.master, v)
        reading = v.value("actualCurrent") or 0 if where == HOME_CHARGING else 0
        seen = v.latest("actualCurrent")
        newer = lambda w: w is not None and (seen is None or w.at > seen.at)
        started, rate = v.lastWrite("chargingState"), v.lastWrite("chargeCurrentRequest")
        charging = started.value == "Charging" if newer(started) else where == HOME_CHARGING
        if not (newer(started) or newer(rate)) or not charging or where not in (HOME_AWAKE, HOME_CHARGING):
            return reading
        return max(reading, rate.value if rate else v.value("chargeCurrentRequestMax") or 0)

    def chargingAmps(self, ledgered: bool = True) -> float:
        # Cars charging at home on chargers that report to no TWC, watched or
        # covered by a feed: a TWC naming a car counts its draw itself.
        # ledgered=False leaves out the cars read only through the cloud
        return sum(
            self.believedAmps(v)
            for v in self.master.vehicleRegistry.vehicles()
            if ledgered or not self._fleet(v)
        )

    def _fleet(self, v) -> bool:
        with self._lock:
            deaf = self._last.get((v.vin, "status"), ("",))[0] == TWICE
        return fleetOnly(self.master, v, deaf)

    def liveWatts(self, vin: str) -> float:
        # An open session's draw since its last snapshot
        v = self.master.vehicleRegistry.get(vin)
        return self.master.convertAmpsToWatts(self.believedAmps(v)) if v else 0.0

    def suspect(self, vin: str, at: float, why: str, step=False) -> None:
        # A charge may have started or ended since at: the poll looks for a
        # snapshot no older. One open keeps its earliest time, and its latest
        # load step, which may be when a charge it finds began
        logger.debug("Suspect %s since %s: %s", vin, at, why)
        with self._lock:
            since, stepAt = self._suspect.get(vin, (at, None))
            self._suspect[vin] = (min(since, at), at if step else stepAt)

    def commanded(self, vin: str, sentAt: float) -> None:
        # The car took a command: its charge may change from the send on
        v = self.master.vehicleRegistry.get(vin)
        if v is not None and self._fleet(v):
            self.suspect(vin, sentAt, "command")

    def unexplainedWatts(self) -> float:
        # Consumption less the charging known of, where the meter sees it
        m = self.master
        if not m.subtractChargerLoad:
            return m.getConsumption()
        return m.getConsumption() - m.getChargerLoad()

    def loadSample(self, now: Optional[float] = None) -> None:
        # A step nothing known explains may be a car starting or ending a
        # charge; a dryer looks the same, so it only asks for a read, or a
        # poll. With no meter, the known draw alone would step with every
        # charge. A step began at the sample before the one that held it
        if not (self._settings["emsTrigger"] and self.master.consumptionValues):
            return
        now = self._clock() if now is None else now
        first, self._sampleAt = self._sampleAt or now, now
        self._stale(now)
        step = self._step.sample(self.unexplainedWatts())
        maybe = (HOME_AWAKE, AWAY_AWAKE, UNKNOWN) if step == "up" else (HOME_CHARGING,)
        for v, where, state in self._watched() if step else ():
            # A car not home is asked only once it could be back. A rise reads
            # a car once a courtesy window, so a cycling load never keeps it
            # awake; a fall, once in five minutes. A poll keeps no car awake
            key, back = (v.vin, step), where == HOME or now >= self._backAt(v, now)
            wait = self._settings["sleepCourtesySecs"] if step == "up" else 300
            calm = now >= self._triggered.get(key, NEVER) + wait
            if where == NOT_HOME or not back:
                continue
            if not self._fleet(v):
                if state in maybe and calm:
                    self._triggered[key] = now
                    self.readNow(v.vin, "load step " + step)
            elif state in maybe or step == "down" and self.believedAmps(v) > 0:
                self.suspect(v.vin, first, "load step " + step, step=True)
            elif step == "up" and state in ASLEEP:
                # Its online check at the next pass, not on its cadence
                with self._lock:
                    self._waking[v.vin] = first

    def _stale(self, now) -> None:
        # More charging believed than the meter sees, twice running (once is
        # a car still ramping to a new rate): a car believed drawing is not
        m = self.master
        over = m.subtractChargerLoad and (
            m.getChargerLoad() > m.getConsumption() + self._step.threshold
        )
        at, self._overAt = self._overAt, now if over else None
        for v in self.master.vehicleRegistry.vehicles() if over and at else ():
            if self.believedAmps(v) > 0 and self._fleet(v) and self._fresh(v):
                self.suspect(v.vin, at, "charge load above consumption")

    def _fresh(self, v) -> bool:
        # Anything read or written of its charge since its last poll gave up
        return self._spent.get(v.vin) != evidence(v)

    def pass_(self, now: Optional[float] = None) -> None:
        if not self._settings["enabled"]:
            return
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
        plugIn = v.markAt("plugIn")
        seen = (state, where, plugIn, door)
        why = notWatched(self.master, v)
        named, ble = bool(why) and why.startswith(NAMED), radio(self.master)
        fleet = self._fleet(v)
        with self._lock:
            # What a car was before a restart is no news: the first pass
            # only learns it. A car first seen later has arrived
            fresh = seen if not self._started else (None, None, NEVER, None)
            wasState, wasWhere, wasPlugIn, wasDoor = self._seen.get(vin, fresh)
            self._seen[vin] = seen
            busy = {kind for key, kind in self._inflight if key == vin}
            (statusAnswer, statusAt), (readAnswer, readAt), (_, scanAt), (_, polled) = (
                self._last.get((vin, kind), ("", NEVER)) for kind in KINDS
            )
            # An arrival by any route, a wake, a plug-in (one a read made by
            # reporting it is already read), or an awake car's door opening,
            # which only a car in BLE range, so here, reports
            woke = wasState in ASLEEP and state in AWAKE
            arrived = where == HOME and wasWhere != HOME and since > readAt
            plugged, opened = plugIn > max(wasPlugIn, readAt), door and not wasDoor
            # A car only the cloud reads has its charge polled for instead,
            # and is read on its wake only for where it is
            if woke or (arrived and not fleet):
                self._ask(vin, EVENT, False)
            if (plugged or opened) and not fleet:
                self._ask(vin, EVENT, True)
            idle = vin not in self._suspect
            if why and not named:
                # Covered or not watched: an old ask must not fire later
                self._want.pop(vin, None)
            want = self._want.get(vin)
            stepAt = None if "status" in busy else self._waking.pop(vin, None)
        if why and why.startswith(COVERED) and state in AWAKE:
            self._look(v, busy, now)
        if fleet and (woke or arrived or plugged or opened):
            at = min(since if arrived else now, seen[2] if plugged else now)
            self.suspect(vin, at, "wake, arrival, plug-in or port door")
        # Believed charging with nothing to poll it: after a restart, or a
        # start no trigger saw, the belief would stand until it slept
        idle = idle and not self.ledger.isOpen(vin)
        idle = idle and self._fresh(v)
        if fleet and idle and (state == HOME_CHARGING or self.believedAmps(v) > 0):
            self.suspect(vin, now, "believed charging")
        if not fleet:
            # Read live again, or no longer watched: nothing polls what the
            # snapshots left open, so it ends at its last one
            with self._lock:
                self._suspect.pop(vin, None)
            self._saved(self.ledger.end(vin))
        elif "poll" not in busy:
            self._poll(v, seen[0], where, polled, now)
        if where != HOME and now < self._backAt(v, now):
            # Nothing an away car does needs asking before it could be back
            return
        # Only the cloud sees an awake car free to leave go, or come back once
        # it could: it is read once its fix is a courtesy window old
        fixAt, wait = _fix(self.master, v)[1], self._settings["sleepCourtesySecs"]
        late = now >= max(fixAt, readAt) + wait and cloudOnly(self.master, v)
        if late and state in (HOME_AWAKE, AWAY_AWAKE):
            want = want or Ask(EVENT, False)
        # That read, and one on its wake, is for where the car is
        forPosition = late or (woke and cloudOnly(self.master, v))
        if "read" not in busy and (why is None or named):
            deaf = statusAnswer == TWICE
            self._read(
                v, state, named, want, readAnswer, readAt, now, deaf, forPosition
            )
        if "status" not in busy and (stepAt or now >= statusAt + self._cadence(state)):
            # Coverage counts for the check only when it is due: a feed
            # blip between checks changes nothing
            source = None if why else self._source(v, where, ble, statusAnswer)
            if source is None:
                self._settle(vin, "status", now)
            else:
                worker, check = source
                self._submit(v, "status", self._check(v, where, check, stepAt), worker)
        if why is not None or "scan" in busy:
            return
        listen = self._listen(v, where, ble, now)
        if listen is None:
            return
        how, secs = listen
        if now >= scanAt + secs:
            if how == "scan":
                ble.requestRangeScan(vin)
                self._settle(vin, "scan", now)
            else:
                self._submit(v, "scan", lambda: ble.ping(vin, scan=False), "ble")

    def _check(self, v, where, ask, stepAt):
        # A check a load step asked suspects the car from the step once it
        # answers awake: a car still asleep cannot have started, and its last
        # snapshot stands
        def check():
            answered = ask()
            if answered is True and situation(self.master, v, where) in AWAKE:
                self.suspect(v.vin, stepAt, "load step up", step=True)
            return answered

        return check if stepAt else ask

    def _read(
        self, v, state, named, want, readAnswer, readAt, now, deaf, forPosition
    ) -> None:
        # The charge read due now, if any; deaf: the radio missed it twice;
        # forPosition: the read is for the car's position.
        # A TWC-named car is touched only for its plug-in, on request, and
        # while its TWC reports current
        if state in ASLEEP:
            # Never woken for a read: an arrival waits for the wake
            return
        if readAnswer in RETRY and now >= readAt + self._settings["chargingReadSecs"]:
            # A failed read asked for, once more, a charging read's cadence on
            want = want or Ask(EVENT, readAnswer == RETRY_EXEMPT)
        recent = now < readAt + self._settings["sleepCourtesySecs"]
        if want and not want.exempt and (named or (state == HOME_AWAKE and recent)):
            self._take(v.vin, want)
            want = None
        if fleetOnly(self.master, v, deaf):
            # Its charge is polled for free: it pays only to learn where it
            # is, and to try such a read again once it failed
            with self._lock:
                locate = v.vin in self._locate
                self._locate.discard(v.vin)
            if not (want and (forPosition or locate or readAnswer in RETRY)):
                return
            forPosition = True
        flowing = getattr(twcChargingState(v), "value", None) == "Charging"
        if not want and not (flowing if named else state == HOME_CHARGING):
            return
        order = self.master.providerOrder
        # The free snapshot provider is no source to price or pace by: its
        # stale answer would end a read that must reach the paid one
        found = [
            p
            for p in order.candidates(v, READ_CHARGE)
            if (not deaf or not p.local) and p.name != "FleetAPICached"
        ]
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
        due = now >= readAt + secs and (not price or policy and policy.readsBattery())
        if not want and not (due and order.candidates(v, SET_AMPS)):
            return
        self._take(v.vin, want)
        cls = want.cls if want else ROUTINE
        if cls == EVENT and readAnswer not in RETRY:
            miss = RETRY_EXEMPT if want.exempt else RETRY_COURTEOUS
        else:
            miss = MISS
        worker = "ble" if found[0].local else "cloud"

        def read():
            # None when the read failed, not the old value. A paid read first
            # takes a snapshot newer than this car's last read for free (a
            # free read is live, which beats one minutes old), and pays only
            # when there is none; a snapshot carries no location, so a read
            # for the position pays. A refusal waits its cadence as a settled
            # read, not a miss
            if price and not forPosition:
                snapshot = order.read(
                    v, "chargingState", False, ["FleetAPICached"], cutoff=readAt
                )
                if snapshot is not None:
                    return True
            if worker == "cloud" and not self.master.watchBudget.spend(price, cls):
                logger.debug("No budget for a %s read of %s", cls, v.vin)
                return ""
            return order.read(v, "chargingState", False, found) is not None

        self._submit(v, "read", read, worker, miss)

    def _poll(self, v, state, where, polled, now) -> None:
        # The free snapshot, a pollSecs apart while a charge at home may have
        # changed or one is open, never paid for: a poll given up leaves the
        # next trigger or courtesy read to catch up. An asleep car is not
        # charging, so its last snapshot stands and ends its session
        vin, s = v.vin, self._settings
        with self._lock:
            since, stepAt = self._suspect.get(vin, (None, None))
            if since is not None and (state in ASLEEP or now >= since + s["pollGiveUpSecs"]):
                del self._suspect[vin]
                since, self._spent[vin] = None, evidence(v)
        if state in ASLEEP:
            self._saved(self.ledger.end(vin))
            return
        cutoff = self.ledger.lastStamp(vin) if since is None else since
        home = where == HOME or self.ledger.isOpen(vin)
        if cutoff is None or not home or now < polled + s["pollSecs"]:
            return

        def poll():
            # A snapshot another read fetched counts as well as this one's
            self.master.providerOrder.read(v, "chargingState", False, [CACHED], cutoff=cutoff)
            snap = snapshot(v, cutoff)
            if snap is None:
                return None
            with self._lock:
                if self._suspect.get(vin, (snap.stamp,))[0] <= snap.stamp:
                    self._suspect.pop(vin, None)
            # Charging away from home (a Supercharger) is no home session:
            # the car may have left since the poll was asked
            opens = snap.state == CHARGING and not self.ledger.isOpen(vin)
            if not opens or presence(self.master, v) == HOME:
                near = stepAt is not None and snap.stamp - stepAt <= STEP_SECS
                self._saved(self.ledger.observe(vin, snap, stepAt if near else None))
            return True

        self._submit(v, "poll", poll, "cloud")

    def _saved(self, changed) -> None:
        if changed:
            self.master.queue_background_task({"cmd": "saveSettings"})

    def _look(self, v, busy, now) -> None:
        # One read of how a car looks, for its picture, when no source said:
        # a feed covering it keeps off the cloud reads that would carry it.
        # A failed read is tried again an hour on
        api = self.master.getModuleByName("TeslaAPI")
        if v.value("carType") or "look" in busy or not v.attachment("TeslaAPI"):
            return
        with self._lock:
            last, at = self._last.get((v.vin, "look"), ("", NEVER))
        if api is None or last not in ("", MISS) or now < at + 3600:
            return
        if not self.master.watchBudget.spend(api.readPrice(), ROUTINE):
            self._settle(v.vin, "look", now)
            return
        self._submit(v, "look", lambda: api.readAppearance(v), "cloud")

    def _backAt(self, v, now) -> float:
        # The soonest an away car could be home: its last fix (with none, the
        # run's start) plus the straight line home at a fast road speed, which
        # real roads only lengthen
        fixAt, home, km = _fix(self.master, v)[1], self.master.getHomeLatLon(), 0.0
        if fixAt > NEVER and home[0] != 10000:
            east = (v.value("lon") - home[1]) * math.cos(math.radians(home[0]))
            km = math.hypot(v.value("lat") - home[0], east) * _FT_PER_DEGREE / 3280.84
        fixAt = fixAt if fixAt > NEVER else self._started or now
        return fixAt + max(300.0, km / self._settings["returnKph"] * 3600)

    def _listen(self, v, where, ble, now):
        # How and how often a lost car's return is heard: a scan, or where none
        # can run a ping; often while it was lately awake, else as an asleep
        # car is asked, as nothing refreshes its asleep reading
        if not lost(v, where) or ble is None:
            return None
        slow, often = self._settings["awayAsleepSecs"], self._settings["scanAwaySecs"]
        secs = often if now < lastLife(v) + slow else slow
        return ("scan" if scans(ble) else "ble"), secs

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
        now, where, ble = self._clock(), presence(self.master, v), radio(self.master)
        with self._lock:
            checked, scanned = (
                self._last.get((v.vin, k), ("", None)) for k in ("status", "scan")
            )
            seen = self._seen.get(v.vin)
        source = self._source(v, where, ble, checked[0])
        # A lost car nothing else asks is listened for on the radio's back-off
        scan = None if source else self._listen(v, where, ble, now)
        via = scan[0] if scan else (source[0] if source else None)
        why = notWatched(self.master, v) or ("" if via else NO_SOURCE)
        last, at = scanned if scan else checked
        nextAt = None
        if at is not None and seen is not None:
            secs = scan[1] if scan else self._cadence(seen[0])
            back = self._backAt(v, now) if where != HOME else NEVER
            nextAt = max(at + secs, back)
        watched = why in ("", NO_SOURCE)
        return dict(
            watched=watched, why=why, via=via, last=last, lastAt=at, nextAt=nextAt
        )
