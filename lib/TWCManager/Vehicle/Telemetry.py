import logging
import paho.mqtt.client as mqtt
import threading
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("Telemetry", "Vehicle")


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
    vehicleNames = {}
    unmappedRetrySecs = 300
    retryScheduled = False

    # Empty default events
    events = {
        # syntax:
        # "battery_level": ["batteryLevel", lambda a: int(a)],
    }

    def __init__(self, master):
        self.master = master
        self.vehicles = {}
        self.unknownVehicles = {}
        self.vehicleNames = {}

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
            timer.daemon = True
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
        timer = threading.Timer(24 * 60 * 60, self.resetMQTT)
        timer.daemon = True
        timer.start()

    def resetMQTT(self):
        self.client.disconnect()
        self.client.loop_stop()
        self.doMQTT()

    def mqttConnect(self, client, userdata, flags, rc, properties=None):
        logger.log(logging.INFO5, "MQTT Connected.")
        # No default subscriptions, should be handled by child class

    def mqttDisconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ):
        if reason_code != 0:
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
            # in the Tesla API module. Remember the name: brokers publish it
            # retained and only on change, so if the API vehicle list isn't
            # loaded yet we must retry the mapping later rather than wait for
            # another message.
            self.vehicleNames[id] = payload
            # Only match against the already-loaded vehicle list here: this
            # runs on the MQTT network thread, which must not block on a cloud
            # call. If it doesn't match, a timer thread loads the list.
            if self.updateVehicles(id, payload, loadList=False):
                self.replayUnknownEvents(id)
            else:
                self.scheduleUnmappedRetry(delay=1)

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

    def replayUnknownEvents(self, vehicle_id):
        if vehicle_id in self.vehicles and vehicle_id in self.unknownVehicles:
            pastEvents = self.unknownVehicles.pop(vehicle_id)
            for pastEvent in pastEvents:
                self.applyDataToVehicle(vehicle_id, pastEvent[0], pastEvent[1])

    def scheduleUnmappedRetry(self, delay=None):
        if self.retryScheduled:
            return
        self.retryScheduled = True
        timer = threading.Timer(
            self.unmappedRetrySecs if delay is None else delay,
            self.retryUnmappedVehicles,
        )
        timer.daemon = True
        timer.start()

    def retryUnmappedVehicles(self):
        # Map any vehicle names we've seen but couldn't match yet (e.g. the API
        # vehicle list wasn't available when the name arrived), rescheduling
        # until every known name is mapped.
        self.retryScheduled = False
        for vehicle_id, vehicle_name in list(self.vehicleNames.items()):
            if vehicle_id not in self.vehicles:
                if self.updateVehicles(vehicle_id, vehicle_name):
                    self.replayUnknownEvents(vehicle_id)

        if any(i not in self.vehicles for i in self.vehicleNames):
            self.scheduleUnmappedRetry()

    def updateVehicles(self, vehicle_id, vehicle_name, loadList=True):
        # Called by mqttMessage each time we get the display_name topic
        # We check to see if this aligns with a vehicle we know of from the API

        vehicle = self.vehicles.get(vehicle_id, None)
        if vehicle:
            # We already have this vehicle mapped
            vehicle.name = vehicle_name
            return True

        carapi = self.master.getModuleByName("TeslaAPI")
        if not carapi:
            return False

        # The API vehicle list is otherwise only loaded when a command needs
        # the API, which may never happen when BLE handles everything. Callers
        # on latency-sensitive threads pass loadList=False.
        if loadList and not carapi.getCarApiRetryRemaining():
            carapi.loadVehicleList()

        for apiVehicle in carapi.getCarApiVehicles():
            if apiVehicle.name == vehicle_name:
                # Found a match
                self.vehicles[vehicle_id] = apiVehicle
                apiVehicle.syncSource = self.__class__.__name__

                logger.info(
                    f"Vehicle {vehicle_name} telemetry being provided by {self.__class__.__name__}"
                )
                return True

        logger.warning(
            f"{self.__class__.__name__}: no Tesla API vehicle named "
            f"'{vehicle_name}' to attach telemetry to; will retry"
        )
        return False

    def updateSettings(self):
        return True
