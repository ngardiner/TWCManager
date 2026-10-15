# The outcome vocabulary every vehicle command reports in.
#
# Provider return contract: every Provider command method (setChargeLimit,
# startCharging, stopCharging, setAmps, wake) returns an Outcome,
# never None, True, "success" or a dict. A raised exception is converted by
# the worker to Outcome(ERROR, <exception type name>). An Outcome's detail is
# a short reason or status code ("401", "timeout", "complete"), never a
# response body or a token.
from typing import NamedTuple

# The command took effect
DONE = "done"
# Nothing to do: the car is already in the desired state
SATISFIED = "satisfied"
# This provider cannot do this for this car
UNSUPPORTED = "unsupported"
# The car or the channel did not answer
UNREACHABLE = "unreachable"
# The car or the service refused; the provider has retired its slot or claim
REFUSED = "refused"
# Anything else
ERROR = "error"

KINDS = frozenset({DONE, SATISFIED, UNSUPPORTED, UNREACHABLE, REFUSED, ERROR})

# The attempt is over
SETTLED = frozenset({DONE, SATISFIED})
# The same provider again after RETRY[aspect]
RETRYABLE = frozenset({UNREACHABLE, ERROR})
# The provider is skipped for the rest of the round
EXCLUDING = frozenset({UNSUPPORTED, REFUSED})

# An ERROR with one of these details was produced by the lane itself, not by
# a provider, and counts against nothing
LANE_DETAILS = frozenset({"cancelled", "dropped", "replaced"})

# An outcome with one of these details, or one starting RULES, made no
# request: it is neither a paid call nor a sent command
NOT_SENT = LANE_DETAILS | {
    "backoff",
    "wake held",
    "wake refused",
    "no command token",
    "not listed",
    "online check failed",
    "read",
    # The worker found no vehicle or no provider to run the attempt on
    "no vehicle",
    "no provider",
}
RULES = "rules: "


class Outcome(NamedTuple):
    kind: str
    detail: str = ""


def isOutcome(x) -> bool:
    return isinstance(x, Outcome) and x.kind in KINDS


def isNotSent(outcome) -> bool:
    return outcome.detail in NOT_SENT or outcome.detail.startswith(RULES)
