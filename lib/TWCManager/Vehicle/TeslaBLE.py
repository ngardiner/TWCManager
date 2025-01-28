import base64
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
import logging
import os
import shutil
import subprocess
from threading import Timer
import time

logger = logging.getLogger("\U0001F697 TeslaBLE")


class TeslaBLE:
    binaryPath = None
    commandTimeout = 10
    config = None
    configConfig = None
    master = None
    pipe = None
    pipeName = "/tmp/ble_data"

    def __init__(self, master):
        self.master = master
        try:
            self.config = self.master.config
        except KeyError:
            pass
        self.configConfig = self.config.get("config", {})

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
            self.master.releaseModule("lib.TWCManager.Vehicle", "TeslaBLE")
            return

    def car_api_charge(self, task):
        # If we know the VIN of the vehicle connected to the TWC Slave, we'll send the command
        # directly to that vehicle
        if task.get("vin", None):
            if task["charge"]:
                self.startCharging(task["vin"])
                return self.pingVehicle(task["vin"])
            else:
                self.stopCharging(task["vin"])
                return self.pingVehicle(task["vin"])

        else:
            # If we don't know the VIN, we send to all vehicles - probably not the best logic for
            # multi-vehicle installs, but it's equally possible that the TWC doesn't read the VIN.
            for vehicle in self.master.settings["Vehicles"].keys():
                if task["charge"]:
                    self.startCharging(vehicle)
                    return self.pingVehicle(vehicle)
                else:
                    self.stopCharging(vehicle)
                    return self.pingVehicle(vehicle)

    def parseCommandOutput(self, output):
        success = False
        if "Updated session info for DOMAIN_VEHICLE_SECURITY" in output:
            success = True

        return success

    def peerWithVehicle(self, vin):
        self.sendPublicKey(vin)
        result = subprocess.run(
            [
                self.binaryPath,
                "-debug",
                "-ble",
                "-vin",
                vin,
                "add-key-request",
                self.pipeName,
                "owner",
                "cloud_key",
            ],
            stdout=subprocess.PIPE,
        )
        self.closeFile()
        return self.parseCommandOutput(ret)

    def pingVehicle(self, vin):
        ret = self.sendCommand(vin, "ping")
        return self.parseCommandOutput(ret)

    def sendCommand(self, vin, command, args=None):
        self.sendPrivateKey(vin)
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
        if args:
            command_string.append(args)

        result = subprocess.Popen(
            command_string,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        timer = Timer(self.commandTimeout, result.kill)
        try:
            timer.start()
            stdout, stderr = result.communicate()
        finally:
            timer.cancel()

        self.closeFile()
        return stderr.decode("utf-8")

    def setChargeRate(self, charge_rate, vehicle=None, set_again=False):
        ret = self.sendCommand(vehicle, "charging-set-amps", charge_rate)
        return self.parseCommandOutput(ret)

    def startCharging(self, vin):
        self.wakeVehicle(vin)
        ret = self.sendCommand(vin, "charging-start")
        return self.parseCommandOutput(ret)

    def stopCharging(self, vin):
        ret = self.sendCommand(vin, "charging-stop")
        return self.parseCommandOutput(ret)

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
        self.sendCommand(vin, "wake")

    def closeFile(self):
        self.pipe.close()
        os.unlink(self.pipeName)

    def openFile(self):
        # Open output file for passing data to tesla-control
        self.pipe = open(self.pipeName, "wb", 0)

    def sendPublicKey(self, vin):
        self.openFile()
        self.pipe.write(
            base64.b64decode(self.master.settings["Vehicles"][vin]["pubKeyPEM"]),
        )

    def sendPrivateKey(self, vin):
        self.openFile()
        self.pipe.write(
            base64.b64decode(self.master.settings["Vehicles"][vin]["privKey"]),
        )

    def updateSettings(self):
        # Called by TWCMaster when settings are read/updated
        self.scanForVehicles()
        return True
