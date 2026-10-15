# The held-limit store: per VIN, the charge limit the car had outside our
# control ("outside") and the limit we hold it at ("held", None when we hold
# none). Persisted in settings["heldLimits"] as
# {VIN: {"outside": int, "held": int|null}}.
#
# Older releases kept settings["chargeLimits"][ID] = (outside, lastApplied)
# with lastApplied -1 meaning "we hold none", keyed by VIN (BLE) or by the
# Tesla API's numeric vehicle ID. load() migrates that key once. An ID that
# cannot be resolved to a VIN yet is kept as "id:<ID>" until TeslaAPI lists
# the car and adoptLegacyId() moves it under the VIN.
#
# Every method reads master.settings and master.settingsLock on each call:
# loadSettings replaces the settings dict. Only dict work happens under the
# lock; saves are queued to the background thread after it is released.
import logging
from typing import Any, Dict, List, NamedTuple, Optional, Tuple

from ..Registry.VIN import isWellFormedVIN

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Reconcile")

LEGACY_PREFIX = "id:"
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


def storeKey(ident: Any) -> str:
    """A well-formed VIN, upper-cased; anything else as "id:<ident>"."""
    text = str(ident)
    if text.startswith(LEGACY_PREFIX):
        return text
    if isWellFormedVIN(text):
        return text.upper()
    return LEGACY_PREFIX + text


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

    def _save(self) -> None:
        self.master.queue_background_task({"cmd": "saveSettings"})

    def load(self) -> None:
        """Migrate settings["chargeLimits"] when no store exists yet.

        The new dict is built and swapped in under settingsLock in one step,
        so a save sees either the old key or the new one, never neither. A
        run that stops before the queued save re-runs the same migration
        from the unchanged file. When the store already exists the legacy
        key is left alone: nothing is deleted without being migrated."""
        dropped: List[str] = []
        unheld: List[str] = []
        collided: List[Tuple[str, str]] = []
        migrated = False
        ignored = False
        with self.master.settingsLock:
            settings = self.master.settings
            legacy = settings.get(self.LEGACY_KEY)
            ignored = self.KEY in settings and legacy is not None
            if self.KEY not in settings and isinstance(legacy, dict):
                store: Dict[str, Dict[str, Any]] = {}
                origin: Dict[str, str] = {}
                for key, value in legacy.items():
                    limit, discarded = _legacyEntry(value)
                    if limit is None:
                        dropped.append(str(key))
                        continue
                    if discarded:
                        unheld.append(str(key))
                    newKey = storeKey(key)
                    if newKey in origin:
                        # Keys differing only in case: the later one wins.
                        collided.append((origin[newKey], str(key)))
                    origin[newKey] = str(key)
                    store[newKey] = _encode(limit)
                settings[self.KEY] = store
                del settings[self.LEGACY_KEY]
                migrated = True
        # Logged outside the lock.
        if ignored:
            # An older release ran in between and wrote chargeLimits again.
            logger.warning(
                "Ignoring chargeLimits written by an older release: the held "
                "limits saved before the downgrade are used"
            )
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
        if migrated:
            logger.info("Migrated chargeLimits to heldLimits")
            self._save()

    def _get(self, key: str) -> Optional[HeldLimit]:
        # Unlocked single-key read, as settings reads are elsewhere.
        store = self.master.settings.get(self.KEY)
        if not isinstance(store, dict):
            return None
        return _storedEntry(store.get(key))

    def _set(self, key: str, outside: int, held: Optional[int]) -> None:
        if held is not None and _isInt(held) and held in (-1, 0):
            # The legacy "holds none" sentinels are never stored.
            held = None
        if not _isInt(outside) or (held is not None and not _isInt(held)):
            logger.warning("Refused a non-integral held limit for %s", key)
            return
        limit = HeldLimit(int(outside), None if held is None else int(held))
        with self.master.settingsLock:
            store = self.master.settings.get(self.KEY)
            if not isinstance(store, dict):
                store = self.master.settings[self.KEY] = {}
            store[key] = _encode(limit)
        self._save()

    def _remove(self, key: str) -> None:
        with self.master.settingsLock:
            store = self.master.settings.get(self.KEY)
            changed = isinstance(store, dict) and store.pop(key, None) is not None
        if changed:
            self._save()

    def get(self, vin: str) -> Optional[HeldLimit]:
        return self._get(str(vin).upper())

    def set(self, vin: str, outside: int, held: Optional[int]) -> None:
        if not isWellFormedVIN(str(vin)):
            # vins() would hide it forever.
            logger.warning("Refused a held limit for %s: not a VIN", vin)
            return
        self._set(str(vin).upper(), outside, held)

    def remove(self, vin: str) -> None:
        self._remove(str(vin).upper())

    def adoptLegacyId(self, legacyId: Any, vin: str) -> None:
        """Move an "id:" entry under vin unless vin already has one; then the
        "id:" entry is kept (nothing is discarded) and nothing is saved."""
        legacy = LEGACY_PREFIX + str(legacyId)
        vin = str(vin).upper()
        store = self.master.settings.get(self.KEY)
        # Unlocked fast path: TeslaAPI binds a car on every rebind.
        if not isinstance(store, dict) or legacy not in store:
            return
        with self.master.settingsLock:
            store = self.master.settings.get(self.KEY)
            moved = (
                isinstance(store, dict)
                and legacy in store
                and vin not in store
                and isWellFormedVIN(vin)
            )
            if moved:
                store[vin] = store.pop(legacy)
        if moved:
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
