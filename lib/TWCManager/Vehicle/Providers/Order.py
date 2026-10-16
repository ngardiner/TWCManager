# The fixed provider order: cheapest cost tier first, then the provider's
# rank, then registration order. Every vehicle operation walks it. Nothing
# here holds a lock; the provider list is replaced, never mutated, so a walk
# works on a snapshot.
import json
import logging
import time
from typing import List, Optional

from ..Registry.ReadSource import ReadSource
from .Capability import READ_CHARGE, READS, SET_AMPS, TIER_BILLED, WAKE
from .Provider import Provider
from .Suppressions import Suppressions

logger = logging.getLogger(__name__.split(".")[0] + ".Vehicle.Providers")

# Where a read source with no registered provider sorts
UNKNOWN_TIER = 9
UNKNOWN_RANK = 999


class ProviderOrder:
    def __init__(self, master, suppressions: Optional[Suppressions] = None):
        self.master = master
        self.suppressions = suppressions or Suppressions(master)
        self._providers: List[Provider] = []

    def register(self, provider) -> None:
        providers = list(self._providers)
        for i, existing in enumerate(providers):
            if existing.name == provider.name:
                # Keeps the replaced provider's registration index
                providers[i] = provider
                break
        else:
            providers.append(provider)
        self._providers = providers

    def provider(self, name) -> Optional[Provider]:
        if not name:
            return None
        for p in self._providers:
            if p.name == name:
                return p
        return None

    def _indexed(self):
        return list(enumerate(self._providers))

    def providers(self) -> List[Provider]:
        return [p for _, p in sorted(self._indexed(), key=lambda e: (e[1].rank, e[0]))]

    def _key(self, provider, capability, index) -> tuple:
        return (provider.cost(capability).tier, provider.rank, index)

    def ordered(self, capability) -> List[Provider]:
        found = []
        for index, p in self._indexed():
            declaration = p.declarations().get(capability)
            if declaration is None or declaration.passive:
                continue
            if self.suppressions.suppressed(p, capability):
                continue
            try:
                if not p.available(capability):
                    continue
            except Exception as e:
                logger.debug("%s available() raised %s", p.name, type(e).__name__)
                continue
            found.append((self._key(p, capability, index), p))
        return [p for _, p in sorted(found, key=lambda e: e[0])]

    def _ready(self, provider, vehicle, capability) -> bool:
        try:
            return bool(provider.ready(vehicle, capability))
        except Exception as e:
            logger.debug("%s ready() raised %s", provider.name, type(e).__name__)
            return False

    def candidates(
        self,
        vehicle,
        capability,
        amps: Optional[float] = None,
        localRule: bool = True,
    ) -> List[Provider]:
        # localRule False: readiness only. A lift or the debug page's rate
        # must reach the car even where the TWC covers the rate, since the
        # TWC cannot lift the car's own current limit (#660)
        found = [
            p for p in self.ordered(capability) if self._ready(p, vehicle, capability)
        ]
        if amps is not None:
            # Outside the preferred provider's range the next provider
            # carries the target
            found = [
                p
                for p in found
                if p.ampsRange(vehicle) is None or p.covers(vehicle, amps)
            ]
        if capability == SET_AMPS and localRule:
            # Paid providers never set amps where the charging point has a
            # local way to
            localPath = any(
                p.local and (amps is None or p.covers(vehicle, amps)) for p in found
            )
            if localPath:
                found = [p for p in found if p.cost(capability).tier < TIER_BILLED]
        return found

    def orderReadSources(self, sources: List[ReadSource]) -> List[ReadSource]:
        keyed = []
        for index, source in enumerate(sources):
            p = self.provider(getattr(source, "provider", ""))
            if p is None:
                tier, rank = UNKNOWN_TIER, UNKNOWN_RANK
            else:
                if self.suppressions.suppressed(p, READ_CHARGE):
                    continue
                tier = min(
                    (d.cost.tier for c, d in p.declarations().items() if c in READS),
                    default=UNKNOWN_TIER,
                )
                rank = p.rank
            keyed.append(((tier, rank, getattr(source, "order", 0), index), source))
        return [s for _, s in sorted(keyed, key=lambda e: e[0])]

    def debugCommand(self, command: str, vin: str, parameters: str) -> bool:
        """The debug page's /api/sendTeslaAPICommand: one one-shot attempt
        through the reconciler, on the first provider able to carry it. One
        provider per command, no fall-through and no retry: a user-initiated
        command is never sent twice. Returns True when an attempt was submitted; the
        reconciler logs its outcome at INFO."""
        vehicle = self.master.vehicleRegistry.get(vin)
        if vehicle is None:
            return False
        amps = None
        if command == "setChargeRate":
            try:
                params = json.loads(parameters)
            except (TypeError, ValueError):
                params = {}
            if not isinstance(params, dict):
                params = {}
            try:
                # Vehicle amps are never 0
                amps = max(1, int(float(params.get("charge_rate", 0))))
            except (TypeError, ValueError, OverflowError):
                return False
            capability = SET_AMPS
        elif command == "wakeVehicle":
            capability = WAKE
        else:
            return False
        reconciler = getattr(self.master, "reconciler", None)
        if reconciler is None:
            logger.debug("Debug %s for %s: no reconciler", command, vin)
            return False
        # Readiness only for a rate (localRule False): the TWC cannot lift
        # the car's own current limit (#660). A TWC provider (no module) is
        # never written from the HTTP thread; the distributor owns its target
        provider = next(
            (
                p
                for p in self.candidates(vehicle, capability, amps, localRule=False)
                if p.module is not None
            ),
            None,
        )
        if provider is None:
            return False
        # Lazy: Reconcile imports this package's Capability
        from ..Reconcile.Desired import Attempt

        attempt = Attempt(
            key=vin,
            aspect=None,
            capability=capability,
            value=amps,
            provider=provider.name,
            wake=False,
            maxAge=None,
            restore=False,
            since=time.time(),
        )
        return bool(reconciler.submit(attempt))


def registerProviders(master) -> List[str]:
    # Called from TWCManager.py after the module-load loop
    for mod in master.getModulesByType("Vehicle"):
        # One module's failure must not stop the others, or startup. Visible:
        # a skipped TeslaBLE leaves its read source unsuppressed under api_only
        try:
            for provider in getattr(mod["ref"], "providers", lambda: [])():
                master.providerOrder.register(provider)
        except Exception as e:
            logger.warning(
                "Could not register %s providers: %s", mod["name"], type(e).__name__
            )
    names = [p.name for p in master.providerOrder.providers()]
    logger.info("Providers: %s", ", ".join(names))
    return names
