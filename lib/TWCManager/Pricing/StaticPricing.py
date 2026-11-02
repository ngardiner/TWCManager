# Static (fixed-rate) electricity pricing module.
#
# For users on a fixed tariff who still want price-aware charging policy. It
# reports constant import/export prices from config; no network access.
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("Static", "Pricing")


class StaticPricing:
    capabilities = {"AdvancePricing": True}
    config = None
    configConfig = None
    configStatic = None
    currency = "$"
    exportPrice = 0
    importPrice = 0
    master = None
    status = False

    def __init__(self, master):
        self.master = master
        self.config = master.config
        self.configConfig = master.config.get("config", {})
        self.configStatic = master.config.get("pricing", {}).get("Static", {})

        self.status = self.configStatic.get("enabled", False)

        # Unload if this module is disabled
        if not self.status:
            self.master.releaseModule("lib.TWCManager.Pricing", "StaticPricing")
            return None

        self.currency = self.configStatic.get("currency", self.currency)
        peak = self.configStatic.get("peak", {})
        self.importPrice = float(peak.get("import", 0))
        self.exportPrice = float(peak.get("export", 0))

    def getCapabilities(self, capability):
        return self.capabilities.get(capability, False)

    def getImportPrice(self):
        if not self.status:
            logger.debug("Static Pricing Module Disabled. Skipping getImportPrice")
            return 0
        return float(self.importPrice)

    def getExportPrice(self):
        if not self.status:
            logger.debug("Static Pricing Module Disabled. Skipping getExportPrice")
            return 0
        return float(self.exportPrice)
