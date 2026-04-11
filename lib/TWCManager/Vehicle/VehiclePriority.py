# This module acts as a superclass which determines the priority of Vehicle modules
# and sends commands to the higher priority modules first, before falling-back to lower
# priority modules on failure.
#
# Retry Logic: For every 10 priority points, we get 1 retry before falling back.
# Example: Priority 20 (TeslaBLE) = 2 retries, Priority 10 (TeslaAPI) = 1 retry
import logging

logger = logging.getLogger("\U0001f697 VehiclePriority")


class VehiclePriority:
    master = None

    def __init__(self, master):
        self.master = master

    def updateSettings(self):
        # Need to catch this one to avoid an exception
        return True

    def _calculate_retries(self, priority):
        """Calculate number of retries based on priority level.
        For every 10 priority points, we get 1 retry.
        Example: priority 20 = 2 retries, priority 10 = 1 retry, priority 5 = 0 retries
        """
        return max(0, priority // 10)

    def __getattr__(self, name):
        def method(*args, **kwargs):
            ret = False
            priority = 100
            module_name = None
            module_ref = None

            logger.debug(
                f"VehiclePriority: Attempting to call {name} with priority fallback"
            )

            # Try each module in priority order
            while priority > 0:
                module_name, module_ref, priority = self.master.getModuleByPriority(
                    "Vehicle", priority
                )

                # If no module found at this priority level, we've exhausted all options
                if not module_ref or not module_name:
                    logger.debug(
                        f"VehiclePriority: No more modules available for {name}"
                    )
                    break

                # Calculate retries for this priority level
                retries = self._calculate_retries(priority)
                max_attempts = retries + 1  # +1 for the initial attempt

                logger.debug(
                    f"VehiclePriority: Trying {module_name} for {name} (priority {priority}, {max_attempts} attempts)"
                )

                # Try this module up to max_attempts times
                for attempt in range(max_attempts):
                    try:
                        # Check if module has the requested method
                        if hasattr(module_ref, name):
                            if attempt > 0:
                                logger.debug(
                                    f"VehiclePriority: Retry {attempt}/{retries} for {module_name}.{name}()"
                                )

                            module_method = getattr(module_ref, name)
                            ret = module_method(*args, **kwargs)

                            # Track success/failure
                            if ret:
                                logger.debug(
                                    f"VehiclePriority: {module_name}.{name}() succeeded on attempt {attempt + 1}"
                                )
                                self.master.stats["moduleSuccess"][module_name] = (
                                    self.master.stats["moduleSuccess"].get(
                                        module_name, 0
                                    )
                                    + 1
                                )
                                return ret  # Success, return immediately
                            else:
                                # Failed, but we might retry
                                if attempt < retries:
                                    logger.debug(
                                        f"VehiclePriority: {module_name}.{name}() failed, retrying ({attempt + 1}/{retries})"
                                    )
                                else:
                                    logger.debug(
                                        f"VehiclePriority: {module_name}.{name}() failed after {max_attempts} attempts, trying next module"
                                    )
                                    self.master.stats["moduleFailures"][module_name] = (
                                        self.master.stats["moduleFailures"].get(
                                            module_name, 0
                                        )
                                        + 1
                                    )
                        else:
                            logger.debug(
                                f"VehiclePriority: {module_name} does not have method {name}"
                            )
                            break  # Method doesn't exist, don't retry
                    except Exception as e:
                        # Log failure and continue to next attempt or module
                        if attempt < retries:
                            logger.debug(
                                f"VehiclePriority: Exception in {module_name}.{name}() on attempt {attempt + 1}: {str(e)}, retrying"
                            )
                        else:
                            logger.debug(
                                f"VehiclePriority: Exception in {module_name}.{name}() after {max_attempts} attempts: {str(e)}, trying next module"
                            )
                            self.master.stats["moduleFailures"][module_name] = (
                                self.master.stats["moduleFailures"].get(module_name, 0)
                                + 1
                            )
                        continue

            logger.debug(
                f"VehiclePriority: All modules exhausted for {name}, returning {ret}"
            )
            return ret

        return method
