# tests/unit/test_charging_points.py
import logging
import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch
from TWCManager.EVSEController.Gen3TWCs import Gen3TWCs
from TWCManager.EVSEController.VehicleEndpoints import VehicleEndpoints
from TWCManager.EVSEInstance.ChargingPoint import ChargingPoint, groupEndpoints
from TWCManager.TWCMaster import TWCMaster
from tests.unit.test_gen2twc_provider import CONFIG
from tests.unit.test_gen3twc_provider import vitals
from tests.unit.test_provider_order import fleet
VIN = "5YJ3E1EA1KF000001"; VIN2 = "5YJ3E1EA1KF000002"

def ep(ID, key=None, local=False, minW=1200, maxW=7680, wants=True, ro=False, occupied=None, amps=0.0):
    e = MagicMock(); e.ID = ID; e.vehicleKey = key; e.isLocal = local; e.minPower = minW; e.maxPower = maxW
    e.wantsToCharge = wants; e.isReadOnly = ro; e.occupied = occupied; e.currentAmps = amps; e.currentVoltage = [240, 0, 0]
    e.currentVIN = key if key and not key.startswith("anon:") else ""; e.controllers = ["Gen2TWCs" if local else "VehicleEndpoints"]; return e

def test_grouping_is_by_vehicle_key_and_merge_count_may_be_one():
    twc = ep("Gen2TWC-8244", VIN.lower(), local=True); car = ep("Vehicle-" + VIN, VIN); idle = ep("Gen2TWC-8245", None, local=True); anon = ep("Gen2TWC-8246", "anon:8246", local=True)
    pts = groupEndpoints(Mock(), [twc, car, idle, anon])
    assert [p.ID for p in pts] == ["Point-" + VIN, "Point-Gen2TWC-8245", "Point-anon:8246"]
    assert pts[0].endpoints == [twc, car] and pts[0].isLocal and pts[0].currentVIN == VIN and pts[0].vehicleKey == VIN
    assert pts[1].endpoints == [idle] and pts[1].vehicleKey == "Gen2TWC-8245"

def test_a_point_holds_at_most_one_local_endpoint():                               # review I1
    t1 = ep("Gen2TWC-8244", VIN, local=True); t2 = ep("Gen2TWC-8245", VIN.lower(), local=True); car = ep("Vehicle-" + VIN, VIN)
    pts = groupEndpoints(Mock(), [t1, t2, car])
    assert [(p.ID, p.endpoints) for p in pts] == [("Point-" + VIN, [t1, car]), ("Point-Gen2TWC-8245", [t2])]

def test_point_limits_and_occupancy():
    p = ChargingPoint(Mock(), VIN, [ep("a", VIN, local=True, minW=1440, maxW=7680, occupied=True), ep("b", VIN, minW=1200, maxW=3840, ro=True)])
    assert (p.minPower, p.maxPower, p.isReadOnly, p.occupied) == (1440, 3840, False, True)
    assert ChargingPoint(Mock(), "x", [ep("a"), ep("b")]).occupied is None
    assert ChargingPoint(Mock(), "x", [ep("a", occupied=False), ep("b", occupied=False)]).occupied is False

def test_target_goes_to_the_local_member_only_when_present():
    twc = ep("t", VIN, local=True); car = ep("c", VIN)
    ChargingPoint(Mock(), VIN, [twc, car]).setTargetPower(2400)
    twc.setTargetPower.assert_called_once_with(2400); car.setTargetPower.assert_not_called()
    ChargingPoint(Mock(), VIN, [car]).setTargetPower(2400); car.setTargetPower.assert_called_once_with(2400)

def distributor(points, amps, controllers=("Gen2TWCs", "VehicleEndpoints")):
    m = TWCMaster.__new__(TWCMaster); m.config = {"config": {"defaultVoltage": 240, "numberOfPhases": 1}}
    ctrl = MagicMock(); ctrl.maxPower = 100000
    m.getModulesByType = lambda t: [{"name": n, "ref": ctrl} for n in controllers]
    m.chargingPoints = lambda: points; m.getMaxAmpsToDivideAmongSlaves = lambda: amps; return m

