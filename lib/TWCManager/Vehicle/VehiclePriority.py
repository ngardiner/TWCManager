# A facade over the provider order. Callers keep calling the legacy module
# methods (car_api_charge, setChargeRate, applyChargeLimit, ...) with their
# module-specific arguments; the walk is the order: each provider's legacy
# module in order position, suppressed providers skipped and, when the call
# names a vehicle, providers not ready for it skipped. Attempts come from the
# provider (local 3, cloud 2, as BLE and TeslaAPI had by priority).
# Phase 3 removes the facade.
import logging
import threading
import time
from typing import Any, List, Optional, Tuple

from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Vehicle.Providers.Capability import (
    TIER_BILLED,
    READ_CHARGE,
    SET_AMPS,
    SET_CHARGE_LIMIT,
    START_CHARGING,
    STOP_CHARGING,
    WAKE,
)
from TWCManager.Vehicle.Providers.Suppressions import Suppressions

logger = LoggerFactory.get_logger("VehiclePriority", "Vehicle")


def _vinOf(value) -> Optional[str]:
    for candidate in (
        getattr(value, "VIN", None),
        getattr(value, "vin", None),
        value,
    ):
        if isinstance(candidate, str) and candidate:
            return candidate.upper()
    return None


class VehiclePriority:
    master = None
    VALID_COMMAND_POLICIES = Suppressions.VALID_COMMAND_POLICIES

    # State-changing (command) methods. They walk the order for their
    # capability, so commandPolicy and providerSuppressions apply; any other
    # method walks every provider not suppressed for reads.
    COMMAND_METHODS = frozenset(
        [
            "car_api_charge",
            "setChargeRate",
            "startCharging",
            "stopCharging",
            "applyChargeLimit",
            "wakeVehicle",
        ]
    )

    # car_api_charge resolves per task: see capabilityFor
    COMMAND_CAPABILITIES = {
        "car_api_charge": None,
        "setChargeRate": SET_AMPS,
        "startCharging": START_CHARGING,
        "stopCharging": STOP_CHARGING,
        "applyChargeLimit": SET_CHARGE_LIMIT,
        "wakeVehicle": WAKE,
    }

    # A provider reached only because a billed one failed is called at most
    # this often per car and command: Fleet's own once-a-minute cadence
    FALLBACK_INTERVAL = 60

    def __init__(self, master):
        self.master = master
        self.order = master.providerOrder
        self._clock = time.monotonic
        # (provider, capability, VIN or None) -> when the fallback last ran
        self._fallbackAt = {}
        self._fallbackLock = threading.Lock()

    @property
    def commandPolicy(self):
        return self.order.suppressions.commandPolicy

    @property
    def commandPolicyOverridden(self):
        # True when config.json pins the policy, disabling the UI control.
        return self.order.suppressions.commandPolicyOverridden

    def updateSettings(self):
        # Need to catch this one to avoid an exception
        return True

    def providers(self):
        # registerProviders asks every Vehicle module; the facade has none of
        # its own, and must not walk the order for this
        return []

    @staticmethod
    def capabilityFor(name, args, kwargs) -> Optional[str]:
        if name == "car_api_charge":
            task = args[0] if args else kwargs.get("task")
            if isinstance(task, dict) and task.get("charge") is False:
                return STOP_CHARGING
            return START_CHARGING
        return VehiclePriority.COMMAND_CAPABILITIES.get(name)

    @staticmethod
    def targetVIN(name, args, kwargs) -> Optional[str]:
        # None when the call names no vehicle: a broadcast
        if name == "car_api_charge":
            task = args[0] if args else kwargs.get("task")
            return _vinOf(task.get("vin")) if isinstance(task, dict) else None
        if name == "applyChargeLimit":
            return _vinOf(kwargs.get("vin"))
        if name == "setChargeRate":
            vehicle = args[1] if len(args) > 1 else kwargs.get("vehicle")
            return _vinOf(vehicle)
        if name in ("startCharging", "stopCharging", "wakeVehicle"):
            if args:
                return _vinOf(args[0])
            return _vinOf(kwargs.get("vin", kwargs.get("vehicle")))
        return None

    @staticmethod
    def targetAmps(name, args, kwargs) -> Optional[float]:
        # The rate a setChargeRate asks for. Provider selection does not use
        # it: setChargeRate walks every ready provider (see modulesFor).
        # Remove with the facade in Phase 3.
        if name != "setChargeRate":
            return None
        value = args[0] if args else kwargs.get("charge_rate")
        if isinstance(value, bool):
            return None
        try:
            amps = float(value)
        except (TypeError, ValueError):
            return None
        if amps != amps:
            return None
        # Vehicle amps are never 0 and every module clamps 0 to 1; 0 would
        # drop every candidate. The module still gets the caller's value.
        return max(1.0, amps)

    def modulesFor(self, name, args, kwargs) -> List[Tuple[Any, Any]]:
        if name in self.COMMAND_METHODS:
            capability = self.capabilityFor(name, args, kwargs)
            vin = self.targetVIN(name, args, kwargs)
            vehicle = self.master.vehicleRegistry.get(vin) if vin else None
            if vehicle is not None:
                # setChargeRate: readiness only. The TWC cannot lift the
                # car's own current limit (#660), so the local-amps rule
                # that keeps Fleet off a rate the TWC covers applies to
                # setAmps, not here
                ready = self.order.candidates(
                    vehicle, capability, localRule=name != "setChargeRate"
                )
                providers = [
                    p
                    for p in self.order.ordered(capability)
                    if p in ready or self._listUnloaded(p)
                ]
            else:
                providers = self.order.ordered(capability)
        else:
            # api_only keeps BLE out of reads too, as orderReadSources does;
            # a disabled module (HomeAssistant) is not available
            providers = [
                p
                for p in self.order.providers()
                if not self.order.suppressions.suppressed(p, READ_CHARGE)
                and self._available(p, READ_CHARGE)
            ]
        walk, seen = [], set()
        for p in providers:
            module = p.module
            if module is None or id(module) in seen:
                # Owner and Fleet share TeslaAPI: walked once, first position
                continue
            seen.add(id(module))
            walk.append((p, module))
        return walk

    @staticmethod
    def _listUnloaded(provider) -> bool:
        # TeslaAPI's providers are ready only for a car on its vehicle list,
        # but only TeslaAPI's own command methods load that list. Until it
        # is loaded, a targeted command keeps them (ordered() has already
        # checked the slot can command), and TeslaAPI loads the list and
        # finds the car itself, as before the provider order.
        if provider.name not in ("OwnerAPI", "FleetAPI"):
            return False
        try:
            return provider.module.getVehicleCount() == 0
        except Exception as e:
            logger.debug(f"{provider.name} getVehicleCount() raised {type(e).__name__}")
            return False

    @staticmethod
    def _available(provider, capability) -> bool:
        try:
            return bool(provider.available(capability))
        except Exception as e:
            logger.debug(f"{provider.name} available() raised {type(e).__name__}")
            return False

    def _argsFor(self, provider, name, args, kwargs):
        # The call's arguments for this provider's module, or None to skip
        # it. A module that takes its own vehicle type (HomeAssistant) gets
        # its own vehicle, looked up by VIN, never the TeslaAPI one.
        if name != "setChargeRate" or not provider.vehicleAttachment:
            return args, kwargs
        positional = len(args) > 1
        vehicle = args[1] if positional else kwargs.get("vehicle")
        if vehicle is None:
            return args, kwargs
        vin = self.targetVIN(name, args, kwargs)
        registered = self.master.vehicleRegistry.get(vin) if vin else None
        own = registered.attachment(provider.vehicleAttachment) if registered else None
        if own is None:
            return None
        if positional:
            return (args[0], own) + tuple(args[2:]), kwargs
        return args, dict(kwargs, vehicle=own)

    @staticmethod
    def _billed(provider, capability) -> bool:
        cost = provider.cost(capability) if capability else None
        return cost is not None and cost.tier >= TIER_BILLED

    def _fallbackDue(self, provider, capability, vin) -> bool:
        # Records the call when it is due; no I/O under the lock
        key = (provider.name, capability, vin)
        now = self._clock()
        with self._fallbackLock:
            last = self._fallbackAt.get(key)
            if last is not None and now - last < self.FALLBACK_INTERVAL:
                return False
            self._fallbackAt[key] = now
            return True

    def _count(self, kind, name):
        counts = self.master.stats.setdefault(kind, {})
        counts[name] = counts.get(name, 0) + 1

    def setAmps(self, vin, amps) -> bool:
        vehicle = self.master.vehicleRegistry.get(vin)
        if vehicle is None:
            return False
        return self.order.dispatch(
            vehicle, SET_AMPS, lambda p: p.setAmps(vin, amps), amps=amps
        )

    def __getattr__(self, name):
        if name.startswith("_"):
            # Probes such as copy's __deepcopy__ never walk the order
            raise AttributeError(name)

        def method(*args, **kwargs):
            walk = self.modulesFor(name, args, kwargs)
            if not walk:
                logger.debug(f"VehiclePriority: No modules available for {name}")
                return False

            capability = (
                self.capabilityFor(name, args, kwargs)
                if name in self.COMMAND_METHODS
                else None
            )
            vin = self.targetVIN(name, args, kwargs) if capability else None
            billedFailed = False
            for provider, module_ref in walk:
                module_name = provider.name
                if not hasattr(module_ref, name):
                    logger.debug(
                        f"VehiclePriority: {module_name} does not have method {name}"
                    )
                    continue
                call = self._argsFor(provider, name, args, kwargs)
                if call is None:
                    logger.debug(
                        f"VehiclePriority: {module_name} has no vehicle of its own for {name}"
                    )
                    continue
                callArgs, callKwargs = call

                if billedFailed and not self._fallbackDue(provider, capability, vin):
                    # A billed provider (Fleet) failed: during its backoff the
                    # next one (Home Assistant) would otherwise run at its
                    # own, faster cadence
                    logger.debug(
                        f"VehiclePriority: {module_name}.{name}() held to the fallback interval"
                    )
                    continue

                max_attempts = provider.attempts()
                self._count("moduleDispatch", module_name)
                logger.debug(
                    f"VehiclePriority: Trying {module_name} for {name} ({max_attempts} attempts)"
                )

                for attempt in range(max_attempts):
                    try:
                        ret = getattr(module_ref, name)(*callArgs, **callKwargs)
                    except Exception as e:
                        ret = False
                        logger.debug(
                            f"VehiclePriority: {module_name}.{name}() raised {type(e).__name__} on attempt {attempt + 1}"
                        )
                        continue
                    # Any truthy return but "error" ends the walk (TeslaAPI
                    # returns "success", "skipped" or "error"); None, False
                    # and "error" try the next provider
                    if ret and ret != "error":
                        logger.debug(
                            f"VehiclePriority: {module_name}.{name}() succeeded on attempt {attempt + 1}"
                        )
                        self._count("moduleSuccess", module_name)
                        return ret
                    logger.debug(
                        f"VehiclePriority: {module_name}.{name}() failed on attempt {attempt + 1}/{max_attempts}"
                    )

                logger.log(
                    logging.INFO2,
                    f"VehiclePriority: {module_name}.{name}() failed after {max_attempts} attempts, trying next provider",
                )
                self._count("moduleFailures", module_name)
                if self._billed(provider, capability):
                    billedFailed = True

            logger.debug(f"VehiclePriority: All providers exhausted for {name}")
            return False

        return method
