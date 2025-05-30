import logging
import psycopg2
import paho.mqtt.client as mqtt
import threading
import time

from TWCManager.Vehicle.Telemetry import TelmetryBase

logger = logging.getLogger("\U0001f697 TeslaMate")


class TeslaMateVehicle(TelmetryBase):
    __db_host = None
    __db_name = None
    __db_pass = None
    __db_user = None

    configName = "TeslaMate"
    syncTokens = False
    events = {
        "battery_level": ["batteryLevel", lambda a: int(a)],
        "charge_limit_soc": ["chargeLimit", lambda a: int(a)],
        "display_name": ["name", lambda a: str(a)],
        "latitude": ["syncLat", lambda a: float(a)],
        "longitude": ["syncLon", lambda a: float(a)],
        "state": ["syncState", lambda a: a],
        "time_to_full_charge": ["timeToFullCharge", lambda a: float(a)],
        "charger_pilot_current": ["availableCurrent", lambda a: int(a)],
        "charger_actual_current": ["actualCurrent", lambda a: int(a)],
        "charger_phases": ["phases", lambda a: int(a) if a else 0],
        "charger_voltage": ["voltage", lambda a: int(a)],
        "charging_state": ["chargingState", lambda a: str(a)],
    }

    def __init__(self, master):
        super().__init__(master)

        if not self.status:
            return None

        # Configure database parameters
        self.__db_host = self.configModule.get("db_host", None)
        self.__db_name = self.configModule.get("db_name", None)
        self.__db_pass = self.configModule.get("db_pass", None)
        self.__db_user = self.configModule.get("db_user", None)
        self.syncTokens = self.configModule.get("syncTokens", False)

        # If we're set to sync the auth tokens from the database, do this at startup
        if self.syncTokens:
            self.doSyncTokens(True)

            # After initial sync, set a timer to continue to sync the tokens every hour
            resync = threading.Timer(3600, self.doSyncTokens)

    def doSyncTokens(self, firstrun=False):
        # Connect to TeslaMate database and synchronize API tokens

        if self.__db_host and self.__db_name and self.__db_user and self.__db_pass:
            conn = None

            try:
                conn = psycopg2.connect(
                    host=self.__db_host,
                    database=self.__db_name,
                    user=self.__db_user,
                    password=self.__db_pass,
                )
            except psycopg2.OperationalError as e:
                logger.log(
                    logging.ERROR,
                    "Failed to connect to TeslaMate database: " + str(e),
                )

                if firstrun:
                    # If this is the first time we try to fetch, disable token sync.
                    # On subsequent fetches, we don't want to disable token sync as it could be a transient connectivity issue
                    self.syncTokens = False

            if conn:
                cur = conn.cursor()

                # Query DB for latest access and refresh token
                cur.execute(
                    "SELECT access, refresh FROM tokens ORDER BY id DESC LIMIT 1"
                )

                # Fetch result
                result = cur.fetchone()

                # Set Bearer and Refresh Tokens
                carapi = self.master.getModuleByName("TeslaAPI")
                # We don't want to refresh the token - let the source handle that.
                carapi.setCarApiTokenExpireTime(99999 * 99999 * 99999)
                carapi.setCarApiBearerToken(result[0])
                carapi.setCarApiRefreshToken(result[1])
                self.lastSync = time.time()

            else:
                logger.log(
                    logging.ERROR,
                    "Failed to connect to TeslaMate database. Disabling Token Sync",
                )

                # Connection failed. Turn off token sync
                if firstrun:
                    # If this is the first time we try to fetch, disable token sync.
                    # On subsequent fetches, we don't want to disable token sync as it could be a transient connectivity issue
                    self.syncTokens = False

        else:
            logger.log(
                logging.ERROR,
                "TeslaMate Database connection settings not specified. Disabling Token Sync",
            )

            # Required database details not provided. Turn off token sync
            self.syncTokens = False

    def mqttConnect(self, client, userdata, flags, rc, properties=None):
        logger.log(logging.INFO5, "MQTT Connected.")
        subscription = "teslamate/"
        if self.mqtt_prefix:
            subscription += self.mqtt_prefix + "/"
        subscription += "cars/+/"
        for topic in self.events.keys():
            topic = subscription + topic
            logger.log(logging.INFO5, "Subscribe to " + topic)
            res = client.subscribe(topic, qos=1)
            logger.log(logging.INFO5, "Res: " + str(res))

    def mqttMessage(self, client, userdata, message):
        topic = str(message.topic).split("/")
        payload = str(message.payload.decode("utf-8"))

        if len(topic) > 4:
            prefix = topic[1:-3].join("/")
            if prefix != self.mqtt_prefix:
                return
            topic = [topic[0]] + topic[-3:]

        if topic[0] == "teslamate" and topic[1] == "cars":
            self.applyDataToVehicle(topic[2], topic[3], payload)
