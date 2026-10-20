from datetime import datetime
import logging
import re
import time
from typing import Dict, NamedTuple, Optional
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("Slave", "Slave")

# The tick's offer to one TWC: what goes on the wire, and the target it
# came from (published as amps_desired). Immutable: the tick replaces it
Offer = NamedTuple("Offer", [("amps", float), ("desired", float)])

# What one control pass knows about every TWC; confirmed maps a TWCID to
# whether the car carrying its point's rate believes that rate
OfferContext = NamedTuple(
    "OfferContext",
    [
        ("budgetAmps", float),
        ("carsCharging", int),
        ("mode", int),
        ("totalAmpsInUse", float),
        ("confirmed", Dict[bytes, bool]),
    ],
)


class TWCSlave:
    config = None
    configConfig = None
    TWCID = None
    lastVINQuery = 0
    maxAmps = None
    master = None
    vinQueryAttempt = 0

    # Protocol 2 TWCs tend to respond to commands sent using protocol 1, so
    # default to that till we know for sure we're talking to protocol 2.
    protocolVersion = 1
    minAmpsTWCSupports = 6
    masterHeartbeatData = bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00\x00")
    timeLastRx = time.time()

    # reported* vars below are reported to us in heartbeat messages from a Slave
    # TWC.
    reportedAmpsMax = 0
    reportedAmpsActual = 0
    reportedState = 0
    # The first reply's time: a slave not yet heard from is offered nothing
    heardFrom = 0

    # history* vars are used to track power usage over time
    historyAvgAmps = 0
    historyNumSamples = 0

    # reportedAmpsActual frequently changes by small amounts, like 5.14A may
    # frequently change to 5.23A and back.
    # reportedAmpsActualSignificantChangeMonitor is set to reportedAmpsActual
    # whenever reportedAmpsActual is at least 0.8A different than
    # reportedAmpsActualSignificantChangeMonitor. Whenever
    # reportedAmpsActualSignificantChangeMonitor is changed,
    # timeReportedAmpsActualChangedSignificantly is set to the time of the
    # change. The value of reportedAmpsActualSignificantChangeMonitor should not
    # be used for any other purpose. timeReportedAmpsActualChangedSignificantly
    # is used for things like preventing start and stop charge on a car more
    # than once per minute.
    reportedAmpsActualSignificantChangeMonitor = -1
    timeReportedAmpsActualChangedSignificantly = time.time()

    lastAmpsOffered = -1
    timeLastAmpsOfferedChanged = 0
    lastHeartbeatDebugOutput = ""
    startStopDelay = 0
    timeLastHeartbeatDebugOutput = 0
    wiringMaxAmps = 0
    lifetimekWh = 0
    voltsPhaseA = 0
    voltsPhaseB = 0
    voltsPhaseC = 0
    isCharging = 0
    lastChargingStart = 0
    chargingDroppedBelowThresholdTime = 0
    VINData = ["", "", ""]
    currentVIN = ""
    lastVIN = ""
    # The registry key of the car on this TWC (Gen2TWC); None decides nothing
    vehicleKey = None

    # Control tick: the distributor's target (Gen2TWC.setTargetPower) and
    # the offer made from it; None until the first pass after a reply, so
    # the heartbeat sends "no change" and a restart never offers 0 A
    pointAmps = 0
    offer: Optional[Offer] = None
    # The once-per-session lift: the serial thread counts sessions as they
    # end, the tick records the session it lifted
    plugSessions = 0
    liftedSession: Optional[int] = None
    # unplugStep's run starts and last max amps (0: this 00 run is no unplug)
    UNPLUG_DEBOUNCE, PLUGGED_RUN, LINK_GRACE = 60, 60, 300
    pluggedSince, ampsMaxBefore, zeroSince, anonEndedAt = 0, 0, None, 0
    # The car keys the charging point last rated (tick)
    _lastKeys = ()
    # The last all-TWC wiring cap logged, so a held cap logs once
    _cappedLogged = None

    def __init__(self, TWCID, maxAmps, config, master):
        self.config = config
        self.configConfig = self.config.get("config", {})
        self.master = master
        self.TWCID = TWCID
        self.maxAmps = maxAmps

        self.wiringMaxAmps = self.configConfig.get("wiringMaxAmpsPerTWC", 6)
        self.startStopDelay = self.configConfig.get("startStopDelay", 60)

    def unplugStep(self, now) -> bool:
        # Serial thread: True once per 00 run that is an unplug. A waking car
        # says 00 too; a car leaving drops the max amps it takes to 0 with it
        if self.reportedState:
            if self.zeroSince is not None:
                self.pluggedSince, self.zeroSince = now, None
            self.ampsMaxBefore = self.reportedAmpsMax
        elif self.zeroSince is None:
            self.zeroSince, before = now, self.ampsMaxBefore
            early = now < self.pluggedSince + self.PLUGGED_RUN
            if early or self.reportedAmpsMax or now < self.heardFrom + self.LINK_GRACE:
                self.ampsMaxBefore = 0
            text = "TWC %s: 00 onset, max %.2f A before, %.2f A now: %s"
            why = "an unplug if it holds" if self.ampsMaxBefore else "not an unplug"
            logger.info(text, self.twcid, before, self.reportedAmpsMax, why)
        elif self.ampsMaxBefore and now - self.zeroSince >= self.UNPLUG_DEBOUNCE:
            self.ampsMaxBefore = 0
            return True
        return False

    def minAmpsToOffer(self):
        # The lowest offer this TWC makes: the configured minAmpsPerTWC, or
        # what the TWC can reliably start a car at when that is higher
        # (6 when unset, as in TWCManager's default config)
        return max(self.configConfig.get("minAmpsPerTWC", 6), self.minAmpsTWCSupports)

    @property
    def vehicleRateRaised(self):
        # The car was raised to wiringMaxAmps this plug session
        return self.liftedSession == self.plugSessions

    def offerFor(self, target, ctx, now):
        # The control tick's offer for a distributor target
        if self.lastAmpsOffered < 0:
            self.lastAmpsOffered = self.reportedAmpsMax
        minOffer, n = self.minAmpsToOffer(), ctx.carsCharging
        desired = target
        if desired < minOffer:
            # Probe an idle TWC (not counted against the budget) when every
            # charging car has more than minOffer, so it sees a car plug in;
            # else 0: a stop. A Gen2 treats 0 as its hardware minimum
            desired = 0
            if n and ctx.budgetAmps / n > minOffer:
                desired = self.minAmpsTWCSupports
        elif ctx.mode == 2 or (ctx.mode == 3 and desired < 6):
            # The car carries the rate: the TWC offers its maximum once the
            # car believes the rate, never before
            confirmed = ctx.confirmed.get(bytes(self.TWCID))
            desired = self.wiringMaxAmps if confirmed else int(desired)
        else:
            # The TWC carries it, in whole amps (a car asked to move between
            # fractional values can drop to ~5 A and stay there); a decrease
            # waits until the last change is 5 s old
            desired = int(desired)
            if (
                desired < self.lastAmpsOffered
                and now - self.timeLastAmpsOfferedChanged < 5
            ):
                desired = self.lastAmpsOffered
        amps = self._capped(desired, ctx.totalAmpsInUse)
        if amps != self.lastAmpsOffered:
            self.lastAmpsOffered, self.timeLastAmpsOfferedChanged = amps, now
        self._publishStatus("amps_offered", "ampsOffered", amps, "A")
        self._publishStatus("amps_desired", "ampsDesired", desired, "A")
        return Offer(amps, desired)

    def _capped(self, desired, totalAmpsInUse):
        # The wiring caps, on every offer: the circuit all TWCs share (with
        # the others' actual draw) and this TWC's own
        allTWCs = self.config["config"]["wiringMaxAmpsAllTWCs"]
        others = totalAmpsInUse - self.reportedAmpsActual
        amps = desired
        if others + desired > allTWCs:
            # Always at least minAmpsTWCSupports: brief overshoot is fine,
            # the next pass rebalances
            amps = max(int(allTWCs - others), self.minAmpsTWCSupports)
            if amps != self._cappedLogged:
                logger.info(
                    "WARNING: Offering slave TWC %02X%02X %.1fA instead of "
                    "%.1fA to avoid overloading wiring shared by all TWCs."
                    % (self.TWCID[0], self.TWCID[1], amps, desired)
                )
            self._cappedLogged = amps
        else:
            self._cappedLogged = None
        if amps > self.wiringMaxAmps:
            # Frequent in some configurations, such as two 80A TWCs sharing
            # a 125A line, so not an error
            logger.debug(
                "Offering slave TWC %02X%02X %.1fA instead of "
                "%.1fA to avoid overloading the TWC rated at %.1fA."
                % (
                    self.TWCID[0],
                    self.TWCID[1],
                    self.wiringMaxAmps,
                    desired,
                    self.wiringMaxAmps,
                )
            )
            amps = self.wiringMaxAmps
        return amps

    def chargeGate(self, now, key) -> Optional[str]:
        # The tick's start/stop for key, this protocol-2 TWC's car, once
        # offered. A drawing car offered 0 stops once the offer is 60 s old
        # and its draw startStopDelay steady: a cut mid-negotiation could
        # strand it, the offer may rise meanwhile, and fewer cycles spare
        # the relay
        if self.protocolVersion != 2 or key is None or self.offer is None:
            return None
        offered, actual = self.offer.amps, self.reportedAmpsActual
        if offered == 0 and actual > 4.0:
            hold = (
                now - self.timeLastAmpsOfferedChanged < 60
                or now - self.timeReportedAmpsActualChangedSignificantly
                < self.startStopDelay
            )
            return "cancelStop" if hold else "stop"
        if (
            offered >= self.minAmpsToOffer()
            and actual < 1.0
            and self.reportedState != 0x02
        ):
            return "start"
        if offered < self.minAmpsToOffer() and actual < 1.0:
            # A start not yet sent is moot, and never wakes the car
            return "withdraw"
        return None

    def print_status(self, heartbeatData):
        try:
            debugOutput = "SHB %02X%02X: %02X %05.2f/%05.2fA %02X%02X" % (
                self.TWCID[0],
                self.TWCID[1],
                heartbeatData[0],
                (((heartbeatData[3] << 8) + heartbeatData[4]) / 100),
                (((heartbeatData[1] << 8) + heartbeatData[2]) / 100),
                heartbeatData[5],
                heartbeatData[6],
            )
            if self.protocolVersion == 2:
                debugOutput += " %02X%02X" % (heartbeatData[7], heartbeatData[8])
            debugOutput += "  M"

            if not self.config["config"]["fakeMaster"]:
                debugOutput += " %02X%02X" % (
                    self.master.getMasterTWCID()[0],
                    self.master.getMasterTWCID()[1],
                )

            debugOutput += ": %02X %05.2f/%05.2fA %02X%02X" % (
                self.masterHeartbeatData[0],
                (
                    ((self.masterHeartbeatData[3] << 8) + self.masterHeartbeatData[4])
                    / 100
                ),
                (
                    ((self.masterHeartbeatData[1] << 8) + self.masterHeartbeatData[2])
                    / 100
                ),
                self.masterHeartbeatData[5],
                self.masterHeartbeatData[6],
            )
            if self.protocolVersion == 2:
                debugOutput += " %02X%02X" % (
                    self.masterHeartbeatData[7],
                    self.masterHeartbeatData[8],
                )

            # Only output once-per-second heartbeat debug info when it's
            # different from the last output or if the only change has been amps
            # in use and it's only changed by 1.0 or less. Also output f it's
            # been 10 mins since the last output or if logLevel is higher than
            # DEBUG (value < 10)
            lastAmpsUsed = 0
            ampsUsed = 1
            debugOutputCompare = debugOutput
            m1 = re.search(r"SHB ....: .. (..\...)/", self.lastHeartbeatDebugOutput)
            if m1:
                lastAmpsUsed = float(m1.group(1))
            m2 = re.search(r"SHB ....: .. (..\...)/", debugOutput)
            if m2:
                ampsUsed = float(m2.group(1))
                if m1:
                    debugOutputCompare = (
                        debugOutputCompare[0 : m2.start(1)]
                        + self.lastHeartbeatDebugOutput[m1.start(1) : m1.end(1)]
                        + debugOutputCompare[m2.end(1) :]
                    )
            if (
                debugOutputCompare != self.lastHeartbeatDebugOutput
                or abs(ampsUsed - lastAmpsUsed) >= 1.0
                or time.time() - self.timeLastHeartbeatDebugOutput > 600
            ):
                logger.info(debugOutput)
                self.lastHeartbeatDebugOutput = debugOutput
                self.timeLastHeartbeatDebugOutput = time.time()

                logger.info(
                    "Slave power for TWCID %02X%02X, status: %s",
                    self.TWCID[0],
                    self.TWCID[1],
                    heartbeatData[0],
                    extra={
                        "logtype": "slave_power",
                        "TWCID": self.TWCID,
                        "status": heartbeatData[0],
                    },
                )

        except IndexError:
            # This happens if we try to access, say, heartbeatData[8] when
            # len(heartbeatData) < 9. This was happening due to a bug I fixed
            # but I may as well leave this here just in case.
            if len(heartbeatData) != (7 if self.protocolVersion == 1 else 9):
                logger.log(
                    1,
                    "Error in print_status displaying heartbeatData %s",
                    self.master.hex_str(heartbeatData),
                    # "based on msg",
                    # self.master.hex_str(msg),
                )
            if len(self.masterHeartbeatData) != (7 if self.protocolVersion == 1 else 9):
                logger.info(
                    "Error in print_status displaying masterHeartbeatData",
                    self.masterHeartbeatData,
                )

    def send_slave_heartbeat(self, masterID):
        # Send slave heartbeat
        #
        # Heartbeat includes data we store in slaveHeartbeatData.
        # Meaning of data:
        #
        # Byte 1 is a state code:
        #   00 Ready
        #      Car may or may not be plugged in.
        #      When car has reached its charge target, I've repeatedly seen it
        #      change from 03 to 00 the moment I wake the car using the phone app.
        #   01 Plugged in, charging
        #   02 Error
        #      This indicates an error such as not getting a heartbeat message
        #      from Master for too long.
        #   03 Plugged in, do not charge
        #      I've seen this state briefly when plug is first inserted, and
        #      I've seen this state remain indefinitely after pressing stop
        #      charge on car's screen or when the car reaches its target charge
        #      percentage. Unfortunately, this state does not reliably remain
        #      set, so I don't think it can be used to tell when a car is done
        #      charging. It may also remain indefinitely if TWCManager script is
        #      stopped for too long while car is charging even after TWCManager
        #      is restarted. In that case, car will not charge even when start
        #      charge on screen is pressed - only re-plugging in charge cable
        #      fixes it.
        #   04 Plugged in, ready to charge or charge scheduled
        #      I've seen this state even when car is set to charge at a future
        #      time via its UI. In that case, it won't accept power offered to
        #      it.
        #   05 Busy?
        #      I've only seen it hit this state for 1 second at a time and it
        #      can seemingly happen during any other state. Maybe it means wait,
        #      I'm busy? Communicating with car?
        #   08 Starting to charge?
        #      This state may remain for a few seconds while car ramps up from
        #      0A to 1.3A, then state usually changes to 01. Sometimes car skips
        #      08 and goes directly to 01.
        #      I saw 08 consistently each time I stopped fake master script with
        #      car scheduled to charge, plugged in, charge port blue. If the car
        #      is actually charging and you stop TWCManager, after 20-30 seconds
        #      the charge port turns solid red, steering wheel display says
        #      "charge cable fault", and main screen says "check charger power".
        #      When TWCManager is started, it sees this 08 status again. If we
        #      start TWCManager and send the slave a new max power value, 08
        #      becomes 00 and car starts charging again.
        #
        #   Protocol 2 adds a number of other states:
        #   06, 07, 09
        #      These are each sent as a response to Master sending the
        #      corresponding state. Ie if Master sends 06, slave responds with
        #      06. See notes in send_master_heartbeat for meaning.
        #   0A Amp adjustment period complete
        #      Master uses state 06 and 07 to raise or lower the slave by 2A
        #      temporarily.  When that temporary period is over, it changes
        #      state to 0A.
        #   0F was reported by another user but I've not seen it during testing
        #      and have no idea what it means.
        #
        # Byte 2-3 is the max current available as provided by bytes 2-3 in our
        # fake master status.
        # For example, if bytes 2-3 are 0F A0, combine them as 0x0fa0 hex which
        # is 4000 in base 10. Move the decimal point two places left and you get
        # 40.00Amps max.
        #
        # Byte 4-5 represents the power the car is actually drawing for
        # charging. When a car is told to charge at 19A you may see a value like
        # 07 28 which is 0x728 hex or 1832 in base 10. Move the decimal point
        # two places left and you see the charger is using 18.32A.
        # Some TWCs report 0A when a car is not charging while others may report
        # small values such as 0.25A. I suspect 0A is what should be reported
        # and any small value indicates a minor calibration error.
        #
        # Remaining bytes are always 00 00 from what I've seen and could be
        # reserved for future use or may be used in a situation I've not
        # observed.  Protocol 1 uses two zero bytes while protocol 2 uses four.

        ###############################
        # How was the above determined?
        #
        # An unplugged slave sends a status like this:
        #   00 00 00 00 19 00 00
        #
        # A real master always sends all 00 status data to a slave reporting the
        # above status. slaveHeartbeatData[0] is the main driver of how master
        # responds, but whether slaveHeartbeatData[1] and [2] have 00 or non-00
        # values also matters.
        #
        # I did a test with a protocol 1 TWC with fake slave sending
        # slaveHeartbeatData[0] values from 00 to ff along with
        # slaveHeartbeatData[1-2] of 00 and whatever
        # value Master last responded with. I found:
        #   Slave sends:     04 00 00 00 19 00 00
        #   Master responds: 05 12 c0 00 00 00 00
        #
        #   Slave sends:     04 12 c0 00 19 00 00
        #   Master responds: 00 00 00 00 00 00 00
        #
        #   Slave sends:     08 00 00 00 19 00 00
        #   Master responds: 08 12 c0 00 00 00 00
        #
        #   Slave sends:     08 12 c0 00 19 00 00
        #   Master responds: 00 00 00 00 00 00 00
        #
        # In other words, master always sends all 00 unless slave sends
        # slaveHeartbeatData[0] 04 or 08 with slaveHeartbeatData[1-2] both 00.
        #
        # I interpret all this to mean that when slave sends
        # slaveHeartbeatData[1-2] both 00, it's requesting a max power from
        # master. Master responds by telling the slave how much power it can
        # use. Once the slave is saying how much max power it's going to use
        # (slaveHeartbeatData[1-2] = 12 c0 = 32.00A), master indicates that's
        # fine by sending 00 00.
        #
        # However, if the master wants to set a lower limit on the slave, all it
        # has to do is send any heartbeatData[1-2] value greater than 00 00 at
        # any time and slave will respond by setting its
        # slaveHeartbeatData[1-2] to the same value.
        #
        # I thought slave might be able to negotiate a lower value if, say, the
        # car reported 40A was its max capability or if the slave itself could
        # only handle 80A, but the slave dutifully responds with the same value
        # master sends it even if that value is an insane 655.35A. I tested
        # these values on car which has a 40A limit when AC charging and
        # slave accepts them all:
        #   0f aa (40.10A)
        #   1f 40 (80.00A)
        #   1f 41 (80.01A)
        #   ff ff (655.35A)

        if self.protocolVersion == 1 and len(self.master.slaveHeartbeatData) > 7:
            # Cut array down to length 7
            self.master.slaveHeartbeatData = self.master.slaveHeartbeatData[0:7]
        elif self.protocolVersion == 2:
            while len(self.master.slaveHeartbeatData) < 9:
                # Increase array length to 9
                self.master.slaveHeartbeatData.append(0x00)

        self.master.getModulesByType("Interface")[0]["ref"].send(
            bytearray(b"\xfd\xe0")
            + self.master.getFakeTWCID()
            + bytearray(masterID)
            + bytearray(self.master.slaveHeartbeatData)
        )

    def send_master_heartbeat(self):
        # Send our fake master's heartbeat to this TWCSlave.
        #
        # Heartbeat includes 7 bytes (Protocol 1) or 9 bytes (Protocol 2) of data
        # that we store in masterHeartbeatData.

        if self.master.settings.get("respondToSlaves", 1) == 0:
            if (
                self.master.settings.get("respondToSlavesExpiry", time.time())
                > time.time()
            ):
                # We have been instructed not to send master heartbeats
                return
            else:
                # We were previously told to stop responding to slaves, but
                # the time limit for this has been exceeded. Start responding
                # again
                self.master.settings["respondToSlaves"] = 1

        # Meaning of data:
        #
        # Byte 1 is a command:
        #   00 Make no changes
        #   02 Error
        #     Byte 2 appears to act as a bitmap where each set bit causes the
        #     slave TWC to enter a different error state. First 8 digits below
        #     show which bits are set and these values were tested on a Protocol
        #     2 TWC:
        #       0000 0001 = Middle LED blinks 3 times red, top LED solid green.
        #                   Manual says this code means 'Incorrect rotary switch
        #                   setting.'
        #       0000 0010 = Middle LED blinks 5 times red, top LED solid green.
        #                   Manual says this code means 'More than three Wall
        #                   Connectors are set to Slave.'
        #       0000 0100 = Middle LED blinks 6 times red, top LED solid green.
        #                   Manual says this code means 'The networked Wall
        #                   Connectors have different maximum current
        #                   capabilities.'
        #       0000 1000 = No effect
        #       0001 0000 = No effect
        #       0010 0000 = No effect
        #       0100 0000 = No effect
        #       1000 0000 = No effect
        #     When two bits are set, the lowest bit (rightmost bit) seems to
        #     take precedence (ie 111 results in 3 blinks, 110 results in 5
        #     blinks).
        #
        #     If you send 02 to a slave TWC with an error code that triggers
        #     the middle LED to blink red, slave responds with 02 in its
        #     heartbeat, then stops sending heartbeat and refuses further
        #     communication. Slave's error state can be cleared by holding red
        #     reset button on its left side for about 4 seconds.
        #     If you send an error code with bitmap 11110xxx (where x is any bit),
        #     the error can not be cleared with a 4-second reset.  Instead, you
        #     must power cycle the TWC or 'reboot' reset which means holding
        #     reset for about 6 seconds till all the LEDs turn green.
        #   05 Tell slave charger to limit power to number of amps in bytes 2-3.
        #
        # Protocol 2 adds a few more command codes:
        #   06 Increase charge current by 2 amps.  Slave changes its heartbeat
        #      state to 06 in response. After 44 seconds, slave state changes to
        #      0A but amp value doesn't change.  This state seems to be used to
        #      safely creep up the amp value of a slave when the Master has extra
        #      power to distribute.  If a slave is attached to a car that doesn't
        #      want that many amps, Master will see the car isn't accepting the
        #      amps and stop offering more.  It's possible the 0A state change
        #      is not time based but rather indicates something like the car is
        #      now using as many amps as it's going to use.
        #   07 Lower charge current by 2 amps. Slave changes its heartbeat state
        #      to 07 in response. After 10 seconds, slave raises its amp setting
        #      back up by 2A and changes state to 0A.
        #      I could be wrong, but when a real car doesn't want the higher amp
        #      value, I think the TWC doesn't raise by 2A after 10 seconds. Real
        #      Master TWCs seem to send 07 state to all children periodically as
        #      if to check if they're willing to accept lower amp values. If
        #      they do, Master assigns those amps to a different slave using the
        #      06 state.
        #   08 Master acknowledges that slave stopped charging (I think), but
        #      the next two bytes contain an amp value the slave could be using.
        #   09 Tell slave charger to limit power to number of amps in bytes 2-3.
        #      This command replaces the 05 command in Protocol 1. However, 05
        #      continues to be used, but only to set an amp value to be used
        #      before a car starts charging. If 05 is sent after a car is
        #      already charging, it is ignored.
        #
        # Byte 2-3 is the max current a slave TWC can charge at in command codes
        # 05, 08, and 09. In command code 02, byte 2 is a bitmap. With other
        # command codes, bytes 2-3 are ignored.
        # If bytes 2-3 are an amp value of 0F A0, combine them as 0x0fa0 hex
        # which is 4000 in base 10. Move the decimal point two places left and
        # you get 40.00Amps max.
        #
        # Byte 4: 01 when a Master TWC is physically plugged in to a car.
        # Otherwise 00.
        #
        # Remaining bytes are always 00.
        #
        # Example 7-byte data that real masters have sent in Protocol 1:
        #   00 00 00 00 00 00 00  (Idle)
        #   02 04 00 00 00 00 00  (Error bitmap 04.  This happened when I
        #                         advertised a fake Master using an invalid max
        #                         amp value)
        #   05 0f a0 00 00 00 00  (Master telling slave to limit power to 0f a0
        #                         (40.00A))
        #   05 07 d0 01 00 00 00  (Master plugged in to a car and presumably
        #                          telling slaves to limit power to 07 d0
        #                          (20.00A). 01 byte indicates Master is plugged
        #                          in to a car.)

        # The tick's offer; none before its first pass after a reply
        offer = self.offer
        self.masterHeartbeatData = (
            bytearray(9) if offer is None else self._amps_heartbeat(offer.amps)
        )
        if len(self.master.getMasterHeartbeatOverride()) >= 7:
            self.masterHeartbeatData = self.master.getMasterHeartbeatOverride()

        self.master.getModulesByType("Interface")[0]["ref"].send(
            bytearray(b"\xfb\xe0")
            + self.master.getFakeTWCID()
            + bytearray(self.TWCID)
            + bytearray(self.masterHeartbeatData)
        )

    def receive_slave_heartbeat(self, heartbeatData):
        # Handle heartbeat message received from real slave TWC.

        self.master.queue_background_task({"cmd": "getLifetimekWh"})

        now = time.time()
        self.timeLastRx = now

        self.reportedAmpsMax = ((heartbeatData[1] << 8) + heartbeatData[2]) / 100
        self.reportedAmpsActual = ((heartbeatData[3] << 8) + heartbeatData[4]) / 100
        self.reportedState = heartbeatData[0]
        self.heardFrom = self.heardFrom or now
        self.refreshingChargerLoadStatus()
        key = self.vehicleKey
        vehicle = self.master.vehicleRegistry.get(key) if key else None
        if vehicle is not None:
            # A VIN heard but not yet confirmed names no registry car: skipped
            vehicle.observe(
                "actualCurrent", self.reportedAmpsActual, "Gen2TWC", seen=False
            )

        if self.reportedState == 0x02:
            logger.info(
                "WARNING: slave TWC %02X%02X is sending error with status data: %s"
                % (
                    self.TWCID[0],
                    self.TWCID[1],
                    self.master.hex_str(heartbeatData),
                )
            )

        self.refreshingChargerLoadStatus()
        self.master.refreshingTotalAmpsInUseStatus()

        for module in self.master.getModulesByType("Status"):
            module["ref"].setStatus(
                self.TWCID, "amps_max", "ampsMax", self.reportedAmpsMax, "A"
            )
            module["ref"].setStatus(
                self.TWCID, "amps_in_use", "ampsInUse", self.reportedAmpsActual, "A"
            )
            module["ref"].setStatus(
                self.TWCID, "state", "state", self.reportedState, ""
            )

        # Log current history
        self.historyAvgAmps = (
            (self.historyAvgAmps * self.historyNumSamples) + self.reportedAmpsActual
        ) / (self.historyNumSamples + 1)
        self.historyNumSamples += 1
        self.master.queue_background_task({"cmd": "snapHistoryData"})

        # If power starts flowing, refresh the cars at home
        if (
            self.reportedAmpsActualSignificantChangeMonitor < 3
            and self.reportedAmpsActual > 3
        ):
            self.master.getModuleByName("Policy").fireWebhook("start")
            self.master.queue_background_task({"cmd": "checkCharge"})

        # If power drops off, check whether a car leaves in the next little while
        if (
            self.reportedAmpsActualSignificantChangeMonitor > 2
            and self.reportedAmpsActual < 2
        ):
            self.master.getModuleByName("Policy").fireWebhook("stop")
            self.master.queue_background_task({"cmd": "checkDeparture"}, 5 * 60)
            self.master.queue_background_task({"cmd": "checkDeparture"}, 20 * 60)
            self.master.queue_background_task({"cmd": "checkDeparture"}, 45 * 60)

        # Keep track of the amps the slave is actually using and the last time it
        # changed by more than 0.8A.
        # Also update self.reportedAmpsActualSignificantChangeMonitor if it's
        # still set to its initial value of -1.
        if (
            self.reportedAmpsActualSignificantChangeMonitor < 0
            or abs(
                self.reportedAmpsActual
                - self.reportedAmpsActualSignificantChangeMonitor
            )
            > 0.8
        ):
            self.timeReportedAmpsActualChangedSignificantly = now
            self.reportedAmpsActualSignificantChangeMonitor = self.reportedAmpsActual

        # Charging sessions start and end here, on the serial thread. The
        # control tick sets the amps this TWC is sent
        self.master.observeChargingSessions()

        if logger.getEffectiveLevel() <= logging.INFO:
            self.print_status(heartbeatData)

    def _amps_heartbeat(self, desiredAmpsOffered):
        """Return the 9-byte master heartbeat payload for this slave."""
        # Rather than only sending desiredAmpsOffered when slave is sending code
        # 04 or 08, it seems to work better to send desiredAmpsOffered whenever
        # it does not equal self.reportedAmpsMax reported by the slave TWC.
        # Doing it that way will get a slave charging again even when it's in
        # state 00 or 03 which it swings between after you set
        # desiredAmpsOffered = 0 to stop charging.
        #
        # I later found that a slave may end up swinging between state 01 and 03
        # when desiredAmpsOffered == 0:
        #   S 032e 0.25/0.00A: 01 0000 0019 0000  M: 00 0000 0000 0000
        #   S 032e 0.25/6.00A: 03 0258 0019 0000  M: 05 0000 0000 0000
        #   S 032e 0.25/0.00A: 01 0000 0019 0000  M: 00 0000 0000 0000
        #   S 032e 0.25/6.00A: 03 0258 0019 0000  M: 05 0000 0000 0000
        #
        # While it's doing this, it's continuously opening and closing the relay
        # on the TWC each second which makes an audible click and will wear out
        # the relay. To avoid that problem, always send code 05 when
        # desiredAmpsOffered == 0. In that case, slave's response should always
        # look like this:
        #   S 032e 0.25/0.00A: 03 0000 0019 0000 M: 05 0000 0000 0000
        #
        # State 08 ("starting to charge") needs the offer resent even when the
        # slave's reported max already matches. A slave seen idle in state 03
        # with the offer already at 47A went to 08 reporting 06.16/47.00A and
        # stayed there at 6A until a mismatch was forced. Only 08 is included:
        # resending every heartbeat in a long-lived state (e.g. 04) risks the
        # slave bouncing between states.
        if (
            self.reportedAmpsMax != desiredAmpsOffered
            or desiredAmpsOffered == 0
            or self.reportedState == 0x08
        ):
            desiredHundredthsOfAmps = int(desiredAmpsOffered * 100)
            return bytearray(
                [
                    (0x09 if self.protocolVersion == 2 else 0x05),
                    (desiredHundredthsOfAmps >> 8) & 0xFF,
                    desiredHundredthsOfAmps & 0xFF,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                    0x00,
                ]
            )
        else:
            return bytearray([0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])

    def _publishStatus(self, key_underscore, key_camelcase, value, unit):
        for module in self.master.getModulesByType("Status"):
            module["ref"].setStatus(
                self.TWCID, key_underscore, key_camelcase, value, unit
            )

    def setLifetimekWh(self, kwh):
        self.lifetimekWh = kwh
        self._publishStatus("lifetime_kwh", "lifetimekWh", self.lifetimekWh, "kWh")

    def setVoltage(self, pa, pb, pc):
        self.voltsPhaseA = pa
        self.voltsPhaseB = pb
        self.voltsPhaseC = pc
        # Publish phase 1, 2 and 3 values via Status modules
        for phase in ("A", "B", "C"):
            self._publishStatus(
                "voltage_phase_" + phase.lower(),
                "voltagePhase" + phase,
                getattr(self, "voltsPhase" + phase, 0),
                "V",
            )
        self.refreshingChargerLoadStatus()

    def refreshingChargerLoadStatus(self):
        self._publishStatus(
            "charger_load_w",
            "chargerLoadInW",
            int(self.getCurrentChargerLoad()),
            "W",
        )

    def getCurrentChargerLoad(self):
        return self.master.convertAmpsToWatts(
            self.reportedAmpsActual
        ) * self.master.getRealPowerFactor(self.reportedAmpsActual)

    def getLastVehicle(self):
        # Registry lookups only: never creates a vehicle, never reads one.
        reg = self.master.vehicleRegistry
        for vin in (self.currentVIN, self.lastVIN):
            v = reg.get(vin) if vin else None
            if v is not None and v.attachment("TeslaAPI") is not None:
                return v
        return None
