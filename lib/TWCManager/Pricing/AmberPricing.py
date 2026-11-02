# Amber Electric wholesale electricity pricing (Australia).
#
# Reports the current import (general) and export (feedIn) price for the
# configured Amber site. Current price only; price forecasting and
# cheapest-window scheduling are not yet implemented, so this module does
# not advertise the "Forecasting" capability.
from datetime import datetime, timezone
import requests
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("Amber", "Pricing")


class AmberPricing:
    cacheTime = 300
    capabilities = {
        "AdvancePricing": True,
        "SpikeDetection": True,
        "Renewables": True,
    }
    config = None
    configConfig = None
    configAmber = None
    currency = "$"
    exportPrice = 0
    fetchFailed = False
    importPrice = 0
    lastFetch = 0
    master = None
    priceDescriptor = "neutral"
    renewables = 0
    siteId = None
    spikeStatus = "none"
    status = False
    timeout = 10

    def __init__(self, master):
        self.master = master
        self.config = master.config
        self.configConfig = master.config.get("config", {})
        self.configAmber = master.config.get("pricing", {}).get("Amber", {})

        self.status = self.configAmber.get("enabled", False)
        self.siteId = self.configAmber.get("siteId", None)
        self.cacheTime = self.configAmber.get("cacheTime", self.cacheTime)

        # Unload if this module is disabled
        if not self.status:
            self.master.releaseModule("lib.TWCManager.Pricing", "AmberPricing")
            return None

        # A token is required to reach the Amber API
        if not self.configAmber.get("token"):
            logger.info("Amber API token not configured. Module will be disabled.")
            self.master.releaseModule("lib.TWCManager.Pricing", "AmberPricing")
            self.status = False
            return None

    def _authHeaders(self):
        return {"Authorization": "Bearer " + str(self.configAmber.get("token", ""))}

    def getCapabilities(self, capability):
        return self.capabilities.get(capability, False)

    def getImportPrice(self):
        if not self.status:
            logger.debug("Amber Pricing Module Disabled. Skipping getImportPrice")
            return 0
        self.update()
        return float(self.importPrice)

    def getExportPrice(self):
        if not self.status:
            logger.debug("Amber Pricing Module Disabled. Skipping getExportPrice")
            return 0
        self.update()
        return float(self.exportPrice)

    def getSpikeStatus(self):
        if not self.status:
            return "none"
        self.update()
        return self.spikeStatus

    def getRenewables(self):
        if not self.status:
            return 0
        self.update()
        return self.renewables

    def discoverSiteId(self):
        url = "https://api.amber.com.au/v1/sites"
        try:
            r = requests.get(url, headers=self._authHeaders(), timeout=self.timeout)
            r.raise_for_status()
            sites = r.json()
        except Exception as e:
            logger.error("Error discovering Amber siteId: %s" % e)
            return False

        if sites:
            for site in sites:
                if site.get("status") == "active":
                    self.siteId = site.get("id")
                    logger.debug("Auto-discovered Amber siteId: %s" % self.siteId)
                    return True
        logger.info("No active Amber sites found")
        return False

    def update(self):
        self.fetchFailed = False

        if (int(time.time()) - self.lastFetch) <= self.cacheTime:
            return

        if not self.siteId:
            if not self.discoverSiteId():
                self.fetchFailed = True
                return

        url = (
            "https://api.amber.com.au/v1/sites/"
            + str(self.siteId)
            + "/prices/current?resolution=30"
        )

        try:
            r = requests.get(url, headers=self._authHeaders(), timeout=self.timeout)
        except requests.exceptions.ConnectionError as e:
            logger.error("Error connecting to Amber API to fetch pricing")
            logger.debug(str(e))
            self.fetchFailed = True
            return

        self.lastFetch = int(time.time())

        try:
            r.raise_for_status()
        except requests.exceptions.HTTPError as e:
            logger.error("HTTP status %s from Amber API" % e.response.status_code)
            self.fetchFailed = True
            return

        try:
            intervals = r.json()
            if not intervals:
                logger.debug("No Amber price data returned")
                return

            now_utc = datetime.now(timezone.utc)
            for interval in intervals:
                channel_type = interval.get("channelType")
                start_time = datetime.fromisoformat(
                    interval.get("startTime").replace("Z", "+00:00")
                )
                end_time = datetime.fromisoformat(
                    interval.get("endTime").replace("Z", "+00:00")
                )
                if start_time <= now_utc < end_time:
                    price_aud_kwh = interval.get("perKwh", 0) / 100.0
                    if channel_type == "general":
                        self.importPrice = price_aud_kwh
                        self.spikeStatus = interval.get("spikeStatus", "none")
                        self.priceDescriptor = interval.get("descriptor", "neutral")
                        self.renewables = interval.get("renewables", 0)
                    elif channel_type == "feedIn":
                        self.exportPrice = abs(price_aud_kwh)
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.error("Exception during parsing Amber pricing: %s" % e)
