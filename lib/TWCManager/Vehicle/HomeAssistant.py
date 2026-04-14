#!/usr/bin/env python3
"""
Home Assistant vehicle integration for TWCManager.

Replaces direct Tesla API usage by consuming Home Assistant's tesla_fleet
integration via WebSocket discovery + REST-state queries.

Expected TWCManager config:

{
  "vehicle": {
    "HomeAssistant": {
      "enabled": true,
      "url": "http://homeautomation.lan:8123",
      "longLivedToken": "abc123..."
    }
  }
}
"""

import asyncio
import json
import logging
import re
import threading
import time
from typing import Dict, List, Optional

import requests
import websockets
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("V_HASS", "Vehicle")

# VIN: 17 chars excluding I,O,Q
VIN_REGEX = re.compile(r"[A-HJ-NPR-Z0-9]{17}")

TESLA_MODELS = {"Model S", "Model 3", "Model X", "Model Y", "Cybertruck"}


def _slugify_name(name: str) -> str:
    s = name.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s)
    return s.strip("_")


class HaVehicle:
    """Represents a Tesla vehicle mirrored from Home Assistant."""

    def __init__(
        self, name: str, vin: str, device_id: str, entity_ids: Dict[str, Optional[str]]
    ):
        self.name = name
        self.vin = vin
        self.device_id = device_id
        self.slug = _slugify_name(name)
        self.entity_ids = entity_ids  # only the entities we need

        # Cached state used by TWCManager:
        self.battery_level: Optional[float] = None
        self.charge_limit: Optional[float] = None
        self.available_current: Optional[float] = None
        self.charging: Optional[bool] = None

        self.lat: Optional[float] = None
        self.lon: Optional[float] = None
        self.at_home: bool = False

        self.last_state_update: float = 0.0

    # ---- Helpers for entity availability ----

    @property
    def has_charge_switch(self) -> bool:
        return bool(self.entity_ids.get("charge_switch"))

    @property
    def has_charge_current_number(self) -> bool:
        return bool(self.entity_ids.get("charge_current_number"))

    @property
    def has_charge_limit_number(self) -> bool:
        return bool(self.entity_ids.get("charge_limit_number"))

    # ---- Refresh from Home Assistant via REST ----

    def refresh_from_home_assistant(self, api: "HomeAssistant") -> bool:
        def get_state(key: str) -> Optional[dict]:
            ent = self.entity_ids.get(key)
            return api._get_state(ent) if ent else None

        # Battery level
        st = get_state("battery_level_sensor")
        if st is not None:
            try:
                self.battery_level = float(st.get("state"))
            except (TypeError, ValueError):
                pass

        # Charge limit
        st = get_state("charge_limit_number")
        if st is not None:
            try:
                self.charge_limit = float(st.get("state"))
            except (TypeError, ValueError):
                pass

        # Charge current number (available amps setting)
        st = get_state("charge_current_number")
        if st is not None:
            try:
                self.available_current = float(st.get("state"))
            except (TypeError, ValueError):
                pass

        # Charging boolean
        st = get_state("charging_sensor")
        if st is not None:
            state = st.get("state", "").lower()
            self.charging = state in ("charging", "starting", "on")

        # Location
        st = get_state("location_tracker")
        if st is not None:
            attrs = st.get("attributes") or {}
            try:
                self.lat = float(attrs.get("latitude"))
                self.lon = float(attrs.get("longitude"))
            except (TypeError, ValueError):
                pass

            if st.get("state") == "home":
                self.at_home = True
            elif self.lat is not None and self.lon is not None:
                self.at_home = api.is_location_home(self.lat, self.lon)

        self.last_state_update = time.time()
        return True


