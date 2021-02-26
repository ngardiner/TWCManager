# MySQLLogging module. Provides output to a MySQL Server for regular statistics
# recording.
from datetime import datetime, timedelta
import time

class MySQLLogging:

    config = None
    configConfig = None
    configLogging = None
    db = None
    slaveSession = {}
    status = False

    def __init__(self, master):
        self.master = master
        self.config = master.config
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}
        try:
            self.configLogging = master.config["logging"]["MySQL"]
        except KeyError:
            self.configLogging = {}
        self.status = self.configLogging.get("enabled", False)

        # Unload if this module is disabled or misconfigured
        if not self.status or not self.configLogging.get("host", None):
            self.master.releaseModule("lib.TWCManager.Logging", "MySQLLogging")
            return None

        # Initialize the mute config tree if it is not already
        if not self.configLogging.get("mute", None):
            self.configLogging["mute"] = {}

        # Import MySQL module if module is not released
        import pymysql

        try:
            self.db = pymysql.connect(
                self.configLogging.get("host", ""),
                self.configLogging.get("username", ""),
                self.configLogging.get("password", ""),
                self.configLogging.get("database", ""),
            )
        except pymysql.err.OperationalError as e:
            self.master.debugLog(1, "MySQLLog", "Error connecting to MySQL database")
            self.master.debugLog(1, "MySQLLog", str(e))

    def debugLog(self, logdata):
        # debugLog is something of a catch-all if we don't have a specific
        # logging function for the given data. It allows a log entry to be
        # passed to us for storage.
        return

    def greenEnergy(self, data):
        # Check if this status is muted
        if self.configLogging["mute"].get("GreenEnergy", 0):
            return None

        # Ensure database connection is alive, or reconnect if not
        self.db.ping(reconnect=True)

        query = """
            INSERT INTO green_energy (time, genW, conW, chgW) 
            VALUES (now(), %s, %s, %s)
        """

        cur = self.db.cursor()
        rows = 0
        try:
            rows = cur.execute(
                query,
                (
                    data.get("genWatts", 0),
                    data.get("conWatts", 0),
                    data.get("chgWatts", 0),
                ),
            )
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", "Error updating MySQL database")
            self.master.debugLog(1, "MySQLLog", str(e))
        if rows:
            # Query was successful. Commit
            self.db.commit()
        else:
            # Issue, log message and rollback
            self.master.debugLog(
                1, "MySQLLog", "Error updating MySQL database. Rows = %d" % rows
            )
            self.db.rollback()

        cur.close()
        
        whenToAcumulate = self.configLogging.get("whenToAcumulate", "false") 
        numberToAcumulate = self.configLogging.get("numberToAcumulate", 0) 
        if whenToAcumulate == "days":
           dateToAcumulate = datetime.now() - timedelta(days=numberToAcumulate)
           self.acumulateGreenEnergyDays(dateToAcumulate)
        elif whenToAcumulate == "hours":
           dateToAcumulate = datetime.now() - timedelta(hours=numberToAcumulate)
           self.acumulateGreenEnergyHours(dateToAcumulate)

        return

    def acumulateGreenEnergyDays(self,date):

        inic = date.strftime("%Y-%m-%dT00:00:00")
        endc = date.strftime("%Y-%m-%dT23:59:59")
        
        cur = self.db.cursor() 
        queryWh = """
            SELECT * from green_energy_wh where time>=%s and time<=%s order by time
        """
        query = """
            SELECT * from green_energy where time>=%s and time<=%s order by time
        """
        delete = """
            DELETE  from green_energy where time>=%s and time<=%s
        """
        insert = """
            INSERT into green_energy_wh values(%s,%s,%s,%s)
        """
        rows = 0
        try:
            rows = cur.execute(queryWh,(inic,endc,),)
            if rows > 23:
               return

            rows = cur.execute(query,(inic,endc,),)

            i = 1
            result = "" 
            if rows:
                self.master.debugLog(10, "MySQLLog", "Date: "+date.strftime("%Y-%m-%dT00:00:00")+" #Registers to acumulate: "+str(rows))
                result = cur.fetchall()

            while i<len(result):
               for j in range(0,24):
                  genWA = 0
                  conWA = 0
                  chgWA = 0
                  while i<len(result) and result[i][0].hour == j:
                    timeI = result[i-1][0]
                    timeE = result[i][0]
                    period = timeE-timeI
                    if period.seconds<60:
                       genW = result[i][1]
                       conW = result[i][2]
                       chgW = result[i][3]
                       genWA = genWA + genW * period.seconds/3600
                       conWA = conWA + conW * period.seconds/3600
                       chgWA = chgWA + chgW * period.seconds/3600
                    i=i+1
                  ini = date.strftime("%Y-%m-%dT"+str(j)+":00:00")         
                  en = date.strftime("%Y-%m-%dT"+str(j)+":59:59")         

                  if self.configLogging.get("purgeAcumulated", 0):
                     res = cur.execute(delete,(ini,en),)
                     if res:
                        self.db.commit()
                  res = cur.execute(insert,(ini,genWA,conWA,chgWA),)
                  if res:
                     self.db.commit()
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", str(e))

    def acumulateGreenEnergyHours(self,date):

        inic = date.strftime("%Y-%m-%dT%H:00:00")
        endc = date.strftime("%Y-%m-%dT%H:59:59")

        cur = self.db.cursor()
        queryWh = """
            SELECT * from green_energy_wh where time>=%s and time<=%s order by time
        """
        query = """
            SELECT * from green_energy where time>=%s and time<=%s order by time
        """
        delete = """
            DELETE  from green_energy where time>=%s and time<=%s
        """
        insert = """
            INSERT into green_energy_wh values(%s,%s,%s,%s)
        """
        rows = 0
        try:
            rows = cur.execute(queryWh,(inic,endc,),)
            if rows:
               return

            rows = cur.execute(query,(inic,endc,),)

            i = 1
            result = ""
            if rows:
                self.master.debugLog(10, "MySQLLog", "Hour: "+date.strftime("%Y-%m-%dT%H:00:00")+" #Registers to acumulate: "+str(rows))
                result = cur.fetchall()

            genWA = 0
            conWA = 0
            chgWA = 0
            while i<len(result):
              timeI = result[i-1][0]
              timeE = result[i][0]
              period = timeE-timeI
              if period.seconds<60:
                 genW = result[i][1]
                 conW = result[i][2]
                 chgW = result[i][3]
                 genWA = genWA + genW * period.seconds/3600
                 conWA = conWA + conW * period.seconds/3600
                 chgWA = chgWA + chgW * period.seconds/3600
              i=i+1

            if self.configLogging.get("purgeAcumulated", 0):
               res = cur.execute(delete,(inic,endc),)
               if res:
                  self.db.commit()
            res = cur.execute(insert,(inic,genWA,conWA,chgWA),)
            if res:
               self.db.commit()
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", str(e))


    def queryGreenEnergy(self, data):
        # Check if this status is muted
        if self.configLogging["mute"].get("GreenEnergy", 0):
            return None
        # Ensure database connection is alive, or reconnect if not
        self.db.ping(reconnect=True)

        query = """
            SELECT * from green_energy where time>%s and time<%s
        """
        cur = self.db.cursor()
        rows = 0
        try:
            rows = cur.execute(
                query,
                (
                    data.get("dateBegin", 0),
                    data.get("dateEnd", 0),
                ),
            )
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", str(e))

        result={}
        if rows:
            # Query was successful. Commit
            result = cur.fetchall()
        else:
            # Issue, log message
            self.master.debugLog(
                1, "MySQLLog", "Error query MySQL database. Rows = %d" % rows
            )
        cur.close()
        return list(result)

    def queryGreenEnergyWh(self, data):
        # Check if this status is muted or acumulation is not active
        if self.configLogging["mute"].get("GreenEnergy", 0) or self.configLogging.get("whenToAcumulate", 0)==0:
            return None
        # Ensure database connection is alive, or reconnect if not
        self.db.ping(reconnect=True)

        query = """
            SELECT * from green_energy_wh where time>%s and time<%s
        """
        cur = self.db.cursor()
        rows = 0
        try:
            rows = cur.execute(
                query,
                (
                    data.get("dateBegin", 0),
                    data.get("dateEnd", 0),
                ),
            )
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", str(e))

        result={}
        if rows:
            # Query was successful. Commit
            result = cur.fetchall()
        else:
            # Issue, log message
            self.master.debugLog(
                1, "MySQLLog", "Error query MySQL database. Rows = %d" % rows
            )
        cur.close()
        return list(result)

    def queryGreenEnergyWhDay(self, day,hour):
        # Check if this status is muted or acumulation is not active
        
        if self.configLogging["mute"].get("GreenEnergy", 0) or self.configLogging.get("whenToAcumulate", 0)==0:
            return 0
        daysNames = [ "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday" ]
        # Ensure database connection is alive, or reconnect if not
        self.db.ping(reconnect=True)

        query = """
            SELECT * from green_energy_wh where time=%s
        """
        cur = self.db.cursor()
        wDay=0
        for i in range(0,6):
           dayN=daysNames[i]
           if dayN == day:
              wDay = i
        ltNow = time.localtime()
        if ltNow.tm_wday >= wDay:
           delta = ltNow.tm_wday-wDay
        else:
           delta = 7-ltNow.tm_wday-wDay
 
        init = datetime.now() - timedelta(days=delta)      

        inic = init.strftime("%Y-%m-%dT"+str(hour)+":00:00")

        rows = 0
        try:
            rows = cur.execute(
                query,
                (
                    inic,
                ),
            )
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", str(e))

        result=0
        if rows:
            # Query was successful. Commit
            result = cur.fetchall()
            genWh = result[0][1]
            conWh = result[0][2]
            chgWh = result[0][3]
            result = conWh - genWh - chgWh

        cur.close()
        return result


    def queryEnergyNotAvailable(self, startHour,endHour):
        # Check if this status is muted
        if self.configLogging["mute"].get("GreenEnergy", 0) or self.configLogging.get("whenToAcumulate", 0)==0:
            return None
      
        end = datetime.now()  
        # Use the last 7 days average
        init = end - timedelta(days=7)

        inic = init.strftime("%Y-%m-%dT%H:00:00")
        ende = end.strftime("%Y-%m-%dT%H:00:00")

        result= self.queryGreenEnergyWh(
                    {
                         "dateBegin": inic,
                         "dateEnd": ende
                    }
                )

        energy = 0
        numReg = 0
        i = 0
        while i<len(result):
            withInPeriod = False
            if startHour < endHour:
               if startHour<=result[i][0].hour and result[i][0].hour<endHour:
                  withInPeriod = True
            elif startHour<=result[i][0].hour:
                  withInPeriod = True
            elif result[i][0].hour<endHour:
                  withInPeriod = True

            if withInPeriod:
               genW = result[i][1]
               conW = result[i][2]
               chgW = result[i][3]
               energy = energy + conW - chgW - genW
               numReg = numReg + 1

            i=i+1

        if numReg > 0:
           energy = energy / numReg
        else:
           self.master.debugLog(10, "MySQLLog", "Average Energy not available: "+str(int(energy)))
        
        return int(energy)

    def slavePower(self, data):
        # Check if this status is muted
        if self.configLogging["mute"].get("SlavePower", 0):
            return None

        # Not Yet Implemented
        return None

    def slaveStatus(self, data):
        # Check if this status is muted
        if self.configLogging["mute"].get("SlaveStatus", 0):
            return None

        # Ensure database connection is alive, or reconnect if not
        self.db.ping(reconnect=True)

        # Otherwise, add to database
        cursor = self.db.cursor()
        query = """
            INSERT INTO slave_status (slaveTWC, time, kWh, voltsPhaseA,
            voltsPhaseB, voltsPhaseC)
            VALUES (%s, now(), %s, %s, %s, %s);
        """
        rows = 0
        try:
            rows = cursor.execute(
                query,
                (
                    "%02X%02X" % (data["TWCID"][0], data["TWCID"][1]),
                    data["kWh"],
                    data["voltsPerPhase"][0],
                    data["voltsPerPhase"][1],
                    data["voltsPerPhase"][2],
                ),
            )
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", "Error updating MySQL database")
            self.master.debugLog(1, "MySQLLog", str(e))
        if rows:
            # Query was successful. Commit
            self.db.commit()
        else:
            # Issue, log message and rollback
            self.master.debugLog(
                1, "MySQLLog", "Error updating MySQL database. Rows = %d" % rows
            )
            self.db.rollback()
        cursor.close()

    def startChargeSession(self, data):
        # Check if this status is muted
        if self.configLogging["mute"].get("ChargeSessions", 0):
            return None

        # Called when a Charge Session Starts.
        twcid = "%02X%02X" % (data["TWCID"][0], data["TWCID"][0])
        self.slaveSession[twcid] = data.get("startTime", 0)
        query = """
            INSERT INTO charge_sessions (chargeid, startTime, startkWh, slaveTWC) 
            VALUES (%s,now(),%s,%s)
        """

        # Ensure database connection is alive, or reconnect if not
        self.db.ping(reconnect=True)

        cur = self.db.cursor()
        rows = 0
        try:
            rows = cur.execute(
                query, (data.get("startTime", 0), data.get("startkWh", 0), twcid)
            )
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", "Error updating MySQL database")
            self.master.debugLog(1, "MySQLLog", str(e))
        if rows:
            # Query was successful. Commit
            self.db.commit()
        else:
            # Issue, log message and rollback
            self.master.debugLog(
                1, "MySQLLog", "Error updating MySQL database. Rows = %d" % rows
            )
            self.db.rollback()
        cur.close()

    def stopChargeSession(self, data):
        # Check if this status is muted
        if self.configLogging["mute"].get("ChargeSessions", 0):
            return None

        # Called when a Charge Session Ends.
        twcid = "%02X%02X" % (data["TWCID"][0], data["TWCID"][0])
        chgid = self.slaveSession.get(twcid, 0)
        query = """
            UPDATE charge_sessions SET endTime = now(), endkWh = %s 
            WHERE chargeid = %s AND slaveTWC = %s
        """

        # Ensure database connection is alive, or reconnect if not
        self.db.ping(reconnect=True)

        cur = self.db.cursor()
        rows = 0
        try:
            rows = cur.execute(query, (data.get("endkWh", 0), chgid, twcid))
        except Exception as e:
            self.master.debugLog(1, "MySQLLog", "Error updating MySQL database")
            self.master.debugLog(1, "MySQLLog", str(e))
        if rows:
            # Query was successful. Commit
            self.db.commit()
        else:
            # Issue, log message and rollback
            self.master.debugLog(
                1, "MySQLLog", "Error updating MySQL database. Rows = %d" % rows
            )
            self.db.rollback()
        cur.close()
        self.slaveSession[twcid] = 0

    def updateChargeSession(self, data):
        # Check if this status is muted
        if self.configLogging["mute"].get("ChargeSessions", 0):
            return None

        # Called when additional information needs to be updated for a
        # charge session
        twcid = "%02X%02X" % (data["TWCID"][0], data["TWCID"][0])
        chgid = self.slaveSession.get(twcid, 0)
        if data.get("vehicleVIN", None):
            query = """
                UPDATE charge_sessions SET vehicleVIN = %s 
                WHERE chargeid = %s AND slaveTWC = %s
            """

            # Ensure database connection is alive, or reconnect if not
            self.db.ping(reconnect=True)

            cur = self.db.cursor()
            rows = 0
            try:
                rows = cur.execute(query, (data.get("vehicleVIN", ""), chgid, twcid))
            except Exception as e:
                self.master.debugLog(1, "MySQLLog", "Error updating MySQL database")
                self.master.debugLog(1, "MySQLLog", str(e))
            if rows:
                # Query was successful. Commit
                self.db.commit()
            else:
                # Issue, log message and rollback
                self.db.rollback()
            cur.close()
        return None

    def greenEnergyQueryAvailable(self):
        return True

