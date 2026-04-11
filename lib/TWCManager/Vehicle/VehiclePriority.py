# This module acts as a superclass which determines the priority of Vehicle modules
# and sends commands to the higher priority modules first, before falling-back to lower
# priority modules on failure.
import logging

logger = logging.getLogger("\U0001f697 VehiclePriority")


class VehiclePriority:
    master = None

    def __init__(self, master):
        self.master = master

    def updateSettings(self):
        # Need to catch this one to avoid an exception
        return True

    def __getattr__(self, name):
        def method(*args, **kwargs):
            ret = False
            priority = 100
            module_name = None
            module_ref = None
            
            logger.debug(f"VehiclePriority: Attempting to call {name} with priority fallback")
            
            # Try each module in priority order
            while priority > 0:
                module_name, module_ref, priority = self.master.getModuleByPriority("Vehicle", priority)
                
                # If no module found at this priority level, we've exhausted all options
                if not module_ref or not module_name:
                    logger.debug(f"VehiclePriority: No more modules available for {name}")
                    break
                
                try:
                    # Check if module has the requested method
                    if hasattr(module_ref, name):
                        logger.debug(f"VehiclePriority: Trying {module_name} for {name}")
                        module_method = getattr(module_ref, name)
                        ret = module_method(*args, **kwargs)
                        
                        # Track success/failure
                        if ret:
                            logger.debug(f"VehiclePriority: {module_name}.{name}() succeeded")
                            self.master.stats["moduleSuccess"][module_name] = (self.master.stats["moduleSuccess"].get(module_name, 0) + 1)
                            return ret  # Success, return immediately
                        else:
                            logger.debug(f"VehiclePriority: {module_name}.{name}() failed, trying next module")
                            self.master.stats["moduleFailures"][module_name] = (self.master.stats["moduleFailures"].get(module_name, 0) + 1)
                    else:
                        logger.debug(f"VehiclePriority: {module_name} does not have method {name}")
                except Exception as e:
                    # Log failure and continue to next module
                    logger.debug(f"VehiclePriority: Exception in {module_name}.{name}(): {str(e)}")
                    self.master.stats["moduleFailures"][module_name] = (self.master.stats["moduleFailures"].get(module_name, 0) + 1)
                    continue

            logger.debug(f"VehiclePriority: All modules exhausted for {name}, returning {ret}")
            return ret
        return method
