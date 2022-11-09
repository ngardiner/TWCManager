import logging
import psycopg2
import paho.mqtt.client as mqtt
import _thread
import threading
import time

logger = logging.getLogger("\U0001F697 TeslaMate")


class TeslaMateVehicle:
    __db_host = None
    __db_name = None
    __db_pass = None
    __db_user = None
    __client = None
    __config = None
    __configConfig = None
    __configTeslaMate = None
    __master = None
    __mqtt_host = None
    __mqtt_user = None
    __mqtt_pass = None
    __mqtt_port = 1883
    __mqtt_prefix = None
    lastSync = 0
    status = None
    syncTokens = False
    vehicles = {}

    def __init__(self, master):
        self.__master = master

        self.__config = master.config
        try:
            self.__configConfig = self.__config["config"]
        except KeyError:
            self.__configConfig = {}
        try:
            self.__configTeslaMate = self.__config["vehicle"]["TeslaMate"]
            self.status = self.__config["vehicle"]["TeslaMate"]["enabled"]
        except KeyError:
            self.__configTeslaMate = {}

        # Unload if this module is disabled or misconfigured
        if not self.status:
            self.__master.releaseModule("lib.TWCManager.Vehicle", "TeslaMate")
            return None

        # Configure database parameters
        self.__db_host = self.__configTeslaMate.get("db_host", None)
        self.__db_name = self.__configTeslaMate.get("db_name", None)
        self.__db_pass = self.__configTeslaMate.get("db_pass", None)
        self.__db_user = self.__configTeslaMate.get("db_user", None)

        # Configure MQTT parameters
        self.__mqtt_host = self.__configTeslaMate.get("mqtt_host", None)
        self.__mqtt_user = self.__configTeslaMate.get("mqtt_user", None)
        self.__mqtt_pass = self.__configTeslaMate.get("mqtt_pass", None)
        self.__mqtt_prefix = self.__configTeslaMate.get("mqtt_prefix", None)

        self.syncTelemetry = self.__configTeslaMate.get("syncTelemetry", False)
        self.syncTokens = self.__configTeslaMate.get("syncTokens", False)

        # If we're set to sync the auth tokens from the database, do this at startup
        if self.syncTokens:
            self.doSyncTokens(True)

            # After initial sync, set a timer to continue to sync the tokens every hour
            resync = threading.Timer(3600, self.doSyncTokens)

        if self.syncTelemetry:
            # We delay collecting TeslaMate telemetry for a short period
            # This gives the TeslaAPI module time to connect to the Tesla API
            # and fetch vehicle information, allowing us to then merge our
            # TeslaMate and Tesla API information cleanly
            logger.log(
                logging.INFO4, "Telemetry information will be fetched in 30 seconds."
            )
            timer = threading.Timer(30, self.doMQTT)
            timer.start()

    def doMQTT(self):
        if hasattr(mqtt, "CallbackAPIVersion"):
            self.__client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, "TWCTeslaMate", protocol=mqtt.MQTTv5
            )
        else:
            self.__client = mqtt.Client("TWCTeslaMate")
        if self.__mqtt_user and self.__mqtt_pass:
            self.__client.username_pw_set(self.__mqtt_user, self.__mqtt_pass)
        self.__client.on_connect = self.mqttConnect
        self.__client.on_message = self.mqttMessage
        self.__client.on_subscribe = self.mqttSubscribe

        logger.log(logging.INFO4, "Attempting connection to MQTT Broker")

        try:
            self.__client.connect_async(
                self.__mqtt_host, port=self.__mqtt_port, keepalive=30
            )
        except ConnectionRefusedError as e:
            logger.log(logging.INFO4, "Error connecting to MQTT Broker")
            logger.debug(str(e))
            return False
        except OSError as e:
            logger.log(logging.INFO4, "Error connecting to MQTT Broker")
            logger.debug(str(e))
            return False

        self.__client.loop_start()

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
                carapi = self.__master.getModuleByName("TeslaAPI")
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
        logger.log(logging.INFO5, "Subscribe to " + self.__mqtt_prefix + "/cars/#")
        res = client.subscribe(self.__mqtt_prefix + "/cars/#", qos=0)
        logger.log(logging.INFO5, "Res: " + str(res))

    def mqttMessage(self, client, userdata, message):
        topic = str(message.topic).split("/")
        payload = str(message.payload.decode("utf-8"))

        if topic[0] == self.__mqtt_prefix and topic[1] == "cars":
            vehicle_id = topic[2]
            event_name = topic[3]

            events = {
                "battery_level": ["batteryLevel", lambda a: int(a), "lastChargeStatusTime"],
                "charge_limit_soc": ["chargeLimit", lambda a: int(a), "lastChargeStatusTime"],
                "latitude": ["syncLat", lambda a: float(a), None],
                "longitude": ["syncLon", lambda a: float(a), None],
                "state": ["syncState", lambda a: a, None],
                "time_to_full_charge": ["timeToFullCharge", lambda a: int(float(a)), "lastChargeStatusTime"],
                "charger_pilot_current": [ "availableCurrent", lambda a: int(a), "lastChargeStatusTime"],
                "charger_actual_current": [ "actualCurrent", lambda a: int(a), "lastChargeStatusTime"],
                "charger_phases": [ "phases", lambda a: int(a), "lastChargeStatusTime"],
                "charger_voltage": [ "voltage", lambda a: int(a), "lastChargeStatusTime"],
                "charging_state": [ "chargingState", lambda a: int(a), "lastChargeStatusTime"],
            }

            if event_name in events:
                if self.vehicles.get(vehicle_id, None):
                    property_name = events[event_name][0]
                    converter = events[event_name][1]
                    status_property = events[event_name][2]
                    
                    self.vehicles[vehicle_id].setattr(property_name, converter(payload))
                    if status_property:
                        self.vehicles[vehicle_id].setattr(status_property, time.time())
                    self.vehicles[vehicle_id].syncTimestamp = time.time()

            elif event_name == "display_name":
                # We can map the car ID in TeslaMate to the vehicle
                # in the Tesla API module
                self.updateVehicles(vehicle_id, payload)

            else:
                pass

    def mqttSubscribe(self, client, userdata, mid, reason_codes, properties=None):
        logger.info("Subscribe operation completed with mid " + str(mid))

    def updateVehicles(self, vehicle_id, vehicle_name):
        # Called by mqttMessage each time we get the display_name topic
        # We check to see if this aligns with a vehicle we know of from the API

        if self.vehicles.get(vehicle_id, None):
            # We already have this vehicle mapped
            pass
        else:
            for apiVehicle in self.__master.getModuleByName(
                "TeslaAPI"
            ).getCarApiVehicles():
                if apiVehicle.name == vehicle_name:
                    # Found a match
                    self.vehicles[vehicle_id] = apiVehicle
                    self.vehicles[vehicle_id].syncSource = "TeslaMateVehicle"

                    logger.info(
                        "Vehicle "
                        + vehicle_name
                        + " telemetry being provided by TeslaMate"
                    )

    def updateSettings(self):
        return True
