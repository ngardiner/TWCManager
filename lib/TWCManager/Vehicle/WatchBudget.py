import logging
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.WatchBudget")

# Reads a state change forces may drain the bucket; routine ones leave a
# reserve for them.
EVENT, ROUTINE = "event", "routine"


class WatchBudget:
    # A token bucket of dollars: it refills at the monthly rate and holds a
    # day's worth, so a quiet stretch can't bank a month of spending.
    def __init__(self, monthlyUSD: float, clock=time.time, floor: float = 0.5):
        self._clock = clock
        self._floor = floor
        self.capacity = monthlyUSD / 30
        self._rate = monthlyUSD / (30 * 86400)
        self._tokens = self.capacity
        self._at = clock()
        self._empty = False
        # A leaf lock: workers spend while the settings saver reads
        self._lock = threading.Lock()

    def _refill(self, now: Optional[float]):
        now = self._clock() if now is None else now
        # A clock stepping back must not drain the bucket
        elapsed = max(0.0, now - self._at)
        return min(self.capacity, self._tokens + elapsed * self._rate), now

    def tokens(self, now: Optional[float] = None) -> float:
        with self._lock:
            return self._refill(now)[0]

    def spend(self, price: float, cls: str, now: Optional[float] = None) -> bool:
        with self._lock:
            tokens, now = self._refill(now)
            reserve = self._floor * self.capacity if cls == ROUTINE else 0.0
            # The epsilon keeps float drift from refusing a price the bucket exactly holds
            ok = price <= 0 or tokens - price >= reserve - 1e-9
            if ok:
                tokens -= max(price, 0.0)
            self._tokens, self._at = tokens, now
            # Only an event read's outcome says the bucket is truly empty; a
            # routine refusal at the floor is the normal steady state.
            if price > 0 and cls == EVENT and ok == self._empty:
                self._empty = not ok
                logger.info(
                    "Paid-read budget %s",
                    "exhausted" if self._empty else "available again",
                )
            return ok

    def toJSON(self) -> dict:
        with self._lock:
            return {"tokens": self._tokens, "at": self._at}

    def load(self, data) -> None:
        # Anything but two numbers starts full
        with self._lock:
            now = self._clock()
            try:
                tokens, at = float(data["tokens"]), float(data["at"])
            except (TypeError, KeyError, ValueError):
                tokens, at = self.capacity, now
            # A stamp ahead of now (a clock stepped back) would stall refilling
            self._tokens, self._at = min(self.capacity, max(0.0, tokens)), min(at, now)
