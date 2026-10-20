import logging
import psycopg2
import paho.mqtt.client as mqtt
import threading
import time
import json
from typing import Optional

from TWCManager.Vehicle.Registry.VIN import isWellFormedVIN
from TWCManager.Vehicle.Telemetry import TelmetryBase
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("FleetTLM", "Vehicle")
_DOC = "docs/modules/Vehicle_FleetTelemetryMQTT.md"


def _fastCharger(a):
    # "FastChargerSupercharger" -> "Supercharger"; an unknown type is not a
    # charger the car is at
    name = a[11:]
    return "" if name in ("Unknown", "SNA") else name


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
        "ChargeCurrentRequest": ["chargeCurrentRequest", lambda a: int(a)],
        "ChargeAmps": ["actualCurrent", lambda a: float(a)],
        "ChargerPhases": ["phases", lambda a: int(a) if a else 0],
        "ChargerVoltage": ["voltage", lambda a: float(a)],
        "DetailedChargeState": ["chargingState", lambda a: a[19:]],
        "FastChargerPresent": ["fastChargerPresent", lambda a: a in (True, "true")],
        "FastChargerType": ["fastChargerType", _fastCharger],
        "ChargingCableType": ["connChargeCable", lambda a: a[9:]],
        "ACChargingPower": ["chargerPower", lambda a: float(a)],
        "ChargeCurrentRequestMax": ["chargeCurrentRequestMax", lambda a: int(a)],
    }
    # Config fields that only narrow a plug-in to a home charger or fill the
    # display; checkConfigs warns of a car whose config lacks any other field
    # read here (Location gives latitude/longitude; syncState is connectivity)
    OPTIONAL = {"FastChargerPresent", "FastChargerType", "ChargingCableType"}
    OPTIONAL |= {"ACChargingPower", "ChargeCurrentRequestMax"}
    OPTIONAL |= {"TimeToFullCharge", "VehicleName"}
    REQUIRED = (set(events) | {"Location", "Gear"}) - OPTIONAL
    REQUIRED -= {"latitude", "longitude", "syncState"}
    configCheckDue = 0.0

    def configProblem(self, answer) -> str:
        # "" for a config with every required field, applied by the car
        config = answer.get("config") if isinstance(answer, dict) else None
        if not isinstance(config, dict):
            return "no Fleet Telemetry config"
        missing = sorted(self.REQUIRED - set(config.get("fields") or {}))
        if missing:
            return "Fleet Telemetry config lacks " + ", ".join(missing)
        return "" if answer.get("synced") else "Fleet Telemetry config not applied yet"

    def checkConfigs(self, carapi):
        # A free read that never wakes the car. The claim holds the shown
        # problem, so each one is warned of once
        now, cars = time.time(), [v for v, _ in self.registry.attached("TeslaAPI")]
        if now < self.configCheckDue or not cars or not carapi.fleetSlot.usable("read"):
            return
        self.configCheckDue = now + 24 * 60 * 60
        for vehicle in cars:
            answer = carapi.telemetryConfig(vehicle.vin)
            if answer is None:
                continue
            status = self.configProblem(answer) or "reports"
            claim = vehicle.claims.get(type(self).__name__)
            if status != getattr(claim, "status", "reports"):
                if status != "reports":
                    logger.warning("%s: %s; see %s", vehicle.vin, status, _DOC)
                vehicle.claim(type(self).__name__, False, status, keepsAlive=False)

    def vinFor(self, vehicle_id) -> Optional[str]:
        # The topic id is the VIN. Anything else under the prefix (another
        # publisher's status topics) is not a car.
        if isinstance(vehicle_id, str) and isWellFormedVIN(vehicle_id):
            return vehicle_id
        return None

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
            self.vehicles[topic[1]].reported("state").get("FleetTelemetryMQTT")
            if self.vehicles.get(topic[1])
            else None
        )
        syncState = obs.value if obs else None
        newSyncState = None
        if len(topic) > 3 and topic[2] == "v":
            newSyncState = "online"
            if topic[3] == "Gear":
                if payload in ("ShiftStateR", "ShiftStateN", "ShiftStateD"):
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
