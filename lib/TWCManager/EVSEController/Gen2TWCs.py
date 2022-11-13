# Gen2 TWC Controller
# Exposes EVSEInstances for Gen2 TWCs connected over serial

from TWCManager.EVSEInstance.Gen2TWC import Gen2TWC
import logging
import time
import threading
import re
import traceback
import random

logger = logging.getLogger("\u26FD Serial")


class Gen2TWCs:

    master = None
    config = {}
    status = False
    stopEvent = None
    thread = None
    name = "Gen2TWCs"

    masterTWCID = b""
    TWCID = None

    slaveHeartbeatData = bytearray(
        [0x01, 0x0F, 0xA0, 0x0F, 0xA0, 0x00, 0x00, 0x00, 0x00]
    )

    knownTWCsByID = {}
    knownTWCs = []
    lastkWhMessage = time.time()
    lastkWhPoll = 0

    overrideMasterHeartbeatData = b""

    # TWCs send a seemingly-random byte after their 2-byte TWC id in a number of
    # messages. I call this byte their "Sign" for lack of a better term. The byte
    # never changes unless the TWC is reset or power cycled. We use hard-coded
    # values for now because I don't know if there are any rules to what values can
    # be chosen. I picked 77 because it's easy to recognize when looking at logs.
    # These shouldn't need to be changed.
    masterSign = bytearray(b"\x77")
    slaveSign = bytearray(b"\x77")

    def __init__(self, master):
        self.master = master

        self.config = master.config
        self.configConfig = self.config.get("config", {})
        self.configTWCs = self.config.get("controller", {}).get("Gen2TWCs", {})

        self.TWCID = self.master.TWCID

        if "enabled" in self.configTWCs:
            self.status = self.configTWCs["enabled"]
        else:
            # Backward-compatible default; assume TWCs enabled if there's
            # a running serial interface
            self.status = self.getInterfaceModule() is not None

        # Unload if this module is disabled or misconfigured
        if not self.status:
            self.master.releaseModule("lib.TWCManager.EVSEController", "Gen2TWCs")
            return None

        self.stopEvent = threading.Event()
        self.thread = threading.Thread(target=self.parseMessages, args=(master,))
        self.thread.start()

    def stop(self):
        self.stopEvent.set()
        self.thread.join()

    def parseMessages(self, master):
        data = ""
        dataLen = 0
        ignoredData = bytearray()
        msg = bytearray()
        msgLen = 0

        numInitMsgsToSend = 10
        msgRxCount = 0

        idxSlaveToSendNextHeartbeat = 0
        timeLastkWhDelivered = time.time()
        timeLastkWhSaved = time.time()
        timeLastHeartbeatDebugOutput = 0

        timeTo0Aafter06 = 0
        timeToRaise2A = 0

        fakeMaster = self.configConfig["fakeMaster"]

        logger.info(
            "TWC Manager starting as fake %s with id %02X%02X and sign %02X"
            % (
                ("Master" if fakeMaster else "Slave"),
                ord(self.getFakeTWCID()[0:1]),
                ord(self.getFakeTWCID()[1:2]),
                ord(self.getSlaveSign()),
            )
        )

        # Add a 25ms sleep to prevent pegging pi's CPU at 100%. Lower CPU means
        # less power used and less waste heat.
        while not self.stopEvent.wait(0.025):
            try:
                # In this area, we always send a linkready message when we first start.
                # Whenever there is no data available from other TWCs to respond to,
                # we'll loop back to this point to send another linkready or heartbeat
                # message. By only sending our periodic messages when no incoming
                # message data is available, we reduce the chance that we will start
                # transmitting a message in the middle of an incoming message, which
                # would corrupt both messages.

                now = time.time()

                if fakeMaster == 1:
                    # A real master sends 5 copies of linkready1 and linkready2 whenever
                    # it starts up, which we do here.
                    # It doesn't seem to matter if we send these once per second or once
                    # per 100ms so I do once per 100ms to get them over with.
                    if numInitMsgsToSend > 5:
                        self.send_master_linkready1()
                        time.sleep(0.1)  # give slave time to respond
                        numInitMsgsToSend -= 1
                    elif numInitMsgsToSend > 0:
                        self.send_master_linkready2()
                        time.sleep(0.1)  # give slave time to respond
                        numInitMsgsToSend = numInitMsgsToSend - 1
                    else:
                        # After finishing the 5 startup linkready1 and linkready2
                        # messages, master will send a heartbeat message to every slave
                        # it's received a linkready message from. Do that here.
                        # A real master would keep sending linkready messages periodically
                        # as long as no slave was connected, but since real slaves send
                        # linkready once every 10 seconds till they're connected to a
                        # master, we'll just wait for that.
                        if time.time() - self.getTimeLastTx() >= 1.0:
                            # It's been about a second since our last heartbeat.
                            if self.countTWCs() > 0:
                                slaveTWC = self.getTWC(idxSlaveToSendNextHeartbeat)
                                if time.time() - slaveTWC.timeLastRx > self.config.get(
                                    "interfaces", {}
                                ).get("RS485", {}).get("slaveTimeout", 26):
                                    # A real master stops sending heartbeats to a slave
                                    # that hasn't responded for ~26 seconds. It may
                                    # still send the slave a heartbeat every once in
                                    # awhile but we're just going to scratch the slave
                                    # from our little black book and add them again if
                                    # they ever send us a linkready.
                                    logger.info(
                                        "WARNING: We haven't heard from slave "
                                        "%02X%02X for over 26 seconds.  "
                                        "Stop sending them heartbeat messages."
                                        % (slaveTWC.TWCID[0], slaveTWC.TWCID[1])
                                    )
                                    self.deleteTWC(slaveTWC.TWCID)
                                else:
                                    slaveTWC.send_master_heartbeat()

                                idxSlaveToSendNextHeartbeat = (
                                    idxSlaveToSendNextHeartbeat + 1
                                )
                                if idxSlaveToSendNextHeartbeat >= self.countTWCs():
                                    idxSlaveToSendNextHeartbeat = 0
                                time.sleep(0.1)  # give slave time to respond
                else:
                    # As long as a slave is running, it sends link ready messages every
                    # 10 seconds. They trigger any master on the network to handshake
                    # with the slave and the master then sends a status update from the
                    # slave every 1-3 seconds. Master's status updates trigger the slave
                    # to send back its own status update.
                    # As long as master has sent a status update within the last 10
                    # seconds, slaves don't send link ready.
                    # I've also verified that masters don't care if we stop sending link
                    # ready as long as we send status updates in response to master's
                    # status updates.
                    if fakeMaster != 2 and time.time() - self.getTimeLastTx() >= 10.0:
                        logger.info(
                            "Advertise fake slave %02X%02X with sign %02X is "
                            "ready to link once per 10 seconds as long as master "
                            "hasn't sent a heartbeat in the last 10 seconds."
                            % (
                                ord(self.getFakeTWCID()[0:1]),
                                ord(self.getFakeTWCID()[1:2]),
                                ord(master.getSlaveSign()),
                            )
                        )
                        self.send_slave_linkready()

                # If it has been more than 2 minutes since the last kWh value,
                # queue the command to request it from slaves
                if fakeMaster == 1 and ((time.time() - self.lastkWhMessage) > (60 * 2)):
                    self.lastkWhMessage = time.time()
                    master.queue_background_task({"cmd": "getLifetimekWh"})

                # If it has been more than 1 minute since the last VIN query with no
                # response, and if we haven't queried more than 5 times already for this
                # slave TWC, repeat the query
                self.retryVINQuery()

                ########################################################################
                # See if there's an incoming message on the input interface.

                timeMsgRxStart = time.time()
                actualDataLen = 0
                while True:
                    now = time.time()
                    dataLen = self.getInterfaceModule().getBufferLen()
                    if dataLen == 0:
                        if msgLen == 0:
                            # No message data waiting and we haven't received the
                            # start of a new message yet. Break out of inner while
                            # to continue at top of outer while loop where we may
                            # decide to send a periodic message.
                            break
                        else:
                            # No message data waiting but we've received a partial
                            # message that we should wait to finish receiving.
                            if now - timeMsgRxStart >= 2.0:
                                logger.log(
                                    logging.INFO9,
                                    "Msg timeout ("
                                    + self.hex_str(ignoredData)
                                    + ") "
                                    + self.hex_str(msg[0:msgLen]),
                                )
                                msgLen = 0
                                ignoredData = bytearray()
                                break

                            time.sleep(0.025)
                            continue
                    else:
                        actualDataLen = dataLen
                        dataLen = 1
                        data = self.getInterfaceModule().read(dataLen)

                    if dataLen != 1:
                        # This should never happen
                        logger.info("WARNING: No data available.")
                        break

                    timeMsgRxStart = now
                    timeLastRx = now
                    if msgLen == 0 and len(data) > 0 and data[0] != 0xC0:
                        # We expect to find these non-c0 bytes between messages, so
                        # we don't print any warning at standard debug levels.
                        logger.log(
                            logging.DEBUG2,
                            "Ignoring byte %02X between messages." % (data[0]),
                        )
                        ignoredData += data
                        continue
                    elif (
                        msgLen > 0 and msgLen < 15 and len(data) > 0 and data[0] == 0xC0
                    ):
                        # If you see this when the program is first started, it
                        # means we started listening in the middle of the TWC
                        # sending a message so we didn't see the whole message and
                        # must discard it. That's unavoidable.
                        # If you see this any other time, it means there was some
                        # corruption in what we received. It's normal for that to
                        # happen every once in awhile but there may be a problem
                        # such as incorrect termination or bias resistors on the
                        # rs485 wiring if you see it frequently.
                        logger.debug(
                            "Found end of message before full-length message received.  "
                            "Discard and wait for new message."
                        )

                        msg = data
                        msgLen = 1
                        continue
                    elif dataLen and len(data) == 0:
                        logger.error(
                            "We received a buffer length of %s from the RS485 module, but data buffer length is %s. This should not occur."
                            % (str(actualDataLen), str(len(data)))
                        )

                    if msgLen == 0:
                        msg = bytearray()
                    msg += data
                    msgLen += 1

                    # Messages are usually 17 bytes or longer and end with \xc0\xfe.
                    # However, when the network lacks termination and bias
                    # resistors, the last byte (\xfe) may be corrupted or even
                    # missing, and you may receive additional garbage bytes between
                    # messages.
                    #
                    # TWCs seem to account for corruption at the end and between
                    # messages by simply ignoring anything after the final \xc0 in a
                    # message, so we use the same tactic. If c0 happens to be within
                    # the corrupt noise between messages, we ignore it by starting a
                    # new message whenever we see a c0 before 15 or more bytes are
                    # received.
                    #
                    # Uncorrupted messages can be over 17 bytes long when special
                    # values are "escaped" as two bytes. See notes in sendMsg.
                    #
                    # To prevent most noise between messages, add a 120ohm
                    # "termination" resistor in parallel to the D+ and D- lines.
                    # Also add a 680ohm "bias" resistor between the D+ line and +5V
                    # and a second 680ohm "bias" resistor between the D- line and
                    # ground. See here for more information:
                    #   https://www.ni.com/support/serial/resinfo.htm
                    #   http://www.ti.com/lit/an/slyt514/slyt514.pdf
                    # This explains what happens without "termination" resistors:
                    #   https://e2e.ti.com/blogs_/b/analogwire/archive/2016/07/28/rs-485-basics-when-termination-is-necessary-and-how-to-do-it-properly
                    if msgLen >= 16 and data[0] == 0xC0:
                        break

                if msgLen >= 16:
                    msg = self.unescape_msg(msg, msgLen)
                    # Set msgLen = 0 at start so we don't have to do it on errors below.
                    # len($msg) now contains the unescaped message length.
                    msgLen = 0

                    msgRxCount += 1

                    # When the sendTWCMsg web command is used to send a message to the
                    # TWC, it sets lastTWCResponseMsg = b''.  When we see that here,
                    # set lastTWCResponseMsg to any unusual message received in response
                    # to the sent message.  Never set lastTWCResponseMsg to a commonly
                    # repeated message like master or slave linkready, heartbeat, or
                    # voltage/kWh report.
                    if (
                        master.lastTWCResponseMsg == b""
                        and msg[0:2] != b"\xFB\xE0"
                        and msg[0:2] != b"\xFD\xE0"
                        and msg[0:2] != b"\xFC\xE1"
                        and msg[0:2] != b"\xFB\xE2"
                        and msg[0:2] != b"\xFD\xE2"
                        and msg[0:2] != b"\xFB\xEB"
                        and msg[0:2] != b"\xFD\xEB"
                        and msg[0:2] != b"\xFD\xE0"
                    ):
                        master.lastTWCResponseMsg = msg

                    logger.log(
                        logging.INFO9,
                        "Rx@"
                        + ": ("
                        + self.hex_str(ignoredData)
                        + ") "
                        + self.hex_str(msg)
                        + "",
                    )

                    ignoredData = bytearray()

                    # After unescaping special values and removing the leading and
                    # trailing C0 bytes, the messages we know about are always 14 bytes
                    # long in original TWCs, or 16 bytes in newer TWCs (protocolVersion
                    # == 2).
                    if len(msg) != 14 and len(msg) != 16 and len(msg) != 20:
                        logger.info(
                            "ERROR: Ignoring message of unexpected length %d: %s"
                            % (len(msg), self.hex_str(msg))
                        )
                        continue

                    checksumExpected = msg[len(msg) - 1]
                    checksum = 0
                    for i in range(1, len(msg) - 1):
                        checksum += msg[i]

                    if (checksum & 0xFF) != checksumExpected:
                        logger.info(
                            "ERROR: Checksum %X does not match %02X.  Ignoring message: %s"
                            % (checksum, checksumExpected, self.hex_str(msg))
                        )
                        continue

                    if fakeMaster == 1:
                        ############################
                        # Pretend to be a master TWC

                        foundMsgMatch = False
                        # We end each regex message search below with \Z instead of $
                        # because $ will match a newline at the end of the string or the
                        # end of the string (even without the re.MULTILINE option), and
                        # sometimes our strings do end with a newline character that is
                        # actually the CRC byte with a value of 0A or 0D.
                        msgMatch = re.search(
                            b"^\xfd\xb1(..)\x00\x00.+\Z", msg, re.DOTALL
                        )
                        if msgMatch and foundMsgMatch == False:
                            # Handle acknowledgement of Start command
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)

                        msgMatch = re.search(
                            b"^\xfd\xb2(..)\x00\x00.+\Z", msg, re.DOTALL
                        )
                        if msgMatch and foundMsgMatch == False:
                            # Handle acknowledgement of Stop command
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)

                        msgMatch = re.search(
                            b"^\xfd\xe2(..)(.)(..)\x00\x00\x00\x00\x00\x00.+\Z",
                            msg,
                            re.DOTALL,
                        )
                        if msgMatch and foundMsgMatch == False:
                            # Handle linkready message from slave.
                            #
                            # We expect to see one of these before we start sending our
                            # own heartbeat message to slave.
                            # Once we start sending our heartbeat to slave once per
                            # second, it should no longer send these linkready messages.
                            # If slave doesn't hear master's heartbeat for around 10
                            # seconds, it sends linkready once per 10 seconds and starts
                            # flashing its red LED 4 times with the top green light on.
                            # Red LED stops flashing if we start sending heartbeat
                            # again.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            sign = msgMatch.group(2)
                            maxAmps = (
                                (msgMatch.group(3)[0] << 8) + msgMatch.group(3)[1]
                            ) / 100

                            logger.info(
                                "%.2f amp Gen2 TWC %02X%02X is ready to link.  Sign: %s"
                                % (
                                    maxAmps,
                                    senderID[0],
                                    senderID[1],
                                    self.hex_str(sign),
                                )
                            )

                            if maxAmps >= 80:
                                # U.S. chargers need a spike to 21A to cancel a 6A
                                # charging limit imposed in an Oct 2017 Tesla car
                                # firmware update. See notes where
                                # spikeAmpsToCancel6ALimit is used.
                                master.setSpikeAmps(21)
                            else:
                                # EU chargers need a spike to only 16A.  This value
                                # comes from a forum post and has not been directly
                                # tested.
                                master.setSpikeAmps(16)

                            if senderID == self.getFakeTWCID():
                                logger.info(
                                    "Gen2 TWC %02X%02X reports same TWCID as master.  "
                                    "TWC should resolve by changing its TWCID."
                                    % (senderID[0], senderID[1])
                                )
                                # I tested sending a linkready to a real master with the
                                # same TWCID as master and instead of master sending back
                                # its heartbeat message, it sent 5 copies of its
                                # linkready1 and linkready2 messages. Those messages
                                # will prompt a real slave to pick a new random value
                                # for its TWCID.
                                #
                                # We mimic that behavior by setting numInitMsgsToSend =
                                # 10 to make the idle code at the top of the for()
                                # loop send 5 copies of linkready1 and linkready2.
                                numInitMsgsToSend = 10
                                continue

                            # We should always get this linkready message at least once
                            # and generally no more than once, so this is a good
                            # opportunity to add the slave to our known pool of slave
                            # devices.
                            slaveTWC = self.newTWC(senderID, maxAmps)

                            if (
                                slaveTWC.protocolVersion == 1
                                and slaveTWC.minAmpsTWCSupports == 6
                            ):
                                if len(msg) == 14:
                                    slaveTWC.protocolVersion = 1
                                    slaveTWC.minAmpsTWCSupports = 5
                                elif len(msg) == 16:
                                    slaveTWC.protocolVersion = 2
                                    slaveTWC.minAmpsTWCSupports = 6

                                logger.info(
                                    "Set TWC %02X%02X protocolVersion to %d, minAmpsTWCSupports to %d."
                                    % (
                                        senderID[0],
                                        senderID[1],
                                        slaveTWC.protocolVersion,
                                        slaveTWC.minAmpsTWCSupports,
                                    )
                                )

                            # We expect maxAmps to be 80 on U.S. chargers and 32 on EU
                            # chargers. Either way, don't allow
                            # slaveTWC.wiringMaxAmps to be greater than maxAmps.
                            if slaveTWC.wiringMaxAmps > maxAmps:
                                logger.info(
                                    "\n\n!!! DANGER DANGER !!!\nYou have set wiringMaxAmpsPerTWC to "
                                    + str(self.configConfig["wiringMaxAmpsPerTWC"])
                                    + " which is greater than the max "
                                    + str(maxAmps)
                                    + " amps your charger says it can handle.  "
                                    "Please review instructions in the source code and consult an "
                                    "electrician if you don't know what to do."
                                )
                                slaveTWC.wiringMaxAmps = maxAmps / 4

                            # Make sure we print one SHB message after a slave
                            # linkready message is received by clearing
                            # lastHeartbeatDebugOutput. This helps with debugging
                            # cases where I can't tell if we responded with a
                            # heartbeat or not.
                            slaveTWC.lastHeartbeatDebugOutput = ""

                            slaveTWC.timeLastRx = time.time()
                            slaveTWC.send_master_heartbeat()
                        else:
                            msgMatch = re.search(
                                b"\A\xfd\xe0(..)(..)(.......+?).\Z", msg, re.DOTALL
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle heartbeat message from slave.
                            #
                            # These messages come in as a direct response to each
                            # heartbeat message from master. Slave does not send its
                            # heartbeat until it gets one from master first.
                            # A real master sends heartbeat to a slave around once per
                            # second, so we do the same near the top of this for()
                            # loop. Thus, we should receive a heartbeat reply from the
                            # slave around once per second as well.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            receiverID = msgMatch.group(2)
                            heartbeatData = msgMatch.group(3)

                            try:
                                slaveTWC = self.getTWCByID(senderID)
                            except KeyError:
                                # Normally, a slave only sends us a heartbeat message if
                                # we send them ours first, so it's not expected we would
                                # hear heartbeat from a slave that's not in our list.
                                logger.info(
                                    "ERROR: Received heartbeat message from "
                                    "TWC %02X%02X that we've not met before."
                                    % (senderID[0], senderID[1])
                                )
                                continue

                            if self.getFakeTWCID() == receiverID:
                                slaveTWC.receive_slave_heartbeat(heartbeatData)
                            else:
                                # I've tried different self.getFakeTWCID() values to verify a
                                # slave will send our self.getFakeTWCID() back to us as
                                # receiverID. However, I once saw it send receiverID =
                                # 0000.
                                # I'm not sure why it sent 0000 and it only happened
                                # once so far, so it could have been corruption in the
                                # data or an unusual case.
                                logger.info(
                                    "WARNING: TWC %02X%02X status data: "
                                    "%s sent to unknown TWC %02X%02X."
                                    % (
                                        senderID[0],
                                        senderID[1],
                                        self.hex_str(heartbeatData),
                                        receiverID[0],
                                        receiverID[1],
                                    )
                                )
                        else:
                            msgMatch = re.search(
                                b"\A\xfd\xeb(..)(....)(..)(..)(..)(.+?).\Z",
                                msg,
                                re.DOTALL,
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle kWh total and voltage message from slave.
                            #
                            # This message can only be generated by TWCs running newer
                            # firmware.  I believe it's only sent as a response to a
                            # message from Master in this format:
                            #   FB EB <Master TWCID> <Slave TWCID> 00 00 00 00 00 00 00 00 00
                            # According to FuzzyLogic, this message has the following
                            # format on an EU (3-phase) TWC:
                            #   FD EB <Slave TWCID> 00000038 00E6 00F1 00E8 00
                            #   00000038 (56) is the total kWh delivered to cars
                            #     by this TWC since its construction.
                            #   00E6 (230) is voltage on phase A
                            #   00F1 (241) is voltage on phase B
                            #   00E8 (232) is voltage on phase C
                            #
                            # I'm guessing in world regions with two-phase power that
                            # this message would be four bytes shorter, but the pattern
                            # above will match a message of any length that starts with
                            # FD EB.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            lifetimekWh = msgMatch.group(2)
                            kWh = (
                                (lifetimekWh[0] << 24)
                                + (lifetimekWh[1] << 16)
                                + (lifetimekWh[2] << 8)
                                + lifetimekWh[3]
                            )
                            vPhaseA = msgMatch.group(3)
                            voltsPhaseA = (vPhaseA[0] << 8) + vPhaseA[1]
                            vPhaseB = msgMatch.group(4)
                            voltsPhaseB = (vPhaseB[0] << 8) + vPhaseB[1]
                            vPhaseC = msgMatch.group(5)
                            voltsPhaseC = (vPhaseC[0] << 8) + vPhaseC[1]
                            data = msgMatch.group(6)

                            logger.info(
                                "TWC %02X%02X: Delivered %d kWh, voltage per phase: (%d, %d, %d).",
                                senderID[0],
                                senderID[1],
                                kWh,
                                voltsPhaseA,
                                voltsPhaseB,
                                voltsPhaseC,
                                extra={
                                    "logtype": "slave_status",
                                    "TWCID": senderID,
                                    "kWh": kWh,
                                    "voltsPerPhase": [
                                        voltsPhaseA,
                                        voltsPhaseB,
                                        voltsPhaseC,
                                    ],
                                },
                            )

                            # Update the timestamp of the last reciept of this message
                            self.lastkWhMessage = time.time()

                            # Every time we get this message, we re-queue the query
                            master.queue_background_task({"cmd": "getLifetimekWh"})

                            # Update this detail for the Slave TWC
                            self.updateLifetime(
                                senderID, kWh, voltsPhaseA, voltsPhaseB, voltsPhaseC
                            )

                        else:
                            msgMatch = re.search(
                                b"\A\xfd(\xee|\xef|\xf1)(..)(.+?).\Z", msg, re.DOTALL
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Get 7 characters of VIN from slave. (XE is first 7, XF second 7)
                            #
                            # This message can only be generated by TWCs running newer
                            # firmware.  I believe it's only sent as a response to a
                            # message from Master in this format:
                            #   FB EE <Master TWCID> <Slave TWCID> 00 00 00 00 00 00 00 00 00

                            # Response message is FD EE <Slave TWCID> VV VV VV VV VV VV VV where VV is an ascii character code
                            # representing a letter or number. VV will be all zero when car CAN communication is disabled
                            # (DIP switch 2 down) or when a non-Tesla vehicle is plugged in using something like a JDapter.

                            foundMsgMatch = True
                            vinPart = msgMatch.group(1)
                            senderID = msgMatch.group(2)
                            data = msgMatch.group(3)

                            logger.log(
                                logging.INFO6,
                                "Slave TWC %02X%02X reported VIN data: %s."
                                % (senderID[0], senderID[1], self.hex_str(data)),
                            )
                            slaveTWC = self.getTWCByID(senderID)
                            if vinPart == b"\xee":
                                vinPart = 0
                            if vinPart == b"\xef":
                                vinPart = 1
                            if vinPart == b"\xf1":
                                vinPart = 2
                            slaveTWC.VINData[vinPart] = data.decode("utf-8").rstrip(
                                "\x00"
                            )
                            if vinPart < 2:
                                vinPart += 1
                                master.queue_background_task(
                                    {
                                        "cmd": "getVehicleVIN",
                                        "slaveTWC": senderID,
                                        "vinPart": str(vinPart),
                                    }
                                )
                            else:
                                potentialVIN = "".join(slaveTWC.VINData)

                                # Ensure we have a valid VIN
                                if len(potentialVIN) == 17 or len(potentialVIN) == 0:
                                    # Record Vehicle VIN
                                    slaveTWC._currentVIN = potentialVIN

                                    # Clear VIN retry timer
                                    slaveTWC.lastVINQuery = 0
                                    slaveTWC.vinQueryAttempt = 0

                                    # Record this vehicle being connected
                                    master.recordVehicleVIN(slaveTWC)

                                    # Send VIN data to Status modules
                                    master.updateVINStatus()

                                    # Establish if this VIN should be able to charge
                                    # If not, send stop command
                                    master.queue_background_task(
                                        {
                                            "cmd": "checkVINEntitlement",
                                            "vin": potentialVIN,
                                        }
                                    )

                                    vinPart += 1
                                else:
                                    # Unfortunately the VIN was not the right length.
                                    # Re-request VIN
                                    master.queue_background_task(
                                        {
                                            "cmd": "getVehicleVIN",
                                            "slaveTWC": slaveTWC.TWCID,
                                            "vinPart": 0,
                                        }
                                    )

                            logger.log(
                                logging.INFO6,
                                "Current VIN string is: %s at part %d."
                                % (str(slaveTWC.VINData), vinPart),
                            )

                        else:
                            msgMatch = re.search(
                                b"\A\xfc(\xe1|\xe2)(..)(.)\x00\x00\x00\x00\x00\x00\x00\x00.+\Z",
                                msg,
                                re.DOTALL,
                            )
                        if msgMatch and foundMsgMatch == False:
                            foundMsgMatch = True
                            logger.info(
                                "ERROR: TWC is set to Master mode so it can't be controlled by TWCManager.  "
                                "Search installation instruction PDF for 'rotary switch' and set "
                                "switch so its arrow points to F on the dial."
                            )
                        if foundMsgMatch == False:
                            logger.info(
                                "*** UNKNOWN MESSAGE FROM SLAVE:"
                                + self.hex_str(msg)
                                + "\nPlease private message user CDragon at http://teslamotorsclub.com "
                                "with a copy of this error."
                            )
                    else:
                        ###########################
                        # Pretend to be a slave TWC

                        foundMsgMatch = False
                        msgMatch = re.search(
                            b"\A\xfc\xe1(..)(.)\x00\x00\x00\x00\x00\x00\x00\x00+?.\Z",
                            msg,
                            re.DOTALL,
                        )
                        if msgMatch and foundMsgMatch == False:
                            # Handle linkready1 from master.
                            # See notes in send_master_linkready1() for details.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            sign = msgMatch.group(2)
                            master.setMasterTWCID(senderID)

                            # This message seems to always contain seven 00 bytes in its
                            # data area. If we ever get this message with non-00 data
                            # we'll print it as an unexpected message.
                            logger.info(
                                "Master TWC %02X%02X Linkready1.  Sign: %s"
                                % (senderID[0], senderID[1], self.hex_str(sign))
                            )

                            if senderID == self.getFakeTWCID():
                                master.master_id_conflict()

                            # Other than picking a new self.getFakeTWCID() if ours conflicts with
                            # master, it doesn't seem that a real slave will make any
                            # sort of direct response when sent a master's linkready1 or
                            # linkready2.

                        else:
                            msgMatch = re.search(
                                b"\A\xfb\xe2(..)(.)\x00\x00\x00\x00\x00\x00\x00\x00+?.\Z",
                                msg,
                                re.DOTALL,
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle linkready2 from master.
                            # See notes in send_master_linkready2() for details.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            sign = msgMatch.group(2)
                            master.setMasterTWCID(senderID)

                            # This message seems to always contain seven 00 bytes in its
                            # data area. If we ever get this message with non-00 data
                            # we'll print it as an unexpected message.

                            logger.info(
                                "Master TWC %02X%02X Linkready2.  Sign: %s"
                                % (senderID[0], senderID[1], self.hex_str(sign))
                            )

                            if senderID == self.getFakeTWCID():
                                master.master_id_conflict()
                        else:
                            msgMatch = re.search(
                                b"\A\xfb\xe0(..)(..)(.......+?).\Z", msg, re.DOTALL
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle heartbeat message from Master.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            receiverID = msgMatch.group(2)
                            heartbeatData = msgMatch.group(3)
                            master.setMasterTWCID(senderID)
                            try:
                                slaveTWC = self.knownTWCsByID[receiverID]
                            except KeyError:
                                slaveTWC = self.newTWC(receiverID, 80)

                            slaveTWC.masterHeartbeatData = heartbeatData

                            if receiverID != self.getFakeTWCID():
                                # This message was intended for another slave.
                                # Ignore it.
                                logger.log(
                                    logging.DEBUG2,
                                    "Master %02X%02X sent "
                                    "heartbeat message %s to receiver %02X%02X "
                                    "that isn't our fake slave."
                                    % (
                                        senderID[0],
                                        senderID[1],
                                        self.hex_str(heartbeatData),
                                        receiverID[0],
                                        receiverID[1],
                                    ),
                                )
                                continue

                            amps = (
                                master.slaveHeartbeatData[1] << 8
                            ) + master.slaveHeartbeatData[2]
                            master.addkWhDelivered(
                                (master.convertAmpsToWatts(amps / 100) / 1000 / 60 / 60)
                                * (now - timeLastkWhDelivered)
                            )
                            timeLastkWhDelivered = now
                            if time.time() - timeLastkWhSaved >= 300.0:
                                timeLastkWhSaved = now
                                logger.log(
                                    logging.INFO9,
                                    "Fake slave has delivered %.3fkWh"
                                    % (master.getkWhDelivered()),
                                )
                                # Save settings to file
                                master.queue_background_task({"cmd": "saveSettings"})

                            if heartbeatData[0] == 0x07:
                                # Lower amps in use (not amps allowed) by 2 for 10
                                # seconds. Set state to 07.
                                master.slaveHeartbeatData[0] = heartbeatData[0]
                                timeToRaise2A = now + 10
                                amps -= 280
                                master.slaveHeartbeatData[3] = (amps >> 8) & 0xFF
                                master.slaveHeartbeatData[4] = amps & 0xFF
                            elif heartbeatData[0] == 0x06:
                                # Raise amp setpoint by 2 permanently and reply with
                                # state 06.  After 44 seconds, report state 0A.
                                timeTo0Aafter06 = now + 44
                                master.slaveHeartbeatData[0] = heartbeatData[0]
                                amps += 200
                                master.slaveHeartbeatData[1] = (amps >> 8) & 0xFF
                                master.slaveHeartbeatData[2] = amps & 0xFF
                                amps -= 80
                                master.slaveHeartbeatData[3] = (amps >> 8) & 0xFF
                                master.slaveHeartbeatData[4] = amps & 0xFF
                            elif (
                                heartbeatData[0] == 0x05
                                or heartbeatData[0] == 0x08
                                or heartbeatData[0] == 0x09
                            ):
                                if ((heartbeatData[1] << 8) + heartbeatData[2]) > 0:
                                    # A real slave mimics master's status bytes [1]-[2]
                                    # representing max charger power even if the master
                                    # sends it a crazy value.
                                    master.slaveHeartbeatData[1] = heartbeatData[1]
                                    master.slaveHeartbeatData[2] = heartbeatData[2]

                                    ampsUsed = (heartbeatData[1] << 8) + heartbeatData[
                                        2
                                    ]
                                    ampsUsed -= 80
                                    master.slaveHeartbeatData[3] = (
                                        ampsUsed >> 8
                                    ) & 0xFF
                                    master.slaveHeartbeatData[4] = ampsUsed & 0xFF
                            elif heartbeatData[0] == 0:
                                if timeTo0Aafter06 > 0 and timeTo0Aafter06 < now:
                                    timeTo0Aafter06 = 0
                                    master.slaveHeartbeatData[0] = 0x0A
                                elif timeToRaise2A > 0 and timeToRaise2A < now:
                                    # Real slave raises amps used by 2 exactly 10
                                    # seconds after being sent into state 07. It raises
                                    # a bit slowly and sets its state to 0A 13 seconds
                                    # after state 07. We aren't exactly emulating that
                                    # timing here but hopefully close enough.
                                    timeToRaise2A = 0
                                    amps -= 80
                                    master.slaveHeartbeatData[3] = (amps >> 8) & 0xFF
                                    master.slaveHeartbeatData[4] = amps & 0xFF
                                    master.slaveHeartbeatData[0] = 0x0A
                            elif heartbeatData[0] == 0x02:
                                logger.info(
                                    "Master heartbeat contains error %ld: %s"
                                    % (heartbeatData[1], self.hex_str(heartbeatData))
                                )
                            else:
                                logger.info(
                                    "UNKNOWN MHB state %s"
                                    % (self.hex_str(heartbeatData))
                                )

                            # Slaves always respond to master's heartbeat by sending
                            # theirs back.
                            slaveTWC.send_slave_heartbeat(senderID)
                            slaveTWC.print_status(master.slaveHeartbeatData)
                        else:
                            msgMatch = re.search(
                                b"\A\xfc\x1d\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00+?.\Z",
                                msg,
                                re.DOTALL,
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle 2-hour idle message
                            #
                            # This message is sent from a Master TWC three times in a
                            # row every 2 hours:
                            #   c0 fc 1d 00 00 00 00 00 00 00 00 00 00 00 1d c0
                            #
                            # I'd say this is used to indicate the master is still
                            # alive, but it doesn't contain the Master's TWCID or any other
                            # data so I don't see what any receiving TWC can do with it.
                            #
                            # I suspect this message is only sent when the master
                            # doesn't see any other TWCs on the network, so I don't
                            # bother to have our fake master send these messages being
                            # as there's no point in playing a fake master with no
                            # slaves around.
                            foundMsgMatch = True
                            logger.info("Received 2-hour idle message from Master.")
                        else:
                            msgMatch = re.search(
                                b"\A\xfd\xe2(..)(.)(..)\x00\x00\x00\x00\x00\x00.+\Z",
                                msg,
                                re.DOTALL,
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle linkready message from slave on network that
                            # presumably isn't us.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            sign = msgMatch.group(2)
                            maxAmps = (
                                (msgMatch.group(3)[0] << 8) + msgMatch.group(3)[1]
                            ) / 100
                            logger.info(
                                "%.2f amp Gen2 TWC %02X%02X is ready to link.  Sign: %s"
                                % (
                                    maxAmps,
                                    senderID[0],
                                    senderID[1],
                                    self.hex_str(sign),
                                )
                            )
                            if senderID == self.getFakeTWCID():
                                logger.info(
                                    "ERROR: Received heartbeat message from "
                                    "TWC %02X%02X that has the same TWCID as our fake TWC."
                                    % (senderID[0], senderID[1])
                                )
                                continue

                            self.newTWC(senderID, maxAmps)
                        else:
                            msgMatch = re.search(
                                b"\A\xfd\xe0(..)(..)(.......+?).\Z", msg, re.DOTALL
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle heartbeat message from slave on network that
                            # presumably isn't us.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            receiverID = msgMatch.group(2)
                            heartbeatData = msgMatch.group(3)

                            if senderID == self.getFakeTWCID():
                                logger.info(
                                    "ERROR: Received heartbeat message from "
                                    "TWC %02X%02X that has the same TWCID as our fake TWC."
                                    % (senderID[0], senderID[1])
                                )
                                continue

                            try:
                                slaveTWC = self.knownTWCsByID[senderID]
                            except KeyError:
                                # Slave is unlikely to send another linkready since it's
                                # already linked with a real Master TWC, so just assume
                                # it's 80A.
                                slaveTWC = self.newTWC(senderID, 80)

                            slaveTWC.print_status(heartbeatData)
                        else:
                            msgMatch = re.search(
                                b"\A\xfb\xeb(..)(..)(\x00\x00\x00\x00\x00\x00\x00\x00\x00+?).\Z",
                                msg,
                                re.DOTALL,
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle voltage request message.  This is only supported in
                            # Protocol 2 so we always reply with a 16-byte message.
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            receiverID = msgMatch.group(2)

                            if senderID == self.getFakeTWCID():
                                logger.info(
                                    "ERROR: Received voltage request message from "
                                    "TWC %02X%02X that has the same TWCID as our fake slave."
                                    % (senderID[0], senderID[1])
                                )
                                continue

                            logger.log(
                                logging.INFO8,
                                "VRQ from %02X%02X to %02X%02X"
                                % (
                                    senderID[0],
                                    senderID[1],
                                    receiverID[0],
                                    receiverID[1],
                                ),
                            )

                            if receiverID == self.getFakeTWCID():
                                kWhCounter = int(master.getkWhDelivered())
                                kWhPacked = bytearray(
                                    [
                                        ((kWhCounter >> 24) & 0xFF),
                                        ((kWhCounter >> 16) & 0xFF),
                                        ((kWhCounter >> 8) & 0xFF),
                                        (kWhCounter & 0xFF),
                                    ]
                                )
                                logger.info(
                                    "VRS %02X%02X: %dkWh (%s) %dV %dV %dV"
                                    % (
                                        self.getFakeTWCID()[0],
                                        self.getFakeTWCID()[1],
                                        kWhCounter,
                                        self.hex_str(kWhPacked),
                                        240,
                                        0,
                                        0,
                                    )
                                )
                                self.getInterfaceModule().send(
                                    bytearray(b"\xFD\xEB")
                                    + self.getFakeTWCID()
                                    + kWhPacked
                                    + bytearray(b"\x00\xF0\x00\x00\x00\x00\x00")
                                )
                        else:
                            msgMatch = re.search(
                                b"\A\xfd\xeb(..)(.........+?).\Z", msg, re.DOTALL
                            )
                        if msgMatch and foundMsgMatch == False:
                            # Handle voltage response message.
                            # Example US value:
                            #   FD EB 7777 00000014 00F6 0000 0000 00
                            # EU value (3 phase power):
                            #   FD EB 7777 00000038 00E6 00F1 00E8 00
                            foundMsgMatch = True
                            senderID = msgMatch.group(1)
                            data = msgMatch.group(2)
                            kWhCounter = (
                                (data[0] << 24)
                                + (data[1] << 16)
                                + (data[2] << 8)
                                + data[3]
                            )
                            voltsPhaseA = (data[4] << 8) + data[5]
                            voltsPhaseB = (data[6] << 8) + data[7]
                            voltsPhaseC = (data[8] << 8) + data[9]

                            # Update this detail for the Slave TWC
                            self.updateLifetime(
                                senderID,
                                kWhCounter,
                                voltsPhaseA,
                                voltsPhaseB,
                                voltsPhaseC,
                            )

                            if senderID == self.getFakeTWCID():
                                logger.info(
                                    "ERROR: Received voltage response message from "
                                    "TWC %02X%02X that has the same TWCID as our fake slave."
                                    % (senderID[0], senderID[1])
                                )
                                continue

                            logger.info(
                                "VRS %02X%02X: %dkWh %dV %dV %dV"
                                % (
                                    senderID[0],
                                    senderID[1],
                                    kWhCounter,
                                    voltsPhaseA,
                                    voltsPhaseB,
                                    voltsPhaseC,
                                )
                            )

                        if foundMsgMatch == False:
                            logger.info(
                                "***UNKNOWN MESSAGE from master: " + self.hex_str(msg)
                            )

            except Exception as e:
                # Print info about unhandled exceptions, then continue.  Search for
                # 'Traceback' to find these in the log.
                traceback.print_exc()
                logger.info("Unhandled Exception:" + traceback.format_exc())
                # Sleep 5 seconds so the user might see the error.
                time.sleep(5)

        # We've been signalled to stop. Close the input module.
        self.getInterfaceModule().close()

    def unescape_msg(self, inmsg: bytearray, msgLen):
        # Given a message received on the RS485 network, remove leading and trailing
        # C0 byte, unescape special byte values, and verify its data matches the CRC
        # byte.

        # Note that a bytearray is mutable, whereas a bytes object isn't.
        # By initializing a bytearray and concatenating the incoming bytearray
        # to it, we protect against being passed an immutable bytes object
        msg = bytearray() + inmsg[0:msgLen]

        # See notes in RS485.send() for the way certain bytes in messages are escaped.
        # We basically want to change db dc into c0 and db dd into db.
        # Only scan to one less than the length of the string to avoid running off
        # the end looking at i+1.
        i = 0
        while i < len(msg):
            if msg[i] == 0xDB:
                if msg[i + 1] == 0xDC:
                    # Replace characters at msg[i] and msg[i+1] with 0xc0,
                    # shortening the string by one character. In Python, msg[x:y]
                    # refers to a substring starting at x and ending immediately
                    # before y. y - x is the length of the substring.
                    msg[i : i + 2] = [0xC0]
                elif msg[i + 1] == 0xDD:
                    msg[i : i + 2] = [0xDB]
                else:
                    logger.info(
                        "ERROR: Special character 0xDB in message is "
                        "followed by invalid character 0x%02X.  "
                        "Message may be corrupted." % (msg[i + 1])
                    )

                    # Replace the character with something even though it's probably
                    # not the right thing.
                    msg[i : i + 2] = [0xDB]
            i = i + 1

        # Remove leading and trailing C0 byte.
        msg = msg[1 : len(msg) - 1]
        return msg

    def hex_str(self, s: str):
        return " ".join("{:02X}".format(ord(c)) for c in s)

    def hex_str(self, ba: bytearray):
        return " ".join("{:02X}".format(c) for c in ba)

    def send_master_linkready1(self):

        logger.log(logging.INFO8, "Send master linkready1")

        # When master is powered on or reset, it sends 5 to 7 copies of this
        # linkready1 message followed by 5 copies of linkready2 (I've never seen
        # more or less than 5 of linkready2).
        #
        # This linkready1 message advertises master's TWCID to other slaves on the
        # network.
        # If a slave happens to have the same id as master, it will pick a new
        # random TWCID. Other than that, slaves don't seem to respond to linkready1.

        # linkready1 and linkready2 are identical except FC E1 is replaced by FB E2
        # in bytes 2-3. Both messages will cause a slave to pick a new id if the
        # slave's id conflicts with master.
        # If a slave stops sending heartbeats for awhile, master may send a series
        # of linkready1 and linkready2 messages in seemingly random order, which
        # means they don't indicate any sort of startup state.

        # linkready1 is not sent again after boot/reset unless a slave sends its
        # linkready message.
        # At that point, linkready1 message may start sending every 1-5 seconds, or
        # it may not be sent at all.
        # Behaviors I've seen:
        #   Not sent at all as long as slave keeps responding to heartbeat messages
        #   right from the start.
        #   If slave stops responding, then re-appears, linkready1 gets sent
        #   frequently.

        # One other possible purpose of linkready1 and/or linkready2 is to trigger
        # an error condition if two TWCs on the network transmit those messages.
        # That means two TWCs have rotary switches setting them to master mode and
        # they will both flash their red LED 4 times with top green light on if that
        # happens.

        # Also note that linkready1 starts with FC E1 which is similar to the FC D1
        # message that masters send out every 4 hours when idle. Oddly, the FC D1
        # message contains all zeros instead of the master's id, so it seems
        # pointless.

        # I also don't understand the purpose of having both linkready1 and
        # linkready2 since only two or more linkready2 will provoke a response from
        # a slave regardless of whether linkready1 was sent previously. Firmware
        # trace shows that slaves do something somewhat complex when they receive
        # linkready1 but I haven't been curious enough to try to understand what
        # they're doing. Tests show neither linkready1 or 2 are necessary. Slaves
        # send slave linkready every 10 seconds whether or not they got master
        # linkready1/2 and if a master sees slave linkready, it will start sending
        # the slave master heartbeat once per second and the two are then connected.
        self.getInterfaceModule().send(
            bytearray(b"\xFC\xE1")
            + self.TWCID
            + self.masterSign
            + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        )

    def send_master_linkready2(self):

        logger.log(logging.INFO8, "Send master linkready2")

        # This linkready2 message is also sent 5 times when master is booted/reset
        # and then not sent again if no other TWCs are heard from on the network.
        # If the master has ever seen a slave on the network, linkready2 is sent at
        # long intervals.
        # Slaves always ignore the first linkready2, but respond to the second
        # linkready2 around 0.2s later by sending five slave linkready messages.
        #
        # It may be that this linkready2 message that sends FB E2 and the master
        # heartbeat that sends fb e0 message are really the same, (same FB byte
        # which I think is message type) except the E0 version includes the TWC ID
        # of the slave the message is intended for whereas the E2 version has no
        # recipient TWC ID.
        #
        # Once a master starts sending heartbeat messages to a slave, it
        # no longer sends the global linkready2 message (or if it does,
        # they're quite rare so I haven't seen them).
        self.getInterfaceModule().send(
            bytearray(b"\xFB\xE2")
            + self.TWCID
            + self.masterSign
            + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
        )

    def send_slave_linkready(self):
        # In the message below, \x1F\x40 (hex 0x1f40 or 8000 in base 10) refers to
        # this being a max 80.00Amp charger model.
        # EU chargers are 32A and send 0x0c80 (3200 in base 10).
        #
        # I accidentally changed \x1f\x40 to \x2e\x69 at one point, which makes the
        # master TWC immediately start blinking its red LED 6 times with top green
        # LED on. Manual says this means "The networked Wall Connectors have
        # different maximum current capabilities".
        msg = (
            bytearray(b"\xFD\xE2")
            + self.TWCID
            + self.slaveSign
            + bytearray(b"\x1F\x40\x00\x00\x00\x00\x00\x00")
        )
        if self.protocolVersion == 2:
            msg += bytearray(b"\x00\x00")

        self.getInterfaceModule().send(msg)

    def getInterfaceModule(self):
        return self.master.getModulesByType("Interface")[0]["ref"]

    def getLifetimekWh(self):

        # This function is called from a Scheduled Task
        # If it's been at least 1 minute, then query all known Slave TWCs
        # to determine their lifetime kWh and per-phase voltages
        now = time.time()
        if now >= self.lastkWhPoll + 60:
            for slaveTWC in self.getTWCs():
                self.getInterfaceModule().send(
                    bytearray(b"\xFB\xEB")
                    + self.TWCID
                    + slaveTWC.TWCID
                    + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
                )
            self.lastkWhPoll = now

    def getTWCByID(self, twcid):
        return self.knownTWCsByID[twcid]

    def getTWCID(self, twc):
        return self.knownTWCs[twc].TWCID

    def getTWC(self, index):
        return self.knownTWCs[index]

    def getTWCs(self):
        # Returns a list of all TWCs
        return self.knownTWCs

    @property
    def allEVSEs(self):
        # Public version of getTWCs()
        return self.knownTWCs

    @property
    def maxPower(self):
        return self.master.convertAmpsToWatts(self.configConfig["wiringMaxAmpsAllTWCs"])
    
    def getSpareAmps(self):
        # Returns the number of amps that are not being used by any TWC
        # This is used to determine how many amps can be allocated to a new TWC
        # that has just been discovered
        spareAmps = self.configConfig["wiringMaxAmpsAllTWCs"]
        for twc in self.knownTWCs:
            spareAmps -= twc.reportedAmpsActual
        return spareAmps

    def getSlaveSign(self):
        return self.slaveSign

    def master_id_conflict(self):
        # We're playing fake slave, and we got a message from a master with our TWCID.
        # By convention, as a slave we must change our TWCID because a master will not.
        self.TWCID[0] = random.randint(0, 0xFF)
        self.TWCID[1] = random.randint(0, 0xFF)

        # Real slaves change their sign during a conflict, so we do too.
        self.slaveSign[0] = random.randint(0, 0xFF)

        logger.info(
            "Master's TWCID matches our fake slave's TWCID.  "
            "Picked new random TWCID %02X%02X with sign %02X"
            % (self.TWCID[0], self.TWCID[1], self.slaveSign[0])
        )

    def getMasterTWCID(self):
        # This is called when TWCManager is in Slave mode, to track the
        # master's TWCID
        return self.masterTWCID

    def setMasterTWCID(self, twcid):
        # This is called when TWCManager is in Slave mode, to track the
        # master's TWCID
        self.masterTWCID = twcid

    def getMasterHeartbeatOverride(self):
        return self.overrideMasterHeartbeatData

    def newTWC(self, newTWCID, maxAmps):
        try:
            newTWC = self.knownTWCsByID[newTWCID]
            # We didn't get KeyError exception, so this TWC is already in
            # knownTWCs and we can simply return it.
            return newTWC
        except KeyError:
            pass

        newTWC = Gen2TWC(newTWCID, maxAmps, self.config, self.master, self)
        self.knownTWCsByID[newTWCID] = newTWC
        self.knownTWCs.append(newTWC)

        if self.countTWCs() > 3:
            logger.info(
                "WARNING: More than 3 TWCs seen on network. Dropping oldest: "
                + self.hex_str(self.getTWCID(0))
                + "."
            )
            self.deleteTWC(self.getTWCID(0))

        return newTWC

    def getVehicleVIN(self, slaveID, part):
        prefixByte = None
        if int(part) == 0:
            prefixByte = bytearray(b"\xFB\xEE")
        if int(part) == 1:
            prefixByte = bytearray(b"\xFB\xEF")
        if int(part) == 2:
            prefixByte = bytearray(b"\xFB\xF1")

        if prefixByte:
            self.getInterfaceModule().send(
                prefixByte
                + self.TWCID
                + slaveID
                + bytearray(b"\x00\x00\x00\x00\x00\x00\x00\x00")
            )

    def deleteTWC(self, deleteTWCID):
        for i in range(0, len(self.knownTWCs)):
            if self.knownTWCs[i].TWCID == deleteTWCID:
                del self.knownTWCs[i]
                break
        try:
            del self.knownTWCsByID[deleteTWCID]
        except KeyError:
            pass

    def countTWCs(self):
        return int(len(self.knownTWCs))

    def stopCharging(self, subTWC=None):
        # This function will loop through each of the TWCs, and send them the stop command.
        # If the subTWC parameter is supplied, we only stop the specified TWC
        for TWC in self.getTWCs():
            if (not subTWC) or (subTWC == TWC.TWCID):
                TWC.stopCharging()

    def startCharging(self):
        # This function will loop through each of the TWCs, and send them the start command.
        for TWC in self.getTWCs():
            TWC.startCharging()

    def retryVINQuery(self):
        # For each TWC, check if it's been more than 60 seconds since the last
        # VIN query without a VIN. If so, query again.
        for slaveTWC in self.getTWCs():
            if slaveTWC.isCharging == 1:
                if (
                    slaveTWC.lastVINQuery > 0
                    and slaveTWC.vinQueryAttempt < 6
                    and not slaveTWC.currentVIN
                ):
                    if (time.time() - slaveTWC.lastVINQuery) >= 60:
                        self.queue_background_task(
                            {
                                "cmd": "getVehicleVIN",
                                "slaveTWC": slaveTWC.TWCID,
                                "vinPart": 0,
                            }
                        )
                        slaveTWC.vinQueryAttempt += 1
                        slaveTWC.lastVINQuery = time.time()
            else:
                slaveTWC.lastVINQuery = 0

    def getTimeLastTx(self):
        return self.getInterfaceModule().timeLastTx

    def getFakeTWCID(self):
        return self.TWCID

    def updateLifetime(self, sender, kWh, vPA, vPB, vPC):
        for twc in self.getTWCs():
            if twc.TWCID == sender:
                twc.setLifetimekWh(kWh)
                twc.setVoltage(vPA, vPB, vPC)

