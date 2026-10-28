# The live_status wall_connectors shape and the DIN format are unverified on
# hardware: these fixtures follow the tesla-fleet-api npm types.
import re, threading
from unittest.mock import MagicMock, Mock, patch
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Order import ProviderOrder
from TWCManager.Vehicle.Providers.WallConnectorVins import WallConnectorVinReader
from tests.unit.test_teslaapi_registry import VIN
from tests.unit.test_gen3twc_provider import Clock, make as make_gen3, vitals, VIN1, VIN2, IP1, IP2
from tests.unit.teslatokens import new_api, owner_token, fleet_token
DIN = "1457768-02-G--TG123"
PRODUCTS = {"response": [{"energy_site_id": 7, "site_name": "Home"}]}
def live(vin=VIN, din=DIN): return {"response": {"wall_connectors": [{"din": din, "vin": vin, "wall_connector_state": 2}]}}

def setup(requests_mock, status=200, vin=VIN, din=DIN):
    m, ctl, p = make_gen3(); m.config["vehicle"] = {"teslaAPI": {"enabled": True}}; m.tokenSyncEnabled.return_value = False
    m.modules = {}; m.settings = {}; m.settingsLock = threading.RLock()
    api = new_api(master=m); api.storeToken(owner_token()); reg = m.vehicleRegistry
    requests_mock.get(re.compile(r".*/api/1/products$"), json=PRODUCTS)
    requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), status_code=status, json=live(vin, din) if status == 200 else {})
    m.providerOrder = ProviderOrder(m); [m.providerOrder.register(x) for x in api.providers()]
    ctl.vinReader = WallConnectorVinReader(m, m.providerOrder, clock=Clock()); (u,) = ctl.devices(); u.device_cfg["din"] = din
    return api, reg, ctl, u

def liveCalls(requests_mock): return [r for r in requests_mock.request_history if "live_status" in r.url]

def test_vin_from_live_status_confirms_the_session(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    vitals(u, True)                                  # connect edge: pending (first report of an unknown VIN)
    assert reg.get(VIN) is None and len(liveCalls(requests_mock)) == 1
    ctl.vinReader.clock.t += 60; vitals(u, True)     # next poll confirms, as a Gen2 re-query would
    assert u.vehicleKey == VIN and reg.get(VIN).value("twcVin") == u.ID and reg.get("anon:" + u.ID) is None

def test_known_vin_confirms_at_once_and_polling_stops(requests_mock):
    api, reg, ctl, u = setup(requests_mock); reg.getOrCreate(VIN, "TeslaMateVehicle")
    vitals(u, True); assert u.vehicleKey == VIN
    ctl.vinReader.clock.t += 600; vitals(u, True); vitals(u, True)
    assert len(liveCalls(requests_mock)) == 1

def test_din_mismatch_keeps_the_single_candidate_guess_and_keeps_asking(requests_mock):   # I1
    api, reg, ctl, u = setup(requests_mock, din="other--din"); u.device_cfg["din"] = DIN
    car = reg.getOrCreate(VIN, "t"); car.observe("pluggedIn", True, "TeslaMateVehicle")
    vitals(u, True)
    assert u.cloudVin is None and u.vehicleKey == VIN            # the plug-in guess holds, provisionally
    assert len(liveCalls(requests_mock)) == 1
    ctl.vinReader.clock.t += 60; vitals(u, True)
    assert len(liveCalls(requests_mock)) == 2 and u.vehicleKey == VIN

def guessed(requests_mock, cloudVin, known):
    # Car VIN2 is the single plug-in candidate; the cloud names cloudVin
    api, reg, ctl, u = setup(requests_mock, vin=cloudVin)
    guess = reg.getOrCreate(VIN2, "TeslaMateVehicle"); guess.observe("pluggedIn", True, "TeslaMateVehicle")
    if known: reg.getOrCreate(cloudVin, "TeslaMateVehicle")
    return api, reg, ctl, u, guess

def test_a_differing_known_cloud_vin_replaces_the_guess(requests_mock):               # I1
    api, reg, ctl, u, guess = guessed(requests_mock, VIN, known=True)
    vitals(u, True)
    assert u.vehicleKey == VIN and reg.get(VIN).value("twcVin") == u.ID
    assert guess.value("twcVin") is None and guess.value("pluggedIn") is False
    ctl.vinReader.clock.t += 600; vitals(u, True)
    assert len(liveCalls(requests_mock)) == 1                                # named by the cloud: polling stops

def test_a_differing_unknown_cloud_vin_replaces_the_guess_once_confirmed(requests_mock):   # I1
    api, reg, ctl, u, guess = guessed(requests_mock, VIN, known=False)
    vitals(u, True)
    assert u.vehicleKey == VIN2 and reg.get(VIN) is None and guess.value("twcVin") == u.ID   # pending: the guess stands
    ctl.vinReader.clock.t += 60; vitals(u, True)
    assert u.vehicleKey == VIN and guess.value("twcVin") is None and guess.value("pluggedIn") is False

def test_an_equal_cloud_vin_confirms_the_guess_and_polling_stops(requests_mock):       # I1
    api, reg, ctl, u, guess = guessed(requests_mock, VIN2, known=True)
    vitals(u, True); assert u.vehicleKey == VIN2 and u.cloudVin == VIN2
    ctl.vinReader.clock.t += 60; vitals(u, True); ctl.vinReader.clock.t += 600; vitals(u, True)
    assert len(liveCalls(requests_mock)) == 1 and guess.value("pluggedIn") is True

def test_the_departed_cars_vin_is_ignored_on_the_next_cars_edge(requests_mock):        # I2
    api, reg, ctl, u = setup(requests_mock); reg.getOrCreate(VIN, "t"); reg.getOrCreate(VIN2, "t")
    requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), [
        {"json": live(VIN)}, {"json": live(VIN)}, {"json": live(VIN2)}])
    vitals(u, True); assert u.vehicleKey == VIN                                  # car A
    vitals(u, False); ctl.vinReader.clock.t += 90
    vitals(u, True)                                                          # car B; the cloud still says A
    assert len(liveCalls(requests_mock)) == 2 and u.vehicleKey == "anon:" + u.ID and u.cloudVin is None
    ctl.vinReader.clock.t += 60; vitals(u, True)
    assert u.vehicleKey == VIN2

