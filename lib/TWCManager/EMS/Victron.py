# Victron CerboGX Modbus TCP Integration
# Reads energy data from Victron CerboGX device via Modbus TCP
# Supports configurable register addresses and unit IDs for flexibility
# (e.g., reading from grid meter instead of system bus)

import logging
import time
from pyModbusTCP.client import ModbusClient
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("Victron", "EMS")

# Default cache time in seconds
MIN_CACHE_SECONDS = 10

# Default register addresses for CerboGX system bus
# These can be overridden in config for different meters
DEFAULT_CONSUMPTION_REGISTERS = [
    (817, 100),  # Output Power Phase A
    (818, 100),  # Output Power Phase B
    (819, 100),  # Output Power Phase C
]

DEFAULT_GENERATION_REGISTERS = [
    (808, 100),  # PV AC-coupled output Phase A
    (809, 100),  # PV AC-coupled output Phase B
    (810, 100),  # PV AC-coupled output Phase C
]


class Victron:
    """
    Victron CerboGX EMS Module
    Reads consumption and generation data via Modbus TCP
    """

    def __init__(self, master):
        self.master = master
        self.config = master.config
        self.enabled = False
        self.client = None
        self.lastUpdate = 0
        self.consumedW = 0
        self.generatedW = 0

        try:
            self.configVictron = master.config["sources"]["Victron"]
        except KeyError:
            self.configVictron = {}

        self.enabled = self.configVictron.get("enabled", False)
        self.serverIP = self.configVictron.get("serverIP", None)
        self.serverPort = int(self.configVictron.get("serverPort", 502))
        self.unitID = int(self.configVictron.get("unitID", 100))
        self.timeout = int(self.configVictron.get("timeout", 10))

        # Allow override of register addresses via config
        self.consumptionRegisters = self.configVictron.get(
            "consumptionRegisters", DEFAULT_CONSUMPTION_REGISTERS
        )
        self.generationRegisters = self.configVictron.get(
            "generationRegisters", DEFAULT_GENERATION_REGISTERS
        )

        # Unload if this module is disabled or misconfigured
        if not self.enabled or not self.serverIP:
            logger.log(
                logging.INFO7,
                "Victron EMS Module disabled or not configured. Unloading.",
            )
            self.master.releaseModule("lib.TWCManager.EMS", "Victron")
            return None

        # Attempt to establish connection
        try:
            self.client = ModbusClient(
                host=self.serverIP,
                port=self.serverPort,
                unit_id=self.unitID,
                timeout=self.timeout,
                auto_open=True,
            )

            # Verify connection is possible
            if not self.client.open():
                raise ConnectionError(
                    f"Failed to connect to Victron at {self.serverIP}:{self.serverPort}"
                )

            logger.log(
                logging.INFO7,
                f"Victron EMS Module loaded. Connected to {self.serverIP}:{self.serverPort} (Unit ID: {self.unitID})",
            )

            # Perform initial update
            self.__update()

        except (ConnectionError, OSError, ValueError) as e:
            logger.log(
                logging.INFO2,
                f"Error connecting to Victron device: {e}",
            )
            logger.debug(str(e))
            self.master.releaseModule("lib.TWCManager.EMS", "Victron")
            return None

    def __del__(self):
        """Cleanup: close Modbus connection on module unload"""
        if self.client and self.client.is_open:
            try:
                self.client.close()
            except Exception as e:
                logger.debug(f"Error closing Victron connection: {e}")

    def getConsumption(self):
        """Return current consumption in watts (negative value)"""
        if not self.enabled:
            logger.debug("Victron EMS Module disabled. Skipping getConsumption")
            return 0

        self.__update()
        return float(self.consumedW) * -1

    def getGeneration(self):
        """Return current generation in watts"""
        if not self.enabled:
            logger.debug("Victron EMS Module disabled. Skipping getGeneration")
            return 0

        self.__update()
        return float(self.generatedW)

    def __update(self):
        """Update cached values from Modbus if cache has expired"""
        if (int(time.time()) - self.lastUpdate) > MIN_CACHE_SECONDS:
            try:
                consumption = self.__readRegisters(self.consumptionRegisters)
                generation = self.__readRegisters(self.generationRegisters)

                self.consumedW = consumption
                self.generatedW = generation
                self.lastUpdate = int(time.time())

                logger.debug(
                    f"Victron update: consumption={consumption}W, generation={generation}W"
                )

            except Exception as e:
                logger.log(
                    logging.INFO4,
                    f"Error updating Victron data: {e}",
                )
                logger.debug(str(e))

    def __readRegisters(self, registers):
        """
        Read multiple registers and sum their values

        Args:
            registers: List of tuples (register_address, unit_id)

        Returns:
            Sum of all register values in watts
        """
        if not self.client or not self.client.is_open:
            if not self.client.open():
                raise ConnectionError("Cannot open Modbus connection")

        total_power = 0

        for reg_address, unit_id in registers:
            try:
                # Temporarily set unit_id for this read
                original_unit_id = self.client.unit_id
                self.client.unit_id = unit_id

                result = self.client.read_input_registers(reg_address, 1)

                # Restore original unit_id
                self.client.unit_id = original_unit_id

                if result is None or len(result) == 0:
                    logger.log(
                        logging.INFO4,
                        f"Failed to read register {reg_address} on unit {unit_id}",
                    )
                    continue

                # Convert register value to watts (Victron uses 1W per unit)
                power_value = int(result[0])
                total_power += power_value

                logger.debug(f"Register {reg_address} (unit {unit_id}): {power_value}W")

            except Exception as e:
                logger.log(
                    logging.INFO4,
                    f"Error reading register {reg_address} on unit {unit_id}: {e}",
                )
                logger.debug(str(e))
                continue

        return total_power
