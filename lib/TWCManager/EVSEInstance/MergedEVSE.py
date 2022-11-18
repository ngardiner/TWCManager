class MergedEVSE:

    evses = []
    master = None

    def __init__(self, master, *evses):
        self.master = master
        self.evses = evses

    @property
    def isReadOnly(self):
        return all([evse.isReadOnly for evse in self.evses])

    @property
    def isLocal(self):
        return any([evse.isLocal for evse in self.evses])

    @property
    def isCharging(self):
        return any([evse.isCharging for evse in self.evses])

    @property
    def wantsToCharge(self):
        return any([evse.wantsToCharge for evse in self.evses])

    @property
    def minPower(self):
        # This might be a bit too simplistic, but it's a start
        return min([evse.minPower for evse in self.evses])

    @property
    def maxPower(self):
        return max([evse.maxPower for evse in self.evses])

    @property
    def currentPower(self):
        consider = self.preferLocal()
        return sum([evse.currentPower for evse in consider]) / len(consider)

    @property
    def currentAmps(self):
        consider = self.preferLocal()
        return sum([evse.currentAmps for evse in consider]) / len(consider)

    @property
    def currentVoltage(self):
        result = []
        for local, allEVSEs in zip(
            zip([evse.currentVoltage for evse in self.preferLocal()]),
            zip([evse.currentVoltage for evse in self.evses]),
        ):
            if all(allEVSEs):
                result.append(sum(local) / len(local))
            else:
                result.append(0)
        return result

    @property
    def ID(self):
        return "Merged-" + "-".join([evse.ID for evse in self.evses])

    @property
    def currentVIN(self):
        return self.preferLocal()[0].currentVIN

    def lastVIN(self):
        return self.preferLocal()[0].lastVIN

    def preferLocal(self):
        localEvses = [evse for evse in self.evses if evse.isLocal]
        if len(localEvses) > 0:
            return localEvses
        else:
            return self.evses

    def startCharging(self):
        for evse in self.evses:
            evse.startCharging()

    def stopCharging(self):
        stopMode = int(self.master.settings.get("chargeStopMode", 1))
        if stopMode == 1:
            # Prefer Tesla API
            for evse in self.evses:
                if not evse.isLocal:
                    evse.stopCharging()
        elif stopMode == 2 or stopMode == 3:
            # Prefer local API
            # (Yes, these are not quite the same.)
            for evse in self.evses:
                evse.stopCharging()

    def setTargetPower(self, power):
        chargeRateControl = self.master.settings.get("chargeRateControl", 1)
        for evse in self.evses:
            if (chargeRateControl == 1 and evse.isLocal) or (
                chargeRateControl == 2 and not evse.isLocal
            ):
                # Send the preferred amount to the selected EVSE(s)
                evse.setTargetPower(power)
            else:
                # All the other EVSEs get their maximum amount
                #
                # Need to take the max() here, since the Vehicle will report
                # its current power as what the TWC is currently offering it.
                # This at least makes it always increase and never decrease.
                evse.setTargetPower(max([evse.maxPower, evse.currentPower, power]))

    @property
    def controllers(self):
        result = []
        for evse in self.evses:
            result.extend(evse.controllers)
        return result

    @property
    def lastPowerChange(self):
        return max([evse.lastPowerChange for evse in self.evses])

    def snapHistoryData(self):
        # Not sure this can be reliably implemented without requesting
        # charge data from the Tesla API after the fact.
        selectedEVSE = self.preferLocal()[0]
        result = selectedEVSE.snapHistoryData()
        for evse in self.evses:
            if evse != selectedEVSE:
                evse.snapHistoryData()
        return result
