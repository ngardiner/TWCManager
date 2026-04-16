"""
Abstract base class for EVSE (Electric Vehicle Supply Equipment) instances.

Architectural design ported from PR #483 (MikeBishop).
"""

import abc


class EVSEInstance(abc.ABC):
    """Represents a single charging connection point.

    Concrete implementations include:
    - Gen2TWC: a TWC Gen2 slave device connected over RS485
    - TeslaAPIEVSE: a vehicle accessible via the Tesla Fleet API
    - MergedEVSE: a deduplicated proxy combining multiple views of the same vehicle
    """

    # ------------------------------------------------------------------
    # Identity
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def ID(self) -> str:
        """Unique string identifier for this EVSE (e.g. "Gen2TWC-1234")."""

    # ------------------------------------------------------------------
    # Capabilities
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def isReadOnly(self) -> bool:
        """True if the charge rate cannot be controlled (monitoring only)."""

    @property
    @abc.abstractmethod
    def isLocal(self) -> bool:
        """True if controlled via a direct local connection (e.g. RS485) rather
        than a remote API."""

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def isCharging(self) -> bool:
        """True if a vehicle is actively drawing current."""

    @property
    @abc.abstractmethod
    def wantsToCharge(self) -> bool:
        """True if a vehicle is plugged in and willing to accept power (e.g.
        charging or paused due to insufficient power offer)."""

    @property
    @abc.abstractmethod
    def currentPower(self) -> float:
        """Power currently being drawn, in watts."""

    @property
    @abc.abstractmethod
    def currentAmps(self) -> float:
        """Current charge current, in amps."""

    @property
    @abc.abstractmethod
    def currentVoltage(self) -> list:
        """Per-phase voltage list [phaseA, phaseB, phaseC]. Inactive phases
        are represented as 0."""

    # ------------------------------------------------------------------
    # Power limits
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def minPower(self) -> float:
        """Minimum power in watts that can be offered to this EVSE."""

    @property
    @abc.abstractmethod
    def maxPower(self) -> float:
        """Maximum power in watts this EVSE can accept."""

    # ------------------------------------------------------------------
    # Optional properties with sensible defaults
    # ------------------------------------------------------------------

    @property
    def currentVIN(self) -> str:
        """VIN of the vehicle currently connected. Empty string if unknown."""
        return ""

    @property
    def controllers(self) -> list:
        """List of EVSEController names that manage this EVSE instance."""
        return []

    # ------------------------------------------------------------------
    # Control
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def setTargetPower(self, watts: float) -> None:
        """Set the target charge power in watts."""

    @abc.abstractmethod
    def startCharging(self) -> None:
        """Initiate a charging session."""

    @abc.abstractmethod
    def stopCharging(self) -> None:
        """Terminate a charging session."""

    # ------------------------------------------------------------------
    # Conversion helpers
    # ------------------------------------------------------------------

    def convertAmpsToWatts(self, amps: float, voltage: list = None) -> float:
        """Convert amps to watts, summing across active phases.

        Args:
            amps: Current in amps (applied equally to each active phase).
            voltage: Per-phase voltage list. Defaults to ``self.currentVoltage``.
        """
        if voltage is None:
            voltage = self.currentVoltage
        return sum(amps * v for v in voltage if v > 0)

    def convertWattsToAmps(self, watts: float, voltage: list = None) -> float:
        """Convert watts to amps using active-phase voltage.

        Args:
            watts: Power in watts.
            voltage: Per-phase voltage list. Defaults to ``self.currentVoltage``.
        """
        if voltage is None:
            voltage = self.currentVoltage
        active_phases = [v for v in voltage if v > 0]
        if not active_phases:
            return 0.0
        return watts / sum(active_phases)
