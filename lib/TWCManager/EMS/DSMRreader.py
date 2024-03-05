import logging
import json

logger = logging.getLogger("\U000026A1 DSMRread")


class DSMRreader:
    # DSMR-reader MQTT EMS Module
    # Subscribes to Consumption and Generation details from DSMR-reader MQTT Publisher

    import paho.mqtt.client as mqtt
    import time

    __config = None
    __configConfig = None
    __configDSMRreader = None
    __connectionState = 0
    consumedW = 0
    consumedA = 0
    generatedW = 0
    master = None
    status = False

    def __init__(self, master):
        self.master = master
        self.__config = master.config
        try:
            self.__configConfig = master.config["config"]
        except KeyError:
            self.__configConfig = {}
        try:
            self.__configDSMRreader = master.config["sources"]["DSMRreader"]
        except KeyError:
            self.__configDSMRreader = {}

        self.status = self.__configDSMRreader.get("enabled", False)
        self.brokerIP = self.__configDSMRreader.get("brokerIP", None)
        self.brokerPort = self.__configDSMRreader.get("brokerPort", 1883)
        self.username = self.__configDSMRreader.get("username", None)
        self.password = self.__configDSMRreader.get("password", None)

        # Unload if this module is disabled or misconfigured
        if (not self.status) or (not self.brokerIP):
            self.master.releaseModule("lib.TWCManager.EMS", "MQTT")
            return None

        self.__topic = self.__configDSMRreader.get("topic", "dsmr/json")

        logger.debug("Attempting to Connect to DSMR-reader MQTT Broker")
        if self.brokerIP:
            if hasattr(self.mqtt, "CallbackAPIVersion"):
                self.__client = self.mqtt.Client(
                    self.mqtt.CallbackAPIVersion.VERSION2,
                    "DSMRreader.EMS",
                    protocol=self.mqtt.MQTTv5,
                )
            else:
                self.__client = self.mqtt.Client("DSMRreader.EMS")
            if self.username and self.password:
                self.__client.username_pw_set(self.username, self.password)
            self.__client.on_connect = self.mqttConnect
            self.__client.on_message = self.mqttMessage
            self.__client.on_subscribe = self.mqttSubscribe
            try:
                self.__client.connect_async(
                    self.brokerIP, port=self.brokerPort, keepalive=30
                )
            except ConnectionRefusedError as e:
                logger.error("Error connecting to DSMR-reader MQTT Broker")
                logger.debug(str(e))
                return False
            except OSError as e:
                logger.error("Error connecting to DSMR-reader MQTT Broker")
                logger.debug(str(e))
                return False

            self.__connectionState = 1
            self.__client.loop_start()

        else:
            logger.log(logging.INFO4, "Module enabled but no brokerIP specified.")

    def mqttConnect(self, client, userdata, flags, rc, properties=None):
        logger.log(logging.INFO5, "DSMRreader MQTT Connected.")

        if self.__topic:
            logger.log(logging.INFO5, "Subscribe to " + self.__topic)
            res = self.__client.subscribe(self.__topic, qos=0)
            logger.log(logging.INFO5, "Res: " + str(res))

    def mqttMessage(self, client, userdata, message):
        # Takes an DSMR-reader JSON MQTT message, and update the associated Generation/Consumption value
        decoded_message = str(message.payload.decode("utf-8", "ignore"))
        logger.debug(f"Decoded message {decoded_message}")
        try:
            payload = json.loads(decoded_message)
            logger.log(logging.INFO5, "Loaded JSON from message")
        except Exception as e:
            payload = {}
            logger.warning(f"Loading JSON from message failed: {str(e)}")

        if message.topic == self.__topic:
            self.consumedW = (
                float(payload.get("electricity_currently_delivered", 0)) * 1000.0
            )
            logger.log(
                logging.INFO3, f"Consumption Value updated to {round(self.consumedW)}W"
            )

            self.generatedW = (
                float(payload.get("electricity_currently_returned", 0)) * 1000.0
            )
            logger.log(
                logging.INFO3, f"Generation Value updated to {round(self.generatedW)}W"
            )

            # Determine the most consumed Amps among all phases
            self.consumedA = 0
            if payload.get("phase_currently_delivered_l1", 0) > payload.get(
                "phase_currently_returned_l1", 0
            ):
                self.consumedA = max(
                    self.consumedA, payload.get("phase_power_current_l1", 0)
                )
            if payload.get("phase_currently_delivered_l2", 0) > payload.get(
                "phase_currently_returned_l2", 0
            ):
                self.consumedA = max(
                    self.consumedA, payload.get("phase_power_current_l2", 0)
                )
            if payload.get("phase_currently_delivered_l3", 0) > payload.get(
                "phase_currently_returned_l3", 0
            ):
                self.consumedA = max(
                    self.consumedA, payload.get("phase_power_current_l3", 0)
                )
            logger.log(
                logging.INFO3, f"Consumption Amps Value updated to {self.consumedA}A"
            )

    def mqttSubscribe(self, client, userdata, mid, reason_codes, properties=None):
        logger.info("Subscribe operation completed with mid " + str(mid))

    def getConsumption(self):
        if not self.status:
            logger.debug("Module Disabled. Skipping getConsumption")
            return 0

        # Return consumption value
        return self.consumedW

    def getConsumptionAmps(self):
        if not self.status:
            logger.debug("Module Disabled. Skipping getConsumptionAmps")
            return 0

        # Return consumption value
        return self.consumedA

    def getGeneration(self):
        if not self.status:
            logger.debug("Module Disabled. Skipping getGeneration")
            return 0

        # Return generation value
        return self.generatedW
