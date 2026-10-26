"""The control tick: the control pass and housekeeping, once a second, off
the serial thread. The serial loop only talks RS-485."""

import threading
import time

from TWCManager.Logging.LoggerFactory import LoggerFactory

logger = LoggerFactory.get_logger("ControlTick", "Master")


# Consecutive failed passes before the offers are withdrawn: a stale offer
# may exceed a budget that has since dropped, so fail to the TWC minimum
WITHDRAW_AFTER = 30


class ControlTick(threading.Thread):
    def __init__(self, master, interval: float = 1.0):
        super().__init__(name="ControlTick", daemon=True)
        self.master = master
        self.interval = interval
        self._stopping = threading.Event()
        self.failures = 0

    def run(self):
        while not self._stopping.wait(self.interval):
            self.pass_()

    def pass_(self) -> None:
        # Distribution only as master (fakeMaster 1): the other modes never
        # send the heartbeats that carry it
        if self.master.config["config"].get("fakeMaster") == 1:
            self._controlPass()
        self._guarded("housekeeping", self.housekeeping)

    def _controlPass(self) -> None:
        try:
            self.master.controlPass()
        except Exception:
            self.failures += 1
            n = self.failures
            if n == 1 or n % 60 == 0:
                logger.warning(
                    "Control tick: controlPass failed (%d in a row)", n, exc_info=True
                )
            if n == WITHDRAW_AFTER:
                logger.error("Control tick: %d failed passes; offers withdrawn", n)
            if n >= WITHDRAW_AFTER:
                self._guarded("withdrawOffers", self.master.withdrawOffers)
            return
        if self.failures >= WITHDRAW_AFTER:
            logger.info("Control tick: controlPass succeeded; offers resume")
        self.failures = 0

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
        if self.master.watch:
            self._guarded("watch", self.master.watch.pass_)
        self._guarded("history", self.master.sampleHistory)
        # Hourly, forget vehicles no one has claimed or seen for the window
        if time.time() - self.master.lastVehicleExpiry > 3600:
            self.master.lastVehicleExpiry = time.time()
            self.master.queue_background_task({"cmd": "expireVehicles"})

    def stop(self) -> None:
        self._stopping.set()
        if self.is_alive():
            self.join(2)
