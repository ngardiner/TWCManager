import logging
from pyModbusTCP.client import ModbusClient

logger = logging.getLogger("Victron")

class Victron:
    def __init__(self, master):
        self.master = master
        self.config = master.config.get("sources", {}).get("CerboGX", {})
        self.enabled = self.config.get("enabled", False)
        self.server_ip = self.config.get("serverIP", "192.168.30.101")
        self.server_port = int(self.config.get("serverPort", 502))
        self.unit_id = int(self.config.get("unitID", 100))

        self.client = ModbusClient(host=self.server_ip, port=self.server_port, unit_id=self.unit_id, auto_open=True)

        if not self.enabled:
            logger.info("Victron EMS Module Disabled.")

    def getConsumption(self):
        if not self.enabled:
            return 0
        return self.update("consumption")

    def getGeneration(self):
        if not self.enabled:
            return 0
        return self.update("generation")

    def update(self, data_type):
        try:
            if not self.client.is_open:
                self.client.open()

            if data_type == "consumption":
                # Addresses for Output Power (per phase)
                registers = [(817,100), (818,100), (819,100)]
                scale_factor = 1
            elif data_type == "generation":
                # Addresses for PV AC-coupled output (per phase)
                registers = [(808,100), (809,100), (810,100)]
                scale_factor = 1
            else:
                return 0

            total_power = 0
            for reg, unit_id in registers:
                # Set the unit_id dynamically for each read operation
                self.client.unit_id = unit_id
                result = self.client.read_input_registers(reg, 1)
                if result:
                    total_power += result[0] * scale_factor
                else:
                    logger.warning(f"Failed to read register {reg} on unit {unit_id}")

            return total_power

        except Exception as e:
            logger.error(f"Error communicating with Victron: {e}")
            return 0

        finally:
            if self.client.is_open:
                self.client.close()