def test_stopped_car_stays_one_point():                             # Review Focus 5 / A18
    twc = ep("Gen2TWC-8244", VIN, local=True, wants=False); car = ep("Vehicle-" + VIN, VIN, wants=True)
    pts = groupEndpoints(Mock(), [twc, car]); assert len(pts) == 1
    distributor(pts, 40).distributeEVSEPower()                                       # the TWC reports no demand: its idle offer (M1): 0, which the TWC treats as its minimum
    twc.setTargetPower.assert_called_once_with(0); car.setTargetPower.assert_not_called()

def test_the_twc_decides_whether_its_point_wants_power():                          # final review M1
    # A stale registry "Charging" must not outweigh a TWC reporting no demand
    finished = ep("Gen2TWC-8244", VIN, local=True, wants=False, minW=1440); stale = ep("Vehicle-" + VIN, VIN, wants=True)
    busy = ep("Gen2TWC-8245", VIN2, local=True, wants=True); car2 = ep("Vehicle-" + VIN2, VIN2, wants=True)
    pts = groupEndpoints(Mock(), [finished, stale, busy, car2])
    assert [p.wantsToCharge for p in pts] == [False, True]
    distributor(pts, 40).distributeEVSEPower()                                       # 9600 W: 7680 to the charging car, the idle offer (0) to the finished one
    assert busy.setTargetPower.call_args.args[0] == 7680 and finished.setTargetPower.call_args.args[0] == 0
    stale.setTargetPower.assert_not_called()
    assert ChargingPoint(Mock(), VIN, [ep("c", VIN, wants=True)]).wantsToCharge is True     # no local member: the car decides

def test_idle_point_target_is_zero_and_never_counted_against_the_budget():
    # A Gen2 TWC treats an offer of 0 as its hardware minimum, so an idle point's target is 0
    # and neither gates on nor takes from the budget
    busy = ep("Vehicle-" + VIN, VIN, maxW=4800); idle = ep("Gen2TWC-8245", None, local=True, wants=False, minW=1440)
    distributor(groupEndpoints(Mock(), [busy, idle]), 40).distributeEVSEPower()           # 9600 W: 4800 to the car
    assert busy.setTargetPower.call_args.args[0] == 4800 and idle.setTargetPower.call_args.args[0] == 0
    busy2 = ep("Vehicle-" + VIN, VIN, maxW=9600); idle2 = ep("Gen2TWC-8245", None, local=True, wants=False, minW=1440)
    distributor(groupEndpoints(Mock(), [busy2, idle2]), 40).distributeEVSEPower()          # the car takes the whole 9600 W budget
    assert busy2.setTargetPower.call_args.args[0] == 9600 and idle2.setTargetPower.call_args.args[0] == 0
    idle3 = ep("Gen2TWC-8245", None, local=True, wants=False, minW=1440); remote = ep("Vehicle-x", "X", wants=False)
    distributor(groupEndpoints(Mock(), [idle3, remote]), 40).distributeEVSEPower()
    assert idle3.setTargetPower.call_args.args[0] == 0 and remote.setTargetPower.call_args.args[0] == 0

def test_fair_share_below_the_point_minimum_is_a_stop_not_a_partial_amount():
    a = ep("Vehicle-" + VIN, VIN, minW=2880, maxW=7680); b = ep("Vehicle-" + VIN2, VIN2, minW=2880, maxW=7680)
    distributor(groupEndpoints(Mock(), [a, b]), 20).distributeEVSEPower()                  # 4800 W: 2400 W each, under the 2880 W floor
    got = sorted([a.setTargetPower.call_args.args[0], b.setTargetPower.call_args.args[0]])
    assert got == [0, 4800]                                                              # one is stopped, the other takes the rest; never a partial amount

