from datetime import datetime
from datetime import timedelta


class AmberPricing:

    import requests
    import time

    capabilities = {
        "AdvancePricing": True,
        "SpikeDetection": True,
        "Renewables": True,
        "Forecasting": True,
    }
    config = None
    configConfig = None
    configAmber = None
    exportPrice = 0
    fetchFailed = False
    importPrice = 0
    lastFetch = 0
    lastForecastFetch = 0
    status = False
    timeout = 10
    siteId = None
    spikeStatus = "none"
    priceDescriptor = "neutral"
    renewables = 0
    cacheTime = 300
    forecastCacheTime = 900
    priceForecast = []

    def __init__(self, master):

        self.master = master
        self.config = master.config
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}

        try:
            self.configAmber = master.config["pricing"]["Amber"]
        except KeyError:
            self.configAmber = {}

        self.status = self.configAmber.get("enabled", self.status)
        self.debugLevel = self.configConfig.get("debugLevel", 0)
        self.siteId = self.configAmber.get("siteId", None)
        self.cacheTime = self.configAmber.get("cacheTime", self.cacheTime)

        if not self.status:
            self.master.releaseModule("lib.TWCManager.Pricing", self.__class__.__name__)
            return None

        if "token" not in self.configAmber:
            self.master.debugLog(
                1,
                "$Amber",
                "Amber API token not configured. Module will be disabled.",
            )
            self.master.releaseModule("lib.TWCManager.Pricing", self.__class__.__name__)
            self.status = False
            return None

    def getCapabilities(self, capability):
        return self.capabilities.get(capability, False)

    def getExportPrice(self):

        if not self.status:
            self.master.debugLog(
                10,
                "$Amber",
                "Amber Pricing Module Disabled. Skipping getExportPrice",
            )
            return 0

        self.update()

        return float(self.exportPrice)

    def getImportPrice(self):

        if not self.status:
            self.master.debugLog(
                10,
                "$Amber",
                "Amber Pricing Module Disabled. Skipping getImportPrice",
            )
            return 0

        self.update()

        return float(self.importPrice)

    def getSpikeStatus(self):
        if not self.status:
            return "none"
        self.update()
        return self.spikeStatus

    def getPriceDescriptor(self):
        if not self.status:
            return "neutral"
        self.update()
        return self.priceDescriptor

    def getRenewables(self):
        if not self.status:
            return 0
        self.update()
        return self.renewables

    def discoverSiteId(self):
        url = "https://api.amber.com.au/v1/sites"
        headers = {"Authorization": f"Bearer {self.configAmber['token']}"}

        try:
            r = self.requests.get(url, headers=headers, timeout=self.timeout)
            r.raise_for_status()
            sites = r.json()

            if sites and len(sites) > 0:
                for site in sites:
                    if site.get("status") == "active":
                        self.siteId = site.get("id")
                        self.master.debugLog(
                            6,
                            "$Amber",
                            f"Auto-discovered siteId: {self.siteId}",
                        )
                        return True

                if sites[0].get("status") in ["pending", "closed"]:
                    self.master.debugLog(
                        4,
                        "$Amber",
                        f"Site found but status is {sites[0].get('status')}",
                    )

            self.master.debugLog(4, "$Amber", "No active sites found")
            return False

        except Exception as e:
            self.master.debugLog(4, "$Amber", f"Error discovering siteId: {e}")
            return False

    def update(self):

        self.fetchFailed = False

        if (int(self.time.time()) - self.lastFetch) <= self.cacheTime:
            return

        if not self.siteId:
            if not self.discoverSiteId():
                self.fetchFailed = True
                return

        url = f"https://api.amber.com.au/v1/sites/{self.siteId}/prices/current?resolution=30"
        headers = {"Authorization": f"Bearer {self.configAmber['token']}"}

        try:
            r = self.requests.get(url, headers=headers, timeout=self.timeout)
        except self.requests.exceptions.ConnectionError as e:
            self.master.debugLog(
                4, "$Amber", "Error connecting to Amber API to fetch pricing"
            )
            self.master.debugLog(10, "$Amber", str(e))
            self.fetchFailed = True
            return

        self.lastFetch = int(self.time.time())

        try:
            r.raise_for_status()
        except self.requests.exceptions.HTTPError as e:
            self.master.debugLog(
                4,
                "$Amber",
                f"HTTP status {e.response.status_code} from Amber API",
            )
            self.fetchFailed = True
            return

        try:
            intervals = r.json()
            if not intervals:
                self.master.debugLog(4, "$Amber", "No price data returned")
                return

            now_utc = datetime.utcnow()

            for interval in intervals:
                channel_type = interval.get("channelType")
                start_time = datetime.fromisoformat(
                    interval.get("startTime").replace("Z", "+00:00")
                ).replace(tzinfo=None)
                end_time = datetime.fromisoformat(
                    interval.get("endTime").replace("Z", "+00:00")
                ).replace(tzinfo=None)

                if start_time <= now_utc < end_time:
                    per_kwh = interval.get("perKwh", 0)
                    price_aud_kwh = per_kwh / 100.0

                    if channel_type == "general":
                        self.importPrice = price_aud_kwh
                        self.spikeStatus = interval.get("spikeStatus", "none")
                        self.priceDescriptor = interval.get("descriptor", "neutral")
                        self.renewables = interval.get("renewables", 0)

                        if self.spikeStatus == "spike":
                            self.master.debugLog(
                                5,
                                "$Amber",
                                f"PRICE SPIKE: {per_kwh} c/kWh - {self.priceDescriptor}",
                            )

                    elif channel_type == "feedIn":
                        self.exportPrice = abs(price_aud_kwh)

            self.master.debugLog(
                8,
                "$Amber",
                f"Prices - Import: {self.importPrice:.4f} AUD/kWh, "
                f"Export: {self.exportPrice:.4f} AUD/kWh, "
                f"Renewables: {self.renewables}%, "
                f"Status: {self.spikeStatus}",
            )

        except (KeyError, TypeError, ValueError) as e:
            self.master.debugLog(4, "$Amber", f"Exception parsing Amber pricing: {e}")
            self.master.debugLog(10, "$Amber", str(r.text[:500] if r.text else "No response"))
            self.fetchFailed = True

    def getPriceForecast(self, hoursAhead=24):
        """
        Returns a list of price forecasts for the next N hours.
        
        Each entry is a dict with:
          - timestamp: datetime object for the interval start
          - importPrice: price in $/kWh
          - exportPrice: price in $/kWh
          - spikeStatus: "none", "potential", or "spike"
          - descriptor: human-readable price level
          - renewables: percentage of renewables in grid
        
        Returns empty list if forecast unavailable or module disabled.
        """
        if not self.status:
            return []

        self._updateForecast(hoursAhead)

        return self.priceForecast[: hoursAhead * 2]

    def getCheapestWindow(self, numHours, startHour=None, endHour=None):
        """
        Find the cheapest contiguous window of numHours within a time range.
        
        Args:
            numHours: Number of consecutive hours needed (e.g., 5 hours of charging)
            startHour: Start of allowed window (0-23). If None, uses current hour.
            endHour: End of allowed window (0-23). If None, searches next 24 hours.
                     If endHour < startHour, window crosses midnight.
        
        Returns:
            dict with:
              - startHour: Best hour to start (0-23)
              - avgPrice: Average price for the window ($/kWh)
              - totalCost: Estimated total cost
            or None if no suitable window found.
        """
        if not self.status:
            return None

        self._updateForecast(48)

        if not self.priceForecast:
            return None

        now = datetime.now()
        if startHour is None:
            startHour = now.hour

        if numHours <= 0 or numHours > 24:
            return None

        window_slots = numHours * 2

        candidates = []
        for i, entry in enumerate(self.priceForecast):
            entry_hour = entry["timestamp"].hour

            if endHour is not None:
                if startHour <= endHour:
                    if not (startHour <= entry_hour < endHour):
                        continue
                else:
                    if not (entry_hour >= startHour or entry_hour < endHour):
                        continue
            else:
                if entry_hour < startHour:
                    continue

            if i + window_slots > len(self.priceForecast):
                break

            window = self.priceForecast[i : i + window_slots]
            if len(window) < window_slots:
                continue

            avg_price = sum(e["importPrice"] for e in window) / len(window)
            candidates.append(
                {
                    "startHour": entry_hour,
                    "startTimestamp": entry["timestamp"],
                    "avgPrice": avg_price,
                    "totalCost": avg_price * numHours,
                    "window": window,
                }
            )

        if not candidates:
            return None

        best = min(candidates, key=lambda x: x["avgPrice"])

        return {
            "startHour": best["startHour"],
            "startTimestamp": best["startTimestamp"],
            "avgPrice": best["avgPrice"],
            "totalCost": best["totalCost"],
        }

    def _updateForecast(self, hoursAhead=24):
        """Fetch and cache forecast data from Amber API."""

        if (int(self.time.time()) - self.lastForecastFetch) <= self.forecastCacheTime:
            if self.priceForecast:
                return

        if not self.siteId:
            if not self.discoverSiteId():
                return

        url = f"https://api.amber.com.au/v1/sites/{self.siteId}/prices/current?next={hoursAhead * 2}&resolution=30"
        headers = {"Authorization": f"Bearer {self.configAmber['token']}"}

        try:
            r = self.requests.get(url, headers=headers, timeout=self.timeout)
        except self.requests.exceptions.ConnectionError as e:
            self.master.debugLog(
                4, "$Amber", "Error connecting to Amber API for forecast"
            )
            self.master.debugLog(10, "$Amber", str(e))
            return

        try:
            r.raise_for_status()
        except self.requests.exceptions.HTTPError as e:
            self.master.debugLog(
                4, "$Amber", f"HTTP status {e.response.status_code} from Amber forecast API"
            )
            return

        try:
            intervals = r.json()
            if not intervals:
                return

            now_utc = datetime.utcnow()
            forecasts_by_time = {}

            for interval in intervals:
                start_time = datetime.fromisoformat(
                    interval.get("startTime").replace("Z", "+00:00")
                ).replace(tzinfo=None)

                if start_time < now_utc:
                    continue

                channel_type = interval.get("channelType")
                per_kwh = interval.get("perKwh", 0)
                price_aud_kwh = per_kwh / 100.0

                time_key = start_time.isoformat()
                if time_key not in forecasts_by_time:
                    forecasts_by_time[time_key] = {
                        "timestamp": start_time,
                        "importPrice": 0,
                        "exportPrice": 0,
                        "spikeStatus": "none",
                        "descriptor": "neutral",
                        "renewables": 0,
                    }

                if channel_type == "general":
                    forecasts_by_time[time_key]["importPrice"] = price_aud_kwh
                    forecasts_by_time[time_key]["spikeStatus"] = interval.get(
                        "spikeStatus", "none"
                    )
                    forecasts_by_time[time_key]["descriptor"] = interval.get(
                        "descriptor", "neutral"
                    )
                    forecasts_by_time[time_key]["renewables"] = interval.get(
                        "renewables", 0
                    )

                elif channel_type == "feedIn":
                    forecasts_by_time[time_key]["exportPrice"] = abs(price_aud_kwh)

            self.priceForecast = sorted(
                forecasts_by_time.values(), key=lambda x: x["timestamp"]
            )
            self.lastForecastFetch = int(self.time.time())

            self.master.debugLog(
                8,
                "$Amber",
                f"Updated forecast: {len(self.priceForecast)} intervals",
            )

        except (KeyError, TypeError, ValueError) as e:
            self.master.debugLog(4, "$Amber", f"Exception parsing Amber forecast: {e}")
            self.master.debugLog(10, "$Amber", str(r.text[:500] if r.text else "No response"))
