"""TeslaMate's plugged_in topic is read onto the vehicle as pluggedIn."""

from TWCManager.Vehicle.TeslaMateVehicle import TeslaMateVehicle, parse_bool


def test_parse_bool():
    assert parse_bool("true") is True
    assert parse_bool(" False ") is False
    assert parse_bool("") is None
    assert parse_bool("nil") is None


def test_plugged_in_maps_to_plugged_in():
    attribute, converter = TeslaMateVehicle.events["plugged_in"]
    assert attribute == "pluggedIn"
    assert converter("true") is True
    assert converter("false") is False
    # An empty or odd payload is unknown, never "unplugged".
    assert converter("") is None