def test_the_same_car_replugged_is_named_a_poll_later(requests_mock):                  # I2
    api, reg, ctl, u = setup(requests_mock); reg.getOrCreate(VIN, "t")
    vitals(u, True); assert u.vehicleKey == VIN
    vitals(u, False); ctl.vinReader.clock.t += 90
    vitals(u, True); assert u.vehicleKey == "anon:" + u.ID
    ctl.vinReader.clock.t += 30; vitals(u, True); assert len(liveCalls(requests_mock)) == 2   # not yet a poll
    ctl.vinReader.clock.t += 30; vitals(u, True)
    assert u.vehicleKey == VIN and len(liveCalls(requests_mock)) == 3

def test_owner_412_latches_and_fleet_is_asked_next(requests_mock):                    # I4
    api, reg, ctl, u = setup(requests_mock); api.storeToken(fleet_token()); reg.getOrCreate(VIN, "t")
    requests_mock.get(re.compile(r"https://owner-api.*/live_status$"), status_code=412, json={})
    vitals(u, True); assert u.vehicleKey == "anon:" + u.ID
    assert not api.wallConnectorVinsReady(api.ownerSlot) and api.providers()[1].ready(None, C.READ_WALL_CONNECTOR_VINS)
    ctl.vinReader.clock.t += 60; vitals(u, True); assert u.vehicleKey == VIN
    vitals(u, False); ctl.vinReader.clock.t += 600; vitals(u, True)
    hosts = [r.url.split("/")[2] for r in liveCalls(requests_mock)]
    assert hosts.count("owner-api.teslamotors.com") == 1 and onlyFreeCalls(requests_mock)

def test_owner_404_from_live_status_latches(requests_mock):                           # I4
    api, reg, ctl, u = setup(requests_mock)
    requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), status_code=404, json={})
    assert api.wallConnectorVins(api.ownerSlot) is None and not api.wallConnectorVinsReady(api.ownerSlot)

