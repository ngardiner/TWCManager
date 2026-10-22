"""
What every charging endpoint (Gen2TWC, Gen3TWC, VehicleEndpoint and the
ChargingPoint grouping them) shares: amps/watts conversion over the active
phases, and defaults for an endpoint that cannot name or see its car.
"""


class EVSEInstance:
    # The VIN of the connected car ("" if unknown), the registry key of the
    # car served, and whether a car is connected (None if unknown)
    currentVIN = ""
    vehicleKey = None
    occupied = None
    # The budgets (TWCMaster._budgets) this endpoint draws from
    controllers = []

    def convertAmpsToWatts(self, amps: float, voltage: list = None) -> float:
        # Summed over the active phases; voltage defaults to currentVoltage
        if voltage is None:
            voltage = self.currentVoltage
        return sum(amps * v for v in voltage if v > 0)

    def convertWattsToAmps(self, watts: float, voltage: list = None) -> float:
        if voltage is None:
            voltage = self.currentVoltage
        active = sum(v for v in voltage if v > 0)
        return watts / active if active else 0.0
