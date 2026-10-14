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
    for source in sources:
        if field not in source.fields:
            continue
        try:
            if not source.canRead(vehicle):
                continue
            result = source.fetch(vehicle, field, allowWake)
        except Exception:
            logger.debug(
                "Read source %s failed for %s", source.name, field, exc_info=True
            )
            continue
        if result is None:
            continue
        for k, v in result.items():
            vehicle.observe(k, v, source.name)
        return vehicle.latest(field)
    return None
