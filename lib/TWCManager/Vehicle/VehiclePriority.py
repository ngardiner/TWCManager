# This module acts as a superclass which determines the priority of Vehicle modules
# and sends commands to the higher priority modules first, before falling-back to lower
# priority modules on failure.
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
            while ret == False and priority > 0:
                module_name, module_ref, priority = self.master.getModuleByPriority("Vehicle", priority)
                module_method = getattr(module_ref, name)
                ret = module_method(*args, **kwargs)
                if ret:
                    self.master.stats["moduleSuccess"][module_name] = (self.master.stats["moduleSuccess"].get(module_name,0) + 1)
                else:
                    self.master.stats["moduleFailure"][module_name] = (self.master.stats["moduleFailure"].get(module_name,0) + 1)

            return ret
        return method