def test_fleet_404_does_not_latch_and_only_403_reports_a_scope(requests_mock):       # I4, M3
    api, reg, ctl, u = setup(requests_mock); api.ownerSlot.clear(); api.storeToken(fleet_token())
    requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), status_code=404, json={})
    with patch.object(api.fleetSlot, "reportScopeRefused") as scope:
        assert api.wallConnectorVins(api.fleetSlot) is None and api.wallConnectorVinsReady(api.fleetSlot)
        for code in (401, 410, 412):
            api.storeToken(fleet_token())
            requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), status_code=code, json={})
            assert api.wallConnectorVins(api.fleetSlot) is None and not api.wallConnectorVinsReady(api.fleetSlot)
        assert scope.call_count == 0
        api.storeToken(fleet_token())
        requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), status_code=403, json={})
        api.wallConnectorVins(api.fleetSlot)
    scope.assert_called_once_with("energy", refused=True)

def test_polling_slows_after_ten_unanswered_polls(requests_mock):                     # M1
    api, reg, ctl, u = setup(requests_mock, din="other--din"); u.device_cfg["din"] = DIN
    vitals(u, True)
    for _ in range(12): ctl.vinReader.clock.t += 60; vitals(u, True)
    n = len(liveCalls(requests_mock)); assert n == 10
    ctl.vinReader.clock.t += 540; vitals(u, True); assert len(liveCalls(requests_mock)) == n + 1
    ctl.vinReader.clock.t += 30; vitals(u, False); vitals(u, True); assert len(liveCalls(requests_mock)) == n + 2   # a new edge resets it

def test_single_local_single_cloud_matches_without_din(requests_mock):
    api, reg, ctl, u = setup(requests_mock, din="whatever"); u.device_cfg.pop("din", None); u._version = {}
    vitals(u, True); ctl.vinReader.clock.t += 60; vitals(u, True)
    assert u.cloudVin == VIN

def test_403_marks_the_capability_not_ready_once(requests_mock):
    api, reg, ctl, u = setup(requests_mock, status=403)
    vitals(u, True); ctl.vinReader.clock.t += 60; vitals(u, True)
    assert len(liveCalls(requests_mock)) == 1
    assert not api.providers()[0].ready(None, C.READ_WALL_CONNECTOR_VINS)
    api.storeToken(owner_token()); assert api.providers()[0].ready(None, C.READ_WALL_CONNECTOR_VINS)   # token change re-arms

def test_no_query_without_an_occupied_gen3_and_no_vehicle_calls(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    ctl.vinReader.apply(ctl.devices()); assert liveCalls(requests_mock) == []
    vitals(u, True)
    assert all(not re.search(r"wake_up|vehicle_data", r.url) for r in requests_mock.request_history)

def test_rate_limit_is_respected(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    for _ in range(4): ctl.vinReader.noteConnect(); ctl.vinReader.apply(ctl.devices()); vitals(u, True)   # one clock, no time passes
    assert len(liveCalls(requests_mock)) <= 2


# Beyond the brief

def onlyFreeCalls(requests_mock):
    # Every request is products or live_status: never a vehicle endpoint,
    # vehicle_data, a wake or a command
    return all(re.search(r"/api/1/products$|/energy_sites/\d+/live_status$", r.url) and r.method == "GET"
               for r in requests_mock.request_history)

def test_wall_connector_edges_are_spaced_thirty_seconds(requests_mock):     # I7, live_status requests counted
    api, reg, ctl, u = setup(requests_mock, din="other--din"); u.device_cfg["din"] = DIN   # never named: it keeps waiting
    vitals(u, True); assert len(liveCalls(requests_mock)) == 1
    for _ in range(5): vitals(u, False); vitals(u, True)                     # a flapping occupied flag
    ctl.vinReader.clock.t += 29; vitals(u, True); assert len(liveCalls(requests_mock)) == 1
    ctl.vinReader.clock.t += 1; vitals(u, True); assert len(liveCalls(requests_mock)) == 2   # the held edge, at 30 s
    ctl.vinReader.clock.t += 59; vitals(u, True); assert len(liveCalls(requests_mock)) == 2   # then the minute's poll
    assert onlyFreeCalls(requests_mock)

def test_a_vin_arriving_after_the_edge_attaches_the_session(requests_mock):          # carry 4
    api, reg, ctl, u = setup(requests_mock)
    requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), [
        {"json": {"response": {"wall_connectors": [{"din": DIN, "wall_connector_state": 2}]}}},
        {"json": live()}, {"json": live()}])
    vitals(u, True); assert u.vehicleKey == "anon:" + u.ID and u.cloudVin is None
    vitals(u, True); assert len(liveCalls(requests_mock)) == 1               # within the poll interval: no query
    ctl.vinReader.clock.t += 60; vitals(u, True); assert u.cloudVin == VIN and reg.get(VIN) is None   # pending
    ctl.vinReader.clock.t += 60; vitals(u, True)
    assert u.vehicleKey == VIN and reg.get("anon:" + u.ID) is None and len(liveCalls(requests_mock)) == 3
    assert onlyFreeCalls(requests_mock)

