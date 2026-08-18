# Dutch SmartMeter Serial Integration (DSMR)
import logging
import time
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("DSMR", "EMS")


class DSMR:
    baudrate = 115200
    consumedW = 0
    generatedW = 0
    master = None
    serial = None
    serialPort = "/dev/ttyUSB2"
    status = False
    timeout = 0
    voltage = 0

    def __init__(self, master):
        self.master = master
        config = master.config.get("sources", {}).get("DSMR", {})
        self.baudrate = int(config.get("baudrate", 115200))
        self.status = config.get("enabled", False)
        self.serialPort = config.get("serialPort", "/dev/ttyUSB2")

        if (not self.status) or (not self.serialPort) or (self.baudrate < 1):
            self.master.releaseModule("lib.TWCManager.EMS", "DSMR")
            return None

    def main(self):
        self.serial.port = self.serialPort
        try:
            self.serial.open()
        except ValueError:
            import sys

            sys.exit("Error opening serial port (%s). exiting" % self.serial.name)
