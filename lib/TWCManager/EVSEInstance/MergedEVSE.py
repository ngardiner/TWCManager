"""
Deduplicating proxy that merges multiple EVSEInstance views of the same vehicle.

When a vehicle is both plugged into a Gen2 TWC (visible as a Gen2TWC EVSE) and
reachable via the Tesla Fleet API (visible as a TeslaAPIEVSE), the power
distribution algorithm would see two separate EVSEs for one car and try to
allocate power twice.  MergedEVSE solves this by combining them into a single
logical EVSE that routes control commands to the appropriate underlying instance
based on the configured chargeRateControl / chargeStopMode settings.

Ported from ngardiner/TWCManager#483 (MikeBishop); adapted for current main.
Bug fixed: currentVoltage now guards against an empty ``evses`` list before
calling zip() (the original PR's zip(*[...]) returned an empty iterator when
the list was empty, producing [] instead of [0, 0, 0]).
"""

from TWCManager.EVSEInstance.EVSEInstance import EVSEInstance


class MergedEVSE(EVSEInstance):
    """A logical EVSE that aggregates multiple physical/API views of one vehicle.

    Reads prefer the local (RS485) view because it has the most accurate
    real-time current measurement.  Control commands are routed based on the
    chargeRateControl and chargeStopMode runtime settings.
    """

    def __init__(self, master, *evses):
        """
        Args:
            master: TWCMaster instance (for runtime settings lookup).
            *evses: Two or more EVSEInstance objects representing the same vehicle.
        """
        self.master = master
        self.evses = list(evses)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _prefer_local(self) -> list:
        """Return local EVSEs if any exist, else return all."""
        local = [e for e in self.evses if e.isLocal]
        return local if local else self.evses

    # ------------------------------------------------------------------
    # EVSEInstance: identity
    # ------------------------------------------------------------------

    @property
    def ID(self) -> str:
        return "Merged-" + "-".join(e.ID for e in self.evses)

    # ------------------------------------------------------------------
    # EVSEInstance: capabilities
    # ------------------------------------------------------------------

    @property
    def isReadOnly(self) -> bool:
        return all(e.isReadOnly for e in self.evses)

    @property
    def isLocal(self) -> bool:
        return any(e.isLocal for e in self.evses)

    # ------------------------------------------------------------------
    # EVSEInstance: state
    # ------------------------------------------------------------------

    @property
    def isCharging(self) -> bool:
        return any(e.isCharging for e in self.evses)

    @property
    def wantsToCharge(self) -> bool:
        return any(e.wantsToCharge for e in self.evses)

    @property
    def currentAmps(self) -> float:
        consider = self._prefer_local()
        if not consider:
            return 0.0
        return sum(e.currentAmps for e in consider) / len(consider)

    @property
    def currentVoltage(self) -> list:
        """Average per-phase voltage across the preferred EVSEs.

        Falls back to [0, 0, 0] when the EVSE list is empty (guards against
        the zip(*[]) → empty-iterator edge case from the original PR).
        """
        consider = self._prefer_local()
        if not consider:
            return [0, 0, 0]
        phase_columns = list(zip(*[e.currentVoltage for e in consider]))
        if not phase_columns:
            return [0, 0, 0]
        return [sum(col) / len(col) for col in phase_columns]

    @property
    def currentPower(self) -> float:
        consider = self._prefer_local()
        if not consider:
            return 0.0
        return sum(e.currentPower for e in consider) / len(consider)

    # ------------------------------------------------------------------
    # EVSEInstance: power limits
    # ------------------------------------------------------------------

    @property
    def minPower(self) -> float:
        # Must satisfy the highest minimum across all underlying EVSEs
        return max(e.minPower for e in self.evses)

    @property
    def maxPower(self) -> float:
        # Constrained by the lowest maximum (the binding limit)
        return min(e.maxPower for e in self.evses)

    # ------------------------------------------------------------------
    # EVSEInstance: optional properties
    # ------------------------------------------------------------------

    @property
    def currentVIN(self) -> str:
        for e in self.evses:
            if e.currentVIN:
                return e.currentVIN
        return ""

    @property
    def controllers(self) -> list:
        seen = []
        for e in self.evses:
            for c in e.controllers:
                if c not in seen:
                    seen.append(c)
        return seen

    # ------------------------------------------------------------------
    # EVSEInstance: control
    # ------------------------------------------------------------------

    def setTargetPower(self, watts: float) -> None:
        """Route power target to the appropriate underlying EVSE(s).

        chargeRateControl setting:
          1 (default) — prefer local RS485 control
          2 — prefer Tesla API control
        """
        charge_rate_control = int(self.master.settings.get("chargeRateControl", 1))
        if charge_rate_control == 2:
            targets = [e for e in self.evses if not e.isLocal and not e.isReadOnly]
        else:
            targets = [e for e in self.evses if e.isLocal and not e.isReadOnly]
        if not targets:
            targets = [e for e in self.evses if not e.isReadOnly]
        for e in targets:
            e.setTargetPower(watts)

    def startCharging(self) -> None:
        for e in self.evses:
            e.startCharging()

    def stopCharging(self) -> None:
        """Route stop command based on chargeStopMode setting.

        chargeStopMode:
          1 — prefer Tesla API stop (softer, notifies the car)
          2, 3 — prefer local RS485 stop
        """
        charge_stop_mode = int(self.master.settings.get("chargeStopMode", "1"))
        if charge_stop_mode == 1:
            targets = [e for e in self.evses if not e.isLocal and not e.isReadOnly]
        else:
            targets = [e for e in self.evses if e.isLocal and not e.isReadOnly]
        if not targets:
            targets = list(self.evses)
        for e in targets:
            e.stopCharging()
