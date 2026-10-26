"""
Reading vehicles with an Owner API (read-only) token: the vehicle list comes
from /products, which also carries energy sites without a VIN.
"""

from tests.unit.teslatokens import fleet_token, new_api, owner_token

OWNER_PRODUCTS = "https://owner-api.teslamotors.com/api/1/products"
OWNER_VEHICLES = "https://owner-api.teslamotors.com/api/1/vehicles"
NA_VEHICLES = "https://fleet-api.prd.na.vn.cloud.tesla.com/api/1/vehicles"

PRODUCTS = {
    "response": [
        {
            "id": 1,
            "vehicle_id": 2,
            "vin": "5YJ3E1EB9LF000001",
            "display_name": "Car",
            "state": "asleep",
        },
        {"energy_site_id": 3, "resource_type": "battery", "site_name": "Home"},
    ],
    "count": 2,
}


def test_owner_token_lists_vehicles_from_products(requests_mock):
    requests_mock.get(OWNER_PRODUCTS, json=PRODUCTS)
    vehicles = requests_mock.get(OWNER_VEHICLES, status_code=412, json={})
    api = new_api()
    api.storeToken(owner_token())
    assert api.loadVehicleList() is True
    assert [v.VIN for v in api.carApiVehicles] == ["5YJ3E1EB9LF000001"]
    assert vehicles.call_count == 0


def test_fleet_token_keeps_vehicles(requests_mock):
    api = new_api()
    api.storeToken(fleet_token())
    listed = requests_mock.get(NA_VEHICLES, json=PRODUCTS)
    assert api.loadVehicleList() is True and listed.call_count == 1
