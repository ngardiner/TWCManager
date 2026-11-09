import json

from TWCManager.Control import Views
from TWCManager.Control.Views import fingerprint, jsonSafe
from tests.unit.live_helpers import callAPI
from unittest.mock import Mock


def test_fingerprint_is_stable_and_shape_sensitive():
    assert fingerprint("a", ["x"]) == fingerprint("a", ["x"])
    assert fingerprint("a", ["x"]) != fingerprint("a", ["y"])
    assert len(fingerprint()) == 12


def test_fingerprint_changes_with_start_time(monkeypatch):
    before = fingerprint("a")
    monkeypatch.setattr(Views, "STARTED", Views.STARTED + 1)
    assert fingerprint("a") != before


def test_json_safe_non_finite_and_objects():
    out = jsonSafe({"a": float("nan"), "b": [float("inf"), 1.5], 3: object, "c": (1,)})
    assert out["a"] is None and out["b"] == [None, 1.5] and out["c"] == [1]
    assert out["3"].startswith("<class")
    json.loads(json.dumps(out, allow_nan=False))


def test_call_api_records_status_headers_body():
    m = Mock()
    m.config = {"config": {}}
    status, headers, body = callAPI(m, "/api/getConfig")
    assert status == 200 and headers["Content-type"] == "application/json"
    assert isinstance(json.loads(body), dict)


def test_send_json_headers_and_safety():
    import io
    from TWCManager.Control.HTTPControl import CreateHTTPHandlerClass
    cls = CreateHTTPHandlerClass(Mock())
    h = cls.__new__(cls)
    h.url = Mock(path="/x"); h.client_address = ("127.0.0.1", 0); h.wfile = io.BytesIO()
    seen = {}
    h.send_response = lambda c, m=None: seen.__setitem__("code", c)
    h.send_header = lambda k, v: seen.__setitem__(k, v)
    h.end_headers = lambda: None
    h.sendJSON({"a": float("nan")}, 201)
    assert seen["code"] == 201 and seen["Cache-Control"] == "no-store"
    assert json.loads(h.wfile.getvalue()) == {"a": None}
