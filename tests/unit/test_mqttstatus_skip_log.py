"""A broker outage logs one skipped-publish line, not one per status update."""

from unittest.mock import Mock, patch

from TWCManager.Status.MQTTStatus import MQTTStatus


def make_status():
    master = Mock()
    master.config = {
        "status": {
            "MQTT": {
                "enabled": True,
                "brokerIP": "[PII_IP_ADDR_59b8d1c0]",
                "topicPrefix": "TWC",
            }
        }
    }
    with patch.object(MQTTStatus, "_init_mqtt_client"):
        status = MQTTStatus(master)
    status.client = Mock()
    status.homeassistantDiscovery = False
    status.connected = False
    return status


def skip_lines(logger):
    return [c for c in logger.debug.call_args_list if "not connected" in str(c)]


def test_outage_logs_once():
    status = make_status()
    with patch("TWCManager.Status.MQTTStatus.logger") as logger:
        for _ in range(5):
            assert (
                status.setStatus(b"ABCD", "amps_in_use", "ampsInUse", 1, "A") is False
            )
    assert len(skip_lines(logger)) == 1


def test_reconnect_rearms_the_log():
    status = make_status()
    with patch("TWCManager.Status.MQTTStatus.logger") as logger:
        status.setStatus(b"ABCD", "amps_in_use", "ampsInUse", 1, "A")
        status.mqttConnected(None, None, None, 0)
        status.connected = False
        status.setStatus(b"ABCD", "amps_in_use", "ampsInUse", 1, "A")
    assert len(skip_lines(logger)) == 2
