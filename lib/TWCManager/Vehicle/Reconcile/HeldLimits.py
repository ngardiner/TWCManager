# The held-limit store: per VIN we hold a limit on, the charge limit the
# car had outside our control ("outside") and the one we hold it at
# ("held"). Persisted as older releases did, in settings["chargeLimits"] as
# {VIN: [outside, held]}. A car we hold nothing on has no entry: a limit is
# only ever sent with the car's current limit known, which then is its
# normal limit, so nothing needs the old one. load() drops the older
# releases' entries holding nothing (an int, or held -1, 0 or None).
#
# The Tesla API path of older releases keyed entries by the numeric vehicle
# ID. One pass migrates them: load() normalises the VIN keys and leaves the
# IDs where they are, waiting; the first vehicle list that names a car makes
# resolveLegacyIds() re-key each ID to its VIN or drop it with a warning.
#
# Every method reads master.settings and master.settingsLock on each call:
# loadSettings replaces the settings dict. Only dict work happens under the
# lock; saves are queued to the background thread after it is released.
import logging
import math
from typing import Any, Dict, FrozenSet, List, NamedTuple, Optional, Tuple

from ..Registry.VIN import isWellFormedVIN

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

# Tesla accepts charge limits in this range.
MIN_LIMIT = 50
MAX_LIMIT = 100


class HeldLimit(NamedTuple):
    outside: int
    held: Optional[int]


def _isInt(x: Any) -> bool:
    # An integral number: Home Assistant reports limits as floats (80.0).
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    return isinstance(x, int) or x.is_integer()


def _rounded(x: Any) -> Optional[int]:
    # A finite number, rounded; None for anything else (a string, a bool,
    # NaN, inf). A limit derived from a fractional battery level is kept.
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return None
    if isinstance(x, float) and not math.isfinite(x):
        return None
    return int(round(x))


def _entry(value: Any) -> Tuple[Optional[HeldLimit], Optional[str]]:
    # A held limit: an [outside, held] pair, held 50-100. Older releases also
    # wrote an int, or held -1, 0 or None, for a car held at nothing: that is
    # no entry. Returns the entry, or None and the warning dropping it needs
    if _isInt(value):
        return None, None
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None, _MALFORMED
    outside, held = value
    if not _isInt(outside):
        return None, _MALFORMED
    if held is None or (_isInt(held) and held in (-1, 0)):
        return None, None
    if _isInt(held) and MIN_LIMIT <= held <= MAX_LIMIT:
        return HeldLimit(int(outside), int(held)), None
    return None, _UNHELD


_MALFORMED = "Dropped a malformed chargeLimits entry for %s while migrating"
_UNHELD = (
    "Dropped the chargeLimits entry for %s while migrating: its held limit is "
    "out of range"
)
_UNLISTED = (
    "Dropped the saved charge limit of Tesla vehicle ID %s, which no Tesla "
    "account lists: its normal limit was %s%%"
)
_COLLIDED = "chargeLimits entries %s and %s name the same car; kept %s while migrating"
_MERGED = "Tesla vehicle ID %s is %s: kept normal limit %s%%, discarded %s%%"


