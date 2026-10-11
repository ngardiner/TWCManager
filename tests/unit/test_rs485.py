from unittest.mock import Mock

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
