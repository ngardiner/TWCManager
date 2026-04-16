"""
Abstract base class for EVSE controllers.

Architectural design ported from PR #483 (MikeBishop).
"""

import abc
import sys


class EVSEController(abc.ABC):
    """Manages one or more EVSEInstance objects.

    A controller is responsible for a set of charging connections that share
    a common communication path or access method. Concrete implementations:

    - Gen2TWCs: TWC Gen2 slaves connected over RS485 serial
    - TeslaAPIController: vehicles accessible via the Tesla Fleet API
    """

    name: str = ""

    # ------------------------------------------------------------------
    # EVSE enumeration
    # ------------------------------------------------------------------

    @property
    @abc.abstractmethod
    def allEVSEs(self) -> list:
        """Return the current list of EVSEInstance objects managed by this
        controller. The list may change between calls (vehicles connect and
        disconnect)."""

    # ------------------------------------------------------------------
    # Power budget
    # ------------------------------------------------------------------

    @property
    def maxPower(self) -> float:
        """Maximum power in watts this controller can deliver across all of
        its EVSEs. Defaults to unlimited (``float('inf')``)."""
        return float("inf")

    # ------------------------------------------------------------------
    # Bulk control — default implementations delegate to instances
    # ------------------------------------------------------------------

    def startAllCharging(self) -> None:
        """Start charging on every EVSE managed by this controller."""
        for evse in self.allEVSEs:
            evse.startCharging()

    def stopAllCharging(self) -> None:
        """Stop charging on every EVSE managed by this controller."""
        for evse in self.allEVSEs:
            evse.stopCharging()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def stop(self) -> None:
        """Shut down the controller (e.g. close serial port, cancel threads).
        Subclasses should override this if they hold resources."""
