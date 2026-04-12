import base64
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
import logging
import os
from pathlib import Path
import signal
import shutil
import subprocess
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("TeslaBLE", "Vehicle")


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

    def __init__(self, master):
        self.master = master
        try:
            self.config = self.master.config
        except KeyError:
            self.config = {}
        self.configConfig = self.config.get("config", {})
        cfg = self.config.get("vehicle", {}).get("teslaBLE", {}) or {}
        self.enabled = cfg.get("enabled", True)

        # Load BLE-specific configuration with enhanced defaults
        ble_config = self.configConfig.get("moduleConfiguration", {}).get("TeslaBLE", {})

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
        self.circuit_breaker = BLECircuitBreaker(circuit_breaker_threshold, circuit_breaker_timeout)

        # Retry statistics tracking
        self.retry_stats = {}

        logger.info(f"BLE module initialized with timeout={self.commandTimeout}s, retries={self.maxRetries}, "
                   f"circuit_breaker_threshold={circuit_breaker_threshold}")

        # Clean up any stale pipe file from previous crashes
        self._cleanup_stale_pipe()

        # Determine best binary location
        self.binaryPath = self.configConfig.get(
            "teslaControlPath", os.path.expanduser("~/gobin/tesla-control")
        )

        # Failing this, search system path
        if not self.binaryPath or not os.path.isfile(self.binaryPath):
            self.binaryPath = shutil.which("tesla-control")

        # Final fallback prior to failure
        if not self.binaryPath or not os.path.isfile(self.binaryPath):
            self.binaryPath = "/home/twcmanager/gobin/tesla-control"

        # Check that binary exists, otherwise unload
        if not self.binaryPath or not os.path.isfile(self.binaryPath):
            logger.error("tesla-control binary not found - BLE module will be disabled")
            self.master.releaseModule("lib.TWCManager.Vehicle", "TeslaBLE")
            return
        else:
            logger.info(f"tesla-control binary found at: {self.binaryPath}")

    def car_api_charge(self, task):
        """
        Enhanced car_api_charge method with proper priority system integration.
        Returns True on success, False on failure to enable proper fallback.

        Args:
            task: Dictionary with 'charge' key (True/False) and optional 'vin' key
        """
        try:
            # Validate input task
            if not task:
                logger.error("car_api_charge called with empty task")
                return False

            if not isinstance(task, dict):
                logger.error(f"car_api_charge expects dict, got {type(task)}")
                return False

            # Extract charge parameter
            charge = task.get("charge", None)
            if charge is None:
                logger.error("Task missing required 'charge' key")
                return False

            # If we know the VIN of the vehicle connected to the TWC Slave, we'll send the command
            # directly to that vehicle
            vin = task.get("vin", None)
            if vin:
                logger.debug(f"BLE command for specific VIN: {vin}, charge: {charge}")

                if charge:
                    charge_success = self.startCharging(vin)
                    if charge_success:
                        ping_success = self.pingVehicle(vin)
                        success = charge_success and ping_success
                        logger.info(f"BLE start charging for {vin}: {'success' if success else 'failed'}")
                        return success
                    else:
                        logger.warning(f"BLE start charging failed for {vin}")
                        return False
                else:
                    stop_success = self.stopCharging(vin)
                    if stop_success:
                        ping_success = self.pingVehicle(vin)
                        success = stop_success and ping_success
                        logger.info(f"BLE stop charging for {vin}: {'success' if success else 'failed'}")
                        return success
                    else:
                        logger.warning(f"BLE stop charging failed for {vin}")
                        return False
            else:
                # If we don't know the VIN, we send to all vehicles
                # This is not optimal for multi-vehicle installs, but may be necessary when TWC doesn't read VIN
                logger.debug(f"BLE command for all vehicles, charge: {charge}")

                if not self.master.settings.get("Vehicles"):
                    logger.error("No vehicles configured for BLE operation")
                    return False

                success_count = 0
                total_vehicles = len(self.master.settings["Vehicles"])

                for vehicle in self.master.settings["Vehicles"].keys():
                    try:
                        if charge:
                            charge_success = self.startCharging(vehicle)
                            ping_success = self.pingVehicle(vehicle) if charge_success else False
                            vehicle_success = charge_success and ping_success
                        else:
                            stop_success = self.stopCharging(vehicle)
                            ping_success = self.pingVehicle(vehicle) if stop_success else False
                            vehicle_success = stop_success and ping_success

                        if vehicle_success:
                            success_count += 1
                            logger.debug(f"BLE command successful for vehicle {vehicle}")
                        else:
                            logger.warning(f"BLE command failed for vehicle {vehicle}")

                    except Exception as e:
                        logger.error(f"BLE command exception for vehicle {vehicle}: {e}")
                        continue

                # Consider operation successful if at least one vehicle responded
                # This allows partial success in multi-vehicle scenarios
                overall_success = success_count > 0
                logger.info(f"BLE command result: {success_count}/{total_vehicles} vehicles responded")
                return overall_success

        except Exception as e:
            logger.error(f"BLE car_api_charge failed with exception: {e}")
            return False

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
            output = output.decode('utf-8', errors='ignore')

        # Success indicators
        success_indicators = [
            "Updated session info for DOMAIN_VEHICLE_SECURITY",
            "Command executed successfully",
            "Vehicle responded",
            "Success"
        ]

        # Error indicators for detailed logging
        error_indicators = {
            "timeout": ["timeout", "timed out", "no response"],
            "connection": ["connection failed", "unable to connect", "bluetooth error"],
            "authentication": ["authentication failed", "invalid key", "unauthorized"],
            "vehicle_unavailable": ["vehicle not found", "vehicle offline", "not available"],
            "command_failed": ["command failed", "error executing", "operation failed"]
        }

        output_lower = output.lower()

        # Check for success
        for indicator in success_indicators:
            if indicator.lower() in output_lower:
                logger.debug(f"BLE command success: {indicator}")
                return True

        # Categorize errors for better debugging
        error_type = "unknown"
        for category, indicators in error_indicators.items():
            for indicator in indicators:
                if indicator in output_lower:
                    error_type = category
                    break
            if error_type != "unknown":
                break

        logger.debug(f"BLE command failed - Error type: {error_type}, Output: {output[:100]}...")
        return False

    def peerWithVehicle(self, vin):
        """
        Pair with a vehicle using BLE key exchange.
        Returns True on success, False on failure.
        """
        try:
            logger.info(f"Initiating BLE pairing with vehicle {vin}")

            if not self.binaryPath or not os.path.isfile(self.binaryPath):
                logger.error("tesla-control binary not available for pairing")
                return False

            # Send public key before pairing
            if not self.sendPublicKey(vin):
                logger.error(f"Failed to send public key for pairing with {vin}")
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
                use_process_group=True
            )

            if stdout is None and stderr is None:
                logger.error(f"BLE pairing with {vin} timed out or failed")
                return False

            # Check both stdout and stderr for pairing result
            output = (stderr.decode("utf-8") if stderr else "") + (stdout.decode("utf-8") if stdout else "")
            success = self.parseCommandOutput(output)

            logger.info(f"BLE pairing with {vin}: {'success' if success else 'failed'}")
            if not success:
                logger.debug(f"Pairing output: {output[:200]}...")

            return success

        except Exception as e:
            logger.error(f"peerWithVehicle exception for {vin}: {e}")
            return False
        finally:
            # Always ensure pipe is closed after pairing attempt
            self._ensure_pipe_closed()

    def pingVehicle(self, vin):
        """
        Ping a vehicle to verify BLE connectivity.
        Returns True on success, False on failure.
        """
        try:
            logger.debug(f"Pinging vehicle {vin}")

            ret = self.sendCommand(vin, "ping")
            if ret is None:
                logger.debug(f"Failed to send ping command to {vin}")
                return False

            success = self.parseCommandOutput(ret)
            logger.debug(f"Ping {vin}: {'success' if success else 'failed'}")
            return success

        except Exception as e:
            logger.error(f"pingVehicle exception for {vin}: {e}")
            return False

    def sendCommand(self, vin, command, args=None):
        """
        Enhanced sendCommand with improved error handling and timeout management.
        Returns command output string or None on failure.
        """
        return self._execute_with_retry(self._sendCommand_internal, vin, command, args)

    def _sendCommand_internal(self, vin, command, args=None):
        """
        Internal sendCommand implementation (called by retry wrapper).
        Returns command output string or None on failure.
        """
        try:
            # Validate inputs
            if not vin or not command:
                logger.error("sendCommand called with invalid vin or command")
                return None

            if not self.binaryPath or not os.path.isfile(self.binaryPath):
                logger.error("tesla-control binary not available")
                return None

            # Check if vehicle exists in settings
            if not self.master.settings.get("Vehicles", {}).get(vin):
                logger.error(f"Vehicle {vin} not found in settings")
                return None

            logger.debug(f"BLE sendCommand: {command} to {vin}" + (f" with args {args}" if args else ""))

            # Send private key before command
            if not self.sendPrivateKey(vin):
                logger.warning(f"Failed to send private key for {vin}, proceeding anyway")

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
                command_string,
                timeout=self.commandTimeout,
                use_process_group=True
            )

            if stdout is None and stderr is None:
                logger.debug(f"BLE command '{command}' timed out or failed")
                return None

            # Check if process was killed due to timeout
            if return_code == -9:  # SIGKILL
                logger.warning(f"BLE command '{command}' timed out after {self.commandTimeout}s")
                return None
            elif return_code != 0:
                logger.warning(f"BLE command '{command}' failed with return code {return_code}")

            output = stderr.decode("utf-8") if stderr else ""
            logger.debug(f"BLE command output: {output[:200]}..." if len(output) > 200 else output)
            return output

        except Exception as e:
            logger.error(f"sendCommand exception: {e}")
            return None
        finally:
            # Always ensure pipe is closed after command
            self._ensure_pipe_closed()

    def setChargeRate(self, charge_rate, vehicle=None, set_again=False):
        """
        Set charge rate for vehicle(s) with enhanced error handling.
        Returns True on success, False on failure.
        """
        try:
            if vehicle:
                # Set charge rate for specific vehicle
                logger.debug(f"Setting charge rate {charge_rate}A for vehicle {vehicle}")

                ret = self.sendCommand(vehicle, "charging-set-amps", charge_rate)
                if ret is None:
                    logger.error(f"Failed to send charging-set-amps command to {vehicle}")
                    return False

                success = self.parseCommandOutput(ret)
                logger.info(f"Set charge rate {charge_rate}A for {vehicle}: {'success' if success else 'failed'}")
                return success
            else:
                # Set charge rate for all vehicles
                # This isn't optimal but may be necessary when TWC doesn't know vehicle VIN
                logger.debug(f"Setting charge rate {charge_rate}A for all vehicles")

                if not self.master.settings.get("Vehicles"):
                    logger.error("No vehicles configured for charge rate setting")
                    return False

                success_count = 0
                total_vehicles = len(self.master.settings["Vehicles"])

                for vehicle_vin in self.master.settings["Vehicles"].keys():
                    try:
                        ret = self.sendCommand(vehicle_vin, "charging-set-amps", charge_rate)
                        if ret is not None and self.parseCommandOutput(ret):
                            success_count += 1
                            logger.debug(f"Set charge rate successful for vehicle {vehicle_vin}")
                        else:
                            logger.warning(f"Set charge rate failed for vehicle {vehicle_vin}")
                    except Exception as e:
                        logger.error(f"Set charge rate exception for vehicle {vehicle_vin}: {e}")
                        continue

                # Consider successful if at least one vehicle responded
                overall_success = success_count > 0
                logger.info(f"Set charge rate {charge_rate}A result: {success_count}/{total_vehicles} vehicles responded")
                return overall_success

        except Exception as e:
            logger.error(f"setChargeRate exception: {e}")
            return False

    def startCharging(self, vin):
        """
        Start charging for a specific vehicle with enhanced error handling.
        Returns True on success, False on failure.
        """
        try:
            logger.debug(f"Starting charging for vehicle {vin}")

            # Wake vehicle first - don't fail if wake fails, but log it
            wake_result = self.wakeVehicle(vin)
            if not wake_result:
                logger.warning(f"Wake command may have failed for {vin}, proceeding with charge start")

            ret = self.sendCommand(vin, "charging-start")
            if ret is None:
                logger.error(f"Failed to send charging-start command to {vin}")
                return False

            success = self.parseCommandOutput(ret)
            logger.info(f"Start charging for {vin}: {'success' if success else 'failed'}")
            return success

        except Exception as e:
            logger.error(f"startCharging exception for {vin}: {e}")
            return False

    def stopCharging(self, vin):
        """
        Stop charging for a specific vehicle with enhanced error handling.
        Returns True on success, False on failure.
        """
        try:
            logger.debug(f"Stopping charging for vehicle {vin}")

            ret = self.sendCommand(vin, "charging-stop")
            if ret is None:
                logger.error(f"Failed to send charging-stop command to {vin}")
                return False

            success = self.parseCommandOutput(ret)
            logger.info(f"Stop charging for {vin}: {'success' if success else 'failed'}")
            return success

        except Exception as e:
            logger.error(f"stopCharging exception for {vin}: {e}")
            return False

    def scanForVehicles(self):
        # This function allows other modules to prompt us to connect to BLE
        # vehicles.

        # Ensure we have a Private Key defined for each known vehicle
        if self.master.settings.get("Vehicles", None):
            for vehicle in self.master.settings["Vehicles"].keys():
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

    def _is_transient_error(self, output):
        """Determine if error is transient (should retry) or permanent (fail fast)."""
        if output is None:
            return True  # Timeout or communication error - transient
        
        output_lower = output.lower()
        
        # Transient error indicators
        transient_indicators = [
            "timeout", "timed out",
            "connection refused", "connection reset", "connection timeout",
            "device busy", "resource temporarily unavailable",
            "try again", "retry", "temporarily",
            "broken pipe", "no such file or directory"
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
            "timeout", "timed out",
            "connection", "broken pipe",
            "resource temporarily unavailable",
            "device busy", "try again"
        ]
        
        for indicator in transient_indicators:
            if indicator in exc_str:
                logger.debug(f"Detected transient exception: {indicator}")
                return True
        
        return False

    def _calculate_backoff(self, attempt):
        """Calculate exponential backoff delay for retry."""
        # Exponential backoff: 2^attempt * retryDelay
        delay = (2 ** attempt) * self.retryDelay
        
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
                "last_error_time": None
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
                result = func(vin, *args, **kwargs)
                
                if result is not None:
                    # Success
                    self.circuit_breaker.record_success(vin)
                    if attempt > 0:
                        logger.info(f"BLE command succeeded on retry attempt {attempt + 1}/{self.maxRetries + 1} for {vin}")
                        self._record_retry_attempt(vin, True)
                    return result
                
                # Result is None - check if error is transient
                if attempt < self.maxRetries:
                    delay = self._calculate_backoff(attempt)
                    logger.info(f"BLE command failed for {vin}, retrying in {delay}s "
                               f"(attempt {attempt + 1}/{self.maxRetries + 1})")
                    self._record_retry_attempt(vin, False, "transient_error")
                    time.sleep(delay)
                else:
                    # All retries exhausted
                    self.circuit_breaker.record_failure(vin)
                    logger.error(f"BLE command failed for {vin} after {self.maxRetries + 1} attempts")
                    self._record_retry_attempt(vin, False, "max_retries_exceeded")
                    return None
                    
            except Exception as e:
                if self._is_transient_exception(e):
                    if attempt < self.maxRetries:
                        delay = self._calculate_backoff(attempt)
                        logger.warning(f"BLE transient exception for {vin}: {e}, "
                                     f"retrying in {delay}s (attempt {attempt + 1}/{self.maxRetries + 1})")
                        self._record_retry_attempt(vin, False, f"transient_exception: {str(e)[:50]}")
                        time.sleep(delay)
                    else:
                        # All retries exhausted
                        self.circuit_breaker.record_failure(vin)
                        logger.error(f"BLE transient exception for {vin} after {self.maxRetries + 1} attempts: {e}")
                        self._record_retry_attempt(vin, False, f"exception_max_retries: {str(e)[:50]}")
                        return None
                else:
                    # Permanent error - don't retry
                    logger.error(f"BLE permanent error for {vin}, not retrying: {e}")
                    self._record_retry_attempt(vin, False, f"permanent_error: {str(e)[:50]}")
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
                        logger.warning(f"Process {pid} didn't respond to SIGTERM, sending SIGKILL")
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

    def _run_command_with_timeout(self, command_string, timeout, use_process_group=True):
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
            
            logger.debug(f"Running command with {timeout}s timeout: {' '.join(command_string[:3])}...")
            
            result = subprocess.Popen(
                command_string,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                preexec_fn=preexec_fn
            )
            
            try:
                # Use communicate with timeout
                stdout, stderr = result.communicate(timeout=timeout)
                return_code = result.returncode
                
                logger.debug(f"Command completed with return code {return_code}")
                return stdout, stderr, return_code
                
            except subprocess.TimeoutExpired:
                # Process timed out, kill it
                logger.warning(f"Command timed out after {timeout}s, terminating process {result.pid}")
                
                try:
                    # Try to kill the process group
                    if use_process_group:
                        pgid = os.getpgid(result.pid)
                        logger.debug(f"Killing process group {pgid}")
                        os.killpg(pgid, signal.SIGTERM)
                        time.sleep(0.5)
                        
                        # Check if still alive
                        if result.poll() is None:
                            logger.warning(f"Process group {pgid} didn't respond to SIGTERM, using SIGKILL")
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
            logger.debug(f"Public key sent for vehicle {vin}")
            return True
        except Exception as e:
            logger.error(f"Error sending public key for {vin}: {e}")
            return False
        finally:
            self._ensure_pipe_closed()

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
            logger.debug(f"Private key sent for vehicle {vin}")
            return True
        except Exception as e:
            logger.error(f"Error sending private key for {vin}: {e}")
            return False
        finally:
            self._ensure_pipe_closed()

    def updateSettings(self):
        # Called by TWCMaster when settings are read/updated
        self.scanForVehicles()
        return True

    def isDocker(self):
        if self.isDockerCached is not None:
            return self.isDockerCached
        else:
            cgroup = Path("/proc/self/cgroup")
            self.isDockerCached = Path("/.dockerenv").is_file() or (
                cgroup.is_file() and "docker" in cgroup.read_text()
            )
            return self.isDockerCached
