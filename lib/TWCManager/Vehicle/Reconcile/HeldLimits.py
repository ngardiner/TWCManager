# The held-limit store: per VIN, the charge limit the car had outside our
# control ("outside") and the limit we hold it at ("held", None when we hold
# none). Persisted in settings["heldLimits"] as
# {VIN: {"outside": int, "held": int|null}}.
#
# Older releases kept settings["chargeLimits"][ID] = (outside, applied),
# keyed by VIN (BLE) or by the Tesla API's numeric vehicle ID. load()
# migrates the VIN keys at once. Numeric IDs (and the "id:" keys development
# builds stored) wait in memory, and on disk where they were, until the
# first vehicle list names them: resolveLegacyIds() then moves each under
# its VIN or drops it with a warning.
#
# Every method reads master.settings and master.settingsLock on each call:
# loadSettings replaces the settings dict. Only dict work happens under the
# lock; saves are queued to the background thread after it is released.
import logging
import math
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

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


class HeldLimits:
    KEY = "heldLimits"
    LEGACY_KEY = "chargeLimits"

    def __init__(self, master):
        self.master = master
        # Vehicle ID -> HeldLimit, until a vehicle list resolves them
        self._waiting: Dict[str, HeldLimit] = {}

    def _save(self) -> None:
        self.master.queue_background_task({"cmd": "saveSettings"})

    def load(self) -> None:
        """Migrate settings["chargeLimits"]: VIN keys now, unless the store
        already has the car; numeric IDs once a vehicle list names them.

        Dict work happens under settingsLock in one step, so a save sees the
        old key or the new one, never neither. A run that stops before the
        queued save re-runs the same migration from the unchanged file."""
        dropped: List[str] = []
        unheld: List[str] = []
        collided: List[Tuple[str, str]] = []
        waiting: Dict[str, HeldLimit] = {}
        changed = False
        with self.master.settingsLock:
            settings = self.master.settings
            legacy = settings.get(self.LEGACY_KEY)
            store = settings.get(self.KEY)
            if not isinstance(store, dict):
                store = {}
            for key, value in store.items():
                # Development builds of this release stored unresolved IDs
                # as "id:<ID>" (M9)
                limit = _storedEntry(value)
                if str(key).startswith("id:") and limit is not None:
                    waiting[str(key)[3:]] = limit
            if isinstance(legacy, dict):
                ids: Dict[str, Any] = {}
                origin: Dict[str, str] = {}
                for key, value in legacy.items():
                    limit, discarded = _legacyEntry(value)
                    if limit is None:
                        dropped.append(str(key))
                        continue
                    if discarded:
                        unheld.append(str(key))
                    if not isWellFormedVIN(str(key)):
                        waiting[str(key)] = limit
                        ids[key] = value
                        continue
                    vin = str(key).upper()
                    if vin in origin:
                        # Keys differing only in case: the later one wins.
                        collided.append((origin[vin], str(key)))
                    elif vin in store:
                        # A newer release saved this car already
                        continue
                    origin[vin] = str(key)
                    store[vin] = _encode(limit)
                settings[self.KEY] = store
                if ids:
                    settings[self.LEGACY_KEY] = ids
                else:
                    del settings[self.LEGACY_KEY]
                changed = ids != legacy
        self._waiting = waiting
        # Logged outside the lock.
        for key in dropped:
            logger.warning(
                "Dropped a malformed chargeLimits entry for %s while migrating", key
            )
        for key in unheld:
            logger.warning(
                "Kept the normal limit but not the out-of-range held limit of the "
                "chargeLimits entry for %s while migrating",
                key,
            )
        for lost, kept in collided:
            logger.warning(
                "chargeLimits entries %s and %s name the same car; kept %s while "
                "migrating",
                lost,
                kept,
                kept,
            )
        if changed:
            logger.info("Migrated chargeLimits to heldLimits")
            self._save()

    def waiting(self) -> bool:
        """True while held limits keyed by a vehicle ID wait for a list."""
        return bool(self._waiting)

    def resolveLegacyIds(self, listed: Dict[Any, Any]) -> None:
        """Move each waiting ID's held limit under the VIN listed for it, and
        drop the rest, once listed (every Tesla account's vehicle IDs to
        VINs) names at least one car. A car the store has already keeps
        its own entry."""
        listed = {str(k): str(v).upper() for k, v in listed.items() if v}
        if not listed:
            return
        lost = []
        with self.master.settingsLock:
            # Taken under the lock: a second caller finds nothing waiting
            waiting, self._waiting = self._waiting, {}
            if not waiting:
                return
            settings = self.master.settings
            store = settings.get(self.KEY)
            if not isinstance(store, dict):
                store = settings[self.KEY] = {}
            for ident, limit in waiting.items():
                vin = listed.get(ident)
                if vin is None or not isWellFormedVIN(vin):
                    lost.append((ident, limit))
                elif vin not in store:
                    store[vin] = _encode(limit)
            for key in [k for k in store if str(k).startswith("id:")]:
                del store[key]
            settings.pop(self.LEGACY_KEY, None)
        for ident, limit in lost:
            logger.warning(
                "Dropped the saved charge limit of Tesla vehicle ID %s, which no "
                "Tesla account lists: its normal limit was %s%%",
                ident,
                limit.outside,
            )
        self._save()

    def get(self, vin: str) -> Optional[HeldLimit]:
        # Unlocked single-key read, as settings reads are elsewhere.
        store = self.master.settings.get(self.KEY)
        if not isinstance(store, dict):
            return None
        return _storedEntry(store.get(str(vin).upper()))

    def set(self, vin: str, outside: int, held: Optional[int]) -> None:
        if not isWellFormedVIN(str(vin)):
            # vins() would hide it forever.
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

    def _snapshot(self) -> List[Tuple[str, Optional[HeldLimit]]]:
        with self.master.settingsLock:
            store = self.master.settings.get(self.KEY)
            items = list(store.items()) if isinstance(store, dict) else []
        return [(k, _storedEntry(v)) for k, v in items if isWellFormedVIN(k)]

    def vins(self) -> List[str]:
        return sorted(k for k, limit in self._snapshot() if limit is not None)

    def holding(self) -> List[str]:
        return sorted(
            k
            for k, limit in self._snapshot()
            if limit is not None and limit.held is not None
        )
