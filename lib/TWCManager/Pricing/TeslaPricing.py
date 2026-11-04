# Electricity prices from the rate plan set in the Tesla app.
#
# The tariff comes from the TeslaPowerwall2 module's cached read, so this
# module makes no requests of its own. A price the tariff does not give is
# left absent rather than reported as 0, so a price policy does not match on
# a price nobody knows.
from datetime import datetime, timedelta, timezone
from TWCManager.EMS.TeslaPowerwall2 import siteZone, tariffPriceAt, touPeriodAt
from TWCManager.Pricing.Forecast import (
    cheapestWindow,
    latchedWindow,
    priceRange,
    serialise,
    windowActive,
)
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


class TeslaPricing:
    capabilities = {"AdvancePricing": True, "Forecasting": True}
    status = False

    def __init__(self, master):
        self.master = master
        self.configTesla = master.config.get("pricing", {}).get("Tesla", {})
        self.status = self.configTesla.get("enabled", False)
        self.warnedNoPowerwall = False
        cheapWindow = self.configTesla.get("cheapWindow", {})
        self.cheapWindowHours = cheapWindow.get("hours", 0)
        self.cheapWindowStartHour = cheapWindow.get("startHour", None)
        self.cheapWindowEndHour = cheapWindow.get("endHour", None)
        self._forecast = []
        self._forecastTariff = None
        self._forecastUntil = None
        self._cheapStart = None

        if not self.status:
            self.master.releaseModule("lib.TWCManager.Pricing", "TeslaPricing")
            return None

    def getCapabilities(self, capability):
        return self.capabilities.get(capability, False)

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

    @staticmethod
    def _known(price):
        # Absent, so policy's modules.* lookup falls back to an unresolved
        # reference instead of a number
        if price is None:
            raise AttributeError("price unknown")
        return price

    @property
    def currency(self):
        tariff, _, _ = self._siteTariff()
        code = tariff.get("currency") if tariff else None
        return SYMBOLS.get(code, code) if code else "$"

    # ------------------------------------------------------------------
    # Forecasting: the tariff is a fixed schedule, so the forecast is exact
    # and only needs rebuilding when the tariff changes or time moves on.
    # ------------------------------------------------------------------
    def _forecastNow(self):
        """(forecast, now, tz), rebuilding the forecast if it is stale."""
        tariff, now, tz = self._siteTariff()
        stale = self._forecastUntil is None or now >= self._forecastUntil
        if tariff is not self._forecastTariff or stale:
            self._forecast = tariffForecast(tariff, now, tz=tz) if tariff else []
            self._forecastTariff = tariff
            first = now.replace(minute=now.minute // 30 * 30, second=0, microsecond=0)
            self._forecastUntil = first + SLOT
        return self._forecast, now, tz

    def updateForecast(self, hoursAhead=48):
        self._forecastNow()

    def getPriceForecast(self, hoursAhead=24):
        return serialise(self._forecastNow()[0], hoursAhead)

    def getCheapestWindow(self, numHours, startHour=None, endHour=None):
        forecast, _, tz = self._forecastNow()
        return cheapestWindow(forecast, numHours, startHour, endHour, tz=tz)

    @property
    def forecastMinPrice(self):
        return self._known((priceRange(self._forecastNow()[0]) or (None,))[0])

    @property
    def forecastMaxPrice(self):
        return self._known((priceRange(self._forecastNow()[0]) or (None, None))[1])

    @property
    def cheapWindowStart(self):
        if not self.cheapWindowHours or self.cheapWindowHours <= 0:
            return None
        _, now, _ = self._siteTariff()
        self._cheapStart = latchedWindow(
            self._cheapStart,
            self.cheapWindowHours,
            now,
            lambda: self.getCheapestWindow(
                self.cheapWindowHours,
                self.cheapWindowStartHour,
                self.cheapWindowEndHour,
            ),
        )
        return self._cheapStart

    @property
    def cheapWindowActive(self):
        _, now, _ = self._siteTariff()
        return windowActive(self.cheapWindowStart, self.cheapWindowHours, now)
