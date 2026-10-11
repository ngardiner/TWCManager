from unittest.mock import MagicMock, patch

from TWCManager.Vehicle import Telemetry
from TWCManager.Vehicle.Telemetry import TelmetryBase


class _Sub(TelmetryBase):
    configName = "Sub"


def _master():
    master = MagicMock()
    master.config = {
        "config": {},
        "vehicle": {"Sub": {"enabled": True, "syncTelemetry": True}},
    }
    return master


def _recording_timer():
    """Mock for threading.Timer. Records daemon value at start() time."""
    created = []

    def factory(interval, function):
        t = MagicMock()
        t.interval = interval
        t.daemon_at_start = None

        def start():
            t.daemon_at_start = t.daemon

        t.start.side_effect = start
        created.append(t)
        return t

    return MagicMock(side_effect=factory), created


def test_startup_and_reset_timers_are_daemon():
    timer_cls, created = _recording_timer()
    with patch.object(Telemetry.threading, "Timer", timer_cls):
        obj = _Sub(_master())
        assert len(created) == 1
        assert created[0].interval == 30
        assert created[0].start.called
        assert created[0].daemon_at_start is True

        with patch.object(Telemetry, "mqtt") as fake_mqtt:
            obj.doMQTT()
        assert fake_mqtt.Client.called
        assert len(created) == 2
        assert created[1].interval == 24 * 60 * 60
        assert created[1].start.called
        assert created[1].daemon_at_start is True
