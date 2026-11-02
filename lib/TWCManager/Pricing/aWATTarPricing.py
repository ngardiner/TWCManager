# aWATTar spot-market electricity pricing (Austria / Germany).
#
# Reports the current wholesale import price from the aWATTar market-data API.
# aWATTar asks that the API be queried no more than once every 15 minutes.
import requests
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("aWATTar", "Pricing")


class aWATTarPricing:
    # aWATTar asks us to limit queries to once every 15 minutes
    cacheTime = 900
    capabilities = {"AdvancePricing": True}
    config = None
    configConfig = None
    configAwattar = None
    currency = "EUR"
    exportPrice = 0
    fetchFailed = False
    importPrice = 0
    lastFetch = 0
    master = None
    status = False
    timeout = 10

    def __init__(self, master):
        self.master = master
        self.config = master.config
        self.configConfig = master.config.get("config", {})
        self.configAwattar = master.config.get("pricing", {}).get("aWATTar", {})

        self.status = self.configAwattar.get("enabled", False)

        # Unload if this module is disabled
        if not self.status:
            self.master.releaseModule("lib.TWCManager.Pricing", "aWATTarPricing")
            return None

    def getCapabilities(self, capability):
        return self.capabilities.get(capability, False)

    def getImportPrice(self):
        if not self.status:
            logger.debug("aWATTar Pricing Module Disabled. Skipping getImportPrice")
            return 0
        self.update()
        return float(self.importPrice)

    def getExportPrice(self):
        if not self.status:
            logger.debug("aWATTar Pricing Module Disabled. Skipping getExportPrice")
            return 0
        self.update()
        return float(self.exportPrice)

    def update(self):
        self.fetchFailed = False

        if (int(time.time()) - self.lastFetch) <= self.cacheTime:
            return

        url = "https://api.awattar.at/v1/marketdata"

        try:
            r = requests.get(url, timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            logger.error("Error connecting to aWATTar API to fetch market pricing")
            logger.debug(str(e))
            self.fetchFailed = True
            return

        self.lastFetch = int(time.time())

        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            logger.error(
                "HTTP status %s connecting to aWATTar API to fetch market pricing"
                % e.response.status_code
            )
            self.fetchFailed = True
            return

        data = r.json()
        if data:
            try:
                price = float(data["data"][0]["marketprice"])
                if data["data"][0]["unit"] == "Eur/MWh":
                    # Convert MWh price to kWh
                    price = price / 1000
                self.importPrice = price
            except (KeyError, TypeError, IndexError):
                logger.error("Exception during parsing aWATTar pricing")
