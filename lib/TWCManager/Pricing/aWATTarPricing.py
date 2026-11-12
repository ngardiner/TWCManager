# aWATTar spot-market electricity pricing (Austria / Germany).
#
# Reports the current wholesale import price from the aWATTar market-data API,
# whose day-ahead prices for the next 24 hours are also the forecast.
# aWATTar asks that the API be queried no more than once every 15 minutes.
from datetime import datetime, timezone
import requests
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Pricing.Forecast import ForecastingPricing

logger = LoggerFactory.get_logger("aWATTar", "Pricing")


class aWATTarPricing(ForecastingPricing):
    # aWATTar asks us to limit queries to once every 15 minutes
    cacheTime = 900
    currency = "EUR"
    exportPrice = 0
    fetchFailed = False
    importPrice = 0
    lastFetch = 0
    timeout = 10

    def __init__(self, master):
        super().__init__(master)
        self.priceForecast = []

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

    def _forecast(self):
        return self.priceForecast, datetime.now(timezone.utc), None

    def updateForecast(self, hoursAhead=48):
        # One request gives both the current price and the forecast
        self.update()

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
                forecast = []
                now = time.time() * 1000
                for slot in data["data"]:
                    if slot["end_timestamp"] <= now:
                        continue
                    price = float(slot["marketprice"])
                    if slot["unit"] == "Eur/MWh":
                        # Convert MWh price to kWh
                        price = price / 1000
                    forecast.append(
                        {
                            "timestamp": datetime.fromtimestamp(
                                slot["start_timestamp"] / 1000, timezone.utc
                            ),
                            "importPrice": price,
                        }
                    )
                    # The API does not document its slot length, so read it
                    if len(forecast) == 1:
                        length = slot["end_timestamp"] - slot["start_timestamp"]
                        self.slotMinutes = length // 60000
                self.priceForecast = forecast
                if forecast and forecast[0]["timestamp"].timestamp() * 1000 <= now:
                    self.importPrice = forecast[0]["importPrice"]
            except (KeyError, TypeError, IndexError, ValueError):
                logger.error("Exception during parsing aWATTar pricing")