def test_idle_gen3_units_get_zero_and_two_units_are_two_points():                  # G2 (grouping side)
    g1 = ep("Gen3TWC-10.0.0.1", "anon:Gen3TWC-10.0.0.1", local=True, wants=True, maxW=11520)
    g2 = ep("Gen3TWC-10.0.0.2", None, local=True, wants=False, minW=1440)
    pts = groupEndpoints(Mock(), [g1, g2]); assert [p.ID for p in pts] == ["Point-anon:Gen3TWC-10.0.0.1", "Point-Gen3TWC-10.0.0.2"]
    distributor(pts, 40).distributeEVSEPower()
    assert g1.setTargetPower.call_args.args[0] == 9600 and g2.setTargetPower.call_args.args[0] == 0

def test_correlated_car_joins_its_gen3_point():
    g = ep("Gen3TWC-10.0.0.1", VIN, local=True); car = ep("Vehicle-" + VIN, VIN)
    (pt,) = groupEndpoints(Mock(), [g, car]); pt.setTargetPower(2400)
    g.setTargetPower.assert_called_once_with(2400); car.setTargetPower.assert_not_called()

def test_pure_gen2_leaves_distributor_off():                         # Review Focus 2
    idle = ep("Gen2TWC-8245", None, local=True, wants=False)
    distributor([ChargingPoint(Mock(), "Gen2TWC-8245", [idle])], 40, controllers=("Gen2TWCs",)).distributeEVSEPower()
    idle.setTargetPower.assert_not_called()

def test_no_type_knowledge_left():
    import subprocess
    from pathlib import Path
    out = subprocess.run(["git", "grep", "-l", "MergedEVSE\\|getDedupedEVSEs\\|TeslaAPIEVSE\\|TeslaAPIController", "--", "lib/TWCManager"],
                         capture_output=True, text=True, cwd=str(Path(__file__).resolve().parents[2])).stdout.split()
    assert out == []

# ----------------------------------------------------------------------
# Real Gen2TWC / VehicleEndpoint / Gen3TWC through chargingPoints() and
# distributeEVSEPower(): the Task 10 interim paid path (a stopped car on its
# TWC budgeted twice) is closed.
# ----------------------------------------------------------------------

def real(mode=1, gen3=False):
    config = dict(CONFIG, config=dict(CONFIG["config"], defaultVoltage=240, numberOfPhases=1, fakeMaster=1))
    if gen3: config["controller.Gen3TWCs"] = {"enabled": True, "devices": [{"ip": "10.0.0.1", "fuseAmps": 48, "phases": 1}]}
    m = TWCMaster("AB", config); m.settings["chargeRateControl"] = mode
    m.queue_vehicle_command = Mock(); m.queue_background_task = Mock()
    carapi = Mock(); carapi.enabled.return_value = True; policy = Mock(); policy.policyIsGreen.return_value = False
    modules = {"TeslaAPI": carapi, "Policy": policy}; m.getModuleByName = lambda n: modules.get(n)
    m.registerModule({"name": "VehicleEndpoints", "ref": VehicleEndpoints(m), "type": "EVSEController"})
    m.providerOrder.register(fleet())
    if gen3:
        with patch.object(Gen3TWCs, "_start_server"): ctl = Gen3TWCs(m)
        ctl.setHouseWatts = Mock(); modules["Gen3TWCs"] = ctl
        m.registerModule({"name": "Gen3TWCs", "ref": ctl, "type": "EVSEController"})
    return m

def budget(m, amps): m.getMaxAmpsToDivideAmongSlaves = lambda: amps

def car(m, vin, state, pluggedIn=None):
    v = m.vehicleRegistry.getOrCreate(vin, "TeslaAPI"); v.attach("TeslaAPI", SimpleNamespace(VIN=vin, atHome=True, syncSource="TeslaAPI", pluggedIn=pluggedIn))
    if state: v.observe("chargingState", state, "TeslaAPI")
    return v

def stoppedTwc(m, vin=VIN, twcid=b"\x82\x44"):
    s = m.newSlave(twcid, 80); s.lastVIN = vin; s.reportedState = 0x03; return s     # currentVIN cleared, lastVIN attributed

