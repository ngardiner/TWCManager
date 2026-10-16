import json
import threading
from unittest.mock import Mock

import pytest

from TWCManager.Control.APIValidator import APIValidator


def test_validate_float_accepts_numbers_and_rejects_text():
    assert APIValidator.validate_float("2.5") == (True, 2.5, None)
    assert APIValidator.validate_float(3) == (True, 3.0, None)
    assert APIValidator.validate_float("x") == (False, None, "Value must be a number")
    assert APIValidator.validate_float(1, min_val=2)[0] is False


def post(master, path, payload):
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass

    handler = CreateHTTPHandlerClass(master)
    request = Mock()
    body = json.dumps(payload).encode("utf-8")
    request.path = path
    request.fields = {}
    request.headers = {"content-length": str(len(body))}
    request.rfile.read.return_value = body
    request.do_API_POST = lambda: handler.do_API_POST(request)   # the real route, not the Mock's
    handler.do_POST(request)
    return request


@pytest.mark.parametrize(
    "payload, status, saved",
    [({"offset": 2.5}, 204, 2.5), ({"offset": "abc"}, 400, None), ({}, 400, None)],
)
def test_set_consumption_offset_validates_and_responds(payload, status, saved):   # slim Task 13 review C1
    master = Mock(settings={}, settingsLock=threading.RLock())
    request = post(master, "/api/setConsumptionOffset", payload)
    request.send_response.assert_called_once_with(status)
    assert master.settings.get("greenEnergyAmpsOffset") == saved
