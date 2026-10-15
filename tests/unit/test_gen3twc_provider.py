import os
import json
from unittest.mock import MagicMock, Mock, patch
from TWCManager.EVSEController.Gen3TWCs import Gen3TWCs
from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Gen3TWCProvider import Gen3TWCProvider
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry
VIN1 = "5YJ3E1EA1KF000001"; VIN2 = "5YJ3E1EA1KF000002"; IP1, IP2 = "10.0.0.1", "10.0.0.2"

class Clock:
    def __init__(self, t=1000.0): self.t = t
    def __call__(self): return self.t

def make(ips=(IP1,), clock=None):
    m = Mock(); m.vehicleRegistry = VehicleRegistry({}, clock=clock or Clock())
    m.config = {"config": {"defaultVoltage": 240}, "controller.Gen3TWCs": {"enabled": True, "devices": [{"ip": ip, "fuseAmps": 48, "phases": 1} for ip in ips]}}
    with patch.object(Gen3TWCs, "_start_server"):
        ctl = Gen3TWCs(m)
    ctl.setHouseWatts = Mock(); return m, ctl, Gen3TWCProvider(ctl, m)

def vitals(unit, connected, amps=0.0, state="charging"):
    resp = MagicMock(); resp.__enter__.return_value.read.return_value = json.dumps(
        {"vehicle_connected": connected, "vehicle_current_a": amps, "grid_v": 240.0, "evse_state": state}).encode()
    with patch("urllib.request.urlopen", return_value=resp):
        return unit.refreshVitals()

def test_properties_never_fetch_and_a_stale_cache_queues_one_background_task():   # G5
    m, ctl, p = make(); (u,) = ctl.devices()
    with patch("urllib.request.urlopen", side_effect=AssertionError("no HTTP on the caller's thread")):
        assert ctl.allEVSEs == [] and u.isReachable is False and u.occupied is None and u.wantsToCharge is False
    m.queue_background_task.assert_called_once_with({"cmd": "gen3Vitals"})
    assert vitals(u, True, 12.0) is True and ctl.allEVSEs == [u] and u.currentAmps == 12.0 and u.isLocal and u.idleMinimum is False

def test_connect_begins_an_anonymous_session_and_disconnect_ends_it():            # G3
    m, ctl, p = make(); (u,) = ctl.devices(); reg = m.vehicleRegistry
    vitals(u, True, 6.0); anon = reg.get("anon:" + u.ID)
    assert anon is not None and u.vehicleKey == anon.key and u.occupied is True
    assert anon.latest("pluggedIn").value is True and anon.latest("actualCurrent").value == 6.0 and anon.latest("voltage").source == "Gen3TWC"
    vitals(u, False)
    assert reg.get("anon:" + u.ID) is None and u.vehicleKey is None and u.occupied is False

def test_single_candidate_car_is_correlated():                                     # G3
    m, ctl, p = make(); (u,) = ctl.devices(); reg = m.vehicleRegistry
    car = reg.getOrCreate(VIN1, "TeslaMateVehicle"); car.observe("pluggedIn", True, "TeslaMateVehicle")
    other = reg.getOrCreate(VIN2, "TeslaMateVehicle"); other.observe("pluggedIn", False, "TeslaMateVehicle")
    vitals(u, True)
    assert u.vehicleKey == VIN1 and reg.get("anon:" + u.ID) is None and car.value("twcVin") == u.ID
    assert p.ready(car, C.SET_AMPS) and p.ampsRange(car) == (6, 48) and not p.ready(other, C.SET_AMPS)

def test_two_candidate_cars_stay_anonymous():                                      # G3
    m, ctl, p = make(); (u,) = ctl.devices(); reg = m.vehicleRegistry
    for vin in (VIN1, VIN2): reg.getOrCreate(vin, "t").observe("pluggedIn", True, "TeslaMateVehicle")
    vitals(u, True)
    assert u.vehicleKey == "anon:" + u.ID and reg.get(VIN1).value("twcVin") is None and reg.get(VIN2).value("twcVin") is None

def test_old_plug_evidence_or_a_car_already_on_a_twc_is_not_a_candidate():
    c = Clock(); m, ctl, p = make(clock=c); (u,) = ctl.devices(); reg = m.vehicleRegistry
    car = reg.getOrCreate(VIN1, "t"); car.observe("pluggedIn", True, "TeslaMateVehicle"); c.t += 121
    vitals(u, True); assert u.vehicleKey == "anon:" + u.ID
    vitals(u, False); car.observe("pluggedIn", True, "TeslaMateVehicle"); car.observe("twcVin", "8244", "TWC")
    vitals(u, True); assert u.vehicleKey == "anon:" + u.ID

def test_capabilities_normal_tier_stop_and_declared_shared_control():              # G2, G4
    m, ctl, p = make()
    assert (p.name, p.rank, p.local, p.sharedControl, ctl.sharedControl) == ("Gen3TWC", 1, True, True, True)
    assert p.cost(C.STOP_CHARGING) == C.LOCAL and p.cost(C.SET_AMPS) == C.LOCAL and p.declarations()[C.READ_PLUG].passive

