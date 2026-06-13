# ConsoleLogging module. Provides output to console for logging.
from sys import modules
import logging
import os
import re
from logging.handlers import TimedRotatingFileHandler
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("FileLogging", "Logging")


class FileLogging:
    capabilities = {"queryGreenEnergy": False}
    config = None
    configConfig = None
    configLogging = None
    status = True
    logger = None
    mute = {}
    muteDebugLogLevelGreaterThan = 1

    def __init__(self, master):
        self.master = master
        self.config = master.config
        try:
            self.configConfig = master.config["config"]
        except KeyError:
            self.configConfig = {}
        try:
            self.configLogging = master.config["logging"]["FileLogger"]
        except KeyError:
            self.configLogging = {}
        self.status = self.configLogging.get("enabled", False)

        # Unload if this module is disabled or misconfigured
        if not self.status:
            self.master.releaseModule("lib.TWCManager.Logging", "FileLogging")
            return None

        # Initialize the mute config tree if it is not already
        self.mute = self.configLogging.get("mute", {})
        self.muteDebugLogLevelGreaterThan = self.mute.get("DebugLogLevelGreaterThan", 1)

        # Initialize Logger
        handler = None
        try:
            log_path = self.configLogging.get("path", "/etc/twcmanager/log")
            if not os.path.exists(log_path):
                try:
                    os.makedirs(log_path, exist_ok=True)
                except Exception as e:
                    logger.error(f"Could not create log directory {log_path}: {e}")

            handler = TimedRotatingFileHandler(
                log_path + "/logfile",
                when="H",
                interval=1,
                backupCount=24,
            )
        except PermissionError:
            logger.error("Permission Denied error opening logfile for writing")
        if handler:
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s - %(name)-10.10s %(levelno)02d %(message)s"
                )
            )
            logging.getLogger("").addHandler(handler)

    def getCapabilities(self, capability):
        # Allows query of module capabilities when deciding which Logging module to use
        return self.capabilities.get(capability, False)
