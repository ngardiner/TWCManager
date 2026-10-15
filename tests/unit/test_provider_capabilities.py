from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Provider import Provider, vehicleMaxAmps
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"


class Stub(Provider):
    name = "Stub"
    local = True

    def declarations(self):
        return {
            C.SET_AMPS: C.Declaration(C.LOCAL),
            C.WAKE: C.Declaration(C.FLEET_WAKE),
        }

    def ampsRange(self, vehicle):
        return (6, 32)


def test_vocabulary_and_prices():
    assert (
        C.READS | C.COMMANDS == C.ALL
        and C.SET_AMPS in C.COMMANDS
        and C.READ_ONLINE in C.READS
    )
    assert (C.FLEET_COMMAND.price, C.FLEET_DATA.price, C.FLEET_WAKE.price) == (
        0.001,
        0.002,
        0.02,
    )
    assert C.LOCAL.tier < C.CLOUD_FREE.tier < C.FLEET_COMMAND.tier < C.DISRUPTIVE.tier
    assert C.HA_BILLED.price is None and C.HA_BILLED.tier == C.TIER_BILLED


def test_provider_defaults_and_declarations():
    p = Stub()
    assert p.declares(C.SET_AMPS) and not p.declares(C.READ_CHARGE)
    assert p.cost(C.WAKE) == C.FLEET_WAKE and p.cost(C.READ_CHARGE) is None
    assert p.declarations()[C.WAKE] == C.Declaration(C.FLEET_WAKE) and not p.declarations()[C.WAKE].passive
    assert p.ready(None, C.SET_AMPS) is False
    assert p.attempts() == 3 and Provider().attempts() == 2
    assert p.limitations(None) == frozenset()
    assert Provider().available(C.WAKE) is True
    assert p.covers(None, 6) and p.covers(None, 32) and not p.covers(None, 4)
    assert not Provider().covers(None, 10)


def test_summary_lists_each_capability():
    s = Stub().summary(None)
    assert s["name"] == "Stub" and s["capabilities"][C.WAKE] == {
        "tier": 2,
        "price": 0.02,
        "ready": False,
    }


def test_vehicle_max_amps_falls_back_to_wiring():
    v = VehicleRegistry({}).getOrCreate(VIN, "t")
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 40}) == 40
    # The charger's pilot is not the car's limit: on our TWC it is our offer
    v.observe("availableCurrent", 6, "TeslaBLE")
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 40}) == 40
    v.observe("chargeCurrentRequestMax", 16, "TeslaAPI")
    assert vehicleMaxAmps(v, {}) == 16
