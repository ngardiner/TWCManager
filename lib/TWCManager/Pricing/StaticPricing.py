class StaticPricing:

    # For environments where dynamic pricing information is not available via an
    # API, the pricing information can be configured statically within the config
    # file

    import time

    capabilities = {
        "AdvancePricing": True
    }
    config = None
    configConfig = None
    configStatic = None
    exportPrice = 0
    importPrice = 0
    status = False

    def __init__(self, master):

        self.master = master
        self.config = master.config
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}

        try:
            self.configStatic = master.config["pricing"]["Static"]
        except KeyError:
            self.configStatic = {}

        self.status = self.configStatic.get("enabled", self.status)
        self.debugLevel = self.configConfig.get("debugLevel", 0)

        # Unload if this module is disabled or misconfigured
        if not self.status:
            self.master.releaseModule("lib.TWCManager.Pricing", self.__class__.__name__)
            return None

    def getCapabilities(self, capability):
        # Allows query of module capabilities
        return self.capabilities.get(capability, False)

    def getExportPrice(self):

        if not self.status:
            self.master.debugLog(
                10,
                "$Static",
                "Static Pricing Module Disabled. Skipping getExportPrice",
            )
            return 0

        # For now, we just use the peak price
        try:
            self.exportPrice = self.configStatic["peak"]["export"]
        except ValueError:
            self.exportPrice = 0

        # Return current export price
        return float(self.exportPrice)

    def getImportPrice(self):

        if not self.status:
            self.master.debugLog(
                10,
                "$Static",
                "Static Pricing Module Disabled. Skipping getImportPrice",
            )
            return 0

        # For now, we just use the peak price
        try:
            self.importPrice = self.configStatic["peak"]["import"]
        except ValueError:
            self.exportPrice = 0

        # Return current import price
        return float(self.importPrice)

