import json
import logging
import random
import time
from pathlib import Path
from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("Dummy", "Interface")


class Dummy:
    enabled = False
    master = None
    msgBuffer = bytes()
    proto = None
    twcID = bytearray(b"\x12\x34")
    timeLastTx = 0

    # Advanced scenario support
    slaves = {}
    scenario = None
    scenario_start_time = None
    use_scenarios = False

    # Behavior constants
    BEHAVIOR_NORMAL = "normal"
    BEHAVIOR_CHARGING = "charging"
    BEHAVIOR_CAR_PLUGGED = "car_plugged"
    BEHAVIOR_ERROR = "error"
    BEHAVIOR_INTERMITTENT = "intermittent"
    BEHAVIOR_SLOW = "slow"

    def __init__(self, master):
        self.master = master
        self.slaves = {}
        self.scenario_start_time = time.time()
        classname = self.__class__.__name__

        # Unload if this module is disabled or misconfigured
        if "interface" in master.config and classname in master.config["interface"]:
            self.enabled = master.config["interface"][classname].get("enabled", True)
        if not self.enabled:
            self.master.releaseModule("lib.TWCManager.Interface", classname)
            return None

        # Configure the module
        if "interface" in master.config:
            if master.config["interface"][classname].get("twcID", False):
                self.twcID = bytearray(
                    str(master.config["interface"][classname].get("twcID")).encode()
                )

        # Instantiate protocol module for sending/recieving TWC protocol
        self.proto = self.master.getModuleByName("TWCProtocol")

        # Load scenario configuration if available
        self._load_scenario()
        if self.scenario:
            self.use_scenarios = True
            self._initialize_slaves()
            logger.log(
                logging.INFO4,
                f"Dummy initialized with scenario: {len(self.slaves)} slave(s)",
            )

    def close(self):
        # NOOP - No need to close anything
        return 0

    def _load_scenario(self):
        """Load TWC scenario configuration from file if available."""
        scenario_file = (
            Path(__file__).parent.parent.parent.parent
            / "tests"
            / "fixtures"
            / "twc_scenarios.json"
        )

        if not scenario_file.exists():
            return

        try:
            with open(scenario_file, "r") as f:
                scenarios = json.load(f)

            # Get scenario name from config or use default
            scenario_name = (
                self.master.config.get("interface", {})
                .get("Dummy", {})
                .get("scenario", None)
            )

            if scenario_name and scenario_name in scenarios.get("scenarios", {}):
                self.scenario = scenarios["scenarios"][scenario_name]
                logger.log(logging.INFO4, f"Loaded scenario: {scenario_name}")
        except Exception as e:
            logger.log(logging.INFO4, f"Error loading scenario: {e}")

    def _initialize_slaves(self):
        """Initialize slave TWCs from scenario."""
        if not self.scenario or "slaves" not in self.scenario:
            return

        for slave_config in self.scenario["slaves"]:
            slave_id = slave_config.get("id", "AB")
            self.slaves[slave_id] = {
                "id": slave_id,
                "maxAmps": slave_config.get("maxAmps", 80),
                "behavior": slave_config.get("behavior", "normal"),
                "requestedAmps": slave_config.get("requestedAmps", 0),
                "actualAmps": slave_config.get("actualAmps", 0),
                "protocolVersion": slave_config.get("protocolVersion", 2),
                "firmwareVersion": slave_config.get("firmwareVersion", "4.5.3"),
                "errorCode": slave_config.get("errorCode", None),
                "responseDelay": slave_config.get("responseDelay", 0),
                "dropRate": slave_config.get("dropRate", 0),
                "lastHeartbeatTime": time.time(),
                "state": "idle",
            }

    def _check_dynamic_slaves(self):
        """Check if any dynamic slaves should be added."""
        if not self.scenario or "dynamicSlaves" not in self.scenario:
            return

        elapsed = time.time() - self.scenario_start_time

        for dynamic_slave in self.scenario.get("dynamicSlaves", []):
            join_after = dynamic_slave.get("joinAfter", 0)
            slave_id = dynamic_slave.get("id", "CD")

            if elapsed >= join_after and slave_id not in self.slaves:
                self.slaves[slave_id] = {
                    "id": slave_id,
                    "maxAmps": dynamic_slave.get("maxAmps", 40),
                    "behavior": dynamic_slave.get("behavior", "normal"),
                    "requestedAmps": 0,
                    "actualAmps": 0,
                    "protocolVersion": dynamic_slave.get("protocolVersion", 2),
                    "firmwareVersion": dynamic_slave.get("firmwareVersion", "4.5.3"),
                    "errorCode": None,
                    "responseDelay": 0,
                    "dropRate": 0,
                    "lastHeartbeatTime": time.time(),
                    "state": "idle",
                }
                logger.log(logging.INFO4, f"Dynamic slave {slave_id} joined")

    def _update_slave_state(self, slave):
        """Update slave state based on behavior configuration."""
        behavior = slave["behavior"]

        if behavior == self.BEHAVIOR_NORMAL:
            slave["state"] = "idle"
            slave["requestedAmps"] = 0
            slave["actualAmps"] = 0
        elif behavior == self.BEHAVIOR_CAR_PLUGGED:
            slave["state"] = "plugged"
            slave["requestedAmps"] = slave.get("requestedAmps", 32)
            slave["actualAmps"] = 0
        elif behavior == self.BEHAVIOR_CHARGING:
            slave["state"] = "charging"
            slave["requestedAmps"] = slave.get("requestedAmps", 32)
            slave["actualAmps"] = slave.get("actualAmps", 32)
        elif behavior == self.BEHAVIOR_ERROR:
            slave["state"] = "error"
            slave["requestedAmps"] = 0
            slave["actualAmps"] = 0

    def _send_internal(self, msg):
        """Send a message from the dummy slave to the master (scenario mode)."""
        msg = bytearray(msg)
        checksum = 0
        for i in range(1, len(msg)):
            checksum += msg[i]

        msg.append(checksum & 0xFF)

        # SLIP encoding: escape special characters
        i = 0
        while i < len(msg):
            if msg[i] == 0xC0:
                msg[i : i + 1] = b"\xdb\xdc"
                i = i + 1
            elif msg[i] == 0xDB:
                msg[i : i + 1] = b"\xdb\xdd"
                i = i + 1
            i = i + 1

        msg = bytearray(b"\xc0" + msg + b"\xc0\xfe")
        logger.log(logging.INFO9, f"TxInt@: {self.master.hex_str(msg)}")

        self.msgBuffer = msg

    def getBufferLen(self):
        # This function returns the size of the recieve buffer.
        # This is used by read functions to determine if information is waiting
        return len(self.msgBuffer)

    def read(self, len):
        # Read our buffered messages. We simulate this by making a copy of the
        # current message buffer, clearing the read message buffer bytes and then
        # returning the copied message to TWCManager. This is what it would look
        # like if we read from a serial interface
        localMsgBuffer = self.msgBuffer[:len]
        self.msgBuffer = self.msgBuffer[len:]
        return localMsgBuffer

    def send(self, msg):
        # This is the external send interface - it is called by TWCManager which expects that it is
        # talking to a live TWC. The key here is that we treat it as our reciept interface and parse
        # the message as if we are a TWC

        if self.use_scenarios:
            return self._send_scenario_mode(msg)
        else:
            return self._send_simple_mode(msg)

    def _send_simple_mode(self, msg):
        """Original simple mode - single slave, basic responses."""
        packet = self.proto.parseMessage(msg)
        if packet["Command"] == "MasterLinkready2":
            self.sendInternal(
                self.proto.createMessage(
                    {
                        "Command": "SlaveLinkready",
                        "SenderID": self.twcID,
                        "Sign": self.master.getSlaveSign(),
                        "Amps": bytearray(b"\x1f\x40"),
                    }
                )
            )
        elif packet["Command"] == "MasterHeartbeat":
            self.sendInternal(
                self.proto.createMessage(
                    {
                        "Command": "SlaveHeartbeat",
                        "SenderID": self.twcID,
                        "RecieverID": packet["SenderID"],
                    }
                )
            )

        logger.log(logging.INFO9, "Tx@: " + self.master.hex_str(msg))
        self.timeLastTx = time.time()
        return 0

    def _send_scenario_mode(self, msg):
        """Scenario mode - multi-slave with configurable behaviors."""
        try:
            packet = self.proto.parseMessage(msg)
            command = packet.get("Command", "")
            receiver_id = packet.get("RecieverID", bytearray(b""))

            receiver_id_str = (
                receiver_id.decode("utf-8", errors="ignore")
                if isinstance(receiver_id, bytes)
                else str(receiver_id)
            )

            self._check_dynamic_slaves()

            if command == "MasterLinkready2":
                self._handle_linkready(packet)
            elif command == "MasterHeartbeat" and receiver_id_str in self.slaves:
                self._handle_heartbeat(packet, receiver_id_str)

            logger.log(logging.INFO9, f"Tx@: {self.master.hex_str(msg)}")
            self.timeLastTx = time.time()

        except Exception as e:
            logger.log(logging.INFO4, f"Error processing message: {e}")

        return 0

    def _handle_linkready(self, packet):
        """Handle MasterLinkready2 message - respond with SlaveLinkready from all slaves."""
        for slave_id, slave in self.slaves.items():
            if slave["behavior"] == self.BEHAVIOR_INTERMITTENT:
                if random.random() < slave["dropRate"]:
                    continue

            if slave["responseDelay"] > 0:
                time.sleep(slave["responseDelay"])

            response = self.proto.createMessage(
                {
                    "Command": "SlaveLinkready",
                    "SenderID": bytearray(slave_id.encode()),
                    "Sign": self.master.getSlaveSign(),
                    "Amps": bytearray(b"\x1f\x40"),
                }
            )
            self._send_internal(response)

     def _handle_heartbeat(self, packet, slave_id):
         """Handle MasterHeartbeat message - respond with SlaveHeartbeat."""
         if slave_id not in self.slaves:
             return
 
         slave = self.slaves[slave_id]
 
         if slave["behavior"] == self.BEHAVIOR_INTERMITTENT:
             if random.random() < slave["dropRate"]:
                 return
 
         if slave["responseDelay"] > 0:
             time.sleep(slave["responseDelay"])
 
         self._update_slave_state(slave)
 
         # Extract the amps TWCMaster commanded in this heartbeat.
         # MasterHeartbeat HeartbeatData bytes [1:3] are the commanded amps
         # encoded as (value * 100) big-endian — e.g. 32.00A → 0x0C80.
         heartbeat_data = packet.get("HeartbeatData", bytearray(9))
         if len(heartbeat_data) >= 3:
             commanded_amps_raw = (heartbeat_data[1] << 8) | heartbeat_data[2]
         else:
             commanded_amps_raw = 0
 
         # Build response heartbeat data:
         #   byte 0    : state code
         #   bytes 1-2 : reportedAmpsMax  (slave's physical max * 100)
         #   bytes 3-4 : reportedAmpsActual (what slave is actually drawing * 100)
         #   bytes 5-6 : padding zeros
         state_code, actual_raw = self._get_slave_response(slave, commanded_amps_raw)
         max_raw = int(slave.get("maxAmps", 32) * 100)
 
         heartbeat_response = bytearray([
             state_code,
             (max_raw >> 8) & 0xFF,
             max_raw & 0xFF,
             (actual_raw >> 8) & 0xFF,
             actual_raw & 0xFF,
             0x00,
             0x00,
         ])
         if self.master.protocolVersion == 2:
             heartbeat_response += bytearray(b"\x00\x00")
 
         response = (
             bytearray(b"\xfd\xe0")
             + bytearray(slave_id.encode())
             + packet["SenderID"]
             + heartbeat_response
         )
         self._send_internal(response)
         slave["lastHeartbeatTime"] = time.time()

    def _get_slave_response(self, slave, commanded_amps_raw):
        """Return (state_code, actual_amps_raw) based on slave behavior and commanded amps."""
        behavior = slave["behavior"]
        if behavior == self.BEHAVIOR_CHARGING and commanded_amps_raw > 0:
            # Slave is charging — echo back whatever master commanded
            return 0x01, commanded_amps_raw
        elif behavior == self.BEHAVIOR_CAR_PLUGGED:
            # Car plugged but not yet charging
            return 0x04, 0
        elif behavior == self.BEHAVIOR_ERROR:
            return 0x02, 0
        else:
            # Normal/idle — unplugged or not charging
            return 0x00, 0

    def sendInternal(self, msg):
        # The sendInternal function takes a message that we would like to send
        # from the dummy module to the TWCManager, adds the required checksum,
        # updates the internal message buffer with the sent message and then
        # allows this to be polled & read by TWCManager on the next loop iteration

        msg = bytearray(msg)
        checksum = 0
        for i in range(1, len(msg)):
            checksum += msg[i]

        msg.append(checksum & 0xFF)

        # Escaping special chars:
        # The protocol uses C0 to mark the start and end of the message.  If a C0
        # must appear within the message, it is 'escaped' by replacing it with
        # DB and DC bytes.
        # A DB byte in the message is escaped by replacing it with DB DD.
        #
        # User FuzzyLogic found that this method of escaping and marking the start
        # and end of messages is based on the SLIP protocol discussed here:
        #   https://en.wikipedia.org/wiki/Serial_Line_Internet_Protocol

        i = 0
        while i < len(msg):
            if msg[i] == 0xC0:
                msg[i : i + 1] = b"\xdb\xdc"
                i = i + 1
            elif msg[i] == 0xDB:
                msg[i : i + 1] = b"\xdb\xdd"
                i = i + 1
            i = i + 1

        msg = bytearray(b"\xc0" + msg + b"\xc0\xfe")
        logger.log(logging.INFO9, "TxInt@: " + self.master.hex_str(msg))

        self.msgBuffer = msg
