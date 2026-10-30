import pytest

from TWCManager.Vehicle.Providers.TeslaAPIProviders import FleetCachedProvider


@pytest.fixture(autouse=True)
def cached_snapshot_wait_costs_no_real_time(monkeypatch):
    # The cached read's wait for a post-event snapshot would sleep 15 s of
    # real time in every test that marks a plug or charge-end event
    monkeypatch.setattr(FleetCachedProvider, "sleep", staticmethod(lambda seconds: None))
