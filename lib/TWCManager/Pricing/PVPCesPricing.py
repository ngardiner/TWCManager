from datetime import datetime
from datetime import timedelta

class PVPCesPricing:

    import requests
    import time

    # https://www.esios.ree.es/es/pvpc publishes at 20:30CET eveyday the prices for next day
    # There is no limitation to fetch prices as it's updated onces a day
    cacheTime = 1
    capabilities = {
        "AdvancePricing": True
    }
    config = None
    configConfig = None
    configPvpc = None
    exportPrice = 0
    fetchFailed = False
    importPrice = 0
    lastFetch = 0
    status = False
    timeout = 10
    headers = {}
    todayImportPrice = {}

    def __init__(self, master):

        self.master = master
        self.config = master.config
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}

        try:
            self.configPvpc = master.config["pricing"]["PVPCes"]
        except KeyError:
            self.configPvpc = {}

        self.status = self.configPvpc.get("enabled", self.status)
        self.debugLevel = self.configConfig.get("debugLevel", 0)

        token=self.configPvpc.get("token")
        if self.status:
            self.headers = {
                'Accept': 'application/json; application/vnd.esios-api-v1+json',
                'Content-Type': 'application/json',
                'Host': 'api.esios.ree.es',
                'Cookie': '',
            }
            self.headers['Authorization']="Token token="+token

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
                "$PVPCes",
                "PVPCes	Pricing Module Disabled. Skipping getExportPrice",
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
                "$PVPCes",
                "PVPCes Pricing Module Disabled. Skipping getImportPrice",
            )
            return 0

        # Perform updates if necessary
        self.update()



        # Return current import price
        return float(self.importPrice)

    def update(self):

        # Fetch the current pricing data from the https://www.esios.ree.es/es/pvpc API
        self.fetchFailed = False
        now=datetime.now()
        tomorrow=datetime.now() + timedelta(days=1)
        if self.lastFetch == 0 or (now.hour < self.lastFetch.hour):
            # Cache not  feched or was feched yesterday. Fetch values from API.
            ini=str(now.year)+"-"+str(now.month)+"-"+str(now.day)+"T"+"00:00:00"
            end=str(tomorrow.year)+"-"+str(tomorrow.month)+"-"+str(tomorrow.day)+"T"+"23:00:00"

            url = "https://api.esios.ree.es/indicators/1014?start_date="+ini+"&end_date="+end

            try:
                r = self.requests.get(url,headers=self.headers, timeout=self.timeout)
            except self.requests.exceptions.ConnectionError as e:
                self.master.debugLog(
                    4,
                    "$PVPCes",
                    "Error connecting to PVPCes API to fetch market pricing",
                )
                self.fetchFailed = True
                return False

            self.lastFetch= now 

            try:
                r.raise_for_status()
            except self.requests.exceptions.HTTPError as e:
                self.master.debugLog(
                    4,
                    "$PVPCes",
                    "HTTP status "
                    + str(e.response.status_code)
                    + " connecting to PVPCes API to fetch market pricing",
                )
                return False

            if r.json():
                self.todayImportPrice=r.json()

        if self.todayImportPrice:
            try:
               self.importPrice = float(
                    self.todayImportPrice['indicator']['values'][now.hour]['value']
               )
               # Convert MWh price to KWh
               self.importPrice = round(self.importPrice / 1000,5)

            except (KeyError, TypeError) as e:
               self.master.debugLog(
                    4,
                    "$PVPCes",
                    "Exception during parsing PVPCes pricing",
               )

    def getCheapestStartHour(self,numHours,ini,end):
        # Perform updates if necessary
        self.update()

        minPriceHstart=ini
        if self.todayImportPrice:
            try:
               if end < ini:
               # If the scheduled hours are bettween days we consider hours going from 0 to 47
               # tomorrow 1am will be 25
                  end = 24 + end

               i=ini
               minPrice=999999999
               while i<=(end-numHours):
                   j=0
                   priceH=0
                   while j<numHours:
                       price =  float(self.todayImportPrice['indicator']['values'][i+j]['value'])

                       priceH = priceH + price

                       j=j+1
                   if  priceH<minPrice:
                       minPrice=priceH
                       minPriceHstart=i
                   i=i+1

                
            except (KeyError, TypeError) as e:
               self.master.debugLog(
                    4,
                    "$PVPCes",
                    "Exception during cheaper pricing analice",
               )

            if minPriceHstart > 23:
               minPriceHstart = minPriceHstart - 24
        
        return minPriceHstart
