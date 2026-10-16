# The held-limit store: per VIN, the charge limit the car had outside our
# control ("outside") and the limit we hold it at ("held", None when we hold
# none). Persisted in settings["heldLimits"] as
# {VIN: {"outside": int, "held": int|null}}.
#
# Older releases kept settings["chargeLimits"][ID] = (outside, applied),
# keyed by VIN (BLE) or by the Tesla API's numeric vehicle ID. One pass
# migrates it: load() moves the VIN keys and leaves the numeric IDs in
# chargeLimits, which is then the waiting list; the first vehicle list that
# names a car makes resolveLegacyIds() move each ID under its VIN or drop it
# with a warning.
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


def _legacyEntry(value: Any) -> Tuple[Optional[HeldLimit], bool]:
    # chargeLimits values: an int (outside only), or an [outside, applied]
    # pair where applied -1, None or 0 means nothing is held. Returns the
    # entry (None when the outside value is unusable) and whether an applied
    # value was discarded: the user's normal limit is never lost over it.
    if _isInt(value):
        return HeldLimit(int(value), None), False
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None, False
    outside, applied = value
    if not _isInt(outside):
        return None, False
    outside = int(outside)
    if applied is None or (_isInt(applied) and applied in (-1, 0)):
        return HeldLimit(outside, None), False
    if _isInt(applied) and MIN_LIMIT <= applied <= MAX_LIMIT:
        return HeldLimit(outside, int(applied)), False
    return HeldLimit(outside, None), True


def _storedEntry(value: Any) -> Optional[HeldLimit]:
    if not isinstance(value, dict) or not _isInt(value.get("outside")):
        return None
    held = value.get("held")
    if held is not None and not _isInt(held):
        return None
    return HeldLimit(int(value["outside"]), None if held is None else int(held))


def _encode(limit: HeldLimit) -> Dict[str, Any]:
    return {"outside": limit.outside, "held": limit.held}


def _vins(store: Dict[Any, Any]) -> FrozenSet[Any]:
    return frozenset(k for k in store if isWellFormedVIN(str(k)))


_MALFORMED = "Dropped a malformed chargeLimits entry for %s while migrating"
_UNHELD = (
    "Kept the normal limit but not the out-of-range held limit of the "
    "chargeLimits entry for %s while migrating"
)
_UNLISTED = (
    "Dropped the saved charge limit of Tesla vehicle ID %s, which no Tesla "
    "account lists: its normal limit was %s%%"
)
_COLLIDED = "chargeLimits entries %s and %s name the same car; kept %s while migrating"
_MERGED = "Tesla vehicle ID %s is %s: kept normal limit %s%%, discarded %s%%"