def test_single_unit_headroom_equals_its_target_as_today():                        # G2
    m, ctl, p = make(); (u,) = ctl.devices()
    u.setTargetPower(4800); ctl.setHouseWatts.assert_called_with(watts=11520 - 4800, phases=1, voltage=240)
    u.setTargetPower(2400); ctl.setHouseWatts.assert_called_with(watts=11520 - 2400, phases=1, voltage=240)
    u.setTargetPower(0);    ctl.setHouseWatts.assert_called_with(watts=11520, phases=1, voltage=240)

def test_two_unconstrained_units_share_the_mean():                                  # G2
    m, ctl, p = make((IP1, IP2)); a, b = ctl.devices(); vitals(a, True); vitals(b, True)
    a.setTargetPower(4800); b.setTargetPower(2400)                             # 20 A + 10 A over two units: h = 15 A
    assert ctl.headroom() == 15 and ctl.setHouseWatts.call_args.kwargs == {"watts": (48 - 15) * 240, "phases": 1, "voltage": 240}

def test_car_limited_unit_raises_h_so_the_net_matches_and_small_changes_are_damped():   # G2
    m, ctl, p = make((IP1, IP2)); a, b = ctl.devices(); vitals(a, True); vitals(b, True)
    a.setTargetPower(4800); b.setTargetPower(2400); n = ctl.setHouseWatts.call_count
    vitals(b, True, amps=3.0)                                                  # b is full and draws 3 A: a may take 27 A
    assert ctl.headroom() == 27 and ctl.setHouseWatts.call_args.kwargs["watts"] == (48 - 27) * 240 and ctl.setHouseWatts.call_count == n + 1
    vitals(b, True, amps=3.4)                                                  # within DAMP_AMPS: no republish
    assert ctl.setHouseWatts.call_count == n + 1

def test_zero_target_beside_a_charging_sibling_declares_no_individual_stop():       # G2
    m, ctl, p = make((IP1, IP2)); a, b = ctl.devices(); vitals(a, True); vitals(b, True); reg = m.vehicleRegistry
    a.setTargetPower(4800); b.setTargetPower(0)                                # h covers a's 20 A only; b may still draw up to h
    assert ctl.headroom() == 20 and ctl.setHouseWatts.call_args.kwargs["watts"] == (48 - 20) * 240
    assert p.limitations(reg.get("anon:" + b.ID)) == frozenset({C.NO_INDIVIDUAL_STOP})
    a.setTargetPower(0); assert ctl.setHouseWatts.call_args.kwargs["watts"] == 11520    # everyone stopped: the full fuse as load
    vitals(b, False); assert p.limitations(reg.get("anon:" + a.ID)) == frozenset()

def test_provider_commands_go_to_the_unit():
    m, ctl, p = make(); (u,) = ctl.devices(); vitals(u, True); anon = m.vehicleRegistry.get("anon:" + u.ID)
    assert p.setAmps(anon.key, 16) is True; ctl.setHouseWatts.assert_called_with(watts=11520 - 3840, phases=1, voltage=240)
    assert p.stopCharging(anon.key) is True; ctl.setHouseWatts.assert_called_with(watts=11520, phases=1, voltage=240)

def test_amps_range_is_never_none_while_set_amps_is_ready():                       # Task 2 constraint
    m, ctl, p = make((IP1, IP2)); a, b = ctl.devices(); reg = m.vehicleRegistry
    assert p.ampsRange(None) is None and not p.ready(None, C.SET_AMPS)
    vitals(a, True); vitals(b, False)
    anon = reg.get("anon:" + a.ID)
    assert p.ready(anon, C.SET_AMPS) and p.ampsRange(anon) == (6, 48)
    vitals(a, False)
    assert not p.ready(anon, C.SET_AMPS) and p.ampsRange(anon) is None


def test_unreachable_unit_keeps_its_failure_backoff():
    m, ctl, p = make(); (u,) = ctl.devices()
    with patch("urllib.request.urlopen", side_effect=OSError("down")):
        assert u.vitalsStale() and u.refreshVitals() is False
    assert not u.vitalsStale() and u.isReachable is False and u.occupied is None


def test_controller_refresh_and_background_task_branch():
    m, ctl, p = make((IP1, IP2)); a, b = ctl.devices()
    with patch.object(type(a), "refreshVitals", autospec=True, return_value=True) as r:
        ctl.refreshVitals()
    assert [c.args[0] for c in r.call_args_list] == [a, b]
    with open(os.path.join(os.path.dirname(__file__), "..", "..", "lib", "TWCManager", "TWCManager.py")) as f:
        src = f.read()
    assert 'task["cmd"] == "gen3Vitals"' in src and "Gen3TWCProvider(gen3, master)" in src
