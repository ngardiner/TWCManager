import base64
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import hashes
import logging
import os
import subprocess
import time

logger = logging.getLogger("\U0001F697 TeslaBLE")


class TeslaBLE:
    binaryPath = "/home/twcmanager/gobin/tesla-control"
    config = None
    master = None
    pipe = None
    pipeName = "/tmp/ble_pipe"

    def __init__(self, master):
        self.master = master
        try:
            self.config = self.master.config
        except KeyError:
            pass

        # Check that binary exists, otherwise unload
        if not os.path.isfile(self.binaryPath):
            self.master.releaseModule("lib.TWCManager.Vehicle", "TeslaBLE")
            return

    def car_api_charge(self, task):
        # This is not very well thought out at all - we'll just loop through
        # and ask all cars to charge for now
        if task["vin"]:
            if task["charge"]:
                self.startCharging(task["vin"])
                return self.pingVehicle(task["vin"])
            else:
                self.stopCharging(task["vin"])
                return self.pingVehicle(task["vin"])

        else:
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
        self.sendPublicKey(vin)
        return self.parseCommandOutput(ret)

    def pingVehicle(self, vin):
        ret = self.sendCommand(vin, "ping")
        return self.parseCommandOutput(ret)

    def sendCommand(self, vin, command, args=None):
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

        result = subprocess.run(
            command_string,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.sendPrivateKey(vin)
        return result.stderr.decode("utf-8")

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
                    self.master.settings["Vehicles"][vehicle][
                        "privKey"
                    ] = base64.b64encode(
                        private_key.private_bytes(
                            serialization.Encoding.PEM,
                            serialization.PrivateFormat.PKCS8,
                            serialization.NoEncryption(),
                        )
                    ).decode()
                    public_key = private_key.public_key()
                    self.master.settings["Vehicles"][vehicle][
                        "pubKey"
                    ] = base64.b64encode(
                        public_key.public_bytes(
                            serialization.Encoding.X962,
                            serialization.PublicFormat.UncompressedPoint,
                        )
                    ).decode()
                    self.master.settings["Vehicles"][vehicle][
                        "pubKeyPEM"
                    ] = base64.b64encode(
                        public_key.public_bytes(
                            serialization.Encoding.PEM,
                            serialization.PublicFormat.SubjectPublicKeyInfo,
                        )
                    ).decode()
                    self.master.queue_background_task({"cmd": "saveSettings"})
        else:
            logger.log(logging.INFO2, "No known vehicles.")

    def wakeVehicle(self, vin):
        self.sendCommand(vin, "wake")

    def closeFIFO(self):
        os.close(self.pipe)

    def openFIFO(self):
        # Open FIFO pipe for passing data to tesla-control
        try:
            os.mkfifo(self.pipeName)
        except FileExistsError:
            pass
        except OSError as oe:
            if oe.errno != errno.EEXIST:
                raise

        self.pipe = os.open(self.pipeName, os.O_WRONLY)

    def sendPublicKey(self, vin):
        self.openFIFO()
        os.write(
            self.pipe,
            base64.b64decode(self.master.settings["Vehicles"][vin]["pubKeyPEM"]),
        )
        self.closeFIFO()

    def sendPrivateKey(self, vin):
        self.openFIFO()
        os.write(
            self.pipe,
            base64.b64decode(self.master.settings["Vehicles"][vin]["privKey"]),
        )
        self.closeFIFO()

    def updateSettings(self):
        # Called by TWCMaster when settings are read/updated
        self.scanForVehicles()
        return True
