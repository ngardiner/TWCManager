import logging
import psycopg2
import paho.mqtt.client as mqtt
import threading
import time
import json
from typing import Optional

from TWCManager.Vehicle.Telemetry import TelmetryBase
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("FleetTLM", "Vehicle")


class FleetTelemetryMQTT(TelmetryBase):
    configName = "teslaFleetTelemetryMQTT"
    vehicleNameTopic = "VehicleName"
    vinInTopic = True
    events = {
        "BatteryLevel": ["batteryLevel", lambda a: int(float(a))],
        "ChargeLimitSoc": ["chargeLimit", lambda a: int(float(a))],
        "VehicleName": ["name", lambda a: a],
        "latitude": ["lat", lambda a: float(a)],
        "longitude": ["lon", lambda a: float(a)],
        "syncState": ["state", lambda a: a],
        "TimeToFullCharge": ["timeToFullCharge", lambda a: float(a)],
        "ChargeCurrentRequest": ["availableCurrent", lambda a: int(a)],
        "ChargeAmps": ["actualCurrent", lambda a: float(a)],
        "ChargerPhases": ["phases", lambda a: int(a) if a else 0],
        "ChargerVoltage": ["voltage", lambda a: float(a)],
        "DetailedChargeState": ["chargingState", lambda a: a[19:]],
    }

    def vinFor(self, vehicle_id) -> Optional[str]:
        # The topic id is the VIN.
        return vehicle_id

    def mqttConnect(self, client, userdata, flags, rc, properties=None):
        super().mqttConnect(client, userdata, flags, rc, properties)
        if not self.connected:
            return
        logger.log(logging.INFO5, "Subscribe to " + self.mqtt_prefix + "/#")
        res = self.client.subscribe(self.mqtt_prefix + "/#", qos=1)
        logger.log(logging.INFO5, "Res: " + str(res))

    def mqttMessage(self, client, userdata, message):
        topic = str(message.topic).split("/")
        try:
            payload = json.loads(message.payload.decode("utf-8"))
        except json.decoder.JSONDecodeError:
            logger.warning(
                f"Can't decode payload {message.payload!r} in topic {message.topic}"
            )
            return

        # The broker's stored copy, redelivered on (re)subscribe.
        retained = getattr(message, "retain", False) is True

        # Topic format is telemetry/VEHICLE-VIN/v/ChargerVoltage
        if topic[0] != self.mqtt_prefix:
            return

        obs = (
            self.vehicles[topic[1]].latest("state")
            if self.vehicles.get(topic[1])
            else None
        )
        syncState = obs.value if obs else None
        newSyncState = None
        if len(topic) > 3 and topic[2] == "v":
            newSyncState = "online"
            if topic[3] == "Gear":
                if payload in (
                    "R",
                    "N",
                    "D",
                    "ShiftStateR",
                    "ShiftStateN",
                    "ShiftStateD",
                ):
                    newSyncState = "driving"
                elif syncState == "driving":
                    syncState = None
            elif topic[3] == "DetailedChargeState":
                self.applyDataToVehicle(topic[1], topic[3], payload, retained=retained)
                if payload == "DetailedChargeStateCharging":
                    newSyncState = "charging"
                elif syncState == "charging":
                    syncState = None
            elif topic[3] == "Location" and isinstance(payload, dict):
                if payload.get("latitude"):
                    self.applyDataToVehicle(
                        topic[1], "latitude", payload["latitude"], retained=retained
                    )
                if payload.get("longitude"):
                    self.applyDataToVehicle(
                        topic[1], "longitude", payload["longitude"], retained=retained
                    )
            else:
                self.applyDataToVehicle(topic[1], topic[3], payload, retained=retained)
            if retained:
                # A broker redelivering a stored value says nothing about the
                # car's state now: only retained connectivity sets it.
                newSyncState = None
        elif len(topic) > 2 and topic[2] == "connectivity":
            status = payload.get("Status")
            if status == "CONNECTED":
                newSyncState = "online"
            elif status == "DISCONNECTED":
                newSyncState = "offline"
        if (
            newSyncState
            and syncState != newSyncState
            and not (newSyncState == "online" and syncState in ("driving", "charging"))
        ):
            self.applyDataToVehicle(
                topic[1], "syncState", newSyncState, retained=retained
            )
