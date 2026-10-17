"""The control tick: the control pass and housekeeping, once a second, off
the serial thread. The serial loop only talks RS-485."""

import threading
import time

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("ControlTick", "Master")


class ControlTick(threading.Thread):
    def __init__(self, master, interval: float = 1.0):
        super().__init__(name="ControlTick", daemon=True)
        self.master = master
        self.interval = interval
        self._stopping = threading.Event()

    def run(self):
        while not self._stopping.wait(self.interval):
            self.pass_()

    def pass_(self) -> None:
        # Distribution only as master (fakeMaster 1): the other modes never
        # send the heartbeats that carry it
        if self.master.config["config"].get("fakeMaster") == 1:
            self._guarded("controlPass", self.master.controlPass)
        self._guarded("housekeeping", self.housekeeping)

    @staticmethod
    def _guarded(name, step):
        try:
            step()
        except Exception as e:
            logger.warning("Control tick: %s raised %s", name, type(e).__name__)

    def housekeeping(self) -> None:
        # One web IPC message a tick (its RS-485 send is queued)
        ipc = self.master.getModuleByName("WebIPCControl")
        if ipc:
            ipc.processIPC()
        # Hourly, forget vehicles no one has claimed or seen for the window
        if time.time() - self.master.lastVehicleExpiry > 3600:
            self.master.lastVehicleExpiry = time.time()
            self.master.queue_background_task({"cmd": "expireVehicles"})

    def stop(self) -> None:
        self._stopping.set()
        if self.is_alive():
            self.join(2)
