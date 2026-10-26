"""vehicle.teslaAPI.enabled=false: getModuleByName("TeslaAPI") answers None,
and Storm Watch must treat that as no Tesla API rather than raise. Startup and
settings saves are pinned in test_teslaapi_token_migration.py, the policy in
test_policy.py."""

from unittest.mock import Mock


def test_storm_watch_without_the_tesla_api_sends_nothing(requests_mock):
    from tests.unit.teslatokens import new_api
    from tests.unit.test_teslapowerwall2_ems import TestStormWatchTokenSlots

    api = new_api()
    pw = TestStormWatchTokenSlots()._powerwall(api)
    api.master.getModuleByName = Mock(return_value=None)
    pw.getStormWatch()
    assert requests_mock.call_count == 0