class HomeAssistant:
    """Home Assistant-backed vehicle module for TWCManager."""

    carApiLastStartOrStopChargeTime = 0
    lastChargeLimitApplied = -1
    minChargeLevel = -1
    lastStateRefreshTime = 0
    stateRefreshInterval = 30  # Minimum seconds between state refreshes per vehicle

    def __init__(self, master):
        self.master = master
        master.teslaLoginAskLater = True
        self.config = getattr(master, "config", {})

        cfg = self.config.get("vehicle", {}).get("HomeAssistant", {}) or {}
        self._enabled: bool = cfg.get("enabled", False)
        self.url: str = cfg.get("url", "").rstrip("/")
        self.token: Optional[str] = cfg.get("longLivedToken")

        if not self._enabled:
            logger.info("HomeAssistant vehicle module disabled.")
            return
        if not self.url or not self.token:
            logger.error("HomeAssistant enabled but URL or longLivedToken is missing.")
            self._enabled = False
            return

        # Validate token format (should be at least 20 chars, typically much longer)
        if len(self.token) < 20:
            logger.error(
                "HomeAssistant longLivedToken appears invalid (too short). "
                "Generate a new token in Home Assistant: Profile → Long-Lived Access Tokens"
            )
            self._enabled = False
            return

        self.rest_base = f"{self.url}/api"
        self.ws_url = f"{self.url}/api/websocket"

        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
            }
        )

        self.carApiVehicles: List[HaVehicle] = []
        self._rest_lock = threading.Lock()

        logger.info("HomeAssistant vehicle module initialising, URL=%s", self.url)

        # Test connectivity before attempting discovery
        try:
            resp = self._session.get(f"{self.rest_base}/config", timeout=5)
            resp.raise_for_status()
            logger.debug("HomeAssistant connectivity verified")
        except requests.exceptions.ConnectionError:
            logger.error(
                "Cannot connect to Home Assistant at %s. Check URL and network connectivity.",
                self.url,
            )
            self._enabled = False
            return
        except requests.exceptions.HTTPError as e:
            if e.response.status_code == 401:
                logger.error(
                    "HomeAssistant authentication failed (401). "
                    "Check that longLivedToken is valid and has not expired."
                )
            else:
                logger.error(
                    "HomeAssistant returned HTTP %d. Check URL and token.",
                    e.response.status_code,
                )
            self._enabled = False
            return
        except Exception as e:
            logger.error("Failed to verify HomeAssistant connectivity: %s", str(e))
            self._enabled = False
            return

        try:
            self._discover_vehicles()
        except Exception:
            logger.exception("Failed to discover Tesla vehicles via Home Assistant")

        if not self.carApiVehicles:
            logger.warning("No Tesla vehicles discovered in Home Assistant.")
        else:
            for v in self.carApiVehicles:
                logger.info(
                    "Discovered Tesla: %s (slug=%s, VIN=%s)", v.name, v.slug, v.vin
                )

    def enabled(self) -> bool:
        return self._enabled

    # ----------------------------------------------------------------------
    # Discovery via WebSocket API
    # ----------------------------------------------------------------------

    def _discover_vehicles(self):
        loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            loop.run_until_complete(self._discover_vehicles_async())
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    async def _discover_vehicles_async(self):
        import ssl

        ws_url = self.ws_url.replace("http://", "ws://").replace("https://", "wss://")
        ssl_ctx = ssl.create_default_context() if ws_url.startswith("wss://") else None

        try:
            async with websockets.connect(
                ws_url,
                ssl=ssl_ctx,
                max_size=20 * 1024 * 1024,  # 20 MB
                close_timeout=10,
            ) as ws:
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_required":
                    raise RuntimeError(f"Unexpected: {msg}")

                await ws.send(json.dumps({"type": "auth", "access_token": self.token}))
                msg = json.loads(await ws.recv())
                if msg.get("type") != "auth_ok":
                    raise RuntimeError(f"Auth failed: {msg}")

                logger.info("Home Assistant WebSocket authenticated.")

                # Request device registry
                await ws.send(
                    json.dumps({"id": 1, "type": "config/device_registry/list"})
                )
                dev_msg = await self._await_result(ws, 1)
                devices = dev_msg.get("result") or []

                # Request entity registry
                await ws.send(
                    json.dumps({"id": 2, "type": "config/entity_registry/list"})
                )
                ent_msg = await self._await_result(ws, 2)
                entities = ent_msg.get("result") or []

                self._build_vehicles_from_registry(devices, entities)
        except asyncio.TimeoutError:
            logger.error(
                "Timeout connecting to Home Assistant WebSocket at %s. Check URL and network connectivity.",
                self.ws_url,
            )
        except Exception as e:
            logger.error(
                "Failed to discover vehicles via Home Assistant WebSocket: %s", str(e)
            )

    @staticmethod
    async def _await_result(ws, req_id: int) -> dict:
        while True:
            msg = json.loads(await ws.recv())
            if msg.get("type") == "result" and msg.get("id") == req_id:
                return msg

    def _build_vehicles_from_registry(self, devices, entities):
        ents_by_device: Dict[str, List[dict]] = {}
        for e in entities:
            dev_id = e.get("device_id")
            if dev_id:
                ents_by_device.setdefault(dev_id, []).append(e)

        logger.debug("Processing %d devices from Home Assistant registry", len(devices))

        for d in devices:
            manufacturer = (
                d.get("manufacturer") or d.get("default_manufacturer") or ""
            ).lower()
            if "tesla" not in manufacturer:
                continue

            model = d.get("model") or d.get("default_model") or ""
            if model and model not in TESLA_MODELS:
                logger.debug(
                    "Device '%s' has unsupported model '%s'",
                    d.get("name") or d.get("id"),
                    model,
                )
                continue

            vin = (
                d.get("serial_number")
                or self._extract_vin_from_identifiers(d.get("identifiers"))
                or ""
            )
            if not VIN_REGEX.fullmatch(vin):
                logger.debug(
                    "Device '%s' has invalid VIN: %s",
                    d.get("name") or d.get("id"),
                    vin,
                )
                continue

            name = (
                d.get("name_by_user") or d.get("name") or d.get("default_name") or vin
            )
            dev_entities = ents_by_device.get(d.get("id"), [])
            entity_ids = self._map_tesla_entities(name, dev_entities)

            if not entity_ids.get("battery_level_sensor"):
                logger.debug("Device '%s' missing battery_level_sensor entity", name)
                continue
            if not entity_ids.get("charge_switch"):
                logger.debug("Device '%s' missing charge_switch entity", name)
                continue

            self.carApiVehicles.append(HaVehicle(name, vin, d.get("id"), entity_ids))

    @staticmethod
    def _extract_vin_from_identifiers(identifiers):
        if not identifiers:
            return None
        for item in identifiers:
            try:
                domain, ident = item
            except (TypeError, ValueError):
                continue
            if ident and VIN_REGEX.fullmatch(str(ident)):
                return str(ident)
        return None

    def _map_tesla_entities(self, name, dev_entities):
        slug = _slugify_name(name)

        result = {
            "battery_level_sensor": None,
            "charging_sensor": None,
            "location_tracker": None,
            "charge_switch": None,
            "charge_current_number": None,
            "charge_limit_number": None,
        }

        for e in dev_entities:
            entity_id = e.get("entity_id", "")
            platform = e.get("platform") or e.get("domain") or ""
            if "tesla_fleet" not in platform:
                continue

            domain = entity_id.split(".", 1)[0]
            ent_l = entity_id.lower()

            if domain == "sensor" and ent_l.endswith(f"{slug}_battery_level"):
                result["battery_level_sensor"] = entity_id
            elif domain == "sensor" and ent_l.endswith(f"{slug}_charging"):
                result["charging_sensor"] = entity_id
            elif domain == "device_tracker" and ent_l.endswith(f"{slug}_location"):
                result["location_tracker"] = entity_id
            elif domain == "switch" and ent_l.endswith(f"{slug}_charge"):
                result["charge_switch"] = entity_id
            elif domain == "number" and ent_l.endswith(f"{slug}_charge_current"):
                result["charge_current_number"] = entity_id
            elif domain == "number" and ent_l.endswith(f"{slug}_charge_limit"):
                result["charge_limit_number"] = entity_id

        # Log which entities were found/missing for debugging
        missing = [k for k, v in result.items() if v is None]
        if missing:
            logger.debug(
                "Vehicle '%s' (slug=%s): missing entities: %s. Expected patterns: "
                "sensor.%s_battery_level, sensor.%s_charging, switch.%s_charge, "
                "number.%s_charge_current, number.%s_charge_limit, device_tracker.%s_location",
                name,
                slug,
                missing,
                slug,
                slug,
                slug,
                slug,
                slug,
                slug,
            )

        return result

    # ------------------------------------------------------------------
    # REST helpers
    # ------------------------------------------------------------------

    def _get_state(self, entity_id: str) -> Optional[dict]:
        url = f"{self.rest_base}/states/{entity_id}"
        with self._rest_lock:
            try:
                resp = self._session.get(url, timeout=5)
                resp.raise_for_status()
                return resp.json()
            except requests.exceptions.Timeout:
                logger.debug("HA: timeout reading state %s (5s)", entity_id)
                return None
            except requests.exceptions.HTTPError as e:
                if e.response.status_code == 404:
                    logger.debug("HA: entity not found: %s", entity_id)
                else:
                    logger.warning(
                        "HA: HTTP %d reading state %s",
                        e.response.status_code,
                        entity_id,
                    )
                return None
            except Exception as exc:
                logger.warning("HA: failed reading state %s: %s", entity_id, exc)
                return None

    def _call_service(self, domain: str, service: str, data: dict) -> bool:
        url = f"{self.rest_base}/services/{domain}/{service}"
        with self._rest_lock:
            try:
                resp = self._session.post(url, json=data, timeout=10)
                resp.raise_for_status()
                logger.debug("HA: service call %s.%s succeeded", domain, service)
                return True
            except requests.exceptions.Timeout:
                logger.warning("HA: timeout calling %s.%s (10s)", domain, service)
                return False
            except requests.exceptions.HTTPError as e:
                logger.warning(
                    "HA: HTTP %d calling %s.%s: %s",
                    e.response.status_code,
                    domain,
                    service,
                    e.response.text[:200],
                )
                return False
            except Exception as exc:
                logger.warning("HA: failed calling %s.%s: %s", domain, service, exc)
                return False

    def _is_vehicle_connected(self, vehicle: HaVehicle) -> bool:
        """
        Returns True if Home Assistant vehicle is currently plugged
        into any TWC in TWCManager.
        """
        try:
            twc = self.master.getTWCbyVIN(vehicle.vin)
            return twc is not None
        except Exception:
            return False

    # ------------------------------------------------------------------
    # TeslaAPI-compatible methods
    # ------------------------------------------------------------------

    def getCarApiVehicles(self):
        return self.carApiVehicles

    def getVehicleCount(self):
        return len(self.carApiVehicles)

    def car_api_available(self, *args, **kwargs):
        if not self._enabled or not self.carApiVehicles:
            return False

        ent = self.carApiVehicles[0].entity_ids.get("battery_level_sensor")
        if not ent:
            return False

        return self._get_state(ent) is not None

    def car_api_charge(self, charge: bool) -> str:
        now = time.time()
        if now - self.carApiLastStartOrStopChargeTime < 10:
            logger.debug("Charge command rate limited (10s minimum between commands)")
            return "error"

        if not self.car_api_available():
            logger.debug("Home Assistant API not available for charge command")
            return "error"

        desired = charge  # True = start, False = stop
        svc = "turn_on" if desired else "turn_off"
        action = "start" if desired else "stop"

        self.carApiLastStartOrStopChargeTime = now
        result = "success"
        vehicles_processed = 0

        for v in self.carApiVehicles:
            v.refresh_from_home_assistant(self)

            if not v.at_home:
                logger.debug("%s not at home; skipping charge %s", v.name, action)
                continue

            if not v.has_charge_switch:
                logger.warning("%s: charge_switch entity not available", v.name)
                result = "error"
                continue

            if not self._is_vehicle_connected(v):
                logger.debug("%s not connected; skipping charging change.", v.name)
                continue

            # Only act if state differs
            if v.charging == desired:
                logger.info(
                    "Skipping %s: already %s charging",
                    v.name,
                    "enabled" if desired else "disabled",
                )
                continue

            ent = v.entity_ids["charge_switch"]
            logger.info("Calling switch.%s on %s to %s charging", svc, ent, action)

            ok = self._call_service("switch", svc, {"entity_id": ent})
            if not ok:
                result = "error"
            else:
                vehicles_processed += 1

        if vehicles_processed == 0 and result == "success":
            logger.info("No vehicles processed for charge %s command", action)

        return result

    def applyChargeLimit(self, limit: int, *args, **kwargs):
        # Validate limit
        try:
            limit = int(limit)
        except (ValueError, TypeError):
            logger.warning("Invalid charge limit value: %s", limit)
            return "error"

        if limit != -1 and not (50 <= limit <= 100):
            logger.warning("Charge limit %d%% out of valid range [50, 100]", limit)
            return "error"

        if not self.car_api_available():
            logger.debug("Home Assistant API not available for charge limit")
            return "error"

        result = "success"
        vehicles_processed = 0

        for v in self.carApiVehicles:
            v.refresh_from_home_assistant(self)
            if not v.at_home:
                logger.debug("%s not at home; skipping charge limit", v.name)
                continue

            ent = v.entity_ids.get("charge_limit_number")
            if not ent:
                logger.debug("%s: charge_limit_number entity not available", v.name)
                continue

            if not self._is_vehicle_connected(v):
                logger.debug("%s not connected; skipping charge limit.", v.name)
                continue

            if limit == -1:
                logger.info("Leaving existing limit unchanged for %s", v.name)
                continue

            # Compare existing limit
            if v.charge_limit is not None and int(v.charge_limit) == int(limit):
                logger.info("Skipping %s: charge limit already %s%%", v.name, limit)
                continue

            logger.info("Setting charge limit of %s to %s%%", v.name, limit)

            ok = self._call_service(
                "number", "set_value", {"entity_id": ent, "value": limit}
            )
            if not ok:
                result = "error"
            else:
                vehicles_processed += 1

        if vehicles_processed == 0 and result == "success":
            logger.info("No vehicles processed for charge limit %d%% command", limit)

        return result

    def setChargeRate(
        self, amps: float, vehicle: Optional[HaVehicle] = None, *args, **kwargs
    ):
        if not self.carApiVehicles:
            logger.debug("No vehicles available for charge rate setting")
            return False

        vehicle = vehicle or self.carApiVehicles[0]

        if not vehicle.has_charge_current_number:
            logger.debug("%s: charge_current_number entity not available", vehicle.name)
            return False

        # Validate and clamp amps to reasonable range
        try:
            amps = float(amps)
        except (ValueError, TypeError):
            logger.warning("Invalid amps value: %s", amps)
            return False

        # Clamp to 1-32A range (typical Tesla charger limits)
        if amps < 1:
            logger.debug("Amps %.1f below minimum (1A), clamping to 1A", amps)
            amps = 1
        elif amps > 32:
            logger.warning(
                "Amps %.1f exceeds typical maximum (32A), clamping to 32A", amps
            )
            amps = 32

        # Pull latest charge_current_number state
        vehicle.refresh_from_home_assistant(self)

        if not self._is_vehicle_connected(vehicle):
            logger.debug("%s not connected; skipping charge rate.", vehicle.name)
            return True

        # Skip if current value already matches (rounded to 1 decimal)
        if vehicle.available_current is not None and round(
            float(vehicle.available_current), 1
        ) == round(float(amps), 1):
            logger.info(
                "Skipping %s: charge current already %.1f A", vehicle.name, amps
            )
            return True

        ent = vehicle.entity_ids["charge_current_number"]
        logger.info("Setting %s charge current to %.1f A", vehicle.name, amps)

        ok = self._call_service(
            "number",
            "set_value",
            {"entity_id": ent, "value": float(amps)},
        )

        if not ok:
            logger.warning("Failed to set charge rate for %s", vehicle.name)

        return ok

    # ------------------------------------------------------------------
    # Home/Location helpers
    # ------------------------------------------------------------------

    def is_location_within_radius(self, lat, lon, r):
        home_lat, home_lon = self.master.getHomeLatLon()
        return abs(home_lat - lat) <= r and abs(home_lon - lon) <= r

    def is_location_home(self, lat, lon):
        home_lat, home_lon = self.master.getHomeLatLon()

        if home_lat == 10000:
            logger.info("Adopting first seen vehicle location as 'home'.")
            self.master.setHomeLat(lat)
            self.master.setHomeLon(lon)
            self.master.queue_background_task({"cmd": "saveSettings"})
            return True

        feet = float(self.config.get("config", {}).get("atHomeRadius", 10560))
        deg = feet / 364488.888
        return self.is_location_within_radius(lat, lon, deg)

    # ------------------------------------------------------------------
    # Aggregate helpers
    # ------------------------------------------------------------------

    def updateChargeAtHome(self):
        for v in self.carApiVehicles:
            v.refresh_from_home_assistant(self)

    @property
    def numCarsAtHome(self):
        self.updateChargeAtHome()
        return len([v for v in self.carApiVehicles if v.at_home])

    @property
    def minBatteryLevelAtHome(self):
        self.updateChargeAtHome()
        levels = [
            v.battery_level
            for v in self.carApiVehicles
            if v.at_home and v.battery_level is not None
        ]
        return min(levels) if levels else 10000.0

    def updateSettings(self):
        return True
