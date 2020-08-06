class SolarLog:

    # SolarLog EMS Module
    # Fetches Consumption and Generation details from SolarLog

    import requests
    import time

    cacheTime = 10
    config = None
    configConfig = None
    configSolarLog = None
    consumedW = 0
    excludeConsumedW = 0
    debugLevel = 0
    fetchFailed = False
    generatedW = 0
    lastFetch = 0    
    master = None
    status = False
    serverIP = None
    excludeConsumptionInverters = {}
    timeout = 2

    def __init__(self, master):
        self.master = master
        self.config = master.config
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}
        try:
            self.configSolarLog = master.config["sources"]["SolarLog"]
        except KeyError:
            self.configSolarLog = {}
        self.status = self.configSolarLog.get("enabled", False)
        self.serverIP = self.configSolarLog.get("serverIP", None)
        self.excludeConsumptionInverters = self.configSolarLog.get("excludeConsumptionInverters", "").split(",")
        self.debugLevel = self.configConfig.get("debugLevel", 0)

        # Unload if this module is disabled or misconfigured
        if (not self.status) or (not self.serverIP):
            self.master.releaseModule("lib.TWCManager.EMS", "SolarLog")
            return None

    def debugLog(self, minlevel, message):
        if self.debugLevel >= minlevel:
            print("debugLog: (" + str(minlevel) + ") " + message)

    def getConsumption(self):

        if not self.status:
            self.debugLog(10, "SolarLog EMS Module Disabled. Skipping getConsumption")
            return 0

        # Perform updates if necessary
        self.update()

        # Return consumption value
        return self.consumedW - self.excludeConsumedW

    def getGeneration(self):

        if not self.status:
            self.debugLog(10, "SolarLog EMS Module Disabled. Skipping getGeneration")
            return 0

        # Perform updates if necessary
        self.update()

        # Return generation value
        return self.generatedW

    def getConsumptionAndGenerationValues(self):
        url = (
            "http://" + self.serverIP + "/getjp"
        )
        headers = {
            "content-type": "application/json",
        }
        payload = "{\"801\":{\"170\":null}}"

        # Update fetchFailed boolean to False before fetch attempt
        # This will change to true if the fetch failed, ensuring we don't then use the value to update our cache
        self.fetchFailed = False

        try:
            self.debugLog(10, "Fetching SolarLog EMS sensor values")
            httpResponse = self.requests.post(url, data=payload, headers=headers, timeout=self.timeout)
        except self.requests.exceptions.ConnectionError as e:
            self.debugLog(
                4, "Error connecting to SolarLog to fetching sensor values"
            )
            self.debugLog(10, str(e))
            self.fetchFailed = True
            return False
        except self.requests.exceptions.ReadTimeout as e:
            self.debugLog(
                4, "Read Timeout occurred fetching SolarLog sensor values"
            )
            self.debugLog(10, str(e))
            self.fetchFailed = True
            return False

        jsonResponse = (
            httpResponse.json()
            if httpResponse and httpResponse.status_code == 200
            else None
        )

        if jsonResponse:
            self.consumedW = float(jsonResponse["801"]["170"]["110"])
            self.generatedW = float(jsonResponse["801"]["170"]["101"])

    def getInverterValues(self):
        if (len(self.excludeConsumptionInverters)==0):
            self.excludeConsumedW = 0
            return False

        url = (
            "http://" + self.serverIP + "/getjp"
        )
        headers = {
            "content-type": "application/json",
        }
        payload = "{\"782\":null}"

        # Update fetchFailed boolean to False before fetch attempt
        # This will change to true if the fetch failed, ensuring we don't then use the value to update our cache
        self.fetchFailed = False

        try:
            self.debugLog(10, "Fetching SolarLog EMS inverter values")
            httpResponse = self.requests.post(url, data=payload, headers=headers, timeout=self.timeout)
        except self.requests.exceptions.ConnectionError as e:
            self.debugLog(
                4, "Error connecting to SolarLog to fetching inverter values"
            )
            self.debugLog(10, str(e))
            self.fetchFailed = True
            return False
        except self.requests.exceptions.ReadTimeout as e:
            self.debugLog(
                4, "Read Timeout occurred fetching SolarLog inverter values"
            )
            self.debugLog(10, str(e))
            self.fetchFailed = True
            return False

        jsonResponse = (
            httpResponse.json()
            if httpResponse and httpResponse.status_code == 200
            else None
        )
        if jsonResponse:
            tmpValue = 0
            for inverterIndex in self.excludeConsumptionInverters:
                tmpValue = tmpValue + float(jsonResponse["782"][str(inverterIndex)])
            self.excludeConsumedW = tmpValue            

    def setCacheTime(self, cacheTime):
        self.cacheTime = cacheTime

    def setTimeout(self, timeout):
        self.timeout = timeout

    def update(self):
        # Update function - determine if an update is required

        if (int(self.time.time()) - self.lastFetch) > self.cacheTime:
            # Cache has expired. Fetch values from HomeAssistant sensor.
            self.getConsumptionAndGenerationValues()
            
            if self.fetchFailed is not True:
                self.getInverterValues()

            # Update last fetch time
            if self.fetchFailed is not True:
                self.lastFetch = int(self.time.time())

            return True
        else:
            # Cache time has not elapsed since last fetch, serve from cache.
            return False