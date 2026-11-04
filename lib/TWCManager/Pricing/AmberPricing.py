# Amber Electric wholesale electricity pricing (Australia).
#
# Reports the current import (general) and export (feedIn) price for the
# configured Amber site, and - because Amber's API returns a forward price
# series - an optional price forecast used to identify the cheapest charging
# window. Forecasting is only acted on when a cheapWindow is configured.
from datetime import datetime, timezone
import requests
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Pricing.Forecast import (
    cheapestWindow,
    priceRange,
    serialise,
    windowActive,
)

logger = LoggerFactory.get_logger("Amber", "Pricing")


class AmberPricing:
    cacheTime = 300
    capabilities = {
        "AdvancePricing": True,
        "Forecasting": True,
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

    # Forecasting state
    forecastCacheTime = 900
    lastForecastFetch = 0
    priceForecast = []
    cheapWindowHours = 0
    cheapWindowStartHour = None
    cheapWindowEndHour = None
    cheapWindowActive = 0
    cheapWindowStart = None
    forecastMinPrice = 0
    forecastMaxPrice = 0

    def __init__(self, master):
        self.master = master
        self.config = master.config
        self.configConfig = master.config.get("config", {})
        self.configAmber = master.config.get("pricing", {}).get("Amber", {})
        # Reset instance forecast state so test instances do not share it
        self.priceForecast = []

        self.status = self.configAmber.get("enabled", False)
        self.siteId = self.configAmber.get("siteId", None)
        self.cacheTime = self.configAmber.get("cacheTime", self.cacheTime)

        # Optional cheapest-window configuration. Absent means the forecast is
        # still fetched on demand (for the API/UI) but no window is computed and
        # cheapWindowActive stays 0, so existing behaviour is unaffected.
        cheapWindow = self.configAmber.get("cheapWindow", {})
        self.cheapWindowHours = cheapWindow.get("hours", 0)
        self.cheapWindowStartHour = cheapWindow.get("startHour", None)
        self.cheapWindowEndHour = cheapWindow.get("endHour", None)

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

    # ------------------------------------------------------------------
    # Forecasting
    # ------------------------------------------------------------------
    def updateForecast(self, hoursAhead=48):
        # Public entry point, safe to call from a background task so the
        # forecast is warm before policy evaluation reads cheapWindowActive.
        if not self.status:
            return
        self._updateForecast(hoursAhead)

    def _updateForecast(self, hoursAhead=48):
        # Fetch and cache the forward price series from the Amber API, then
        # recompute the derived attributes (min/max price and cheap window).
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
        self._recomputeDerived()

    def _recomputeDerived(self):
        # Derive min/max price across the horizon and whether we are currently
        # inside the cheapest contiguous window. All attributes are plain
        # numbers so the policy engine can read them directly.
        self.forecastMinPrice, self.forecastMaxPrice = priceRange(
            self.priceForecast
        ) or (0, 0)

        best = None
        if self.cheapWindowHours and self.cheapWindowHours > 0:
            best = self.getCheapestWindow(
                self.cheapWindowHours,
                self.cheapWindowStartHour,
                self.cheapWindowEndHour,
            )
        self.cheapWindowStart = best["startTimestamp"] if best else None
        self.cheapWindowActive = windowActive(
            self.cheapWindowStart, self.cheapWindowHours, datetime.now(timezone.utc)
        )

    def getPriceForecast(self, hoursAhead=24):
        # Return a JSON-serialisable copy of the forecast for the API/UI.
        if not self.status:
            return []
        self._updateForecast(max(hoursAhead, self.cheapWindowHours or 0))
        return serialise(self.priceForecast, hoursAhead)

    def getCheapestWindow(self, numHours, startHour=None, endHour=None):
        # Find the cheapest contiguous numHours window within an optional local
        # time-of-day range. Returns a dict (startHour, startTimestamp,
        # avgPrice, totalCost) or None.
        if not self.status:
            return None
        self._updateForecast(48)
        return cheapestWindow(self.priceForecast, numHours, startHour, endHour)