class HeldLimits:
    KEY = "heldLimits"
    LEGACY_KEY = "chargeLimits"

    def __init__(self, master):
        self.master = master
        # None until load() runs: nothing waits. Then the VINs the store held
        # when load() ended; their entries beat a waiting ID's. A record made
        # later (by the reconciler, from a car still at our old limit) keeps
        # its hold but takes the legacy normal limit
        self._loadedVins: Optional[FrozenSet[str]] = None

    def _save(self) -> None:
        self.master.queue_background_task({"cmd": "saveSettings"})

    def load(self) -> None:
        """Migrate settings["chargeLimits"]: VIN keys now, unless the store
        already has the car; numeric IDs once a vehicle list names them.

        A run that stops before the queued save re-runs the same migration
        from the unchanged file."""
        self._migrate({}, final=False)

    def waiting(self) -> bool:
        """True while held limits keyed by a vehicle ID wait for a list."""
        legacy = self.master.settings.get(self.LEGACY_KEY)
        return (
            self._loadedVins is not None and isinstance(legacy, dict) and bool(legacy)
        )

    def resolveLegacyIds(self, listed: Dict[Any, Any]) -> None:
        """Move each waiting ID's held limit under the VIN listed for it, and
        drop the rest, once listed (every Tesla account's vehicle IDs to
        VINs) names at least one car."""
        listed = {str(k): str(v).upper() for k, v in listed.items() if v}
        if listed and self.waiting():
            self._migrate(listed, final=True)

    def _migrate(self, listed: Dict[str, str], final: bool) -> None:
        # load (final False) moves the VIN keys and leaves the IDs waiting;
        # resolve (final True) moves each ID under its listed VIN or drops
        # it. Dict work is one step under settingsLock, so a save sees the
        # old key or the new one, never neither; logs and the save follow.
        warnings: List[Tuple[Any, ...]] = []
        infos: List[Tuple[Any, ...]] = []
        with self.master.settingsLock:
            settings = self.master.settings
            legacy, store = settings.get(self.LEGACY_KEY), settings.get(self.KEY)
            store = store if isinstance(store, dict) else {}
            loaded = self._loadedVins if final else _vins(store)
            if not isinstance(legacy, dict):
                if not final:
                    self._loadedVins = loaded
                return
            keep: Dict[Any, Any] = {}
            written: Dict[str, Any] = {}
            for key, value in legacy.items():
                limit, discarded = _legacyEntry(value)
                if limit is None:
                    warnings.append((_MALFORMED, key))
                    continue
                isVin = isWellFormedVIN(str(key))
                vin = str(key).upper() if isVin else listed.get(str(key))
                own = _storedEntry(store.get(vin)) if vin else None
                if vin in loaded and vin not in written and (isVin or own is not None):
                    if not isVin:  # the store keeps its own; a VIN key silently
                        infos.append((key, vin, own.outside, limit.outside))
                    continue
                if discarded and not final:
                    warnings.append((_UNHELD, key))
                if vin is None or not isWellFormedVIN(vin):
                    if final:
                        warnings.append((_UNLISTED, key, limit.outside))
                    else:
                        keep[key] = value  # waits, on disk where it was
                    continue
                if isVin and vin in written:  # keys differing only in case
                    warnings.append((_COLLIDED, written[vin], key, key))
                    store[vin] = _encode(limit)
                elif own is None:  # absent or malformed: the legacy entry wins
                    store[vin] = _encode(limit)
                else:  # recorded since load: keep the hold
                    store[vin] = _encode(HeldLimit(limit.outside, own.held))
                    infos.append((key, vin, limit.outside, own.outside))
                written[vin] = key
            settings[self.KEY] = store
            if keep:
                settings[self.LEGACY_KEY] = keep
            else:
                del settings[self.LEGACY_KEY]
            changed = keep != legacy
            if not final:
                self._loadedVins = _vins(store)
        for args in warnings:
            logger.warning(*args)
        for args in infos:  # either way one saved normal limit is discarded
            logger.info(_MERGED, *args)
        if changed and not final:
            logger.info("Migrated chargeLimits to heldLimits")
        if changed:
            self._save()

    def get(self, vin: str) -> Optional[HeldLimit]:
        # Unlocked single-key read, as settings reads are elsewhere.
        store = self.master.settings.get(self.KEY)
        if not isinstance(store, dict):
            return None
        return _storedEntry(store.get(str(vin).upper()))

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
        if heldInt in (-1, 0):
            # The legacy "holds none" sentinels are never stored.
            heldInt = None
        with self.master.settingsLock:
            store = self.master.settings.get(self.KEY)
            if not isinstance(store, dict):
                store = self.master.settings[self.KEY] = {}
            store[str(vin).upper()] = _encode(HeldLimit(outsideInt, heldInt))
        self._save()

    def remove(self, vin: str) -> None:
        with self.master.settingsLock:
            store = self.master.settings.get(self.KEY)
            changed = (
                isinstance(store, dict)
                and store.pop(str(vin).upper(), None) is not None
            )
        if changed:
            self._save()

    def holding(self) -> List[str]:
        with self.master.settingsLock:
            store = self.master.settings.get(self.KEY)
            items = list(store.items()) if isinstance(store, dict) else []
        limits = [(k, _storedEntry(v)) for k, v in items if isWellFormedVIN(k)]
        return sorted(
            k for k, limit in limits if limit is not None and limit.held is not None
        )