def endpoint(m, vin):
    (e,) = [e for e in m.modules["VehicleEndpoints"]["ref"].allEVSEs if e.currentVIN == vin]                # cached by VIN
    e.setTargetPower = Mock(wraps=e.setTargetPower); return e

def setAmps(m): return [c.args[0] for c in m.queue_vehicle_command.call_args_list if c.args[0]["cmd"] == "setAmps"]

def test_stopped_car_and_its_endpoint_form_one_point_with_the_twc_minimum():       # 1
    m = real(); s = stoppedTwc(m); car(m, VIN, "Stopped"); budget(m, 40)
    (pt,) = m.chargingPoints()
    assert pt.ID == "Point-" + VIN and [e.ID for e in pt.endpoints] == ["Gen2TWC-8244", "Vehicle-" + VIN]
    assert pt.minPower == s.minPower == 6 * 240
    m.distributeEVSEPower()
    # The TWC decides (M1): reporting no demand, it gets its idle offer, 0 (the TWC's hardware minimum)...
    assert s._evseTargetAmps == 0 and setAmps(m) == []
    s.reportedAmpsActual = 5.5                                                   # ...the paused car takes it...
    m.distributeEVSEPower()
    assert s._evseTargetAmps == 32 and setAmps(m) == []                         # ...and the full share follows

def test_lower_case_vin_on_the_twc_groups_with_the_endpoint():                     # 2
    m = real(); s = stoppedTwc(m, VIN.lower()); car(m, VIN, "Stopped")
    assert [p.ID for p in m.chargingPoints()] == ["Point-" + VIN]
    s.lastVIN = ""; s.currentVIN = VIN.lower()
    assert [p.ID for p in m.chargingPoints()] == ["Point-" + VIN]

def test_mode_1_budget_below_the_twc_floor_offers_zero_and_no_fleet_send():        # 3
    m = real(mode=1); s = stoppedTwc(m); car(m, VIN, "Stopped"); budget(m, 5)   # 1200 W: the car's 5 A, under the TWC's 6 A
    m.distributeEVSEPower()
    assert s._evseTargetAmps == 0 and setAmps(m) == []

def test_mode_2_named_twc_leaves_the_rate_to_the_heartbeat():                      # 4
    m = real(mode=2); s = m.newSlave(b"\x82\x44", 80); s.currentVIN = VIN; s.reportedAmpsMax = 32; car(m, VIN, "Charging"); budget(m, 20)
    assert m.modules["VehicleEndpoints"]["ref"].allEVSEs[0].provider == "FleetAPI"   # mode 2: the endpoint is writable over Fleet
    m.distributeEVSEPower()
    assert setAmps(m) == [] and s._evseTargetAmps == 20
    s._TWCSlave__lastAPIAmpsRequest = 0                                          # past the 30 s boot hold-off
    s.receive_slave_heartbeat(bytearray([0x01, 0x0C, 0x80, 0x03, 0xE8, 0x00, 0x00]))   # charging, 32 A max, 10 A actual
    assert [c.args[0]["cmd"] for c in m.queue_vehicle_command.call_args_list] == ["setChargeRate"]
    assert m.queue_vehicle_command.call_args.args[0]["amps"] == 20

def test_twc_point_sets_the_twc_target_and_never_the_endpoint():                   # 5
    m = real(); s = stoppedTwc(m); car(m, VIN, "Stopped"); budget(m, 20); e = endpoint(m, VIN)
    s.reportedAmpsMax = 20                                                       # the TWC echoes its offer: it wants power
    m.distributeEVSEPower(); assert s._evseTargetAmps == 20                     # water-fill
    s.reportedAmpsMax = 0
    m.vehicleRegistry.get(VIN).observe("chargingState", "Complete", "TeslaAPI")
    m.distributeEVSEPower(); assert s._evseTargetAmps == 0                      # nobody wants power: the idle offer
    car(m, VIN2, "Charging"); budget(m, 32)
    m.distributeEVSEPower(); assert s._evseTargetAmps == 0                      # zero-out: VIN2 takes all 32 A
    budget(m, 40)
    m.distributeEVSEPower(); assert s._evseTargetAmps == 0                      # zero-out loop with room: the idle offer
    e.setTargetPower.assert_not_called()
    assert [a["vin"] for a in setAmps(m)] == [VIN2]

