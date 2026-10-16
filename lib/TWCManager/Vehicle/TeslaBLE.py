import base64
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
import functools
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import threading
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory
from TWCManager.Vehicle.Providers.TeslaBLEProvider import TeslaBLEProvider
from TWCManager.Vehicle.Reconcile.Outcome import (
    DONE,
    ERROR,
    REFUSED,
    SATISFIED,
    UNREACHABLE,
    Outcome,
)
from TWCManager.Vehicle.Registry.Sources import TeslaBLEReadSource
from TWCManager.Vehicle.Registry.VIN import isWellFormedVIN
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

logger = LoggerFactory.get_logger("TeslaBLE", "Vehicle")


def _serialized(method):
    """Hold the instance's BLE lock for the whole call.

    Every tesla-control invocation writes key material to the shared
    pipeName path, runs, and unlinks it. Overlapping invocations delete each
    other's key file and contend for the adapter, so each "write key -> run ->
    unlink" unit must run alone, whatever thread it is called from.
    """

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._bleLock:
            return method(self, *args, **kwargs)

    return wrapper


class BLECircuitBreaker:
    """Circuit breaker pattern to prevent thrashing on persistent failures."""

    def __init__(self, failure_threshold=5, recovery_timeout=60):
        self.failure_threshold = failure_threshold
        self.recovery_timeout = recovery_timeout
        self.failures = {}  # {vin: count}
        self.failure_times = {}  # {vin: timestamp}

    def record_failure(self, vin):
        """Record a failure for a vehicle."""
        now = time.time()
        self.failures[vin] = self.failures.get(vin, 0) + 1
        self.failure_times[vin] = now

    def record_success(self, vin):
        """Record a success for a vehicle, reset failure count."""
        self.failures[vin] = 0

    def is_open(self, vin):
        """Check if circuit is open (stop retrying for this vehicle)."""
        if vin not in self.failures:
            return False

        if self.failures[vin] < self.failure_threshold:
            return False

        # Check if recovery timeout has passed
        now = time.time()
        time_since_failure = now - self.failure_times.get(vin, now)

        if time_since_failure > self.recovery_timeout:
            # Try again (half-open state)
            return False

        return True

    def get_status(self, vin):
        """Get circuit breaker status for a vehicle."""
        if vin not in self.failures:
            return "closed"  # Normal operation

        if self.is_open(vin):
            return "open"  # Stop retrying

        return "half-open"  # Trying to recover


