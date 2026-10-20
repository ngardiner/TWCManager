import base64
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag
from hashlib import sha256
import logging
import psycopg2
import paho.mqtt.client as mqtt
import threading
import time
from typing import Dict, Optional

from TWCManager.Vehicle.Telemetry import TelmetryBase
from TWCManager.Logging.LoggerFactory import LoggerFactory


def fix_base64_padding(data):
    """Add Base64 padding characters."""
    if not data:
        return data

    # Convert memoryview/bytes to string but preserve the base64 content
    if isinstance(data, memoryview):
        data = data.tobytes()
    if isinstance(data, bytes):
        # Try to decode as UTF-8, but if it fails, it's likely raw base64 bytes
        try:
            data = data.decode("utf-8")
        except UnicodeDecodeError:
            # It's raw binary data - convert to base64 string
            data = base64.b64encode(data).decode("ascii")

    # Remove any whitespace
    data = data.strip()
    # Add padding if needed
    missing_padding = len(data) % 4
    if missing_padding:
        data += "=" * (4 - missing_padding)
    return data


logger = LoggerFactory.get_logger("TeslaMate", "Vehicle")


def parse_bool(payload):
    """True or False for a "true"/"false" payload, else None (unknown): an
    empty or odd payload must not read as unplugged."""
    value = str(payload).strip().lower()
    if value == "true":
        return True
    if value == "false":
        return False
    return None


def _optNum(a):
    # An empty or null payload is no reading
    return float(a) if a else None


