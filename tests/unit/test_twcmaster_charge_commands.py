"""
Tests for charge start/stop commands in TWCMaster's background task table:
cancelling a pending stop (#667) and the per-car interval between commands.
"""

from unittest.mock import Mock

import pytest

from TWCManager.TWCMaster import TWCMaster


@pytest.fixture
def master():
    config = {
        "config": {
            "debugOutputToFile": False,
            "subtractChargerLoad": False,
            "treatGenerationAsGridDelivery": False,
            "wiringMaxAmpsAllTWCs": 80,
            "maxAmpsAllowedFromGrid": None,
        }
    }
    m = TWCMaster("AB", config)
    m.backgroundTasksCmds = {}
    m.backgroundTasksDelayed = []
    m.getModuleByName = Mock(return_value=Mock())
    while not m.backgroundTasksQueue.empty():
        m.backgroundTasksQueue.get_nowait()
    yield m
    # The queue is class-level state shared with other tests
    while not m.backgroundTasksQueue.empty():
        m.backgroundTasksQueue.get_nowait()


def stop(vin):
    return {"cmd": "charge", "charge": False, "vin": vin}


def run_next(master):
    task = master.getBackgroundTask()
    master.doneBackgroundTask(task)
    return task


class TestCancelStop:
    def test_cancels_pending_stop_with_vin(self, master):
        master.queue_background_task(stop("V1"))
        master.cancelStopCarsCharging("V1")
        assert "charge" not in master.backgroundTasksCmds
        assert "cmd" not in run_next(master)

    @pytest.mark.parametrize("vin", [None, ""])
    def test_cancels_stop_without_vin(self, master, vin):
        master.queue_background_task(stop(None))
        master.cancelStopCarsCharging(vin)
        assert "charge" not in master.backgroundTasksCmds

    def test_cancels_stop_for_every_car(self, master):
        master.queue_background_task(stop(None))
        master.cancelStopCarsCharging("V1")
        assert "charge" not in master.backgroundTasksCmds

    def test_keeps_stop_for_another_car(self, master):
        master.queue_background_task(stop("V2"))
        master.cancelStopCarsCharging("V1")
        assert master.backgroundTasksCmds["charge"]["vin"] == "V2"

    def test_keeps_pending_start(self, master):
        master.queue_background_task({"cmd": "charge", "charge": True})
        master.cancelStopCarsCharging("V1")
        assert master.backgroundTasksCmds["charge"]["charge"] is True

    def test_stop_merged_into_start_is_cancelled(self, master):
        master.queue_background_task({"cmd": "charge", "charge": True})
        master.queue_background_task(stop("V1"))
        master.cancelStopCarsCharging("V1")
        assert "charge" not in master.backgroundTasksCmds


class TestChargeInterval:
    def test_repeat_within_interval_is_not_queued(self, master):
        master.queue_background_task(stop("V1"))
        run_next(master)
        master.queue_background_task(stop("V1"))
        assert "charge" not in master.backgroundTasksCmds
        assert master.backgroundTasksQueue.empty()

    def test_other_car_is_not_limited(self, master):
        master.queue_background_task(stop("V1"))
        run_next(master)
        master.queue_background_task(stop("V2"))
        assert master.backgroundTasksCmds["charge"]["vin"] == "V2"

    def test_command_for_every_car_waits_for_any_car(self, master):
        master.queue_background_task(stop("V1"))
        run_next(master)
        master.queue_background_task({"cmd": "charge", "charge": True})
        assert "charge" not in master.backgroundTasksCmds

    def test_queued_again_after_interval(self, master):
        master.queue_background_task(stop("V1"))
        run_next(master)
        master.lastChargeCommandTime["V1"] -= master.CHARGE_COMMAND_INTERVAL
        master.queue_background_task(stop("V1"))
        assert master.backgroundTasksCmds["charge"]["vin"] == "V1"

    def test_cancelled_stop_does_not_count(self, master):
        master.queue_background_task(stop("V1"))
        master.cancelStopCarsCharging("V1")
        run_next(master)
        master.queue_background_task(stop("V1"))
        assert master.backgroundTasksCmds["charge"]["vin"] == "V1"

    def test_other_tasks_are_not_limited(self, master):
        master.queue_background_task(stop("V1"))
        run_next(master)
        master.queue_background_task({"cmd": "saveSettings"})
        assert "saveSettings" in master.backgroundTasksCmds