class TeslaBLE:
    binaryPath = None
    commandTimeout = 5
    config = None
    configConfig = None
    isDockerCached = None
    master = None
    pipe = None
    enabled = True
    pipeName = "/tmp/ble_data"
    pipeOpen = False  # Track pipe state

    # tesla-control's ErrKeyNotPaired (vehicle-command pkg/protocol/error.go)
    KEY_REFUSED_TEXT = "your public key has not been paired with the vehicle"
    # After a pairing request reaches the car, the owner has this long to
    # tap the key card; a key refusal inside it is expected, not a refusal.
    PAIRING_WINDOW = 600
    # bluetoothctl listens this long per range scan.
    SCAN_SECONDS = 8
    # A request not served in this long no longer shows as "Scanning...".
    SCAN_PENDING_MAX = 120
    # A request for a car scanned (or being scanned) this recently is ignored.
    SCAN_DEBOUNCE = 10
    # One bleScan task drains requests at most this many times; anything left
    # is picked up by requeueIfRequested after the task finishes.
    SCAN_DRAIN_PASSES = 3
    # bluetoothctl output meaning the scan never ran (no adapter, bluetoothd
    # down, discovery refused); compared case-insensitively.
    SCAN_ERROR_TEXT = (
        "no default controller",
        "waiting to connect to bluetoothd",
        "failed to start discovery",
    )
    STATUS_LABELS = {
        "pairing": "Pairing (tap your key card in the car)",
        "paired": "Paired",
        "pairing_failed": "Pairing failed",
        "refused": "Key refused (re-pair)",
    }

    def __init__(self, master):
        self.registry = VehicleRegistry.forMaster(master)
        # Set once the binary is found; the provider reads through it
        self.readSource = None
        self._bleLock = threading.Lock()
        # Last pairing attempt per VIN: transient, for the vehicle page.
        self._pairingAttempts = {}
        # Last range scan per VIN: {"inRange", "at"}; transient.
        self._rangeScan = {}
        # VIN -> request time for scans not yet run. Guarded by its own lock,
        # never held while taking _bleLock.
        self._scanRequests = {}
        # VINs a drain has taken and not finished scanning; same lock.
        self._scanInProgress = set()
        self._scanLock = threading.Lock()
        # Set by _sendCommand_internal on a key refusal so the retry loop
        # (same thread) stops: a refused key is refused again on retry.
        self._keyRefusal = threading.local()
        self.master = master
        try:
            self.config = self.master.config
        except KeyError:
            self.config = {}
        self.configConfig = self.config.get("config", {})
        cfg = self.config.get("vehicle", {}).get("teslaBLE", {}) or {}
        self._enabled = cfg.get("enabled", True)

        # Load BLE-specific configuration with enhanced defaults
        ble_config = self.configConfig.get("moduleConfiguration", {}).get(
            "TeslaBLE", {}
        )

        # Enhanced configuration parameters
        self.commandTimeout = ble_config.get("commandTimeout", 5)
        self.maxRetries = ble_config.get("maxRetries", 3)
        self.retryDelay = ble_config.get("retryDelay", 2)
        self.processGroupManagement = ble_config.get("processGroupManagement", True)
        self.dockerCompatibility = ble_config.get("dockerCompatibility", True)
        self.statsReporting = ble_config.get("statsReporting", True)

        # Circuit breaker configuration
        circuit_breaker_threshold = ble_config.get("circuitBreakerThreshold", 5)
        circuit_breaker_timeout = ble_config.get("circuitBreakerTimeout", 60)
        self.circuit_breaker = BLECircuitBreaker(
            circuit_breaker_threshold, circuit_breaker_timeout
        )

        # Retry statistics tracking
        self.retry_stats = {}

        # Persistent D-Bus session daemon shared across all tesla-control calls.
        # dbus-launch only spawns a new daemon when DBUS_SESSION_BUS_ADDRESS is
        # absent from the environment. By starting one daemon here and passing its
        # address to every subprocess, we prevent tesla-control's internal
        # dbus-launch from spawning a new daemon on each invocation.
        self._dbus_session_address = None
        self._dbus_session_pid = None
        self._start_dbus_session()

        logger.info(
            f"BLE module initialized with timeout={self.commandTimeout}s, retries={self.maxRetries}, "
            f"circuit_breaker_threshold={circuit_breaker_threshold}"
        )

        # Clean up any stale pipe file from previous crashes
        self._cleanup_stale_pipe()

        # Determine best binary location
        self.binaryPath = self.configConfig.get(
            "teslaControlPath", os.path.expanduser("~/gobin/tesla-control")
        )

        # Failing this, search system path
        if not self.binaryPath or not os.path.isfile(self.binaryPath):
            self.binaryPath = shutil.which("tesla-control")

        # Check that binary exists and is executable, otherwise unload
        if not self.binaryPath or not os.path.isfile(self.binaryPath):
            logger.error(
                "tesla-control binary not found - BLE module will be disabled. "
                "Set vehicle.teslaBLE.enabled=false in config to suppress this module."
            )
            self.master.releaseModule("lib.TWCManager.Vehicle", "TeslaBLE")
            return
        elif not os.access(self.binaryPath, os.X_OK):
            logger.error(
                f"tesla-control binary at {self.binaryPath} is not executable - "
                "BLE module will be disabled. Check file permissions or set "
                "vehicle.teslaBLE.enabled=false in config to suppress this module."
            )
            self.master.releaseModule("lib.TWCManager.Vehicle", "TeslaBLE")
            return
        else:
            logger.info(f"tesla-control binary found at: {self.binaryPath}")

        # Reads cascade BLE first (local and free), then the cloud.
        self.readSource = TeslaBLEReadSource(self, master)
        self.registry.registerReadSource(self.readSource)

    def providers(self):
        return [TeslaBLEProvider(self, self.master)]

    def _scheduleBlocksStart(self, vin):
        # Don't override the car's own Scheduled Charging / Departure timer
        # with a BLE start command (the reconciler's handsOff is the same
        # rule for every provider). Schedule state comes from the TeslaAPI
        # module; without it (BLE-only installs) we have no schedule data and
        # allow the command.
        if not self.master.config["config"].get("respectVehicleSchedule", True):
            return False
        teslaapi = self.master.getModuleByName("TeslaAPI")
        if not teslaapi:
            return False
        try:
            return teslaapi.vehicleScheduledChargingPending(vin)
        except Exception:
            return False

    def _is_already_satisfied(self, output):
        """Return True if output indicates the car is already in the desired charge state."""
        return self._satisfiedReason(output) is not None

    def _satisfiedReason(self, output):
        """The car's reason word when output says it is already in the
        desired state, else None."""
        if not output:
            return None
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="ignore")
        output_lower = output.lower()
        for reason in (
            "complete",
            "is_charging",
            "charging",
            "requested",
            "disconnected",
            "already_set",
        ):
            if ("car could not execute command: " + reason) in output_lower:
                return reason
        return None

    def parseCommandOutput(self, output):
        """
        Enhanced command output parsing with detailed error categorization.
        Returns True for success, False for failure.
        """
        if not output:
            logger.debug("BLE command returned empty output")
            return False

        # Convert to string if needed
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="ignore")

        # Success indicators
        success_indicators = [
            "Updated session info for DOMAIN_VEHICLE_SECURITY",
            "Command executed successfully",
            "Vehicle responded",
            "Success",
            "ok",
        ]

        output_lower = output.lower()

        # Check for success
        for indicator in success_indicators:
            if indicator.lower() in output_lower:
                logger.debug(f"BLE command success: {indicator}")
                return True

        if self._is_already_satisfied(output):
            logger.debug("BLE command success: already in desired state")
            return True

        error_type = self._errorCategory(output)
        logger.debug(
            f"BLE command failed - Error type: {error_type}, Output: {output[:100]}..."
        )
        return False

    def _errorCategory(self, output):
        """The failure category for command output parseCommandOutput
        rejects; "unknown" when nothing matches."""
        if not output:
            return "unknown"
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="ignore")
        output_lower = output.lower()
        # Categorize errors for detailed logging
        error_indicators = {
            "timeout": ["timeout", "timed out", "no response"],
            "connection": ["connection failed", "unable to connect", "bluetooth error"],
            "authentication": ["authentication failed", "invalid key", "unauthorized"],
            "vehicle_unavailable": [
                "vehicle not found",
                "vehicle offline",
                "not available",
            ],
            "command_failed": ["command failed", "error executing", "operation failed"],
        }
        for category, indicators in error_indicators.items():
            for indicator in indicators:
                if indicator in output_lower:
                    return category
        return "unknown"

    def commandOutcome(self, vin, command, args=None):
        """One command through sendCommand (with its retries and circuit
        breaker), classified. Never touches the claim: a key refusal has
        already been recorded through _setBleStatus, which keeps the claim
        inside the pairing window and drops it outside."""
        vin = str(vin).upper()
        self._keyRefusal.refused = False
        if args is None:
            output = self.sendCommand(vin, command)
        else:
            output = self.sendCommand(vin, command, args)
        return self._classify(vin, output)

    def _classify(self, vin, output):
        if getattr(self._keyRefusal, "refused", False):
            if self._inPairingWindow(vin):
                # The owner has not tapped the key card yet: the claim is
                # kept, and the next try may prove the key
                return Outcome(ERROR, "pairing")
            return Outcome(REFUSED, "key")
        if self._storedStatus(vin)["status"] == "refused":
            return Outcome(REFUSED, "key")
        if output is None:
            return Outcome(UNREACHABLE, "no reply")
        reason = self._satisfiedReason(output)
        if reason is not None:
            return Outcome(SATISFIED, reason)
        if self.parseCommandOutput(output):
            return Outcome(DONE)
        return Outcome(ERROR, self._errorCategory(output))

    @_serialized
    def peerWithVehicle(self, vin):
        """
        Pair with a vehicle using BLE key exchange.
        Returns True on success, False on failure.
        """
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            logger.error("BLE pairing refused: not a well-formed VIN")
            return False
        vin = self._settingsKey(vin)
        success = False
        reason = "Pairing request failed"
        try:
            logger.info(f"Initiating BLE pairing with vehicle {vin}")

            if (
                not self.binaryPath
                or not os.path.isfile(self.binaryPath)
                or not os.access(self.binaryPath, os.X_OK)
            ):
                logger.error(
                    "tesla-control binary not available or not executable for pairing"
                )
                reason = "tesla-control is not available"
                return False

            # Send public key before pairing
            if not self.sendPublicKey(vin):
                logger.error(f"Failed to send public key for pairing with {vin}")
                reason = "Could not prepare the public key"
                return False

            command_string = [
                self.binaryPath,
                "-debug",
                "-ble",
                "-vin",
                vin,
                "add-key-request",
                self.pipeName,
                "owner",
                "cloud_key",
            ]

            if self.isDocker():
                command_string.insert(0, "nsenter --net=/rootns/net ")

            # Use improved timeout handling (pairing takes longer)
            stdout, stderr, return_code = self._run_command_with_timeout(
                command_string,
                timeout=30,  # Pairing can take longer
                use_process_group=True,
            )

            if stdout is None and stderr is None:
                logger.error(f"BLE pairing with {vin} timed out or failed")
                reason = "Pairing request timed out; is the car in range?"
                return False

            # Check both stdout and stderr for pairing result
            output = (stderr.decode("utf-8") if stderr else "") + (
                stdout.decode("utf-8") if stdout else ""
            )
            success = self.parseCommandOutput(output)

            logger.info(f"BLE pairing with {vin}: {'success' if success else 'failed'}")
            if not success:
                logger.debug(f"Pairing output: {output[:200]}...")
                reason = "The car did not accept the pairing request"

            return success

        except Exception as e:
            logger.error(f"peerWithVehicle exception for {vin}: {e}")
            success = False
            return False
        finally:
            # Always ensure pipe is closed after pairing attempt
            self._ensure_pipe_closed()
            self._safeNote(self._notePairingResult, vin, success, reason)

    def sendCommand(self, vin, command, args=None):
        """
        Enhanced sendCommand with improved error handling and timeout management.
        Returns command output string or None on failure.
        """
        # Accept either a VIN string or a vehicle object with a .VIN attribute
        if hasattr(vin, "VIN"):
            vin = vin.VIN
        return self._execute_with_retry(self._sendCommand_internal, vin, command, args)

    @_serialized
    def _sendCommand_internal(self, vin, command, args=None):
        """
        Internal sendCommand implementation (called by retry wrapper).
        Returns command output string or None on failure.
        """
        self._keyRefusal.refused = False
        try:
            # Validate inputs
            if not vin or not command:
                logger.error("sendCommand called with invalid vin or command")
                return None
            vin = self._settingsKey(vin)

            if (
                not self.binaryPath
                or not os.path.isfile(self.binaryPath)
                or not os.access(self.binaryPath, os.X_OK)
            ):
                logger.error("tesla-control binary not available or not executable")
                return None

            # Check if vehicle exists in settings
            if not self.master.settings.get("Vehicles", {}).get(vin):
                logger.error(f"Vehicle {vin} not found in settings")
                return None

            logger.debug(
                f"BLE sendCommand: {command} to {vin}"
                + (f" with args {args}" if args else "")
            )

            # Send private key before command
            if not self.sendPrivateKey(vin):
                logger.warning(
                    f"Failed to send private key for {vin}, proceeding anyway"
                )

            command_string = [
                self.binaryPath,
                "-debug",
                "-ble",
                "-vin",
                vin,
                "-key-file",
                self.pipeName,
                command,
            ]
            if self.isDocker():
                command_string.insert(0, "nsenter --net=/rootns/net ")

            if args:
                command_string.append(str(args))

            # Use improved timeout handling with process group management
            stdout, stderr, return_code = self._run_command_with_timeout(
                command_string, timeout=self.commandTimeout, use_process_group=True
            )

            if stdout is None and stderr is None:
                logger.debug(f"BLE command '{command}' timed out or failed")
                return None

            # Check if process was killed due to timeout
            if return_code == -9:  # SIGKILL
                logger.warning(
                    f"BLE command '{command}' timed out after {self.commandTimeout}s"
                )
                return None
            output = stderr.decode("utf-8") if stderr else ""

            if return_code != 0:
                if self._is_already_satisfied(output):
                    # Car rejected the command because it's already in the
                    # desired state. That's a success, not something to
                    # retry - retrying would just get the same rejection.
                    logger.debug(
                        f"BLE command '{command}' already satisfied: {output[:200]}"
                    )
                    # The car executed and answered a signed command.
                    self._safeNote(self._noteBleProof, vin)
                    return output

                # The -debug output carries session detail: DEBUG only
                logger.warning(
                    f"BLE command '{command}' failed with return code "
                    f"{return_code}: {self._errorCategory(output)}"
                )
                logger.debug(f"BLE command '{command}' error output: {output[:200]}")
                if self._isKeyRefusal(output):
                    self._keyRefusal.refused = True
                    self._safeNote(self._noteBleRefusal, vin, output)
                return None

            logger.debug(
                f"BLE command output: {output[:200]}..."
                if len(output) > 200
                else output
            )
            self._safeNote(self._noteBleProof, vin)
            return output or "ok"

        except Exception as e:
            logger.error(f"sendCommand exception: {e}")
            return None
        finally:
            # Always ensure pipe is closed after command
            self._ensure_pipe_closed()

    def scanForVehicles(self):
        # This function allows other modules to prompt us to connect to BLE
        # vehicles.

        # Ensure we have a Private Key defined for each known vehicle
        if self.master.settings.get("Vehicles", None):
            for vehicle in list(self.master.settings["Vehicles"].keys()):
                if not "privKey" in self.master.settings["Vehicles"][vehicle]:
                    logger.log(
                        logging.INFO2,
                        "Vehicle "
                        + str(vehicle)
                        + " has no Private Key defined for BLE. Creating one.",
                    )
                    private_key = ec.generate_private_key(
                        ec.SECP256R1(), default_backend()
                    )
                    with self.master.settingsLock:
                        self.master.settings["Vehicles"][vehicle]["privKey"] = (
                            base64.b64encode(
                                private_key.private_bytes(
                                    serialization.Encoding.PEM,
                                    serialization.PrivateFormat.PKCS8,
                                    serialization.NoEncryption(),
                                )
                            ).decode()
                        )
                        public_key = private_key.public_key()
                        self.master.settings["Vehicles"][vehicle]["pubKey"] = (
                            base64.b64encode(
                                public_key.public_bytes(
                                    serialization.Encoding.X962,
                                    serialization.PublicFormat.UncompressedPoint,
                                )
                            ).decode()
                        )
                        self.master.settings["Vehicles"][vehicle]["pubKeyPEM"] = (
                            base64.b64encode(
                                public_key.public_bytes(
                                    serialization.Encoding.PEM,
                                    serialization.PublicFormat.SubjectPublicKeyInfo,
                                )
                            ).decode()
                        )
                    self.master.queue_background_task({"cmd": "saveSettings"})
        else:
            logger.log(logging.INFO2, "No known vehicles.")

    def wakeVehicle(self, vin):
        """
        Wake a vehicle from sleep mode.
        Returns True on success, False on failure.
        """
        try:
            logger.debug(f"Waking vehicle {vin}")

            ret = self.sendCommand(vin, "wake")
            if ret is None:
                logger.debug(f"Failed to send wake command to {vin}")
                return False

            success = self.parseCommandOutput(ret)
            logger.debug(f"Wake {vin}: {'success' if success else 'failed'}")
            return success

        except Exception as e:
            logger.error(f"wakeVehicle exception for {vin}: {e}")
            return False

    def _extract_enum_value(self, val):
        """Extract string key from Tesla protobuf enum format like {"Charging": {}}."""
        if isinstance(val, dict) and val:
            return next(iter(val))
        return val

    def _parse_proto_timestamp(self, val):
        """Parse a protobuf Timestamp ({"seconds":…} or bare number) to Unix epoch."""
        if val is None:
            return None
        if isinstance(val, (int, float)):
            return float(val)
        if isinstance(val, dict):
            return float(val.get("seconds", 0)) or None
        return None

    @_serialized
    def _fetch_state_json(self, vin, state_type):
        """Run `tesla-control state STATE_TYPE` and return parsed JSON dict, or None."""
        if not self.binaryPath or not os.path.isfile(self.binaryPath):
            return None
        vin = self._settingsKey(vin)
        if not self.master.settings.get("Vehicles", {}).get(vin):
            return None

        self.sendPrivateKey(vin)

        command_string = [
            self.binaryPath,
            "-ble",
            "-vin",
            vin,
            "-key-file",
            self.pipeName,
            "state",
            state_type,
        ]
        if self.isDocker():
            command_string.insert(0, "nsenter --net=/rootns/net")

        try:
            stdout, stderr, return_code = self._run_command_with_timeout(
                command_string, timeout=self.commandTimeout
            )
            if stdout is None or return_code != 0:
                logger.debug(
                    f"BLE state {state_type} failed for {vin} (rc={return_code})"
                )
                if stderr:
                    self._safeNote(
                        self._noteBleRefusal,
                        vin,
                        stderr.decode("utf-8", errors="ignore"),
                    )
                return None
            state = json.loads(stdout.decode("utf-8"))
            self._safeNote(self._noteBleProof, vin)
            return state
        except (json.JSONDecodeError, UnicodeDecodeError, TypeError) as e:
            logger.debug(f"BLE state {state_type} JSON parse error for {vin}: {e}")
            return None
        finally:
            self._ensure_pipe_closed()

    def get_charge_state(self, vin):
        """Fetch charge state via BLE. Returns normalized dict or None on failure."""
        raw = self._fetch_state_json(vin, "charge")
        if raw is None:
            return None
        cs = raw.get("chargeState")
        if not cs:
            logger.debug(f"BLE state charge: no chargeState in response for {vin}")
            return None
        return {
            "batteryLevel": cs.get("batteryLevel"),
            "chargeLimit": cs.get("chargeLimitSoc"),
            "chargingState": self._extract_enum_value(cs.get("chargingState")),
            "availableCurrent": cs.get("chargerPilotCurrent"),
            "chargeCurrentRequest": cs.get("chargeCurrentRequest"),
            "actualCurrent": cs.get("chargerActualCurrent"),
            "voltage": cs.get("chargerVoltage"),
            "phases": cs.get("chargerPhases"),
            "scheduledChargingPending": cs.get("scheduledChargingPending"),
            "timeToFullCharge": cs.get("timeToFullCharge"),
        }

    def get_location_state(self, vin):
        """Fetch location state via BLE. Returns normalized dict or None on failure."""
        raw = self._fetch_state_json(vin, "location")
        if raw is None:
            return None
        ls = raw.get("locationState")
        if not ls:
            logger.debug(f"BLE state location: no locationState in response for {vin}")
            return None
        return {
            "latitude": ls.get("latitude"),
            "longitude": ls.get("longitude"),
            "heading": ls.get("heading"),
            "gpsAsOf": self._parse_proto_timestamp(ls.get("gpsAsOf")),
        }

    def _is_transient_error(self, output):
        """Determine if error is transient (should retry) or permanent (fail fast)."""
        if output is None:
            return True  # Timeout or communication error - transient

        output_lower = output.lower()

        # Transient error indicators
        transient_indicators = [
            "timeout",
            "timed out",
            "connection refused",
            "connection reset",
            "connection timeout",
            "device busy",
            "resource temporarily unavailable",
            "try again",
            "retry",
            "temporarily",
            "broken pipe",
            "no such file or directory",
        ]

        for indicator in transient_indicators:
            if indicator in output_lower:
                logger.debug(f"Detected transient error: {indicator}")
                return True

        return False

    def _is_transient_exception(self, exception):
        """Determine if exception is transient (should retry) or permanent (fail fast)."""
        exc_str = str(exception).lower()

        transient_indicators = [
            "timeout",
            "timed out",
            "connection",
            "broken pipe",
            "resource temporarily unavailable",
            "device busy",
            "try again",
        ]

        for indicator in transient_indicators:
            if indicator in exc_str:
                logger.debug(f"Detected transient exception: {indicator}")
                return True

        return False

    def _calculate_backoff(self, attempt):
        """Calculate exponential backoff delay for retry."""
        # Exponential backoff: 2^attempt * retryDelay
        delay = (2**attempt) * self.retryDelay

        # Cap at reasonable maximum (30 seconds)
        max_delay = 30
        return min(delay, max_delay)

    def _init_retry_stats(self, vin):
        """Initialize retry statistics for a vehicle."""
        if vin not in self.retry_stats:
            self.retry_stats[vin] = {
                "total_attempts": 0,
                "successful_retries": 0,
                "failed_retries": 0,
                "circuit_breaker_trips": 0,
                "last_error": None,
                "last_error_time": None,
            }

    def _record_retry_attempt(self, vin, success, error=None):
        """Record a retry attempt for statistics."""
        self._init_retry_stats(vin)
        stats = self.retry_stats[vin]

        stats["total_attempts"] += 1
        if success:
            stats["successful_retries"] += 1
        else:
            stats["failed_retries"] += 1

        if error:
            stats["last_error"] = error
            stats["last_error_time"] = time.time()

    def _execute_with_retry(self, func, vin, *args, **kwargs):
        """
        Execute a function with retry logic, exponential backoff, and circuit breaker.

        Args:
            func: Function to execute
            vin: Vehicle VIN (for circuit breaker tracking)
            *args, **kwargs: Arguments to pass to func

        Returns:
            Result from func, or None if all retries exhausted
        """
        self._init_retry_stats(vin)

        # Check circuit breaker
        if self.circuit_breaker.is_open(vin):
            logger.warning(f"Circuit breaker OPEN for {vin}, skipping retry logic")
            self.retry_stats[vin]["circuit_breaker_trips"] += 1
            return None

        for attempt in range(self.maxRetries + 1):
            try:
                self._keyRefusal.refused = False
                result = func(vin, *args, **kwargs)

                if result is None and getattr(self._keyRefusal, "refused", False):
                    # Not transient: the car refuses this key until re-paired.
                    logger.debug(f"BLE key refused by {vin}; not retrying")
                    return None

                if result is not None:
                    # Success
                    self.circuit_breaker.record_success(vin)
                    if attempt > 0:
                        logger.info(
                            f"BLE command succeeded on retry attempt {attempt + 1}/{self.maxRetries + 1} for {vin}"
                        )
                        self._record_retry_attempt(vin, True)
                    return result

                # Result is None - check if error is transient
                if attempt < self.maxRetries:
                    delay = self._calculate_backoff(attempt)
                    logger.info(
                        f"BLE command failed for {vin}, retrying in {delay}s "
                        f"(attempt {attempt + 1}/{self.maxRetries + 1})"
                    )
                    self._record_retry_attempt(vin, False, "transient_error")
                    time.sleep(delay)
                else:
                    # All retries exhausted
                    self.circuit_breaker.record_failure(vin)
                    logger.error(
                        f"BLE command failed for {vin} after {self.maxRetries + 1} attempts"
                    )
                    self._record_retry_attempt(vin, False, "max_retries_exceeded")
                    return None

            except Exception as e:
                if self._is_transient_exception(e):
                    if attempt < self.maxRetries:
                        delay = self._calculate_backoff(attempt)
                        logger.warning(
                            f"BLE transient exception for {vin}: {e}, "
                            f"retrying in {delay}s (attempt {attempt + 1}/{self.maxRetries + 1})"
                        )
                        self._record_retry_attempt(
                            vin, False, f"transient_exception: {str(e)[:50]}"
                        )
                        time.sleep(delay)
                    else:
                        # All retries exhausted
                        self.circuit_breaker.record_failure(vin)
                        logger.error(
                            f"BLE transient exception for {vin} after {self.maxRetries + 1} attempts: {e}"
                        )
                        self._record_retry_attempt(
                            vin, False, f"exception_max_retries: {str(e)[:50]}"
                        )
                        return None
                else:
                    # Permanent error - don't retry
                    logger.error(f"BLE permanent error for {vin}, not retrying: {e}")
                    self._record_retry_attempt(
                        vin, False, f"permanent_error: {str(e)[:50]}"
                    )
                    return None

        return None

    def _kill_process_group(self, pid, timeout=1.0):
        """
        Kill a process and its entire process group gracefully.
        First tries SIGTERM, then SIGKILL if process doesn't die.

        Args:
            pid: Process ID to kill
            timeout: Time to wait between SIGTERM and SIGKILL

        Returns:
            True if process was killed, False if already dead
        """
        try:
            # Check if process is still alive
            if os.waitpid(pid, os.WNOHANG)[0] == 0:
                # Process is still running, try graceful shutdown first
                try:
                    # Get process group ID
                    pgid = os.getpgid(pid)

                    # Try SIGTERM first (graceful)
                    logger.debug(f"Sending SIGTERM to process group {pgid}")
                    os.killpg(pgid, signal.SIGTERM)

                    # Wait for graceful shutdown
                    time.sleep(timeout)

                    # Check if process is still alive
                    if os.waitpid(pid, os.WNOHANG)[0] == 0:
                        # Still alive, use SIGKILL (brutal)
                        logger.warning(
                            f"Process {pid} didn't respond to SIGTERM, sending SIGKILL"
                        )
                        os.killpg(pgid, signal.SIGKILL)
                        time.sleep(0.1)

                    logger.debug(f"Process group {pgid} terminated")
                    return True
                except ProcessLookupError:
                    # Process already dead
                    logger.debug(f"Process {pid} already terminated")
                    return False
            else:
                logger.debug(f"Process {pid} already dead")
                return False
        except Exception as e:
            logger.warning(f"Error killing process group: {e}")
            return False

    def _start_dbus_session(self):
        """Start a single persistent D-Bus session daemon for this module instance.

        Parses the output of dbus-launch --sh-syntax to obtain the bus address
        and daemon PID, then stores them for use by all subprocess invocations.
        If dbus-launch is unavailable the module continues without a managed
        session (tesla-control will still work, but may still accumulate daemons).
        """
        dbus_launch = shutil.which("dbus-launch")
        if not dbus_launch:
            logger.debug(
                "dbus-launch not found; skipping persistent D-Bus session setup"
            )
            return
        try:
            result = subprocess.run(
                [dbus_launch, "--sh-syntax"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=5,
            )
            for line in result.stdout.decode(errors="replace").splitlines():
                line = line.strip().rstrip(";")
                if line.startswith("DBUS_SESSION_BUS_ADDRESS="):
                    self._dbus_session_address = line.split("=", 1)[1].strip("'\"")
                elif line.startswith("DBUS_SESSION_BUS_PID="):
                    try:
                        self._dbus_session_pid = int(line.split("=", 1)[1].strip("'\""))
                    except ValueError:
                        pass
            if self._dbus_session_address:
                logger.debug(
                    f"D-Bus session started (PID {self._dbus_session_pid}): "
                    f"{self._dbus_session_address}"
                )
            else:
                logger.warning(
                    "dbus-launch ran but produced no DBUS_SESSION_BUS_ADDRESS"
                )
        except Exception as e:
            logger.warning(f"Could not start persistent D-Bus session: {e}")

    def _stop_dbus_session(self):
        """Terminate the persistent D-Bus session daemon started by this module."""
        if self._dbus_session_pid:
            try:
                os.kill(self._dbus_session_pid, signal.SIGTERM)
                logger.debug(
                    f"Terminated D-Bus session daemon PID {self._dbus_session_pid}"
                )
            except (ProcessLookupError, PermissionError):
                pass
            self._dbus_session_pid = None
            self._dbus_session_address = None

    def _run_command_with_timeout(
        self, command_string, timeout, use_process_group=True
    ):
        """
        Run a command with proper timeout handling and process group management.

        Args:
            command_string: List of command arguments
            timeout: Timeout in seconds
            use_process_group: Whether to use process groups (for Docker compatibility)

        Returns:
            Tuple of (stdout, stderr, return_code) or (None, None, None) on timeout
        """
        try:
            # Prepare preexec_fn for process group creation
            preexec_fn = None
            if use_process_group and not self.isDocker():
                # Create new process group (only works on Unix, not in Docker)
                preexec_fn = os.setsid

            logger.debug(
                f"Running command with {timeout}s timeout: {' '.join(command_string[:3])}..."
            )

            # Pass the persistent D-Bus session address so that tesla-control's
            # internal dbus-launch sees DBUS_SESSION_BUS_ADDRESS already set and
            # skips spawning a new daemon.
            env = os.environ.copy()
            if self._dbus_session_address:
                env["DBUS_SESSION_BUS_ADDRESS"] = self._dbus_session_address

            result = subprocess.Popen(
                command_string,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=preexec_fn,
                env=env,
            )

            try:
                # Use communicate with timeout
                stdout, stderr = result.communicate(timeout=timeout)
                return_code = result.returncode

                logger.debug(f"Command completed with return code {return_code}")
                return stdout, stderr, return_code

            except subprocess.TimeoutExpired:
                # Process timed out, kill it
                logger.warning(
                    f"Command timed out after {timeout}s, terminating process {result.pid}"
                )

                try:
                    # Try to kill the process group
                    if use_process_group:
                        pgid = os.getpgid(result.pid)
                        logger.debug(f"Killing process group {pgid}")
                        os.killpg(pgid, signal.SIGTERM)
                        time.sleep(0.5)

                        # Check if still alive
                        if result.poll() is None:
                            logger.warning(
                                f"Process group {pgid} didn't respond to SIGTERM, using SIGKILL"
                            )
                            os.killpg(pgid, signal.SIGKILL)
                    else:
                        # Fallback: kill just the process
                        result.terminate()
                        time.sleep(0.5)
                        if result.poll() is None:
                            result.kill()

                    # Wait for process to actually die
                    result.wait(timeout=1.0)
                except Exception as e:
                    logger.error(f"Error terminating process: {e}")

                return None, None, None

        except Exception as e:
            logger.error(f"Error running command: {e}")
            return None, None, None

    def _cleanup_stale_pipe(self):
        """Clean up any stale pipe file from previous crashes."""
        try:
            if os.path.exists(self.pipeName):
                os.unlink(self.pipeName)
                logger.debug(f"Cleaned up stale pipe file: {self.pipeName}")
        except Exception as e:
            logger.warning(f"Failed to clean up stale pipe file: {e}")

    def _ensure_pipe_closed(self):
        """Safely close pipe if it's open, tracking state."""
        if self.pipeOpen and self.pipe is not None:
            try:
                self.pipe.close()
                self.pipeOpen = False
                logger.debug("Pipe closed successfully")
            except Exception as e:
                logger.warning(f"Error closing pipe: {e}")
                self.pipeOpen = False

        # Always try to clean up the file
        try:
            if os.path.exists(self.pipeName):
                os.unlink(self.pipeName)
        except Exception as e:
            logger.debug(f"Could not remove pipe file: {e}")

    def closeFile(self):
        """Close pipe file with proper error handling and state tracking."""
        self._ensure_pipe_closed()

    def openFile(self):
        """Open output file for passing data to tesla-control with error handling."""
        try:
            # Ensure any previous pipe is closed
            if self.pipeOpen:
                self._ensure_pipe_closed()

            # Open new pipe file with buffering disabled
            self.pipe = open(self.pipeName, "wb", 0)
            self.pipeOpen = True
            logger.debug(f"Pipe opened successfully: {self.pipeName}")
        except Exception as e:
            logger.error(f"Failed to open pipe file: {e}")
            self.pipe = None
            self.pipeOpen = False
            raise

    def sendPublicKey(self, vin):
        """Send public key to vehicle via pipe with error handling."""
        try:
            if vin not in self.master.settings.get("Vehicles", {}):
                logger.error(f"Vehicle {vin} not found in settings")
                return False

            if "pubKeyPEM" not in self.master.settings["Vehicles"][vin]:
                logger.error(f"Vehicle {vin} has no pubKeyPEM defined")
                return False

            self.openFile()
            if not self.pipeOpen or self.pipe is None:
                logger.error("Failed to open pipe for public key transmission")
                return False

            self.pipe.write(
                base64.b64decode(self.master.settings["Vehicles"][vin]["pubKeyPEM"]),
            )
            self.pipe.flush()  # Ensure data is written
            logger.debug(f"Public key sent for vehicle {vin}")
            return True
        except Exception as e:
            logger.error(f"Error sending public key for {vin}: {e}")
            return False
        # NOTE: Do NOT close pipe here - it needs to stay open for tesla-control to read

    def sendPrivateKey(self, vin):
        """Send private key to vehicle via pipe with error handling."""
        try:
            if vin not in self.master.settings.get("Vehicles", {}):
                logger.error(f"Vehicle {vin} not found in settings")
                return False

            if "privKey" not in self.master.settings["Vehicles"][vin]:
                logger.debug(f"Vehicle {vin} has no privKey defined, skipping")
                return True  # Not an error, just skip

            self.openFile()
            if not self.pipeOpen or self.pipe is None:
                logger.error("Failed to open pipe for private key transmission")
                return False

            self.pipe.write(
                base64.b64decode(self.master.settings["Vehicles"][vin]["privKey"]),
            )
            self.pipe.flush()  # Ensure data is written
            logger.debug(f"Private key sent for vehicle {vin}")
            return True
        except Exception as e:
            logger.error(f"Error sending private key for {vin}: {e}")
            return False
        # NOTE: Do NOT close pipe here - it needs to stay open for tesla-control to read

    def enabled(self) -> bool:
        return self._enabled

    def updateSettings(self):
        # Called by TWCMaster when settings are read/updated
        self.scanForVehicles()
        self._claimKeyedVehicles()
        return True

    # BLE pairing status and registry claims
    #
    # settings["Vehicles"][vin]["ble"] = {"status": ..., "since": epoch}
    # with status one of pairing, paired, pairing_failed, refused. A key is
    # proven when the car accepts a command or answers a state read signed
    # with it; tesla-control's ErrKeyNotPaired text means the car dropped it.
    # Lock order: _bleLock (callers may hold it) -> settingsLock -> registry
    # and vehicle locks, which are only taken after settingsLock is released.

    def _safeNote(self, fn, *args):
        # Status bookkeeping must never turn a command result into a failure.
        try:
            fn(*args)
        except Exception as e:
            logger.debug(f"BLE status update failed: {type(e).__name__}")

    def _settingsKey(self, vin):
        """The settings["Vehicles"] key for this VIN, matched without regard
        to case (older releases stored form VINs as typed); upper case for a
        VIN not stored yet."""
        if not isinstance(vin, str):
            return vin
        with self.master.settingsLock:
            vehicles = self.master.settings.get("Vehicles") or {}
            if vin in vehicles:
                return vin
            upper = vin.upper()
            if upper in vehicles:
                return upper
            for key in list(vehicles):
                if isinstance(key, str) and key.upper() == upper:
                    return key
        return upper

    def _storedStatus(self, vin):
        vin = self._settingsKey(vin)
        with self.master.settingsLock:
            rec = (self.master.settings.get("Vehicles") or {}).get(vin)
            ble = rec.get("ble") if isinstance(rec, dict) else None
            if isinstance(ble, dict) and ble.get("status"):
                return {"status": ble["status"], "since": ble.get("since")}
        return {"status": "unpaired", "since": None}

    def bleStatus(self, vin):
        """Pairing status for the vehicle page.

        status/since are the persisted pairing state; label is what the
        page shows: the pairing state when there is one, otherwise the last
        range scan's result ("Range unknown" before any scan, when no scan
        is possible, or when the last scan gave no answer)."""
        st = self._storedStatus(vin)
        rng = self.rangeStatus(vin)
        now = time.time()
        canScan = self.canScan()
        label = self.STATUS_LABELS.get(st["status"])
        if label is None:
            if rng["inRange"] is True:
                label = "In range (not paired)"
            elif rng["inRange"] is False:
                label = "Not in range"
            else:
                label = "Range unknown"
        since = st["since"]
        attempt = self.blePairingAttempt(vin)
        if attempt and isinstance(attempt.get("at"), (int, float)):
            attempt["age"] = max(0.0, now - attempt["at"])
        st.update(
            {
                "label": label,
                "sinceAge": (
                    max(0.0, now - since) if isinstance(since, (int, float)) else None
                ),
                "inRange": rng["inRange"],
                "scannedAt": rng["at"],
                "lastScanAge": (
                    max(0.0, now - rng["at"]) if rng["at"] is not None else None
                ),
                "scanPending": rng["pending"],
                "canScan": canScan,
                "attempt": attempt,
            }
        )
        return st

    # Range scan
    #
    # tesla-control has no scan subcommand; the car advertises the local
    # name bleLocalName(vin), which BlueZ's bluetoothctl reports while
    # scanning. Scans share the adapter with tesla-control, so they run
    # under _bleLock on the background-task thread; the HTTP thread only
    # queues a request.

    @staticmethod
    def bleLocalName(vin):
        # vehicle-command pkg/connector/ble VehicleLocalName
        digest = hashlib.sha1(vin.upper().encode("ascii")).hexdigest()
        return "S" + digest[:16] + "C"

    def canScan(self):
        return not self.isDocker() and shutil.which("bluetoothctl") is not None

    @_serialized
    def scanForVehicleInRange(self, vin):
        """Scan for the car's BLE advertisement. True/False, or None when no
        scan is possible (Docker or no bluetoothctl) or the scan gave no
        answer (timed out, failed, or BlueZ reported an error)."""
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            return None
        vin = vin.upper()
        if not self.canScan():
            return None
        stdout, stderr, code = self._run_command_with_timeout(
            ["bluetoothctl", "--timeout", str(self.SCAN_SECONDS), "scan", "on"],
            timeout=self.SCAN_SECONDS + 4,
            use_process_group=True,
        )
        # [DEL] lines report BlueZ forgetting a device it no longer hears;
        # they carry the cached name and address, so they say nothing here.
        lines = [
            line
            for line in self._stripAnsi(stdout).splitlines()
            if not line.lstrip().startswith("[DEL]")
        ]
        output = "\n".join(lines)
        name = self.bleLocalName(vin)
        inRange = name in output
        failure = None
        if stdout is None:
            failure = "timed out"
        if not inRange and failure is None:
            errors = (output + "\n" + self._stripAnsi(stderr)).lower()
            if any(text in errors for text in self.SCAN_ERROR_TEXT):
                failure = "bluetoothctl reported an error"
            elif code != 0:
                # A match above wins whatever the exit code; without one, a
                # failed run says nothing about the car.
                failure = f"bluetoothctl exited {code}"
        if not inRange and failure is None:
            # BlueZ prints a device's name only when it learns it; a car it
            # already holds (an earlier scan, or tesla-control) shows up by
            # address alone, so look the address up.
            heard = self._heardAddresses(lines)
            if heard:
                known = self._bluezAddresses(name)
                if known is None:
                    failure = "device lookup failed"
                else:
                    inRange = bool(heard & known)
        if failure is not None:
            self._rangeScan[vin] = {"inRange": None, "at": time.time()}
            logger.warning(f"BLE scan for {vin}: range unknown ({failure})")
            return None
        self._rangeScan[vin] = {"inRange": inRange, "at": time.time()}
        logger.info(f"BLE scan for {vin}: {'in range' if inRange else 'not in range'}")
        v = self.registry.get(vin)
        if v is not None:
            # Not a sighting: a scan that misses the car says nothing new.
            self._safeNote(v.observe, "bleReach", inRange, "TeslaBLE", None, False)
        return inRange

    @staticmethod
    def _stripAnsi(data):
        return re.sub(r"\x1b\[[0-9;]*m", "", (data or b"").decode("utf-8", "replace"))

    # Lines that mean BlueZ heard a device during this scan: a new device, or
    # a fresh numeric RSSI ("RSSI: -60" or "RSSI: 0xffffffb2 (-78)"). Not
    # "RSSI is nil", "Connected: no" and the like, which a device that is not
    # advertising can produce.
    _HEARD_RE = re.compile(
        r"\[(?:NEW\] Device ([0-9A-Fa-f:]{17})\b"
        r"|CHG\] Device ([0-9A-Fa-f:]{17}) RSSI: (?:0x[0-9A-Fa-f]+|-?\d+))"
    )

    def _heardAddresses(self, lines):
        macs = set()
        for line in lines:
            m = self._HEARD_RE.search(line)
            if m:
                macs.add((m.group(1) or m.group(2)).upper())
        return macs

    def _bluezAddresses(self, name):
        """Addresses BlueZ holds under the car's local name, or None when
        bluetoothctl did not answer or failed (its output is then no list)."""
        stdout, stderr, code = self._run_command_with_timeout(
            ["bluetoothctl", "devices"], timeout=5, use_process_group=True
        )
        if stdout is None or code != 0:
            return None
        macs = set()
        for line in self._stripAnsi(stdout).splitlines():
            parts = line.strip().split(" ", 2)
            if len(parts) == 3 and parts[0] == "Device" and parts[2].strip() == name:
                macs.add(parts[1].upper())
        return macs

    def rangeStatus(self, vin):
        vin = vin.upper() if isinstance(vin, str) else vin
        # Unlocked read: _rangeScan entries are replaced whole, never mutated,
        # and a single dict get is atomic under the GIL.
        scan = self._rangeScan.get(vin) or {}
        with self._scanLock:
            requested = self._scanRequests.get(vin)
            scanning = vin in self._scanInProgress
        pending = scanning or (
            requested is not None and time.time() - requested < self.SCAN_PENDING_MAX
        )
        return {
            "inRange": scan.get("inRange"),
            "at": scan.get("at"),
            "pending": pending,
        }

    def requestRangeScan(self, vin):
        """Queue a range scan; safe from any thread (it only queues). Ignored
        for a car being scanned or scanned in the last SCAN_DEBOUNCE s."""
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            return
        vin = vin.upper()
        # Unlocked read, as in rangeStatus.
        last = (self._rangeScan.get(vin) or {}).get("at")
        with self._scanLock:
            if vin in self._scanInProgress:
                return
            if last is not None and time.time() - last < self.SCAN_DEBOUNCE:
                return
            self._scanRequests[vin] = time.time()
        self.master.queue_background_task({"cmd": "bleScan"})

    def runPendingScans(self):
        """Background task: scan for every requested VIN. The queue keeps one
        bleScan task, so requests arriving while this runs are drained here,
        for at most SCAN_DRAIN_PASSES rounds; requeueIfRequested picks up the
        rest once the task is done."""
        for _ in range(self.SCAN_DRAIN_PASSES):
            with self._scanLock:
                vins = sorted(self._scanRequests)
                self._scanRequests.clear()
                # Same critical section: a VIN is never in neither map.
                self._scanInProgress.update(vins)
            if not vins:
                return
            for vin in vins:
                try:
                    self.scanForVehicleInRange(vin)
                except Exception as e:
                    logger.error(f"BLE scan for {vin} failed: {type(e).__name__}")
                finally:
                    with self._scanLock:
                        self._scanInProgress.discard(vin)

    def requeueIfRequested(self):
        """Called after the bleScan task is marked done: a request that came
        in after the drain's last check was coalesced into the finishing
        task, so queue one more. Queues at most one task per call."""
        with self._scanLock:
            pending = bool(self._scanRequests)
        if pending:
            self.master.queue_background_task({"cmd": "bleScan"})

    def blePairingAttempt(self, vin):
        """The last pairing attempt this run: {"ok", "at", "message"}, or None.

        Transient: a failed re-pair of a paired car is reported here, not as
        its status."""
        if not isinstance(vin, str):
            return None
        attempt = self._pairingAttempts.get(vin.upper())
        return dict(attempt) if attempt else None

    def _isKeyRefusal(self, output):
        return bool(output) and self.KEY_REFUSED_TEXT in output.lower()

    def _setBleStatus(self, vin, status, pairingSent=None):
        if not isinstance(vin, str) or not isWellFormedVIN(vin):
            return
        vin = self._settingsKey(vin)
        now = time.time()
        with self.master.settingsLock:
            vehicles = self.master.settings.setdefault("Vehicles", {})
            rec = vehicles.get(vin)
            if not isinstance(rec, dict):
                if status == "pairing_failed":
                    # A failed request for a VIN we hold nothing for (the
                    # key could not even be prepared) is no reason to start
                    # a record; the attempt is still shown on the page.
                    return
                rec = vehicles[vin] = {
                    "chargeSessions": 0,
                    "startkWh": 0,
                    "totalkWh": 0,
                }
            old = rec.get("ble") if isinstance(rec.get("ble"), dict) else {}
            ble = {"status": status, "since": now}
            if old.get("status") == status and old.get("since"):
                ble["since"] = old["since"]
            sent = pairingSent if pairingSent is not None else old.get("pairingSent")
            if sent is not None:
                ble["pairingSent"] = sent
            rec["ble"] = ble
        self.master.queue_background_task({"cmd": "saveSettings"})

        if status in ("paired", "pairing"):
            try:
                v = self.registry.getOrCreate(vin, "TeslaBLE")
            except ValueError:
                return
            if status == "paired":
                v.claim("TeslaBLE", True, "paired")
            else:
                self._claimUnproven(v)
        elif status == "refused":
            v = self.registry.get(vin)
            if v is not None:
                v.dropClaim("TeslaBLE")
        # pairing_failed leaves any claim as it was: a request that did not
        # reach the car says nothing about the key the car already holds.

    def _claimUnproven(self, v):
        # BLE is still tried, the car is not managed, and the claim does not
        # keep an unseen car in the registry past the forget window.
        v.claim("TeslaBLE", False, "unproven", keepsAlive=False)

    def _noteBleProof(self, vin):
        if self._storedStatus(vin)["status"] != "paired":
            logger.info(f"BLE key for {vin} accepted")
            self._setBleStatus(vin, "paired")

    def _notePairingResult(self, vin, ok, reason):
        now = time.time()
        self._pairingAttempts[vin.upper()] = {
            "ok": bool(ok),
            "at": now,
            "message": (
                "Pairing request sent; tap your key card on the car" if ok else reason
            ),
        }
        paired = self._storedStatus(vin)["status"] == "paired"
        if ok:
            # A paired car stays paired; the window still opens, so a
            # refusal before the owner taps the card is not a refusal.
            self._setBleStatus(vin, "paired" if paired else "pairing", now)
        elif not paired:
            # Only a key refusal changes a paired car's status.
            self._setBleStatus(vin, "pairing_failed")

    def _inPairingWindow(self, vin):
        vin = self._settingsKey(vin)
        with self.master.settingsLock:
            rec = (self.master.settings.get("Vehicles") or {}).get(vin)
            ble = rec.get("ble") if isinstance(rec, dict) else None
            sent = ble.get("pairingSent") if isinstance(ble, dict) else None
        return (
            isinstance(sent, (int, float)) and time.time() - sent < self.PAIRING_WINDOW
        )

    def _noteBleRefusal(self, vin, output):
        if not self._isKeyRefusal(output):
            return False
        if self._inPairingWindow(vin):
            # The owner has not tapped the key card yet.
            logger.debug(f"BLE key for {vin} not yet accepted; pairing in progress")
            return True
        # Retries see the same refusal: record it once.
        if self._storedStatus(vin)["status"] != "refused":
            logger.info(f"BLE key for {vin} refused; re-pair from the vehicle page")
            self._setBleStatus(vin, "refused")
        return True

    def _claimKeyedVehicles(self):
        # The one place settings["Vehicles"] feeds the registry: it is the BLE
        # key store. Releases before 1.4.0 never recorded pairing, and
        # scanForVehicles makes a key for every known VIN, so a key is the
        # only evidence there is. On the upgrade boot (no saved registry yet)
        # every keyed VIN is seeded with an unproven claim, which keeps BLE
        # being tried without making the car managed; proof moves a claim to
        # paired, a refusal drops it. Later boots claim only cars the
        # registry still holds, plus paired ones, so an expired or forgotten
        # car is not brought back on every restart.
        with self.master.settingsLock:
            blob = self.master.settings.get("vehicleRegistry")
            upgradeBoot = not (
                isinstance(blob, dict) and isinstance(blob.get("vehicles"), dict)
            )
            entries = list((self.master.settings.get("Vehicles") or {}).items())
            seeds = []
            for vin, rec in entries:
                if not isinstance(rec, dict) or "privKey" not in rec:
                    continue
                if not isinstance(vin, str) or not isWellFormedVIN(vin):
                    continue
                ble = rec.get("ble")
                status = ble.get("status") if isinstance(ble, dict) else None
                seeds.append((vin, status))
        for vin, status in seeds:
            try:
                if status == "paired":
                    self.registry.getOrCreate(vin, "TeslaBLE").claim(
                        "TeslaBLE", True, "paired"
                    )
                    continue
                if upgradeBoot:
                    v = self.registry.getOrCreate(vin, "TeslaBLE")
                else:
                    v = self.registry.get(vin)
            except ValueError:
                continue
            # A refused key is not claimed; the upgrade boot still lists the car.
            if v is not None and status != "refused":
                self._claimUnproven(v)

    def __del__(self):
        self._stop_dbus_session()

    def isDocker(self):
        if self.isDockerCached is not None:
            return self.isDockerCached
        else:
            cgroup = Path("/proc/self/cgroup")
            self.isDockerCached = Path("/.dockerenv").is_file() or (
                cgroup.is_file() and "docker" in cgroup.read_text()
            )
            return self.isDockerCached