def test_car_on_no_twc_is_its_own_point_and_queues_amps():                          # 6
    m = real(); s = m.newSlave(b"\x82\x44", 80); s.currentVIN = VIN2; s.reportedAmpsMax = 32; s.reportedAmpsActual = 20
    car(m, VIN2, "Charging"); car(m, VIN, "Charging"); budget(m, 40)
    assert sorted(p.ID for p in m.chargingPoints()) == ["Point-" + VIN, "Point-" + VIN2]
    m.distributeEVSEPower()
    assert setAmps(m) == [{"cmd": "setAmps", "vin": VIN, "amps": 20}] and s._evseTargetAmps == 20

def test_correlated_gen3_car_joins_the_gen3_point_and_queues_no_amps():             # 7
    m = real(gen3=True); v = car(m, VIN, "Charging"); v.observe("pluggedIn", True, "TeslaMateVehicle")
    (u,) = m.modules["Gen3TWCs"]["ref"].devices(); vitals(u, True, 10.0); assert u.vehicleKey == VIN
    (pt,) = m.chargingPoints(); budget(m, 20)
    assert sorted(e.ID for e in pt.endpoints) == ["Gen3TWC-10.0.0.1", "Vehicle-" + VIN]
    m.distributeEVSEPower()
    assert u.lastTarget == 20 * 240 and setAmps(m) == []

def test_gen2_at_00_with_a_sleeping_car_is_two_points_as_before_task_10():          # pinned; Phase 5 presence attribution
    m = real(); s = m.newSlave(b"\x82\x44", 80); car(m, VIN, "Stopped"); budget(m, 40)
    assert s.vehicleKey is None and [p.ID for p in m.chargingPoints()] == ["Point-Gen2TWC-8244", "Point-" + VIN]
    m.distributeEVSEPower()
    assert [a["vin"] for a in setAmps(m)] == [VIN] and s._evseTargetAmps == 0

# Fix round 1 (review I1): a stale Gen2 lastVIN must not put two TWCs in one point

def twcs(m): return [s for s in m.getSlaveTWCs()]

def pointsHolding(m, vin): return [p for p in m.chargingPoints() if any(e.ID == "Vehicle-" + vin for e in p.endpoints)]

def test_two_twcs_claiming_one_car_stay_separate_points_within_budget():
    for staleState, servingVin in ((0x01, True), (0x03, False)):
        m = real(); car(m, VIN, "Charging"); budget(m, 40)
        s1 = m.newSlave(b"\x82\x44", 80); s1.lastVIN = VIN; s1.reportedState = 0x01; s1.reportedAmpsMax = 32   # stale: car A left, something else plugged in
        s2 = m.newSlave(b"\x82\x45", 80); s2.reportedState = staleState; s2.reportedAmpsMax = 32
        if servingVin: s2.currentVIN = VIN
        else: s2.lastVIN = VIN                                                      # both TWCs report the same stale lastVIN
        assert s1.vehicleKey == s2.vehicleKey == VIN
        assert sorted(p.ID for p in m.chargingPoints()) == ["Point-" + VIN, "Point-Gen2TWC-8245"]
        assert len(pointsHolding(m, VIN)) == 1
        for _ in range(3): m.distributeEVSEPower()
        assert s1._evseTargetAmps + s2._evseTargetAmps <= 40 and setAmps(m) == []

