import logging
import paho.mqtt.client as mqtt
import threading
from typing import Optional

from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

logger = LoggerFactory.get_logger("Telemetry", "Vehicle")

# Fields whose changes are logged: what the next presence analysis needs
_LOGGED = {"pluggedIn", "state"}


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
    # The car id in every topic is the VIN (Fleet): a live message attaches
    # without waiting for the name.
    vinInTopic = False
    registry = None
    # MQTT connection state: the feed's liveness. everConnected and
    # lostConnection let a reconnect after a drop be logged once.
    connected = False
    everConnected = False
    lostConnection = False

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
        # Car ids whose vehicle was forgotten (never re-created by a list
        # load), and those already warned about as unmappable.
        self.orphanedIds = set()
        self.warnedUnmapped = set()
        # (car, field) -> the last value logged for it
        self.loggedValues = {}
        self.registry = VehicleRegistry.forMaster(master)

        self.config = master.config
        self.configConfig = self.config.get("config", {})
        try:
            self.configModule = self.config["vehicle"][self.configName]
            self.status = self.config["vehicle"][self.configName]["enabled"]
        except KeyError:
            self.configModule = {}

        # Unload if this module is disabled or misconfigured
        if not self.status or self.__class__.__name__ == "TelmetryBase":
            self.master.releaseModule("lib.TWCManager.Vehicle", self.__class__.__name__)
            return None

        # A report-on-change source: while the feed is live, its silence
        # means its last values still hold.
        self.registry.registerLiveness(self.__class__.__name__, self.feedLiveAt)

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
        # A planned reconnect: not live until connected again, not a drop.
        self.connected = False
        self.client.disconnect()
        self.client.loop_stop()
        self.doMQTT()

    def mqttConnect(self, client, userdata, flags, rc, properties=None):
        # Subscriptions are the child class's; subclasses call this first.
        # paho also calls this for a refused connection.
        failed = rc.is_failure if hasattr(rc, "is_failure") else rc != 0
        if failed:
            self.connected = False
            logger.log(logging.INFO5, f"MQTT connection refused ({rc}).")
            return
        logger.log(logging.INFO5, "MQTT Connected.")
        if self.everConnected and self.lostConnection:
            logger.info(f"Telemetry from {self.__class__.__name__} has resumed")
        self.connected = True
        self.everConnected = True
        self.lostConnection = False

    def mqttDisconnect(
        self, client, userdata, disconnect_flags, reason_code, properties=None
    ):
        self.connected = False
        if reason_code != 0:
            self.lostConnection = True
            logger.log(
                logging.INFO5, "MQTT Disconnected. Should reconnect automatically."
            )

    def carHealthy(self, vehicle) -> bool:
        # Per-car health, where the feed reports it (TeslaMate).
        return True

    def feedLiveAt(self, vehicle=None) -> Optional[float]:
        # Registered liveness: "now" while connected and healthy, so a value
        # this feed last reported is current; None otherwise.
        if self.connected and self.carHealthy(vehicle):
            return self.registry.now()
        return None

    def vinFor(self, vehicle_id) -> Optional[str]:
        # The VIN for a telemetry car id, where the feed can tell.
        return None

    def mqttMessage(self, client, userdata, message):
        # No defaults, should be handled by child class
        pass

    def applyDataToVehicle(self, id, event, payload, retained=False):
        # retained: the broker's stored copy, redelivered on every
        # (re)subscribe, not a new report.
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
            if self.updateVehicles(id, payload, loadList=False, retained=retained):
                self.replayUnknownEvents(id)
            else:
                self.scheduleUnmappedRetry(delay=1)

        elif event in events:
            vehicle = self.vehicles.get(id, None)
            name = self.vehicleNames.get(id)
            orphaned = vehicle is not None and self._dropIfOrphan(id, vehicle)
            if orphaned:
                vehicle = None
                self.orphanedIds.add(id)
            if vehicle is None and not retained:
                # A live report is a sighting: once the car's name is known,
                # attach now (no I/O here), by VIN (creating the vehicle) or
                # to a vehicle of that name. A retained copy never creates or
                # revives one.
                vin = self.vinFor(id)
                if (name or (vin and self.vinInTopic)) and (
                    vin or self.registry.findByName(name)
                ):
                    if self.updateVehicles(id, name, loadList=False):
                        self.replayUnknownEvents(id)
                        vehicle = self.vehicles.get(id)
            if orphaned and vehicle is None and name:
                self.scheduleUnmappedRetry()
            if vehicle:
                field, converter = events[event][0], events[event][1]
                self._observe(vehicle, field, converter(payload), retained)
            else:
                # If we don't know this vehicle yet, save the data: the
                # latest per topic, so a car the broker retains but that
                # never reports live cannot grow it without bound.
                parked = [event, payload]
                if retained:
                    parked.append(True)
                kept = [e for e in self.unknownVehicles.get(id, []) if e[0] != event]
                self.unknownVehicles[id] = kept + [parked]

    def _dropIfOrphan(self, vehicle_id, vehicle) -> bool:
        # True (and the mapping dropped) when the registry no longer holds
        # this vehicle object: it was forgotten, and perhaps re-created.
        if self.registry.get(vehicle.key) is vehicle:
            return False
        if self.vehicles.get(vehicle_id) is vehicle:
            del self.vehicles[vehicle_id]
        return True

    def logChange(self, car, label, field, value):
        # One line per change of a car's value, the first sighting included
        old = self.loggedValues.get((car, field))
        if value != old:
            self.loggedValues[(car, field)] = value
            logger.info("%s: %s %s -> %s", label, field, old, value)

    def _observe(self, vehicle, field, value, retained=False):
        source = self.__class__.__name__
        if not retained:
            if field in _LOGGED:
                self.logChange(vehicle.key, vehicle.name, field, value)
            vehicle.observe(field, value, source)
            if source not in vehicle.claims:
                # Listed on the car's provider list; it commands nothing and
                # is not kept alive: a car it stops reporting still ages out.
                vehicle.claim(source, False, "reports", keepsAlive=False)
            return
        # A retained redelivery is not a sighting. One that repeats this
        # source's last report is not news either: restamping it would make
        # a stale value look newer than our own write, and an unhealthy
        # feed's value look current.
        mine = vehicle.reported(field).get(source)
        if mine is not None and mine.value == value:
            return
        # A different retained value may predate our own write: stamped no
        # later than the write, so FEED_LAG decides whether it stands. Not
        # when this source reported after the write: the broker keeps only
        # the latest publish, so this one came later still.
        write = vehicle.lastWrite(field)
        at = None
        if write is not None and (mine is None or mine.at <= write.at):
            at = min(self.registry.now(), write.at)
        vehicle.observe(field, value, source, at=at, seen=False)

    def mqttSubscribe(self, client, userdata, mid, reason_codes, properties=None):
        logger.info("Subscribe operation completed with mid " + str(mid))

    def replayUnknownEvents(self, vehicle_id):
        if vehicle_id in self.vehicles and vehicle_id in self.unknownVehicles:
            pastEvents = self.unknownVehicles.pop(vehicle_id)
            for pastEvent in pastEvents:
                self.applyDataToVehicle(
                    vehicle_id, pastEvent[0], pastEvent[1], len(pastEvent) > 2
                )

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
        self.refreshIdentities()
        for vehicle_id, vehicle_name in list(self.vehicleNames.items()):
            if vehicle_id not in self.vehicles:
                # A replay of a name already received: not a sighting. A
                # forgotten car is never re-created by a vehicle-list load.
                if self.updateVehicles(
                    vehicle_id,
                    vehicle_name,
                    loadList=vehicle_id not in self.orphanedIds,
                    retained=True,
                ):
                    self.replayUnknownEvents(vehicle_id)

        if any(i not in self.vehicles for i in self.vehicleNames):
            self.scheduleUnmappedRetry()

    def refreshIdentities(self):
        # Reload what vinFor uses, if an earlier load failed. Runs on the
        # retry timer thread, never the MQTT thread.
        pass

    def _attach(self, vehicle_id, vehicle, label):
        self.vehicles[vehicle_id] = vehicle
        self.orphanedIds.discard(vehicle_id)
        self.warnedUnmapped.discard(vehicle_id)
        logger.info(
            f"Vehicle {label} telemetry being provided by {self.__class__.__name__}"
        )
        return True

    def updateVehicles(self, vehicle_id, vehicle_name, loadList=True, retained=False):
        # Attaches the telemetry car id to a registry vehicle, by VIN where
        # the feed knows it, else by name. A retained message (or a replay of
        # one) only attaches to a vehicle the registry holds: it never
        # creates one, or revives one that was forgotten or expired.
        source = self.__class__.__name__
        vehicle = self.vehicles.get(vehicle_id, None)
        if vehicle is not None and not self._dropIfOrphan(vehicle_id, vehicle):
            # Already mapped: a rename.
            if vehicle_name:
                self._observe(vehicle, "name", vehicle_name, retained)
            return True

        vin = self.vinFor(vehicle_id)
        if vin:
            try:
                if retained:
                    vehicle = self.registry.get(vin)
                else:
                    vehicle = self.registry.getOrCreate(vin, source)
            except ValueError:
                vehicle = None
            if vehicle is not None:
                if vehicle_name:
                    self._observe(vehicle, "name", vehicle_name, retained)
                return self._attach(vehicle_id, vehicle, vehicle_name or vin)

        if not vehicle_name:
            return False
        vehicle = self.registry.findByName(vehicle_name)
        if vehicle is None and loadList:
            # The API vehicle list may not be loaded yet (checkCharge loads
            # it at startup and on each arrival). Callers on
            # latency-sensitive threads pass loadList=False.
            carapi = self.master.getModuleByName("TeslaAPI")
            if carapi and not carapi.getCarApiRetryRemaining():
                carapi.loadVehicleList()
                vehicle = self.registry.findByName(vehicle_name)

        if vehicle is not None:
            return self._attach(vehicle_id, vehicle, vehicle_name)

        # Once per car id at WARNING; the retries that follow at DEBUG.
        level = logging.DEBUG if vehicle_id in self.warnedUnmapped else logging.WARNING
        self.warnedUnmapped.add(vehicle_id)
        logger.log(
            level,
            f"{source}: no known vehicle named "
            f"'{vehicle_name}' to attach telemetry to; will retry",
        )
        return False