def test_a_late_vin_for_a_known_car_attaches_at_once(requests_mock):                # carry 4
    api, reg, ctl, u = setup(requests_mock); reg.getOrCreate(VIN, "TeslaMateVehicle")
    requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), [
        {"json": {"response": {"wall_connectors": [{"din": DIN}]}}}, {"json": live()}])
    vitals(u, True); assert u.vehicleKey == "anon:" + u.ID
    ctl.vinReader.clock.t += 60; vitals(u, True); assert u.vehicleKey == VIN

def test_disconnect_clears_the_cloud_vin_and_the_next_car_queries_again(requests_mock):
    api, reg, ctl, u = setup(requests_mock); reg.getOrCreate(VIN, "TeslaMateVehicle")
    vitals(u, True); assert u.cloudVin == VIN
    vitals(u, False); assert u.cloudVin is None and u.vehicleKey is None
    ctl.vinReader.clock.t += 30; vitals(u, True); assert len(liveCalls(requests_mock)) == 2 and u.vehicleKey == "anon:" + u.ID   # I2: the departed VIN waits a poll
    ctl.vinReader.clock.t += 60; vitals(u, True); assert u.vehicleKey == VIN

def test_owner_refusal_falls_to_fleet_on_the_next_poll(requests_mock):
    api, reg, ctl, u = setup(requests_mock); api.storeToken(fleet_token()); reg.getOrCreate(VIN, "TeslaMateVehicle")
    requests_mock.get(re.compile(r"https://owner-api.*/live_status$"), status_code=403, json={})
    vitals(u, True)
    assert u.vehicleKey == "anon:" + u.ID
    fleet = api.providers()[1]
    assert fleet.ready(None, C.READ_WALL_CONNECTOR_VINS) and not api.providers()[0].ready(None, C.READ_WALL_CONNECTOR_VINS)
    ctl.vinReader.clock.t += 60; vitals(u, True)
    assert u.vehicleKey == VIN
    assert [r.url.split("/")[2] for r in liveCalls(requests_mock)] == ["owner-api.teslamotors.com", "fleet-api.prd.na.vn.cloud.tesla.com"]
    assert onlyFreeCalls(requests_mock)

def test_fleet_waits_while_the_owner_slot_can_read_energy(requests_mock):
    api, reg, ctl, u = setup(requests_mock); api.storeToken(fleet_token())
    owner, fleet = api.providers()[:2]
    assert owner.ready(None, C.READ_WALL_CONNECTOR_VINS) and not fleet.ready(None, C.READ_WALL_CONNECTOR_VINS)
    assert owner.declarations()[C.READ_WALL_CONNECTOR_VINS] == fleet.declarations()[C.READ_WALL_CONNECTOR_VINS] == C.CLOUD_FREE

def test_ready_without_a_vehicle_is_only_relaxed_for_wall_connector_vins(requests_mock):   # carry 1
    api, reg, ctl, u = setup(requests_mock); api.storeToken(fleet_token())
    api.ownerSlot.clear()
    owner, fleet = api.providers()[:2]
    assert fleet.ready(None, C.READ_WALL_CONNECTOR_VINS)
    for p in (owner, fleet):
        for cap in (C.READ_CHARGE, C.SET_AMPS, C.WAKE):
            assert p.ready(None, cap) is False
    assert not owner.ready(None, C.READ_WALL_CONNECTOR_VINS)                 # no Owner token

