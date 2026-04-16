"""
EVSEController for Tesla Wall Connector Generation 2 slave devices.

Wraps the existing TWCMaster slave registry so that Gen2 TWCs participate in
the controller-agnostic power distribution algorithm.

Ported from ngardiner/TWCManager#483 (MikeBishop); adapted for current main.
"""

from TWCManager.EVSEController.EVSEController import EVSEController


class Gen2TWCs(EVSEController):
    """Controller for Gen2 TWC slaves connected over RS485.

    The actual serial communication and protocol parsing remain in TWCMaster /
    TWCSlave for now (Phase 2 of the abstraction).  This controller provides
    the EVSEController interface on top of the existing slave registry so that
    the power distribution loop can treat them uniformly alongside other EVSE
    types (e.g. Tesla API vehicles).
    """

    name = "Gen2TWCs"

    def __init__(self, master):
        self.master = master

    # ------------------------------------------------------------------
    # EVSEController interface
    # ------------------------------------------------------------------

    @property
    def allEVSEs(self) -> list:
        """Return the current list of Gen2TWC slave instances from TWCMaster."""
        return self.master.getSlaveTWCs()

    @property
    def maxPower(self) -> float:
        """Maximum power in watts limited by the wiring configuration."""
        cfg = self.master.config["config"]
        voltage = cfg.get("defaultVoltage", 240)
        phases = cfg.get("numberOfPhases", 1)
        max_amps = cfg.get("wiringMaxAmpsAllTWCs", 6)
        return max_amps * voltage * phases
