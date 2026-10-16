# Which provider may not perform which operation: the explicit
# providerSuppressions setting, plus each provider's SUPPRESSED table for the
# conditions in force (commandPolicy, and chargeStopMode 2/3). Settings are
# read on every call, so a change applies at once.
import logging
from typing import Dict, FrozenSet

from .Capability import ALL
from .Provider import suppressedFor

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Providers")


def _vehicleConfig(master) -> dict:
    config = getattr(master, "config", None)
    vehicle = config.get("vehicle", {}) if isinstance(config, dict) else {}
    return vehicle if isinstance(vehicle, dict) else {}


def _parse(value, origin: str) -> Dict[str, FrozenSet[str]]:
    # {"FleetAPI": ["setAmps", "wake"], "HomeAssistant": ["*"]}; anything
    # malformed is ignored name by name, never raised
    if value is None:
        return {}
    if not isinstance(value, dict):
        logger.debug("Ignoring %s providerSuppressions: not an object", origin)
        return {}
    parsed = {}
    for provider, names in value.items():
        if not isinstance(provider, str) or not isinstance(names, list):
            logger.debug("Ignoring %s providerSuppressions entry", origin)
            continue
        capabilities = set()
        for name in names:
            if name == "*":
                capabilities |= ALL
            elif isinstance(name, str) and name in ALL:
                capabilities.add(name)
            else:
                logger.debug(
                    "Ignoring unknown capability in %s providerSuppressions for %s",
                    origin,
                    provider,
                )
        parsed[provider] = frozenset(capabilities)
    return parsed


class Suppressions:
    VALID_COMMAND_POLICIES = ("prefer_ble", "ble_only", "api_only")

    def __init__(self, master):
        self.master = master
        self._noticed = False
        # commandPolicy in config.json is a hard override of the Settings page
        policy = _vehicleConfig(master).get("commandPolicy", None)
        if policy:
            policy = str(policy).lower()
            if policy not in self.VALID_COMMAND_POLICIES:
                logger.error(
                    "Invalid vehicle commandPolicy '%s' in config.json; ignoring override",
                    policy,
                )
                policy = None
        self.configPolicy = policy

    @property
    def commandPolicy(self) -> str:
        if self.configPolicy:
            return self.configPolicy
        settings = getattr(self.master, "settings", None) or {}
        policy = str(settings.get("commandPolicy", "prefer_ble")).lower()
        if policy not in self.VALID_COMMAND_POLICIES:
            return "prefer_ble"
        return policy

    def explicit(self) -> Dict[str, FrozenSet[str]]:
        settings = getattr(self.master, "settings", None) or {}
        merged = _parse(settings.get("providerSuppressions"), "settings")
        merged.update(
            _parse(_vehicleConfig(self.master).get("providerSuppressions"), "config")
        )
        return merged

    def conditions(self) -> FrozenSet[str]:
        # The commandPolicy, plus "localStops" when the user chose a
        # disruptive local stop, and "silentStops" for mode 2
        conditions = {self.commandPolicy}
        settings = getattr(self.master, "settings", None) or {}
        try:
            mode = int(settings.get("chargeStopMode", 1))
            if mode in (2, 3):
                conditions.add("localStops")
            if mode == 2:
                conditions.add("silentStops")
        except (TypeError, ValueError):
            pass
        return frozenset(conditions)

    def suppressed(self, provider, capability) -> bool:
        if capability in self.explicit().get(provider.name, frozenset()):
            return True
        return any(capability in suppressedFor(provider, c) for c in self.conditions())

    def noticeOnce(self) -> None:
        if self.commandPolicy != "api_only" or self._noticed:
            return
        self._noticed = True
        logger.info(
            "commandPolicy api_only: TeslaBLE is suppressed for every operation "
            "(set providerSuppressions for finer control)"
        )
