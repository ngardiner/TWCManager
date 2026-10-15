# A telemetry feed (TeslaMate, Fleet Telemetry) as a provider: free and
# passive. It reports on its own and cannot be asked, so it never enters a
# walk; it is registered so the order knows the feed and its liveness.
from .Capability import (
    CLOUD_FREE,
    READ_CHARGE,
    READ_LOCATION,
    READ_ONLINE,
    READ_PLUG,
    Declaration,
)
from .Provider import CLOUD_SUPPRESSED, Provider

CAPABILITIES = (READ_CHARGE, READ_LOCATION, READ_PLUG, READ_ONLINE)


class TelemetryProvider(Provider):
    rank = 3
    local = False
    DECLARATIONS = {
        capability: Declaration(CLOUD_FREE, passive=True) for capability in CAPABILITIES
    }
    SUPPRESSED = CLOUD_SUPPRESSED

    def __init__(self, telemetry):
        self.telemetry = telemetry
        # The registry's liveness and observation source name
        self.name = telemetry.__class__.__name__

    def ready(self, vehicle, capability) -> bool:
        if capability not in CAPABILITIES:
            return False
        at = self.telemetry.feedLiveAt(vehicle)
        return isinstance(at, (int, float)) and not isinstance(at, bool)
