from datetime import datetime
from datetime import timedelta
import time

class PVPCesPricing:

    import requests
    import time

    # https://www.esios.ree.es/es/pvpc publishes at 20:30CET eveyday the prices for next day
    # There is no limitation to fetch prices as it's updated onces a day
    cacheTime = 1
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
    weekImportPrice = {}

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

    def getWeekImportPrice(self):

        if not self.status:
            self.master.debugLog(
                10,
                "$PVPCes",
                "PVPCes Pricing Module Disabled. Skipping getWeekImportPrice",
            )
            return 0

        # Perform updates if necessary
        self.update()

        # Return current import price
        return self.weekImportPrice


    def update(self):
        # Fetch the current pricing data from the https://www.esios.ree.es/es/pvpc API
        self.fetchFailed = False
        days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        now=datetime.now()
        lastweek=datetime.now() - timedelta(days=6)
        tomorrow=datetime.now() + timedelta(days=1)
        if self.lastFetch == 0 or (now.hour != self.lastFetch.hour):
            # Cache not  feched or was feched last hour, fetch values from API.
            # we are going to fetch a week + tomorrow
            ini=str(lastweek.year)+"-"+str(lastweek.month)+"-"+str(lastweek.day)+"T"+"00:00:00"
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

            if r.json() and len(r.json()['indicator']['values']) >= 24*7:
              #Update settings with the new prices info for week
              self.weekImportPrice = {}
              ltNow = time.localtime()
              if ltNow.tm_wday < 5:
                 i=ltNow.tm_wday+2 
              elif ltNow.tm_wday == 5:
                 i=1 
              else:
                 i=0 

              try:
                for day in range(0,8):
                   sufix = ""
                   if day > 6 and len(r.json()['indicator']['values'])>7*24:
                      #This is tomorrow we add the "next" sufix to the day name 
                      sufix = "next"
                   elif day > 6:
                      break
                   if (self.weekImportPrice.get(sufix+days[i], None) == None):
                      self.weekImportPrice[sufix+days[i]] = {}
                    
                   for hour in range(0,24):
                      self.weekImportPrice[sufix+days[i]][str(hour)]= round(r.json()['indicator']['values'][day*24+hour]['value']/1000,5)
                   if i < 6:
                      i=i+1
                   else:
                      i=0

                self.importPrice = float(
                       r.json()['indicator']['values'][6*24+now.hour]['value']
                  )
                # Convert MWh price to KWh
                self.importPrice = round(self.importPrice / 1000,5)

              except Exception as e:
                self.master.debugLog(4,"$PVPCes","Exception updating todays prices: "+str(e))
            else:
              self.master.debugLog(4,"$PVPCes","Not enought info fetched")


    def getCheapestStartHour(self,numHours,ini,end):
        # Perform updates if necessary
        self.master.debugLog(10,"PVPC","getCheapestStartHour: "+str(numHours)+" "+str(ini)+" "+str(end))
        self.update()
        days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]
        now=datetime.now()
        ltNow = time.localtime()
        if ltNow.tm_wday < 6:
           today=days[ltNow.tm_wday+1]
           if ltNow.tm_wday < 5:
              tomorrow=days[ltNow.tm_wday+2]
           else:
              tomorrow=days[2]
        else:
           today=days[0]
           tomorrow=days[1]

        
        minPriceHstart=ini
        ini = int(ini)
        end = int(end)

        if len(self.weekImportPrice[today])>23:
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
                       indice = int(i+j)
                       if indice < 24:
                          price =  float(self.weekImportPrice[today][str(indice)])
                       elif len(self.weekImportPrice[tomorrow])>23:
                          price =  float(self.weekImportPrice[tomorrow][str(indice-24)])
                       else:
                          self.master.debugLog(10,"$PVPCes", "There is not enough price info")
                          return ini
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
                    "Exception during cheaper pricing analice: "+str(e),
               )
               minPriceHstart  = ini

            if minPriceHstart > 23:
               minPriceHstart = minPriceHstart - 24
        
        return minPriceHstart

    def getPricingInAdvanceAvailable(self):
        return True

