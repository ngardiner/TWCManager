# Amber Electric wholesale electricity pricing (Australia).
#
# Reports the current import (general) and export (feedIn) price for the
# configured Amber site, and the forward price series Amber's API returns
# as its forecast.
from datetime import datetime, timezone
import requests
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Pricing.Forecast import ForecastingPricing

logger = LoggerFactory.get_logger("Amber", "Pricing")


class AmberPricing(ForecastingPricing):
    cacheTime = 300
    capabilities = {
        "AdvancePricing": True,
        "Forecasting": True,
        "SpikeDetection": True,
        "Renewables": True,
    }
    currency = "$"
    exportPrice = 0
    fetchFailed = False
    forecastCacheTime = 900
    importPrice = 0
    lastFetch = 0
    lastForecastFetch = 0
    priceDescriptor = "neutral"
    renewables = 0
    spikeStatus = "none"
    timeout = 10

    def __init__(self, master):
        super().__init__(master)
        self.priceForecast = []
        self.siteId = self.configPricing.get("siteId", None)
        self.cacheTime = self.configPricing.get("cacheTime", self.cacheTime)

        # A token is required to reach the Amber API
        if self.status and not self.configPricing.get("token"):
            logger.info("Amber API token not configured. Module will be disabled.")
            self.master.releaseModule("lib.TWCManager.Pricing", "AmberPricing")
            self.status = False

    def _authHeaders(self):
        return {"Authorization": "Bearer " + str(self.configPricing.get("token", ""))}

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

    # ------------------------------------------------------------------
    # Forecasting
    # ------------------------------------------------------------------
    def _forecast(self):
        return self.priceForecast, datetime.now(timezone.utc), None

    def updateForecast(self, hoursAhead=48):
        # Fetches, so it runs as a background task or for the HTTP API,
        # never from the attribute reads policy makes
        if not self.status:
            return

        if (int(time.time()) - self.lastForecastFetch) <= self.forecastCacheTime:
            return

        if not self.siteId:
            if not self.discoverSiteId():
                return

        # Amber returns the current interval plus "next" future intervals. At
        # 30 minute resolution two intervals make an hour.
        nextIntervals = int(hoursAhead) * 2
        url = (
            "https://api.amber.com.au/v1/sites/"
            + str(self.siteId)
            + "/prices/current?resolution=30&next="
            + str(nextIntervals)
        )

        try:
            r = requests.get(url, headers=self._authHeaders(), timeout=self.timeout)
            r.raise_for_status()
            intervals = r.json()
        except Exception as e:
            logger.error("Error fetching Amber price forecast: %s" % e)
            return

        self.lastForecastFetch = int(time.time())

        forecast = []
        try:
            now_utc = datetime.now(timezone.utc)
            for interval in intervals:
                if interval.get("channelType") != "general":
                    continue
                end_time = datetime.fromisoformat(
                    interval.get("endTime").replace("Z", "+00:00")
                )
                # Only keep intervals that are still in the future
                if end_time <= now_utc:
                    continue
                start_time = datetime.fromisoformat(
                    interval.get("startTime").replace("Z", "+00:00")
                )
                forecast.append(
                    {
                        "timestamp": start_time,
                        "importPrice": interval.get("perKwh", 0) / 100.0,
                        "descriptor": interval.get("descriptor", "neutral"),
                        "spikeStatus": interval.get("spikeStatus", "none"),
                        "renewables": interval.get("renewables", 0),
                    }
                )
        except (KeyError, TypeError, ValueError, AttributeError) as e:
            logger.error("Exception during parsing Amber forecast: %s" % e)
            return

        self.priceForecast = forecast
