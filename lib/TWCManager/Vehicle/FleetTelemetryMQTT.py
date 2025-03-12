import logging
import psycopg2
import paho.mqtt.client as mqtt
import threading
import time
import json

from TWCManager.Vehicle.Telemetry import TelmetryBase

logger = logging.getLogger("\U0001f697 FleetTLM")


class FleetTelemetryMQTT(TelmetryBase):
    configName = "teslaFleetTelemetryMQTT"
    vehicleNameTopic = "VehicleName"
    events = {
        "BatteryLevel": ["batteryLevel", lambda a: int(float(a))],
        "ChargeLimitSoc": ["chargeLimit", lambda a: int(float(a))],
        "VehicleName": ["name", lambda a: a],
        "latitude": ["syncLat", lambda a: float(a)],
        "longitude": ["syncLon", lambda a: float(a)],
        "syncState": ["syncState", lambda a: a],
        "TimeToFullCharge": ["timeToFullCharge", lambda a: float(a)],
        "ChargeCurrentRequest": ["availableCurrent", lambda a: int(a)],
        "ChargeAmps": ["actualCurrent", lambda a: float(a)],
        "ChargerPhases": ["phases", lambda a: int(a) if a else 0],
        "ChargerVoltage": ["voltage", lambda a: float(a)],
        "DetailedChargeState": ["chargingState", lambda a: a[19:]],
    }

    def mqttConnect(self, client, userdata, flags, rc, properties=None):
        logger.log(logging.INFO5, "MQTT Connected.")
        logger.log(logging.INFO5, "Subscribe to " + self.mqtt_prefix + "/#")
        res = self.client.subscribe(self.mqtt_prefix + "/#", qos=1)
        logger.log(logging.INFO5, "Res: " + str(res))

    def mqttMessage(self, client, userdata, message):
        topic = str(message.topic).split("/")
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except json.decoder.JSONDecodeError:
            logger.warning(f"Can't decode payload {payload} in topic {message.topic}")
            return

        # Topic format is telemetry/VEHICLE-VIN/v/ChargerVoltage
        if topic[0] != self.mqtt_prefix:
            return

        syncState = (
            self.vehicles[topic[1]].syncState if self.vehicles.get(topic[1]) else ""
        )
        if len(topic) > 3 and topic[2] == "v":
            if topic[3] == "Gear":
                if payload in (
                    "R",
                    "N",
                    "D",
                ):
                    self.applyDataToVehicle(topic[1], "syncState", "driving")
                elif syncState == "driving":
                    self.applyDataToVehicle(topic[1], "syncState", "online")
            elif topic[3] == "DetailedChargeState":
                self.applyDataToVehicle(topic[1], topic[3], payload)
                if payload == "DetailedChargeStateCharging":
                    self.applyDataToVehicle(topic[1], "syncState", "charging")
                elif syncState == "charging":
                    self.applyDataToVehicle(topic[1], "syncState", "online")
            elif topic[3] == "Location" and isinstance(payload, dict):
                if payload.get("latitude"):
                    self.applyDataToVehicle(topic[1], "latitude", payload["latitude"])
                if payload.get("longitude"):
                    self.applyDataToVehicle(topic[1], "longitude", payload["longitude"])
            else:
                self.applyDataToVehicle(topic[1], topic[3], payload)
        elif len(topic) > 2 and topic[2] == "connectivity":
            status = payload.get("Status")
            if status == "CONNECTED" and syncState not in (
                "driving",
                "charging",
            ):
                self.applyDataToVehicle(topic[1], "syncState", "online")
            elif status == "DISCONNECTED":
                self.applyDataToVehicle(topic[1], "syncState", "offline")
