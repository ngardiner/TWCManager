class aWATTarPricing:

    import requests
    import time

    # aWATTar asks us to limit queries to once every 15 minutes
    cacheTime = 900
    capabilities = {
        "AdvancePricing": True
    }
    config = None
    configConfig = None
    configAwattar = None
    exportPrice = 0
    fetchFailed = False
    importPrice = 0
    lastFetch = 0
    status = False
    timeout = 10

    def __init__(self, master):

        self.master = master
        self.config = master.config
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}

        try:
            self.configAwattar = master.config["pricing"]["aWATTar"]
        except KeyError:
            self.configAwattar = {}

        self.status = self.configAwattar.get("enabled", self.status)
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
                "$aWATTar",
                "aWATTar Pricing Module Disabled. Skipping getExportPrice",
            )
            return 0

        # Perform updates if necessary
        self.update()

        # Return current export price
        return float(self.exportPrice)

    def getImportPrice(self):

        if not self.status:
            self.master.debugLog(
                10,
                "$aWATTar",
                "aWATTar Pricing Module Disabled. Skipping getImportPrice",
            )
            return 0

        # Perform updates if necessary
        self.update()

        # Return current import price
        return float(self.importPrice)

    def update(self):

        # Fetch the current pricing data from the Awattar API
        self.fetchFailed = False

        if (int(self.time.time()) - self.lastFetch) > self.cacheTime:
            # Cache has expired. Fetch values from API.

            url = "https://api.awattar.at/v1/marketdata"

            try:
                r = self.requests.get(url, timeout=self.timeout)
            except self.requests.exceptions.ConnectionError as e:
                self.master.debugLog(
                    4,
                    "$aWATTar",
                    "Error connecting to aWATTar API to fetch market pricing",
                )
                self.master.debugLog(10, "$aWATTar", str(e))
                self.fetchFailed = True
                return False

            try:
                r.raise_for_status()
            except self.requests.exceptions.HTTPError as e:
                self.master.debugLog(
                    4,
                    "$aWATTar",
                    "HTTP status "
                    + str(e.response.status_code)
                    + " connecting to aWATTar API to fetch market pricing",
                )

            if r.json():
                try:
                    self.importPrice = float(
                        r.json()["data"][0]["marketprice"]
                    )
                    if r.json()["data"][0]["unit"] == "Eur/MWh":
                        # Convert MWh price to KWh
                        self.importPrice = self.importPrice / 1000

                except (KeyError, TypeError) as e:
                    self.master.debugLog(
                        4,
                        "$aWATTar",
                        "Exception during parsing aWATTar pricing",
                    )


