# MQTT Status Output
# Publishes the provided key and value pair to the provided topic prefix

import logging
import time
import json

logger = logging.getLogger("\U0001f4ca MQTTStatus")


class MQTTStatus:
    import paho.mqtt.client as mqtt

    brokerIP = None
    brokerPort = 1883
    __carsCharging = {}
    __config = None
    __configMQTT = {}
    connectionState = 0  # 0 = disconnected/connecting, 1 = connected
    __master = None
    __msgRate = {}
    __msgRatePerTopic = 60
    password = None
    status = False
    brokerTLS = False
    topicPrefix = None
    username = None

    homeassistantDiscovery = False
    discoveryPrefix = "homeassistant"
    deviceNamePrefix = "TWC"
    deviceNamePrefixUnderscore = "TWC"
    discoveryPublished = set()

    client = None
    connected = False

    def __init__(self, master):
        self.__config = master.config
        self.__master = master

        try:
            self.__configMQTT = self.__config["status"]["MQTT"]
        except KeyError:
            self.__configMQTT = {}

        self.status = self.__configMQTT.get("enabled", False)
        self.brokerIP = self.__configMQTT.get("brokerIP", None)
        self.brokerPort = int(self.__configMQTT.get("brokerPort", 1883))
        self.topicPrefix = self.__configMQTT.get("topicPrefix", None)
        self.username = self.__configMQTT.get("username", None)
        self.password = self.__configMQTT.get("password", None)
        self.brokerTLS = bool(self.__configMQTT.get("brokerTLS", False))
        self.__msgRatePerTopic = int(self.__configMQTT.get("ratelimit", 60))

        self.homeassistantDiscovery = bool(
            self.__configMQTT.get("homeassistantDiscovery", False)
        )
        self.discoveryPrefix = self.__configMQTT.get(
            "discoveryPrefix", "homeassistant"
        ).strip("/")
        self.deviceNamePrefix = self.__configMQTT.get("deviceNamePrefix", "TWC")
        self.deviceNamePrefixUnderscore = self.deviceNamePrefix.replace(' ', '_')

        # Unload if this module is disabled or misconfigured
        if (not self.status) or (not self.brokerIP) or (not self.topicPrefix):
            self.__master.releaseModule("lib.TWCManager.Status", "MQTTStatus")
            return

        # Create a persistent MQTT client and connect
        self._init_mqtt_client()

    def _init_mqtt_client(self):
        """
        Create a persistent MQTT client, configure it, and start the network loop.
        Connection (and reconnection) will be handled by connect_async + loop_start.
        """
        try:
            if hasattr(self.mqtt, "CallbackAPIVersion"):
                client = self.mqtt.Client(
                    self.mqtt.CallbackAPIVersion.VERSION2,
                    "MQTTStatus",
                    protocol=self.mqtt.MQTTv5,
                )
            else:
                client = self.mqtt.Client(client_id="MQTTStatus")

            if self.username and self.password:
                client.username_pw_set(self.username, self.password)

            if self.brokerTLS:
                # Basic TLS enablement; details (CA, certs) can be added via config if needed
                client.tls_set()

            client.on_connect = self.mqttConnected
            client.on_disconnect = self.mqttDisconnected

            # Optional: configure automatic reconnect delays (backoff)
            if hasattr(client, "reconnect_delay_set"):
                client.reconnect_delay_set(min_delay=1, max_delay=120)

            self.client = client
            self.connectionState = 0
            self.connected = False

            logger.debug(
                f"MQTT Status: Connecting to {self.brokerIP}:{self.brokerPort}"
            )
            # connect_async + loop_start -> persistent connection with auto reconnect
            self.client.connect_async(self.brokerIP, port=self.brokerPort, keepalive=30)
            self.client.loop_start()

        except Exception as e:
            logger.log(
                logging.INFO4,
                "Error initialising MQTT client to publish topic values",
            )
            logger.debug(str(e))
            self.client = None
            self.connected = False
            self.connectionState = 0

    def mqttConnected(self, client, userdata, flags, rc, properties=None):
        """
        Called when the MQTT client connects (or reconnects) to the broker.
        """
        logger.debug("Connected to MQTT Broker with RC: " + str(rc))

        if rc == 0:
            self.connected = True
            self.connectionState = 1
            logger.log(logging.DEBUG2, "MQTT connection established")
        else:
            # Non-zero rc means connection was refused.
            self.connected = False
            self.connectionState = 0
            logger.log(
                logging.INFO4,
                f"MQTT connection failed with result code {rc}",
            )

    def mqttDisconnected(self, client, userdata, *extra):
        """
        Called when the MQTT client disconnects from the broker.

        Supports both:
        - Paho 1.x: on_disconnect(client, userdata, rc)
        - Paho 2.x: on_disconnect(client, userdata, flags, rc, properties)
        """
        # Determine rc from the extra args
        # old API: extra == (rc,)
        # new API: extra == (flags, rc, properties)
        rc = 0
        if len(extra) == 1:
            rc = extra[0]
        elif len(extra) >= 2:
            rc = extra[1]

        self.connected = False
        self.connectionState = 0

        if rc != 0:
            logger.warning(
                f"Unexpected MQTT disconnect (rc={rc}). Client will attempt to reconnect."
            )
        else:
            logger.debug("MQTT client disconnected cleanly")

    # Internal helpers

    def _handleCarsCharging(self, twc, twident, value):
        # When an update comes in for the carsCharging value, check if it was previously 1 for the
        # given TWC, and if it is now 0. If so, zero out relevant topics related to charge rate
        if self.__carsCharging.get(twident, "0") != str(value):
            if str(value) == "0":
                self.setStatus(twc, "amps_in_use", "ampsInUse", 0, "A")
        self.__carsCharging[twident] = str(value)

    def _determine_classes(self, unit):
        device_class = None
        state_class = None
        if unit in ("W", "kW"):
            device_class = "power"
            state_class = "measurement"
        elif unit in ("Wh", "kWh", "MWh"):
            device_class = "energy"
            state_class = "total_increasing"  # change to "total" if not monotonic
        elif unit == "A":
            device_class = "current"
            state_class = "measurement"
        elif unit == "V":
            device_class = "voltage"
            state_class = "measurement"
        return device_class, state_class

    def _publish_discovery_if_needed(
        self, twident, key_underscore, unit, state_topic
    ):
        """
        Publish Home Assistant discovery config once per sensor, using the
        persistent MQTT connection. Messages are retained.
        """

        uid = f"twcmanager_{self.deviceNamePrefixUnderscore}_{twident}_{key_underscore}"
        if uid in self.discoveryPublished:
            return

        object_id = f"{twident}_{key_underscore}"
        node = f"{self.topicPrefix}_{twident}".replace("/", "_")
        cfg_topic = f"{self.discoveryPrefix}/sensor/{node}/{object_id}/config"

        device_class, state_class = self._determine_classes(unit)

        payload = {
            "name": key_underscore.replace("_", " ").capitalize(),
            "unique_id": uid,
            "state_topic": state_topic,
            "device": {
                "identifiers": [
                    f"twcmanager_{self.deviceNamePrefixUnderscore}_{twident}"
                ],
                "manufacturer": "Open Source",
                "model": "TWCManager",
                "name": f"{self.deviceNamePrefix} {twident}",
            },
        }
        if unit:
            payload["unit_of_measurement"] = unit
        if device_class:
            payload["device_class"] = device_class
        if state_class:
            payload["state_class"] = state_class

        if not self.client:
            logger.log(
                logging.INFO4,
                "MQTT discovery publish requested but MQTT client is not initialised",
            )
            return

        try:
            logger.log(
                logging.INFO8,
                f"Publishing MQTT Discovery Topic {cfg_topic} (payload omitted)",
            )
            result = self.client.publish(
                cfg_topic, payload=json.dumps(payload), qos=0, retain=True
            )
            if result.rc != self.mqtt.MQTT_ERR_SUCCESS:
                logger.log(
                    logging.INFO4,
                    f"Error publishing MQTT Discovery Topic {cfg_topic}: rc={result.rc}",
                )
                return
        except Exception as e:
            logger.log(logging.INFO4, "Error publishing MQTT Discovery Topic")
            logger.debug(str(e))
            return

        self.discoveryPublished.add(uid)

    def setStatus(self, twcid, key_underscore, key_camelcase, value, unit):
        if not self.status:
            return

        if not self.client:
            # Try to initialise (or re-initialise) client if something went wrong earlier
            logger.debug("MQTT client not initialised; attempting to re-create")
            self._init_mqtt_client()
            if not self.client:
                # Still no client — give up for now
                return

        # Format TWCID nicely
        if len(twcid) == 2:
            twident = "%02X%02X" % (twcid[0], twcid[1])
        else:
            twident = str(twcid.decode("utf-8"))

        topic = self.topicPrefix + "/" + twident + "/" + key_camelcase

        # We have a special case where we perform extra handling of the carsCharging topic
        # This is because, once carsCharging goes from 1 to 0 for a given TWC, we no longer
        # get any status events about charge rate, but it will effectively be 0
        # So in this case, if we see carsCharging drop from 1 to 0, we publish 0 for the
        # sensors that should be updated as a result
        if key_camelcase == "carsCharging":
            self._handleCarsCharging(twcid, twident, value)

        # Home Assistant discovery (publish once)
        if self.homeassistantDiscovery:
            self._publish_discovery_if_needed(
                twident=twident,
                key_underscore=key_underscore,
                unit=unit,
                state_topic=topic,
            )

        # Perform rate limiting first (as there are some very chatty topics).
        # For each message that comes through, we take the topic name and check
        # when we last sent a message. If it was less than msgRatePerTopic
        # seconds ago, we dampen it.
        now = time.time()
        if self.__msgRatePerTopic and topic in self.__msgRate:
            if (now - self.__msgRate[topic]) < self.__msgRatePerTopic:
                return True

            self.__msgRate[topic] = now
        else:
            self.__msgRate[topic] = now

        try:
            logger.log(
                logging.INFO8,
                f"Publishing MQTT Topic {topic} (value is {value})",
            )
            result = self.client.publish(topic, payload=value, qos=0, retain=True)
            if result.rc != self.mqtt.MQTT_ERR_SUCCESS:
                logger.log(
                    logging.INFO4,
                    f"Error publishing MQTT Topic {topic}: rc={result.rc}",
                )
                logger.debug(
                    f"MQTT publish error details: mid={getattr(result, 'mid', None)}"
                )
                return False
        except Exception as e:
            logger.log(logging.INFO4, "Error publishing MQTT Topic Status")
            logger.debug(str(e))
            return False

        return True
