from TWCManager.Vehicle.Providers import Capability as C
from TWCManager.Vehicle.Providers.Provider import Provider, vehicleMaxAmps
from TWCManager.Vehicle.Registry.VehicleRegistry import VehicleRegistry

VIN = "5YJ3E1EA1KF000001"


class Stub(Provider):
    name = "Stub"
    local = True

    def declarations(self):
        return {
            C.SET_AMPS: C.LOCAL,
            C.WAKE: C.FLEET_WAKE,
        }

    def ampsRange(self, vehicle):
        return (6, 32)


def test_vocabulary_and_prices():
    assert (
        C.READS | C.COMMANDS == C.ALL
        and C.SET_AMPS in C.COMMANDS
        and C.READ_CHARGE in C.READS
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
    assert p.declarations()[C.WAKE] == C.FLEET_WAKE
    assert p.ready(None, C.SET_AMPS) is False
    assert not hasattr(Provider, "attempts")                      # retries are the reconciler's
    assert p.limitations(None) == frozenset()
    assert Provider().available(C.WAKE) is True
    assert p.covers(None, 6) and p.covers(None, 32) and not p.covers(None, 4)
    assert not Provider().covers(None, 10)


def test_vehicle_max_amps_falls_back_to_wiring():
    v = VehicleRegistry({}).getOrCreate(VIN, "t")
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 40}) == 40
    assert vehicleMaxAmps(v, {}) == 32
    v.observe("availableCurrent", None, "TeslaMate")
    v.observe("chargeCurrentRequestMax", None, "TeslaMate")
    assert vehicleMaxAmps(v, {}) == 32


def test_a_request_maximum_at_the_pilot_is_the_chargers_limit_not_the_cars():
    # Read as a Gen2 offers 6 A going idle, the car's request maximum is that
    # offer, and says nothing about the car
    v = VehicleRegistry({}).getOrCreate(VIN, "t")
    v.observe("availableCurrent", 6, "FleetAPI")
    v.observe("chargeCurrentRequestMax", 6, "FleetAPI")
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 48}) == 48


def test_a_request_maximum_below_the_pilot_is_the_cars_own():
    v = VehicleRegistry({}).getOrCreate(VIN, "t")
    v.observe("availableCurrent", 48, "TeslaBLE")
    v.observe("chargeCurrentRequestMax", 32, "TeslaBLE")
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 48}) == 32
    # The car's own limit holds under a lower pilot another source reads later
    v.observe("availableCurrent", 6, "FleetAPI")
    v.observe("chargeCurrentRequestMax", 6, "FleetAPI")
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 48}) == 32


def test_a_pilot_and_maximum_pair_only_within_one_sources_reading():
    v = VehicleRegistry({}).getOrCreate(VIN, "t")
    v.observe("availableCurrent", 48, "TeslaBLE")
    v.observe("chargeCurrentRequestMax", 16, "OwnerAPI")
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 48}) == 48
    # A pilot newer than the maximum: the maximum may be from the old pilot
    v.observe("chargeCurrentRequestMax", 6, "TeslaMate", at=100)
    v.observe("availableCurrent", 48, "TeslaMate", at=101)
    assert vehicleMaxAmps(v, {"wiringMaxAmpsPerTWC": 48}) == 48
