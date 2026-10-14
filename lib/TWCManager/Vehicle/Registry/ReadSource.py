# Read sources: where a Vehicle can fetch a stale field from, and the cascade
# that tries them in order. Sources are called with no lock held.
import logging
from typing import Any, Dict, FrozenSet, List, Optional

from .Fields import Observation

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Registry")


class ReadSource:
    # order is a sequencing hint (0 local, 1 cloud; lower first), not a cost model.
    name: str = ""
    order: int = 0
    fields: FrozenSet[str] = frozenset()

    def canRead(self, vehicle) -> bool:
        return True

    def fetch(self, vehicle, field: str, allowWake: bool) -> Optional[Dict[str, Any]]:
        # Returns observations keyed by field name, {} if the source recorded
        # them itself, None on failure. Must not wake the car unless allowWake.
        raise NotImplementedError


def cascade(
    vehicle, sources: List[ReadSource], field: str, allowWake: bool
) -> Optional[Observation]:
    """Try sources in the order given (callers pass them pre-sorted by
    `order`). A source whose canRead or fetch raises is treated as skipped or
    failed; only the exception type is logged, never its message. A source
    that returns a result without `field` is passed over after its
    observations are recorded."""
    for source in sources:
        if field not in source.fields:
            continue
        try:
            if not source.canRead(vehicle):
                continue
            result = source.fetch(vehicle, field, allowWake)
        except Exception as e:
            logger.debug(
                "Read source %s failed for %s: %s",
                source.name,
                field,
                type(e).__name__,
            )
            continue
        if result is None:
            continue
        for k, v in result.items():
            vehicle.observe(k, v, source.name)
        # {} means the source recorded its observations itself.
        if field in result or (not result and vehicle.latest(field) is not None):
            return vehicle.latest(field)
    return None
