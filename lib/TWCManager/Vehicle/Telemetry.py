import logging
import paho.mqtt.client as mqtt
import threading
import time

logger = logging.getLogger("\U0001f697 Telemetry")


class TelmetryBase:
    client = None
    config = None
    configConfig = None
    configModule = None
    configName = "Telemetry"
    vehicleNameTopic = "display_name"
    master = None
    mqtt_host = None
    mqtt_user = None
    mqtt_pass = None
    mqtt_port = 1883
    mqtt_prefix = None
    syncTelemetry = False
    lastSync = 0
    status = None
    vehicles = {}
    unknownVehicles = {}

    # Empty default events
    events = {
        # syntax:
        # "battery_level": ["batteryLevel", lambda a: int(a)],
    }

    def __init__(self, master):
        self.master = master

        self.config = master.config
        try:
            self.configConfig = self.config["config"]
        except KeyError:
            self.configConfig = {}
        try:
            self.configModule = self.config["vehicle"][self.configName]
            self.status = self.config["vehicle"][self.configName]["enabled"]
        except KeyError:
            self.configModule = {}

        # Unload if this module is disabled or misconfigured
        if not self.status or self.__class__.__name__ == "TelmetryBase":
            self.master.releaseModule("lib.TWCManager.Vehicle", self.__class__.__name__)
            return None

        # Configure MQTT parameters
        self.mqtt_host = self.configModule.get("mqtt_host", None)
        self.mqtt_user = self.configModule.get("mqtt_user", None)
        self.mqtt_pass = self.configModule.get("mqtt_pass", None)
        self.mqtt_prefix = self.configModule.get("mqtt_prefix", None)

        self.syncTelemetry = self.configModule.get("syncTelemetry", False)

        if self.syncTelemetry:
            # We delay collecting telemetry for a short period
            # This gives the TeslaAPI module time to connect to the Tesla API
            # and fetch vehicle information, allowing us to then merge our
            # telemetry and Tesla API information cleanly
            logger.log(
                logging.INFO4, "Telemetry information will be fetched in 30 seconds."
            )
            timer = threading.Timer(30, self.doMQTT)
            timer.start()

    def doMQTT(self):
        client_name = f"TWC{self.__class__.__name__}"
        if hasattr(mqtt, "CallbackAPIVersion"):
            self.client = mqtt.Client(
                mqtt.CallbackAPIVersion.VERSION2, client_name, protocol=mqtt.MQTTv5
            )
        else:
            self.client = mqtt.Client(client_name)
        if self.mqtt_user and self.mqtt_pass:
            self.client.username_pw_set(self.mqtt_user, self.mqtt_pass)
        self.client.on_connect = self.mqttConnect
        self.client.on_message = self.mqttMessage
        self.client.on_subscribe = self.mqttSubscribe
        self.client.on_disconnect = self.mqttDisconnect

        logger.log(logging.INFO4, "Attempting connection to MQTT Broker")

        try:
            self.client.connect_async(self.mqtt_host, port=self.mqtt_port, keepalive=30)
        except ConnectionRefusedError as e:
            logger.log(logging.INFO4, "Error connecting to MQTT Broker")
            logger.debug(str(e))
            return False
        except OSError as e:
            logger.log(logging.INFO4, "Error connecting to MQTT Broker")
            logger.debug(str(e))
            return False

        self.client.loop_start()
        threading.Timer(24 * 60 * 60, self.resetMQTT).start()

    def resetMQTT(self):
        self.client.disconnect()
        self.client.loop_stop()
        self.doMQTT()

    def mqttConnect(self, client, userdata, flags, rc, properties=None):
        logger.log(logging.INFO5, "MQTT Connected.")
        # No default subscriptions, should be handled by child class

    def mqttDisconnect(self, client, userdata, flags, rc, properties=None):
        if rc != 0:
            logger.log(
                logging.INFO5, "MQTT Disconnected. Should reconnect automatically."
            )

    def mqttMessage(self, client, userdata, message):
        # No defaults, should be handled by child class
        pass

    def applyDataToVehicle(self, id, event, payload):
        logger.log(logging.INFO8, f"applyDataToVehicle event={event} payload={payload}")

        # Do not try to apply unset event payloads
        if payload is None:
            return

        events = self.events

        if event == self.vehicleNameTopic:
            # We can map the car ID in Telemetry data to the vehicle
            # in the Tesla API module
            self.updateVehicles(id, payload)
            if id in self.unknownVehicles:
                pastEvents = self.unknownVehicles[id]
                del self.unknownVehicles[id]
                for pastEvent in pastEvents:
                    self.applyDataToVehicle(id, pastEvent[0], pastEvent[1])

        elif event in events:
            vehicle = self.vehicles.get(id, None)
            if vehicle:
                property_name = events[event][0]
                converter = events[event][1]

                setattr(vehicle, property_name, converter(payload))
                vehicle.syncTimestamp = time.time()

                # If sync timed out and has recovered, log it
                if vehicle.syncSource != self.__class__.__name__:
                    vehicle.syncSource = self.__class__.__name__
                    logger.info(
                        f"Vehicle {vehicle.name} telemetry has resumed being provided by {self.__class__.__name__}"
                    )
            else:
                # If we don't know this vehicle yet, save the data.
                if id not in self.unknownVehicles:
                    self.unknownVehicles[id] = []
                self.unknownVehicles[id].append([event, payload])

    def mqttSubscribe(self, client, userdata, mid, reason_codes, properties=None):
        logger.info("Subscribe operation completed with mid " + str(mid))

    def updateVehicles(self, vehicle_id, vehicle_name):
        # Called by mqttMessage each time we get the display_name topic
        # We check to see if this aligns with a vehicle we know of from the API

        vehicle = self.vehicles.get(vehicle_id, None)
        if vehicle:
            # We already have this vehicle mapped
            vehicle.name = vehicle_name
        else:
            for apiVehicle in self.master.getModuleByName(
                "TeslaAPI"
            ).getCarApiVehicles():
                if apiVehicle.name == vehicle_name:
                    # Found a match
                    self.vehicles[vehicle_id] = apiVehicle
                    self.vehicles[vehicle_id].syncSource = self.__class__.__name__

                    logger.info(
                        f"Vehicle {vehicle_name} telemetry being provided by {self.__class__.__name__}"
                    )

    def updateSettings(self):
        return True