def test_fleet_energy_gate_follows_the_energy_scope_not_vehicle_data(requests_mock):
    api, reg, ctl, u = setup(requests_mock); api.ownerSlot.clear()
    api.storeToken(fleet_token(scp=["openid", "energy_device_data"]))
    fleet = api.providers()[1]
    assert fleet.available(C.READ_WALL_CONNECTOR_VINS) and fleet.ready(None, C.READ_WALL_CONNECTOR_VINS)
    api.storeToken(fleet_token(scp=["openid", "vehicle_device_data"]))
    assert not fleet.available(C.READ_WALL_CONNECTOR_VINS) and not fleet.ready(None, C.READ_WALL_CONNECTOR_VINS)

def test_products_are_cached_for_an_hour(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    for _ in range(3): api.wallConnectorVins(api.ownerSlot)
    assert len([r for r in requests_mock.request_history if r.url.endswith("/products")]) == 1
    with patch("TWCManager.Vehicle.TeslaAPI.time.time", return_value=__import__("time").time() + 3601):
        api.wallConnectorVins(api.ownerSlot)
    assert len([r for r in requests_mock.request_history if r.url.endswith("/products")]) == 2

def test_no_energy_sites_makes_the_capability_not_ready(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    requests_mock.get(re.compile(r".*/api/1/products$"), json={"response": [{"vin": VIN, "id": 1}]})
    assert api.wallConnectorVins(api.ownerSlot) == {}
    assert not api.wallConnectorVinsReady(api.ownerSlot) and liveCalls(requests_mock) == []

def test_a_failed_products_fetch_is_not_cached_as_empty(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    requests_mock.get(re.compile(r".*/api/1/products$"), status_code=500, json={})
    assert api.wallConnectorVins(api.ownerSlot) is None and api._fetchEnergySites(api.ownerSlot) is None
    assert api.wallConnectorVinsReady(api.ownerSlot)

def test_a_site_without_wall_connectors_is_dropped_from_later_polls(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    requests_mock.get(re.compile(r".*/api/1/products$"), json={"response": [{"energy_site_id": 7}, {"energy_site_id": 8}]})
    requests_mock.get(re.compile(r".*/energy_sites/8/live_status$"), json={"response": {"battery_power": 0}})
    assert api.wallConnectorVins(api.ownerSlot) == {DIN: VIN}
    assert api.wallConnectorVins(api.ownerSlot) == {DIN: VIN}
    assert len([r for r in requests_mock.request_history if "/energy_sites/8/" in r.url]) == 1

def test_a_transport_error_returns_none_and_never_raises(requests_mock):
    import requests
    api, reg, ctl, u = setup(requests_mock)
    requests_mock.get(re.compile(r".*/energy_sites/7/live_status$"), exc=requests.exceptions.ConnectTimeout)
    assert api.wallConnectorVins(api.ownerSlot) is None and api.wallConnectorVinsReady(api.ownerSlot)

def test_match_rules():
    m, ctl, p = make_gen3((IP1, IP2)); reader = WallConnectorVinReader(m, Mock(), clock=Clock())
    a, b = ctl.devices(); vitals(a, True); vitals(b, True)
    a.device_cfg["din"] = DIN; b._version = {"part_number": "1529455-02-D", "serial_number": "PGT1"}
    assert b.din == "1529455-02-D--PGT1"
    assert reader.match(ctl.devices(), {DIN: VIN, "1529455-02-D--PGT1": VIN1}) == {a: VIN, b: VIN1}
    with patch("TWCManager.Vehicle.Providers.WallConnectorVins.logger") as log:
        assert reader.match(ctl.devices(), {"other--din": VIN}) == {}         # known DINs that match nothing
        b._version = {}; assert b.din is None
        assert reader.match(ctl.devices(), {"x--y": VIN1}) == {}            # two local units: no guessing
    assert log.info.call_count == 2 and {c.args[1] for c in log.info.call_args_list} == {a.ID, b.ID}   # once per unit and session
    assert all("no DIN match for %s" in c.args[0] for c in log.info.call_args_list)

def test_din_comes_from_config_then_version():
    m, ctl, p = make_gen3(); (u,) = ctl.devices()
    assert u._version is None and u.din is None
    resp = MagicMock(); resp.__enter__.return_value.read.return_value = b'{"part_number": "1529455-02-D", "serial_number": "PGT9", "firmware_version": "x"}'
    with patch("urllib.request.urlopen", return_value=resp) as get:
        assert u.refreshVersion() is True
    assert get.call_args.args[0] == "http://10.0.0.1/api/1/version" and u.din == "1529455-02-D--PGT9"
    u.device_cfg["din"] = "override--1"; assert u.din == "override--1"

def test_a_failed_version_fetch_is_retried():
    m, ctl, p = make_gen3(); (u,) = ctl.devices()
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert u.refreshVersion() is False
    assert u._version is None

def test_the_reader_never_breaks_the_vitals_loop():
    m, ctl, p = make_gen3(); (u,) = ctl.devices()                             # master.providerOrder is a bare Mock
    assert vitals(u, True) is True and u.vehicleKey == "anon:" + u.ID

def test_a_retried_rising_edge_leaves_no_orphan_anonymous_vehicle():                  # carry 2 (Task 4 N1)
    m, ctl, p = make_gen3(); (u,) = ctl.devices(); reg = m.vehicleRegistry
    car = reg.getOrCreate(VIN1, "TeslaMateVehicle"); car.observe("pluggedIn", True, "TeslaMateVehicle")
    real = car.observe; failed = []
    def flaky(field, value, source, *a, **k):
        if source == "Gen3TWC" and not failed:
            failed.append(field); raise RuntimeError("boom")
        return real(field, value, source, *a, **k)
    with patch.object(car, "observe", side_effect=flaky):
        vitals(u, True)                                                       # correlated, then an observe raised
        assert failed and u.vehicleKey == VIN1
        vitals(u, True)                                                       # the retried edge
    assert reg.get("anon:" + u.ID) is None and u.vehicleKey == VIN1
    vitals(u, False); assert u.vehicleKey is None

def test_a_configured_din_skips_the_version_fetch(requests_mock):
    api, reg, ctl, u = setup(requests_mock)
    vitals(u, True); assert u._version is None and u.din == DIN

def test_query_asks_the_first_candidate_directly():
    p = Mock(); p.name = "X"; p.declarations.return_value = {C.READ_WALL_CONNECTOR_VINS: C.CLOUD_FREE}
    p.wallConnectorVins.return_value = {DIN: VIN}
    order = Mock(); order.candidates.return_value = [p]
    assert WallConnectorVinReader(Mock(), order, clock=Clock()).query([]) == {DIN: VIN}

def versionResp(body):
    resp = MagicMock(); resp.__enter__.return_value.read.return_value = body; return resp

def test_a_failing_version_fetch_backs_off_for_ten_minutes():                         # I3
    m, ctl, p = make_gen3(); (u,) = ctl.devices()
    with patch("TWCManager.EVSEInstance.Gen3TWC.time.monotonic", return_value=5000.0), \
         patch("urllib.request.urlopen", side_effect=OSError("down")) as get:
        assert u.refreshVersion() is False and u.refreshVersion() is False
    assert get.call_count == 1 and u._version is None
    with patch("TWCManager.EVSEInstance.Gen3TWC.time.monotonic", return_value=5600.0), \
         patch("urllib.request.urlopen", return_value=versionResp(b'{"part_number": "P", "serial_number": "S"}')) as get:
        assert u.refreshVersion() is True
    assert get.call_count == 1 and u.din == "P--S"

def test_a_404_version_is_final():                                                    # I3
    import urllib.error
    m, ctl, p = make_gen3(); (u,) = ctl.devices()
    err = urllib.error.HTTPError("http://10.0.0.1/api/1/version", 404, "Not Found", {}, None)
    with patch("urllib.request.urlopen", side_effect=err):
        assert u.refreshVersion() is True
    assert u._version == {} and u.din is None


def test_a_failed_query_never_logs_its_text(caplog):
    # 12b parked: exc_info could carry a token
    from tests.unit.test_gen3twc_provider import Clock

    token = fleet_token(jti="leak")
    order = Mock()
    order.candidates.side_effect = RuntimeError("Bearer " + token)
    reader = WallConnectorVinReader(Mock(), order, clock=Clock())
    unit = Mock(occupied=True, vehicleKey="anon:u1", cloudVin=None, ID="u1")
    with caplog.at_level(1):
        reader.apply([unit])
    assert order.candidates.called and "RuntimeError" in caplog.text
    assert token not in caplog.text