class HeldLimits:
    KEY = "chargeLimits"

    def __init__(self, master):
        self.master = master
        # True after load() while entries keyed by a vehicle ID remain
        self._waiting = False
        # The VINs held when load() ended; their entries beat a waiting ID's.
        # A record made later (by the reconciler, from a car still at our
        # old limit) keeps its hold but takes the ID's normal limit
        self._loadedVins: FrozenSet[str] = frozenset()

    def _save(self) -> None:
        self.master.queue_background_task({"cmd": "saveSettings"})

    def load(self) -> None:
        """Normalise settings["chargeLimits"]: VIN keys now; numeric IDs
        once a vehicle list names them."""
        self._migrate({}, final=False)

    def waiting(self) -> bool:
        """True while held limits keyed by a vehicle ID wait for a list."""
        return self._waiting

    def resolveLegacyIds(self, listed: Dict[Any, Any]) -> None:
        """Re-key each waiting ID's held limit to the VIN listed for it, and
        drop the rest, once listed (every Tesla account's vehicle IDs to
        VINs) names at least one car."""
        listed = {str(k): str(v).upper() for k, v in listed.items() if v}
        if listed and self.waiting():
            self._migrate(listed, final=True)

    def _migrate(self, listed: Dict[str, str], final: bool) -> None:
        # load (final False) normalises the VIN keys and leaves the IDs
        # waiting; resolve (final True) re-keys each ID to its listed VIN or
        # drops it. Dict work is one step under settingsLock; logs and the
        # save follow.
        warnings: List[Tuple[Any, ...]] = []
        infos: List[Tuple[Any, ...]] = []
        with self.master.settingsLock:
            settings = self.master.settings
            limits = settings.get(self.KEY)
            if not isinstance(limits, dict):
                self._waiting, self._loadedVins = False, frozenset()
                return
            kept: Dict[Any, Any] = {}
            ids: Dict[Any, Tuple[Any, HeldLimit]] = {}
            origin: Dict[str, Any] = {}
            for key, value in limits.items():
                limit, warning = _entry(value)
                if limit is None:
                    if warning:
                        warnings.append((warning, key))
                    continue
                if not isWellFormedVIN(str(key)):
                    ids[key] = (value, limit)
                    continue
                vin = str(key).upper()
                if vin in origin:  # keys differing only in case
                    warnings.append((_COLLIDED, origin[vin], key, key))
                kept[vin], origin[vin] = value, key
            moved = set()
            for key, (value, limit) in ids.items():
                vin = listed.get(str(key))
                own = _entry(kept.get(vin))[0] if vin else None
                if vin is None or not isWellFormedVIN(vin):
                    if final:
                        warnings.append((_UNLISTED, key, limit.outside))
                    else:
                        kept[key] = value  # waits, on disk where it was
                elif own is None:  # absent: the ID's entry moves
                    kept[vin] = value
                    moved.add(vin)
                elif vin in self._loadedVins and vin not in moved:
                    infos.append((key, vin, own.outside, limit.outside))
                else:  # recorded since load: keep the hold
                    kept[vin] = [limit.outside, own.held]
                    infos.append((key, vin, limit.outside, own.outside))
                    moved.add(vin)
            changed = kept != limits
            if changed and kept:
                settings[self.KEY] = kept
            elif changed:
                del settings[self.KEY]
            self._waiting = any(not isWellFormedVIN(str(k)) for k in kept)
            if not final:
                self._loadedVins = frozenset(k for k in kept if isWellFormedVIN(str(k)))
        for args in warnings:
            logger.warning(*args)
        for args in infos:  # either way one saved normal limit is discarded
            logger.info(_MERGED, *args)
        if changed:
            self._save()

    def get(self, vin: str) -> Optional[HeldLimit]:
        # Unlocked single-key read, as settings reads are elsewhere.
        limits = self.master.settings.get(self.KEY)
        if not isinstance(limits, dict):
            return None
        return _entry(limits.get(str(vin).upper()))[0]

    def set(self, vin: str, outside: int, held: Optional[int]) -> None:
        if not isWellFormedVIN(str(vin)):
            # holding() would hide it forever.
            logger.warning("Refused a held limit for %s: not a VIN", vin)
            return
        outsideInt = _rounded(outside)
        heldInt = None if held is None else _rounded(held)
        if outsideInt is None or (held is not None and heldInt is None):
            logger.warning("Refused a held limit for %s: not a finite number", vin)
            return
        if heldInt in (None, -1, 0):
            # Holding nothing: no entry
            self.remove(vin)
            return
        with self.master.settingsLock:
            limits = self.master.settings.get(self.KEY)
            if not isinstance(limits, dict):
                limits = self.master.settings[self.KEY] = {}
            limits[str(vin).upper()] = [outsideInt, heldInt]
        self._save()

    def remove(self, vin: str) -> None:
        with self.master.settingsLock:
            limits = self.master.settings.get(self.KEY)
            changed = (
                isinstance(limits, dict)
                and limits.pop(str(vin).upper(), None) is not None
            )
        if changed:
            self._save()

    def holding(self) -> List[str]:
        with self.master.settingsLock:
            limits = self.master.settings.get(self.KEY)
            items = list(limits.items()) if isinstance(limits, dict) else []
        return sorted(
            k for k, v in items if isWellFormedVIN(str(k)) and _entry(v)[0] is not None
        )
