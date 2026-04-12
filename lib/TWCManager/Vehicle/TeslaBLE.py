import base64
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
import logging
import os
from pathlib import Path
import shutil
import subprocess
from threading import Timer
import time

logger = logging.getLogger("\U0001f697 TeslaBLE")


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

        logger.info(f"BLE module initialized with timeout={self.commandTimeout}s, retries={self.maxRetries}")

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

            result = subprocess.run(
                command_string,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=30  # Pairing can take longer than normal commands
            )

            # Check both stdout and stderr for pairing result
            output = result.stderr.decode("utf-8") + result.stdout.decode("utf-8")
            success = self.parseCommandOutput(output)

            logger.info(f"BLE pairing with {vin}: {'success' if success else 'failed'}")
            if not success:
                logger.debug(f"Pairing output: {output[:200]}...")

            return success

        except subprocess.TimeoutExpired:
            logger.error(f"BLE pairing with {vin} timed out after 30 seconds")
            return False
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

            result = subprocess.Popen(
                command_string,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )

            timer = Timer(self.commandTimeout, result.kill)
            try:
                timer.start()
                stdout, stderr = result.communicate()
                return_code = result.returncode
            finally:
                timer.cancel()

            # Check if process was killed due to timeout
            if return_code == -9:  # SIGKILL
                logger.warning(f"BLE command '{command}' timed out after {self.commandTimeout}s")
                return None
            elif return_code != 0:
                logger.warning(f"BLE command '{command}' failed with return code {return_code}")

            output = stderr.decode("utf-8")
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
