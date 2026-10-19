import logging
import time
from unittest.mock import Mock, patch

import pytest
import serial

from TWCManager.Interface.RS485 import RS485


def make_rs485():
    rs = RS485.__new__(RS485)
    rs.ser = Mock()
    rs.connect = Mock()
    rs.master = Mock()
    rs.master.hex_str.return_value = ""
    return rs


def test_serial_error_reconnects_instead_of_raising_nameerror():
    rs = make_rs485()
    rs.ser.inWaiting.side_effect = serial.serialutil.SerialException("gone")
    assert rs.getBufferLen() == 0
    rs.connect.assert_called_once()


def test_serial_error_on_read_reconnects():
    rs = make_rs485()
    rs.ser.read.side_effect = serial.serialutil.SerialException("gone")
    assert rs.read(1) == b""
    rs.connect.assert_called_once()


def test_serial_error_on_send_reconnects():
    rs = make_rs485()
    rs.ser.write.side_effect = serial.serialutil.SerialException("gone")
    rs.send(b"\xfb\xe0")
    rs.connect.assert_called_once()


def test_keyboard_interrupt_in_the_read_reaches_the_caller():
    # SIGTERM is turned into KeyboardInterrupt; if the except clause itself
    # failed, the main loop would see some other error and keep running.
    rs = make_rs485()
    rs.ser.inWaiting.side_effect = KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        rs.getBufferLen()


# Opening the Gen2 port


def gen2Master(port="/dev/ttyUSB0", gen3=None):
    master = Mock()
    master.config = {"config": {}, "interface": {"RS485": {"port": port}}}
    if gen3 is not None:
        master.config["controller.Gen3TWCs"] = gen3
    master.getSlaveTWCs.return_value = []
    return master


def absent(*args, **kwargs):
    raise serial.serialutil.SerialException("could not open port")


def errors(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_port_that_cannot_open_leaves_the_link_loaded_and_closed(caplog):
    master = gen2Master()
    with patch.object(serial, "serial_for_url", side_effect=absent):
        rs = RS485(master)
    assert not rs.connected and rs.ser is None
    master.releaseModule.assert_not_called()
    (err,) = errors(caplog)
    assert "/dev/ttyUSB0" in err.getMessage()
    assert "No Gen2 TWCs can be controlled" in err.getMessage()


def test_the_link_reopens_every_30_s_and_logs_once(caplog):
    master = gen2Master()
    clock = [1000.0]
    with patch.object(time, "time", lambda: clock[0]):
        with patch.object(serial, "serial_for_url", side_effect=absent) as open_:
            rs = RS485(master)
            clock[0] += 29
            rs.connect()
            assert open_.call_count == 1  # not yet
            clock[0] += 1
            rs.connect()
            assert open_.call_count == 2 and not rs.connected
        assert len(errors(caplog)) == 1  # the failure is logged once
        clock[0] += 30
        caplog.clear()
        with caplog.at_level(logging.INFO), patch.object(
            serial, "serial_for_url", return_value=Mock()
        ):
            rs.connect()
    assert rs.connected
    assert [r.levelno for r in caplog.records] == [logging.INFO]
    assert "/dev/ttyUSB0" in caplog.records[0].getMessage()


def test_a_closed_link_is_neither_polled_nor_written():
    rs = make_rs485()
    rs.ser = None
    assert rs.getBufferLen() == 0
    rs.send(b"\xfb\xe0")
    rs.connect.assert_not_called()


def test_a_write_that_loses_the_port_closes_the_link_without_raising():
    master = gen2Master()
    with patch.object(serial, "serial_for_url", return_value=Mock()):
        rs = RS485(master)
    rs.master.hex_str.return_value = ""
    rs.ser.write.side_effect = serial.serialutil.SerialException("gone")
    with patch.object(serial, "serial_for_url", side_effect=absent):
        rs.send(b"\xfb\xe0")
        rs.send(b"\xfb\xe0")  # the next message of the same flush
        assert rs.getBufferLen() == 0
    assert not rs.connected


def test_the_gen3_port_is_never_opened_as_a_gen2_bus(caplog):
    master = gen2Master("/dev/ttyUSB1", gen3={"enabled": True})
    with caplog.at_level(logging.INFO), patch.object(serial, "serial_for_url") as open_:
        RS485(master)
    open_.assert_not_called()
    master.releaseModule.assert_called_once_with("lib.TWCManager.Interface", "RS485")
    assert any("Gen3 Wall Connector" in r.getMessage() for r in caplog.records)


def test_the_gen2_link_opens_beside_the_gen3_on_another_port():
    for gen3 in ({"enabled": True, "port": "/dev/ttyUSB2"}, {"enabled": False}):
        master = gen2Master("/dev/ttyUSB1", gen3=gen3)
        with patch.object(serial, "serial_for_url", return_value=Mock()):
            rs = RS485(master)
        assert rs.connected
        master.releaseModule.assert_not_called()


def test_an_unplugged_adapter_reconnects():
    # pyserial reports a vanished USB device as a plain OSError (EIO)
    rs = make_rs485()
    rs.ser.inWaiting.side_effect = OSError(5, "Input/output error")
    assert rs.getBufferLen() == 0
    rs.connect.assert_called_once()
