import asyncio
import logging
import queue
import threading
from datetime import datetime, timezone

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("OCPP", "Control")

try:
    import websockets
    from ocpp.routing import on
    from ocpp.v16 import ChargePoint, call, call_result
    from ocpp.v16.datatypes import ChargingProfile, ChargingSchedule, ChargingSchedulePeriod
    from ocpp.v16.enums import (
        Action,
        AuthorizationStatus,
        AvailabilityStatus,
        AvailabilityType,
        ChargingProfileKindType,
        ChargingProfilePurposeType,
        ChargingProfileStatus,
        ChargingRateUnitType,
        ChargePointErrorCode,
        ChargePointStatus,
        RegistrationStatus,
        RemoteStartStopStatus,
    )

    OCPP_AVAILABLE = True
except ImportError as e:
    OCPP_AVAILABLE = False
    logger.error(f"OCPPControl disabled: missing dependency ({e})")

OCPP_SUBPROTOCOL = "ocpp1.6"


class TWCManagerCP(ChargePoint):
    """Manages one connected OCPP 1.6 Charge Point connection."""

    def __init__(self, cp_id, connection, master, cmd_queue):
        super().__init__(cp_id, connection)
        self._master = master
        self._cmd_queue = cmd_queue
        self._status = {}        # connector_id -> ChargePointStatus
        self._transactions = {}  # connector_id -> transaction_id
        self._meter_watts = {}   # connector_id -> current watts
        self._txn_counter = 1

    # ------------------------------------------------------------------
    # Incoming messages (Charge Point -> Central System)
    # ------------------------------------------------------------------

    @on(Action.boot_notification)
    def on_boot_notification(self, charge_point_model, charge_point_vendor, **kwargs):
        logger.log(
            logging.INFO2,
            f"OCPP BootNotification: {self.id} ({charge_point_vendor} {charge_point_model})",
        )
        return call_result.BootNotification(
            current_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            interval=30,
            status=RegistrationStatus.accepted,
        )

    @on(Action.heartbeat)
    def on_heartbeat(self, **kwargs):
        logger.log(logging.DEBUG2, f"OCPP Heartbeat from {self.id}")
        return call_result.Heartbeat(
            current_time=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    @on(Action.authorize)
    def on_authorize(self, id_tag, **kwargs):
        # Accept all tags — TWCManager does not maintain an id_tag whitelist
        logger.log(logging.INFO5, f"OCPP Authorize id_tag={id_tag} from {self.id}: accepted")
        return call_result.Authorize(
            id_tag_info={"status": AuthorizationStatus.accepted}
        )

    @on(Action.status_notification)
    def on_status_notification(self, connector_id, error_code, status, **kwargs):
        prev = self._status.get(connector_id)
        self._status[connector_id] = status
        if status != prev:
            logger.log(
                logging.INFO3,
                f"OCPP StatusNotification: {self.id} connector {connector_id} -> {status}",
            )
        return call_result.StatusNotification()

    @on(Action.start_transaction)
    def on_start_transaction(self, connector_id, id_tag, meter_start, timestamp, **kwargs):
        txn_id = self._txn_counter
        self._txn_counter += 1
        self._transactions[connector_id] = txn_id
        logger.log(
            logging.INFO3,
            f"OCPP StartTransaction: {self.id} connector {connector_id} txn={txn_id} id_tag={id_tag}",
        )
        return call_result.StartTransaction(
            transaction_id=txn_id,
            id_tag_info={"status": AuthorizationStatus.accepted},
        )

    @on(Action.stop_transaction)
    def on_stop_transaction(self, meter_stop, timestamp, transaction_id, **kwargs):
        logger.log(
            logging.INFO3,
            f"OCPP StopTransaction: {self.id} txn={transaction_id}",
        )
        for conn_id, txn_id in list(self._transactions.items()):
            if txn_id == transaction_id:
                del self._transactions[conn_id]
                self._meter_watts.pop(conn_id, None)
                break
        return call_result.StopTransaction()

    @on(Action.meter_values)
    def on_meter_values(self, connector_id, meter_value, **kwargs):
        for mv in meter_value:
            for sv in mv.get("sampled_value", []):
                measurand = sv.get("measurand", "Energy.Active.Import.Register")
                value = sv.get("value", "0")
                unit = sv.get("unit", "")
                logger.log(
                    logging.DEBUG2,
                    f"OCPP MeterValues: {self.id} connector {connector_id} {measurand}={value}{unit}",
                )
                # Track instantaneous power for TWCManager's consumption accounting
                if measurand == "Power.Active.Import":
                    try:
                        self._meter_watts[connector_id] = float(value)
                    except ValueError:
                        pass
        return call_result.MeterValues()

    # ------------------------------------------------------------------
    # Outgoing commands (Central System -> Charge Point)
    # ------------------------------------------------------------------

    async def remote_start(self, connector_id=None, id_tag="TWCManager"):
        """Send RemoteStartTransaction. Returns True on Accepted."""
        request = call.RemoteStartTransaction(id_tag=id_tag, connector_id=connector_id)
        try:
            response = await self.call(request)
            accepted = response.status == RemoteStartStopStatus.accepted
            logger.log(
                logging.INFO3 if accepted else logging.INFO1,
                f"OCPP RemoteStartTransaction on {self.id} connector {connector_id}: {response.status}",
            )
            return accepted
        except Exception as e:
            logger.error(f"OCPP RemoteStartTransaction failed on {self.id}: {e}")
            return False

    async def remote_stop(self, connector_id):
        """Send RemoteStopTransaction for the active transaction on connector_id."""
        txn_id = self._transactions.get(connector_id)
        if txn_id is None:
            logger.warning(
                f"OCPP RemoteStopTransaction: no active transaction on {self.id} connector {connector_id}"
            )
            return False
        request = call.RemoteStopTransaction(transaction_id=txn_id)
        try:
            response = await self.call(request)
            accepted = response.status == RemoteStartStopStatus.accepted
            logger.log(
                logging.INFO3 if accepted else logging.INFO1,
                f"OCPP RemoteStopTransaction on {self.id} connector {connector_id}: {response.status}",
            )
            return accepted
        except Exception as e:
            logger.error(f"OCPP RemoteStopTransaction failed on {self.id}: {e}")
            return False

    async def set_charge_rate(self, connector_id, amps):
        """Push a TxDefaultProfile charging profile to cap this connector at amps."""
        profile = ChargingProfile(
            charging_profile_id=1,
            stack_level=0,
            charging_profile_purpose=ChargingProfilePurposeType.tx_default_profile,
            charging_profile_kind=ChargingProfileKindType.absolute,
            charging_schedule=ChargingSchedule(
                charging_rate_unit=ChargingRateUnitType.amps,
                charging_schedule_period=[
                    ChargingSchedulePeriod(start_period=0, limit=float(amps))
                ],
            ),
        )
        request = call.SetChargingProfile(
            connector_id=connector_id, cs_charging_profiles=profile
        )
        try:
            response = await self.call(request)
            accepted = response.status == ChargingProfileStatus.accepted
            logger.log(
                logging.INFO3 if accepted else logging.INFO1,
                f"OCPP SetChargingProfile on {self.id} connector {connector_id} {amps}A: {response.status}",
            )
            return accepted
        except Exception as e:
            logger.error(f"OCPP SetChargingProfile failed on {self.id}: {e}")
            return False

    async def _process_commands(self):
        """Drain per-CP commands from the shared queue."""
        while True:
            await asyncio.sleep(1)
            pending = []
            while True:
                try:
                    cmd = self._cmd_queue.get_nowait()
                except queue.Empty:
                    break
                if cmd.get("cp_id") in (self.id, None):
                    pending.append(cmd)
                else:
                    # Not for us — put back; another CP will pick it up
                    self._cmd_queue.put(cmd)

            for cmd in pending:
                action = cmd.get("action")
                conn = cmd.get("connector_id", 1)
                if action == "start":
                    await self.remote_start(connector_id=conn)
                elif action == "stop":
                    await self.remote_stop(connector_id=conn)
                elif action == "setrate":
                    await self.set_charge_rate(connector_id=conn, amps=cmd.get("amps", 6))

    async def run(self):
        await asyncio.gather(self.start(), self._process_commands())


class OCPPControl:
    config = None
    configOCPP = None
    master = None
    status = False

    def __init__(self, master):
        self.config = master.config
        self.master = master
        self._charge_points = {}   # cp_id -> TWCManagerCP
        self._cmd_queue = queue.Queue()

        try:
            self.configOCPP = self.config["control"]["OCPP"]
        except KeyError:
            self.configOCPP = {}

        self.status = self.configOCPP.get("enabled", False)
        self.server_port = int(self.configOCPP.get("serverPort", 9000))
        self.server_host = self.configOCPP.get("serverHost", "0.0.0.0")

        if not self.status:
            self.master.releaseModule("lib.TWCManager.Control", "OCPPControl")
            return

        if not OCPP_AVAILABLE:
            logger.error("OCPPControl enabled but ocpp/websockets packages not available")
            self.master.releaseModule("lib.TWCManager.Control", "OCPPControl")
            return

        thread = threading.Thread(target=self._run_server, daemon=True, name="OCPPControl")
        thread.start()
        logger.log(logging.INFO2, f"OCPP Central System listening on {self.server_host}:{self.server_port}")

    # ------------------------------------------------------------------
    # WebSocket server
    # ------------------------------------------------------------------

    def _run_server(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._serve())
        except Exception as e:
            logger.error(f"OCPP server thread exited unexpectedly: {e}")

    async def _serve(self):
        async with websockets.serve(
            self._on_connect,
            self.server_host,
            self.server_port,
            subprotocols=[OCPP_SUBPROTOCOL],
        ) as server:
            await server.serve_forever()

    async def _on_connect(self, websocket):
        # Extract CP id from the URL path: ws://host:port/<cp_id>
        path = getattr(websocket, "path", "") or ""
        cp_id = path.strip("/").split("/")[-1] or "unknown"

        logger.log(logging.INFO2, f"OCPP Charge Point connected: {cp_id}")
        cp = TWCManagerCP(cp_id, websocket, self.master, self._cmd_queue)
        self._charge_points[cp_id] = cp

        try:
            await cp.run()
        except websockets.exceptions.ConnectionClosed:
            pass
        finally:
            self._charge_points.pop(cp_id, None)
            logger.log(logging.INFO2, f"OCPP Charge Point disconnected: {cp_id}")

    # ------------------------------------------------------------------
    # Public control interface (called from TWCManager main thread)
    # ------------------------------------------------------------------

    def sendStartCharge(self, cp_id=None, connector_id=1):
        """Request a connected Charge Point to start a transaction."""
        self._cmd_queue.put({"action": "start", "cp_id": cp_id, "connector_id": connector_id})

    def sendStopCharge(self, cp_id=None, connector_id=1):
        """Request a connected Charge Point to stop its active transaction."""
        self._cmd_queue.put({"action": "stop", "cp_id": cp_id, "connector_id": connector_id})

    def sendChargeRate(self, amps, cp_id=None, connector_id=1):
        """Push a charging profile to cap a Charge Point at amps."""
        self._cmd_queue.put({"action": "setrate", "cp_id": cp_id, "connector_id": connector_id, "amps": amps})

    def connectedChargePoints(self):
        """Return list of currently connected Charge Point IDs."""
        return list(self._charge_points.keys())

    def chargePointStatus(self, cp_id, connector_id=1):
        """Return the last known ChargePointStatus for a connector, or None."""
        cp = self._charge_points.get(cp_id)
        if cp:
            return cp._status.get(connector_id)
        return None

    def updateSettings(self):
        pass
