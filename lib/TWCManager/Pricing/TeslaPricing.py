# Electricity prices from the rate plan set in the Tesla app.
#
# The tariff comes from the TeslaPowerwall2 module's cached read, so this
# module makes no requests of its own. A price the tariff does not give is
# left absent rather than reported as 0, so a price policy does not match on
# a price nobody knows.
from datetime import datetime, timedelta, timezone
from TWCManager.EMS.TeslaPowerwall2 import siteZone, tariffPriceAt, touPeriodAt
from TWCManager.Pricing.Forecast import ForecastingPricing
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("Tesla", "Pricing")

SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£"}
SLOT = timedelta(minutes=30)


def tariffForecast(tariff, now, hours=48, tz=None):
    """Half-hour slots of the tariff's import price from the slot holding now,
    stepped in UTC so a DST change neither skips nor repeats a slot, and
    priced by the wall clock in tz (None: the host's, DST included). Slots
    the tariff does not price are left out."""
    first = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
    start = first.astimezone(timezone.utc)
    forecast = []
    for i in range(int(hours * 2)):
        local = (start + i * SLOT).astimezone(tz)
        price = tariffPriceAt(tariff, local)
        if price is not None:
            forecast.append(
                {
                    "timestamp": local,
                    "importPrice": price,
                    "period": touPeriodAt(tariff, local),
                }
            )
    return forecast


class TeslaPricing(ForecastingPricing):
    def __init__(self, master):
        super().__init__(master)
        self.warnedNoPowerwall = False
        self._forecastRows = []
        self._forecastTariff = None
        self._forecastUntil = None

    def _siteTariff(self):
        """(tariff or None, now (aware), the site's zone or None for the
        host's clock)."""
        # Looked up on each call: module load order is not guaranteed
        pw = self.master.getModuleByName("TeslaPowerwall2")
        tariff, zone = pw.getTariff() if pw is not None else (None, None)
        if pw is None and not self.warnedNoPowerwall:
            logger.info("Tesla pricing needs the Powerwall source; no prices yet.")
            self.warnedNoPowerwall = True
        tz = siteZone(zone)
        now = datetime.now(tz) if tz else datetime.now().astimezone()
        return (tariff if isinstance(tariff, dict) else None), now, tz

    def update(self):
        # Keeps the Powerwall module's tariff cache warm
        self._siteTariff()

    def getImportPrice(self):
        if not self.status:
            return None
        tariff, now, _ = self._siteTariff()
        return tariffPriceAt(tariff, now) if tariff else None

    def getExportPrice(self):
        if not self.status:
            return None
        tariff, now, _ = self._siteTariff()
        sell = tariff.get("sell_tariff") if tariff else None
        return tariffPriceAt(sell, now) if isinstance(sell, dict) else None

    @property
    def importPrice(self):
        return self._known(self.getImportPrice())

    @property
    def exportPrice(self):
        return self._known(self.getExportPrice())

    @property
    def currency(self):
        tariff, _, _ = self._siteTariff()
        code = tariff.get("currency") if tariff else None
        return SYMBOLS.get(code, code) if code else "$"

    # ------------------------------------------------------------------
    # Forecasting: the tariff is a fixed schedule, so the forecast is exact
    # and only needs rebuilding when the tariff changes or time moves on.
    # ------------------------------------------------------------------
    def _forecast(self):
        tariff, now, tz = self._siteTariff()
        stale = self._forecastUntil is None or now >= self._forecastUntil
        if tariff is not self._forecastTariff or stale:
            self._forecastRows = tariffForecast(tariff, now, tz=tz) if tariff else []
            self._forecastTariff = tariff
            first = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
            self._forecastUntil = first + SLOT
        return self._forecastRows, now, tz

    def updateForecast(self, hoursAhead=48):
        self._forecast()