def test_twc_offers_never_exceed_the_budget_or_wiring_across_point_shapes():
    def serving(s, vin): s.currentVIN = vin; s.reportedState = 0x01; s.reportedAmpsMax = 32
    def stopped(s, vin): s.lastVIN = vin; s.reportedState = 0x03
    def stale(s, vin): s.lastVIN = vin; s.reportedState = 0x01; s.reportedAmpsMax = 32      # car left; something else plugged in
    def idle(s, vin): pass
    shapes = [((serving, VIN), (stopped, VIN2.lower()), (stale, VIN)),                       # two cars on two TWCs, one stale lastVIN
              ((stale, VIN), (serving, VIN), (idle, None)),
              ((stale, VIN2), (stale, VIN2), (stale, VIN2)),
              ((serving, VIN), (serving, VIN2), (idle, None))]
    for shape in shapes:
        for amps in (5, 6, 12, 20, 40, 64, 80, 100):
            m = real(); car(m, VIN, "Charging"); car(m, VIN2, "Stopped"); budget(m, amps)
            for i, (make, vin) in enumerate(shape): make(m.newSlave(bytes([0x82, 0x44 + i]), 80), vin)
            for _ in range(2): m.distributeEVSEPower()
            offers = [s._evseTargetAmps for s in twcs(m)]
            assert sum(offers) <= min(amps, CONFIG["config"]["wiringMaxAmpsAllTWCs"]) + 1e-9, (shape, amps, offers)
            claimed = {v.upper() for _, v in shape if v}                                   # a car on no TWC still queues its own amps
            assert not [a for a in setAmps(m) if a["vin"] in claimed] and all(len(pointsHolding(m, v)) <= 1 for v in (VIN, VIN2))

# A car asleep on its TWC, read while the TWC held its 6 A sleep pilot: on our
# own TWC the pilot is our last offer, so it must not cap the point

def asleepUnderSixAmps(m, twcVolts, carMax=None):
    m.config["config"].update(wiringMaxAmpsAllTWCs=48, wiringMaxAmpsPerTWC=48)
    s = stoppedTwc(m); s.reportedAmpsMax = 6; s.voltsPhaseA = twcVolts             # the TWC's 6 A sleep pilot
    v = car(m, VIN, "Stopped"); v.observe("availableCurrent", 6, "TeslaBLE"); v.observe("voltage", 2, "TeslaBLE")   # not charging: 240 V default
    if carMax is not None: v.observe("chargeCurrentRequestMax", carMax, "TeslaBLE")
    budget(m, 48); return s

def test_a_car_read_under_the_sleep_pilot_still_gets_the_share():
    m = real(); s = asleepUnderSixAmps(m, 243)
    m.distributeEVSEPower(); assert s._evseTargetAmps == pytest.approx(48 * 240 / 243)
    m.vehicleRegistry.get(VIN).observe("chargeCurrentRequestMax", 32, "TeslaBLE")    # the car's own maximum binds
    m.distributeEVSEPower(); assert s._evseTargetAmps == pytest.approx(32)

def test_members_are_compared_at_the_points_voltage():
    # A car whose own maximum is the TWC's minimum, priced at 240 V against the TWC's measured volts
    for volts in (240, 241, 243):
        m = real(); s = asleepUnderSixAmps(m, volts, carMax=6)
        (pt,) = m.chargingPoints(); assert pt.maxPower == pt.minPower == 6 * volts
        m.distributeEVSEPower(); assert s._evseTargetAmps == pytest.approx(6), volts

def refusalsLogged(caplog, carMax=None, amps=48, wiring=48):
    TWCMaster._refusedPointLogged.clear(); caplog.clear()
    m = real(); s = asleepUnderSixAmps(m, 243, carMax); m.config["config"]["wiringMaxAmpsAllTWCs"] = wiring; budget(m, amps)
    with caplog.at_level(logging.INFO):
        for _ in range(3): m.distributeEVSEPower()
    assert s._evseTargetAmps == 0
    return [r.getMessage() for r in caplog.records if "its limits leave it none" in r.getMessage()]

def test_a_named_plugged_car_refused_by_its_point_is_logged_once(caplog):
    lines = refusalsLogged(caplog, carMax=5)                                       # the car's cap is under the TWC's minimum
    assert len(lines) == 1 and lines[0].startswith("Point-%s: %s is plugged in" % (VIN, VIN))
    assert refusalsLogged(caplog, wiring=5) == []                                  # the wiring's budget refuses, not the members
    assert refusalsLogged(caplog, carMax=5, amps=5) == []                          # too little power is not a refusal
