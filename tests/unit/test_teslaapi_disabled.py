"""vehicle.teslaAPI.enabled=false: getModuleByName("TeslaAPI") answers None,
and code that runs on its own (the limit reconciler, Storm Watch) must treat
that as no Tesla API rather than raise. Startup and settings saves are pinned
in test_teslaapi_token_migration.py, the policy override in test_policy.py."""

from unittest.mock import Mock

from TWCManager.Vehicle.Reconcile.Targets import located


def test_a_car_with_a_tesla_api_record_is_not_located_without_the_module():
    master = Mock()
    master.getModuleByName = Mock(return_value=None)
    vehicle = Mock()
    vehicle.value = Mock(return_value=None)  # no TWC names it
    vehicle.attachment = Mock(return_value=object())
    assert located(master, vehicle) is None


def test_storm_watch_without_the_tesla_api_sends_nothing(requests_mock):
    from tests.unit.teslatokens import new_api
    from tests.unit.test_teslapowerwall2_ems import TestStormWatchTokenSlots

    api = new_api()
    pw = TestStormWatchTokenSlots()._powerwall(api)
    api.master.getModuleByName = Mock(return_value=None)
    pw.getStormWatch()
    assert requests_mock.call_count == 0
