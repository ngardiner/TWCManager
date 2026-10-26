"""HTTPControl falls back to IPv4 on a host with IPv6 disabled (#721)."""

import errno
import socket
from unittest.mock import Mock, patch

import pytest

from TWCManager.Control import HTTPControl as mod


def no_ipv6(*args, **kwargs):
    raise OSError(errno.EAFNOSUPPORT, "Address family not supported by protocol")


def test_ipv6_used_when_available():
    with patch.object(mod, "ThreadingSimpleServer") as v6, patch.object(
        mod, "ThreadingSimpleServerIPv4"
    ) as v4:
        assert mod.createServer(8080, Mock()) is v6.return_value
    v6.assert_called_once()
    assert v6.call_args[0][0] == ("::", 8080)
    v4.assert_not_called()


def test_falls_back_to_ipv4_without_ipv6():
    with patch.object(mod, "ThreadingSimpleServer", side_effect=no_ipv6), patch.object(
        mod, "ThreadingSimpleServerIPv4"
    ) as v4:
        assert mod.createServer(8080, Mock()) is v4.return_value
    assert v4.call_args[0][0] == ("0.0.0.0", 8080)


def test_port_in_use_is_not_masked_by_ipv4():
    busy = OSError(errno.EADDRINUSE, "Address already in use")
    with patch.object(mod, "ThreadingSimpleServer", side_effect=busy), patch.object(
        mod, "ThreadingSimpleServerIPv4"
    ) as v4:
        with pytest.raises(OSError) as e:
            mod.createServer(8080, Mock())
    assert e.value.errno == errno.EADDRINUSE
    v4.assert_not_called()


def test_ipv6_disabled_by_sysctl_also_falls_back():
    unavailable = OSError(errno.EADDRNOTAVAIL, "Cannot assign requested address")
    with patch.object(
        mod, "ThreadingSimpleServer", side_effect=unavailable
    ), patch.object(mod, "ThreadingSimpleServerIPv4") as v4:
        assert mod.createServer(8080, Mock()) is v4.return_value


def test_ipv4_server_family():
    assert mod.ThreadingSimpleServerIPv4.address_family == socket.AF_INET


def test_httpcontrol_starts_without_ipv6():
    master = Mock()
    master.config = {
        "config": {},
        "control": {"HTTP": {"enabled": True, "listenPort": 8080}},
    }
    with patch.object(mod, "ThreadingSimpleServer", side_effect=no_ipv6), patch.object(
        mod, "ThreadingSimpleServerIPv4"
    ), patch.object(mod.threading, "Thread"):
        mod.HTTPControl(master)
    master.releaseModule.assert_not_called()
