# Fronius Datamanager Solar.API Integration (Inverter Web Interface)

class Fronius:

  import requests
  import time

  cacheTime   = 60
  consumedW   = 0
  debugLevel  = 0
  fetchFailed = False
  generatedW  = 0
  importW     = 0
  exportW     = 0
  lastFetch   = 0
  serverIP    = None
  serverPort  = 80
  status      = False
  timeout     = 10
  voltage     = 0

  def __init__(self, debugLevel, config):
    self.debugLevel  = debugLevel
    self.status      = config.get('enabled', False)
    self.serverIP    = config.get('serverIP', None)
    self.serverPort  = config.get('serverPort','80')

  def debugLog(self, minlevel, message):
    if (self.debugLevel >= minlevel):
      print("Fronius: (" + str(minlevel) + ") " + message)

  def getConsumption(self):

    if (not self.status):
      self.debugLog(10, "Fronius EMS Module Disabled. Skipping getConsumption")
      return 0
    
    # Perform updates if necessary
    self.update()

    # Return consumption value. Fronius consumption is either negative
    # (export to grid) or positive (import from grid). We add generation
    # value to make it the delta between this and current consumption
    if ((self.consumedW < 0) or (self.consumedW > 0)):
      return float(self.generatedW + self.consumedW)
    else:
      return float(0)

  def getGeneration(self):

    if (not self.status):
      self.debugLog(10, "Fronius EMS Module Disabled. Skipping getGeneration")
      return 0

    # Perform updates if necessary
    self.update()

    # Return generation value
    return float(self.generatedW)
    
  def getInverterData(self):
    url = "http://" + self.serverIP + ":" + self.serverPort
    url = url + "/solar_api/v1/GetInverterRealtimeData.cgi?Scope=Device&DeviceID=1&DataCollection=CommonInverterData"

    return self.getInverterValue(url)

  def getInverterValue(self, url):
    
    # Fetch the specified URL from the Fronius Inverter and return the data
    self.fetchFailed = False
    
    try:
        r = self.requests.get(url, timeout=self.timeout)
    except self.requests.exceptions.ConnectionError as e: 
        self.debugLog(4, "Error connecting to Fronius Inverter to fetch sensor value")
        self.debugLog(10, str(e))
        self.fetchFailed = True
        return False
      
    r.raise_for_status()
    jsondata = r.json()
    return jsondata

  def getMeterData(self):
    url = "http://" + self.serverIP + ":" + self.serverPort
    url = url + "/solar_api/v1/GetMeterRealtimeData.cgi?Scope=Device&DeviceId=0"

    return self.getInverterValue(url)

  def update(self):

    if ((int(self.time.time()) - self.lastFetch) > self.cacheTime):
      # Cache has expired. Fetch values from HomeAssistant sensor.

      inverterData = self.getInverterData()
      if (inverterData):
        self.generatedW = inverterData['Body']['Data']['PAC']['Value']
        self.voltage = inverterData['Body']['Data']['UAC']['Value']

      meterData = self.getMeterData()
      if (meterData):
        self.consumedW = float(meterData['Body']['Data']['PowerReal_P_Sum'])

      # Update last fetch time
      if (self.fetchFailed is not True):
        self.lastFetch = int(self.time.time())
        
      return True
    else:
      # Cache time has not elapsed since last fetch, serve from cache.
      return False