class TeslaMateVehicle(TelmetryBase):
    __db_host = None
    __db_name = None
    __db_pass = None
    __db_user = None

    configName = "TeslaMate"
    syncTokens = False
    events = {
        "battery_level": ["batteryLevel", lambda a: int(a)],
        "charge_limit_soc": ["chargeLimit", lambda a: int(a)],
        "display_name": ["name", lambda a: str(a)],
        "latitude": ["lat", lambda a: float(a)],
        "longitude": ["lon", lambda a: float(a)],
        "state": ["state", lambda a: a],
        "time_to_full_charge": ["timeToFullCharge", lambda a: float(a)],
        "charger_pilot_current": ["availableCurrent", lambda a: int(a)],
        "charger_actual_current": ["actualCurrent", lambda a: int(a)],
        "charger_phases": ["phases", lambda a: int(a) if a else 0],
        "charger_voltage": ["voltage", lambda a: int(a)],
        "charging_state": ["chargingState", lambda a: str(a)],
        "plugged_in": ["pluggedIn", parse_bool],
        "charge_current_request_max": ["chargeCurrentRequestMax", _optNum],
    }

    def __init__(self, master):
        # TeslaMate car id -> VIN (from its database) and -> its "healthy"
        # topic. Set before the base class registers our liveness.
        self.carVINs: Dict[str, str] = {}
        self.healthy: Dict[str, bool] = {}
        super().__init__(master)

        if not self.status:
            return None

        # Configure database parameters
        self.__db_host = self.configModule.get("db_host", None)
        self.__db_name = self.configModule.get("db_name", None)
        self.__db_pass = self.configModule.get("db_pass", None)
        self.__db_user = self.configModule.get("db_user", None)
        self.syncTokens = self.configModule.get("syncTokens", False)
        self.encryption_key = self.configModule.get("encryption_key", None)

        # Identify cars by VIN where the database is configured; otherwise
        # they are matched by display name.
        self.carVINs = self.loadCarVINs()

        # If we're set to sync the auth tokens from the database, do this at startup
        if self.syncTokens:
            self.doSyncTokens(True)

            # After initial sync, keep resyncing the tokens every hour
            self.scheduleTokenResync()

    def loadCarVINs(self) -> Dict[str, str]:
        # TeslaMate car id -> VIN, from its cars table.
        if not (
            self.__db_host and self.__db_name and self.__db_user and self.__db_pass
        ):
            return {}
        try:
            conn = psycopg2.connect(
                host=self.__db_host,
                database=self.__db_name,
                user=self.__db_user,
                password=self.__db_pass,
                connect_timeout=5,
            )
        except Exception as e:
            # The error type only: a driver message can name the host or user.
            logger.info(
                "TeslaMate car VINs not loaded (%s); matching cars by name",
                type(e).__name__,
            )
            return {}
        try:
            cur = conn.cursor()
            try:
                cur.execute("SELECT id, vin FROM cars")
                rows = cur.fetchall()
            finally:
                cur.close()
        except Exception as e:
            logger.info(
                "TeslaMate car VINs not loaded (%s); matching cars by name",
                type(e).__name__,
            )
            return {}
        finally:
            conn.close()
        return {str(car_id): vin for car_id, vin in rows if car_id is not None and vin}

    def refreshIdentities(self):
        # A database that was down at startup: try again for VINs.
        if not self.carVINs:
            self.carVINs = self.loadCarVINs()

    def vinFor(self, vehicle_id) -> Optional[str]:
        return self.carVINs.get(str(vehicle_id))

    def noteHealthy(self, car_id, payload) -> None:
        # TeslaMate's per-car health: false when it has stopped polling.
        healthy = str(payload).strip().lower() == "true"
        if self.healthy.get(car_id, True) != healthy:
            logger.info(
                "TeslaMate reports car %s %s",
                car_id,
                "healthy again" if healthy else "unhealthy",
            )
        self.healthy[car_id] = healthy

    def carHealthy(self, vehicle) -> bool:
        if vehicle is None:
            return True
        for car_id, mapped in list(self.vehicles.items()):
            if mapped is vehicle:
                # Unpublished: assume healthy.
                return self.healthy.get(car_id, True)
        # Not attached (yet, or again after a forget): its car id by VIN if
        # known, else assume healthy, as for an unpublished healthy topic.
        for car_id, vin in list(self.carVINs.items()):
            if vin == vehicle.vin:
                return self.healthy.get(car_id, True)
        return True

    def scheduleTokenResync(self):
        resync = threading.Timer(3600, self.resyncTokens)
        resync.daemon = True
        resync.start()

    def resyncTokens(self):
        self.doSyncTokens()
        self.scheduleTokenResync()

    def decrypt_data(self, encrypted_data):
        """
        Decrypts data encrypted by TeslaMate's vault.ex.
        """
        if not self.encryption_key:
            logger.error("TeslaMate encryption key not found in config.json")
            return None

        if self.encryption_key == "chanegme":
            logger.error(
                "TeslaMate encryption key is still set to default placeholder 'chanegme'. Please set the correct encryption key."
            )

        if not encrypted_data:
            logger.error("No encrypted data provided for decryption")
            return None

        try:
            # 1. Get the raw Cloak blob. psycopg2 returns TeslaMate's bytea
            # columns as memoryview/bytes, already raw; only a text value
            # needs Base64 decoding.
            if isinstance(encrypted_data, (bytes, bytearray, memoryview)):
                decoded_data = bytes(encrypted_data)
            else:
                decoded_data = None
            try:
                if decoded_data is None:
                    decoded_data = base64.b64decode(encrypted_data)
            except Exception as e:
                logger.info(f"Base64 decode failed: {e}. Attempting to fix padding...")
                try:
                    # Fix Base64 padding
                    fixed_data = fix_base64_padding(encrypted_data)
                    decoded_data = base64.b64decode(fixed_data)
                    logger.info("Base64 decode successful after padding fix")
                except Exception as e2:
                    logger.error(
                        f"Base64 decode failed even after padding fix: {e2}. Data may be corrupted."
                    )
                    return None

            # Check minimum data length
            if len(decoded_data) < 12:
                logger.error(
                    f"Decoded data too short: {len(decoded_data)} bytes (minimum 12 required)"
                )
                return None

            # 2. Parse TeslaMate Cloak format: | Type | Length | Key Tag | IV | Ciphertag | Ciphertext |
            if len(decoded_data) < 3:
                logger.error(
                    f"Data too short for TeslaMate format: {len(decoded_data)} bytes"
                )
                return None

            # Parse the key tag header
            tag_type = decoded_data[0]
            tag_length = decoded_data[1]

            if len(decoded_data) < 2 + tag_length + 12 + 16:
                logger.error("Data too short for complete TeslaMate format")
                return None

            try:
                key_tag = decoded_data[2 : 2 + tag_length].decode("utf-8")
            except UnicodeDecodeError:
                logger.error("Invalid key tag in TeslaMate data")
                return None

            # Extract IV (nonce), ciphertag, and ciphertext
            iv_start = 2 + tag_length
            iv = decoded_data[iv_start : iv_start + 12]
            ciphertag = decoded_data[iv_start + 12 : iv_start + 12 + 16]
            ciphertext = decoded_data[iv_start + 12 + 16 :]

            # 3. Derive the key by hashing the user-provided key with SHA-256
            key = sha256(self.encryption_key.encode("utf-8")).digest()

            # 4. Decrypt using AES-256-GCM. Cloak's AES.GCM cipher always
            # authenticates the fixed AAD "AES256GCM", not the key tag;
            # the key tag is kept as a fallback for any data written the
            # other way.
            aesgcm = AESGCM(key)
            ciphertext_with_tag = ciphertext + ciphertag
            try:
                decrypted_data = aesgcm.decrypt(iv, ciphertext_with_tag, b"AES256GCM")
            except InvalidTag:
                decrypted_data = aesgcm.decrypt(
                    iv, ciphertext_with_tag, key_tag.encode("utf-8")
                )

            return decrypted_data.decode("utf-8")
        except Exception as e:
            logger.error(f"Error decrypting TeslaMate data: {e}")
            logger.error(
                "This usually indicates an incorrect encryption key or corrupted data in TeslaMate database"
            )
            return None

    # TeslaMate moved its tokens table into the "private" schema; older
    # installs still have it in public. Try the current location first.
    TOKEN_TABLES = ("private.tokens", "tokens")

    def fetchLatestTokens(self, conn, cur):
        for table in self.TOKEN_TABLES:
            try:
                cur.execute("SELECT access FROM " + table + " ORDER BY id DESC LIMIT 1")
            except psycopg2.errors.UndefinedTable:
                # A failed statement aborts the transaction; clear it
                # before trying the next location.
                conn.rollback()
                continue
            return cur.fetchone()
        logger.error("No tokens table found in the TeslaMate database.")
        return None

    def doSyncTokens(self, firstrun=False):
        # Connect to TeslaMate database and synchronize API tokens

        if self.__db_host and self.__db_name and self.__db_user and self.__db_pass:
            conn = None
            access_token = None

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

                try:
                    # Query DB for latest access and refresh token
                    result = self.fetchLatestTokens(conn, cur)

                    if result and result[0]:
                        access_token = self.decrypt_data(result[0])

                    if access_token:
                        carapi = self.master.getModuleByName("TeslaAPI")
                        if carapi:
                            # Access token only: a synced token expires at
                            # its own exp, and TeslaMate does the refreshing.
                            if carapi.applySyncedToken(access_token) == "success":
                                self.lastSync = time.time()
                                logger.log(
                                    logging.INFO,
                                    "Successfully synced tokens from TeslaMate database",
                                )
                        else:
                            logger.error("TeslaAPI module not found")
                    else:
                        logger.error("No tokens found in TeslaMate database.")

                except Exception as e:
                    logger.error(f"Error querying TeslaMate database: {e}")

                finally:
                    cur.close()
                    conn.close()
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
        super().mqttConnect(client, userdata, flags, rc, properties)
        if not self.connected:
            return
        subscription = "teslamate/"
        if self.mqtt_prefix:
            subscription += self.mqtt_prefix + "/"
        subscription += "cars/+/"
        for topic in list(self.events.keys()) + ["geofence", "healthy"]:
            topic = subscription + topic
            logger.log(logging.INFO5, "Subscribe to " + topic)
            res = client.subscribe(topic, qos=1)
            logger.log(logging.INFO5, "Res: " + str(res))

    def mqttMessage(self, client, userdata, message):
        topic = str(message.topic).split("/")
        payload = str(message.payload.decode("utf-8"))

        if len(topic) > 4:
            prefix = "/".join(topic[1:-3])
            if prefix != self.mqtt_prefix:
                return
            topic = [topic[0]] + topic[-3:]

        if topic[0] == "teslamate" and topic[1] == "cars":
            if topic[3] == "healthy":
                self.noteHealthy(topic[2], payload)
            elif topic[3] == "geofence":
                # Logged only: no decision reads it
                self.logChange(
                    topic[2],
                    self.vehicleNames.get(topic[2], topic[2]),
                    "geofence",
                    payload,
                )
            else:
                self.applyDataToVehicle(
                    topic[2],
                    topic[3],
                    payload,
                    getattr(message, "retain", False) is True,
                )
