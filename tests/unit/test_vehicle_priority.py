"""
Unit tests for TWCManager Vehicle Priority module.

Tests vehicle module priority selection and fallback logic.
"""

import pytest
from unittest.mock import Mock, MagicMock, patch


class TestVehiclePriorityInitialization:
    """Test VehiclePriority initialization."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
        return master

    def test_vehicle_priority_initialization(self, mock_master):
        """Test VehiclePriority initializes correctly."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        priority = VehiclePriority(mock_master)

        assert priority.master == mock_master

    def test_update_settings_returns_true(self, mock_master):
        """Test updateSettings returns True."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        priority = VehiclePriority(mock_master)
        result = priority.updateSettings()

        assert result is True


class TestRetryCalculation:
    """Test retry calculation logic."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
        return master

    @pytest.fixture
    def priority(self, mock_master):
        """Create a VehiclePriority instance."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        return VehiclePriority(mock_master)

    def test_calculate_retries_priority_20(self, priority):
        """Test retry calculation for priority 20."""
        retries = priority._calculate_retries(20)

        assert retries == 2

    def test_calculate_retries_priority_10(self, priority):
        """Test retry calculation for priority 10."""
        retries = priority._calculate_retries(10)

        assert retries == 1

    def test_calculate_retries_priority_5(self, priority):
        """Test retry calculation for priority 5."""
        retries = priority._calculate_retries(5)

        assert retries == 0

    def test_calculate_retries_priority_0(self, priority):
        """Test retry calculation for priority 0."""
        retries = priority._calculate_retries(0)

        assert retries == 0

    def test_calculate_retries_priority_30(self, priority):
        """Test retry calculation for priority 30."""
        retries = priority._calculate_retries(30)

        assert retries == 3

    def test_calculate_retries_priority_100(self, priority):
        """Test retry calculation for priority 100."""
        retries = priority._calculate_retries(100)

        assert retries == 10


class TestMethodDelegation:
    """Test method delegation to vehicle modules."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
        return master

    @pytest.fixture
    def priority(self, mock_master):
        """Create a VehiclePriority instance."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        return VehiclePriority(mock_master)

    def test_method_delegation_success(self, priority):
        """Test successful method delegation."""
        module = Mock()
        module.test_method = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        result = priority.test_method()

        assert result is True
        module.test_method.assert_called_once()

    def test_method_delegation_failure_fallback(self, priority):
        """Test method delegation with fallback on failure."""
        module1 = Mock()
        module1.test_method = Mock(return_value=False)

        module2 = Mock()
        module2.test_method = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("Module1", module1, 20),
                ("Module2", module2, 10),
                (None, None, 0),
            ]
        )

        result = priority.test_method()

        assert result is True
        # Module1 at priority 20 gets 3 attempts (2 retries) before fallback
        assert module1.test_method.call_count == 3
        module2.test_method.assert_called_once()

    def test_method_delegation_no_module(self, priority):
        """Test method delegation when no module available."""
        priority.master.getModuleByPriority = Mock(return_value=(None, None, 0))

        result = priority.test_method()

        assert result is False

    def test_method_delegation_module_missing_method(self, priority):
        """Test method delegation when module doesn't have method."""
        module = Mock(spec=[])  # Empty spec means no methods

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        result = priority.test_method()

        assert result is False


class TestRetryLogic:
    """Test retry logic for failed attempts."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
        return master

    @pytest.fixture
    def priority(self, mock_master):
        """Create a VehiclePriority instance."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        return VehiclePriority(mock_master)

    def test_retry_on_failure(self, priority):
        """Test that failed attempts are retried."""
        module = Mock()
        module.test_method = Mock(side_effect=[False, False, True])

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        result = priority.test_method()

        assert result is True
        assert module.test_method.call_count == 3

    def test_exception_handling_with_retry(self, priority):
        """Test that exceptions trigger retries."""
        module = Mock()
        module.test_method = Mock(
            side_effect=[Exception("Test error"), Exception("Test error"), True]
        )

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        result = priority.test_method()

        assert result is True
        assert module.test_method.call_count == 3


class TestStatisticsTracking:
    """Test statistics tracking for module success/failure."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
        return master

    @pytest.fixture
    def priority(self, mock_master):
        """Create a VehiclePriority instance."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        return VehiclePriority(mock_master)

    def test_success_statistics_tracked(self, priority):
        """Test that successful calls are tracked."""
        module = Mock()
        module.test_method = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        priority.test_method()

        assert priority.master.stats["moduleSuccess"]["TestModule"] == 1

    def test_failure_statistics_tracked(self, priority):
        """Test that failed calls are tracked."""
        module = Mock()
        module.test_method = Mock(return_value=False)

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        priority.test_method()

        assert priority.master.stats["moduleFailures"]["TestModule"] == 1

    def test_multiple_successes_tracked(self, priority):
        """Test that multiple successes are accumulated."""
        module = Mock()
        module.test_method = Mock(return_value=True)

        # Each call to priority.test_method() consumes one getModuleByPriority
        # entry (success returns immediately; the None sentinel is never reached)
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TestModule", module, 20),
                ("TestModule", module, 20),
            ]
        )

        priority.test_method()
        priority.test_method()

        assert priority.master.stats["moduleSuccess"]["TestModule"] == 2


class TestPriorityOrdering:
    """Test that modules are tried in priority order."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
        return master

    @pytest.fixture
    def priority(self, mock_master):
        """Create a VehiclePriority instance."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        return VehiclePriority(mock_master)

    def test_higher_priority_tried_first(self, priority):
        """Test that higher priority modules are tried first."""
        call_order = []

        module_high = Mock()
        module_high.test_method = Mock(
            side_effect=lambda *a, **kw: call_order.append("high") or False
        )

        module_low = Mock()
        module_low.test_method = Mock(
            side_effect=lambda *a, **kw: call_order.append("low") or True
        )

        # Use priority 5 (0 retries) so each module is attempted exactly once
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("HighPriority", module_high, 5),
                ("LowPriority", module_low, 5),
                (None, None, 0),
            ]
        )

        result = priority.test_method()

        assert result is True
        assert call_order == ["high", "low"]


class TestMethodArguments:
    """Test that method arguments are passed correctly."""

    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {"moduleSuccess": {}, "moduleFailures": {}, "moduleDispatch": {}}
        return master

    @pytest.fixture
    def priority(self, mock_master):
        """Create a VehiclePriority instance."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        return VehiclePriority(mock_master)

    def test_positional_arguments_passed(self, priority):
        """Test that positional arguments are passed to module."""
        module = Mock()
        module.test_method = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        priority.test_method("arg1", "arg2")

        module.test_method.assert_called_with("arg1", "arg2")

    def test_keyword_arguments_passed(self, priority):
        """Test that keyword arguments are passed to module."""
        module = Mock()
        module.test_method = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        priority.test_method(key1="value1", key2="value2")

        module.test_method.assert_called_with(key1="value1", key2="value2")

    def test_mixed_arguments_passed(self, priority):
        """Test that mixed arguments are passed to module."""
        module = Mock()
        module.test_method = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[("TestModule", module, 20), (None, None, 0)]
        )

        priority.test_method("arg1", key1="value1")

        module.test_method.assert_called_with("arg1", key1="value1")


class TestBLENotLoaded:
    """Backwards compatibility: VehiclePriority with only TeslaAPI (no BLE)."""

    @pytest.fixture
    def mock_master(self):
        master = Mock()
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {},
        }
        return master

    @pytest.fixture
    def priority(self, mock_master):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        return VehiclePriority(mock_master)

    def test_car_api_charge_only_api_loaded(self, priority):
        """car_api_charge succeeds when only TeslaAPI is loaded (no BLE)."""
        tesla_api = Mock()
        tesla_api.car_api_charge = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TeslaAPI", tesla_api, 10),
                (None, None, 0),
            ]
        )

        task = {"cmd": "charge", "charge": True}
        result = priority.car_api_charge(task)

        assert result is True
        tesla_api.car_api_charge.assert_called_once_with(task)

    def test_car_api_charge_task_dict_passed_through(self, priority):
        """Task dict is forwarded intact to TeslaAPI when BLE is absent."""
        tesla_api = Mock()
        tesla_api.car_api_charge = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TeslaAPI", tesla_api, 10),
                (None, None, 0),
            ]
        )

        task = {"cmd": "charge", "charge": False, "vin": "5YJ3E1EA1KF000001"}
        priority.car_api_charge(task)

        tesla_api.car_api_charge.assert_called_once_with(task)

    def test_car_api_charge_no_modules_loaded(self, priority):
        """car_api_charge returns False when no vehicle modules are loaded."""
        priority.master.getModuleByPriority = Mock(return_value=(None, None, 0))

        task = {"cmd": "charge", "charge": True}
        result = priority.car_api_charge(task)

        assert result is False

    def test_ble_tried_before_api_when_both_loaded(self, priority):
        """When BLE and TeslaAPI are both loaded, BLE is tried first."""
        ble_module = Mock()
        ble_module.car_api_charge = Mock(return_value=False)

        tesla_api = Mock()
        tesla_api.car_api_charge = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TeslaBLE", ble_module, 20),
                ("TeslaAPI", tesla_api, 10),
                (None, None, 0),
            ]
        )

        task = {"cmd": "charge", "charge": True}
        result = priority.car_api_charge(task)

        # BLE at priority 20 gets 3 attempts before fallback (priority // 10 = 2 retries)
        assert result is True
        assert ble_module.car_api_charge.call_count == 3
        tesla_api.car_api_charge.assert_called_once_with(task)

    def test_ble_success_does_not_call_api(self, priority):
        """When BLE succeeds, TeslaAPI is not called."""
        ble_module = Mock()
        ble_module.car_api_charge = Mock(return_value=True)

        tesla_api = Mock()
        tesla_api.car_api_charge = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TeslaBLE", ble_module, 20),
                (None, None, 0),
            ]
        )

        task = {"cmd": "charge", "charge": True}
        priority.car_api_charge(task)

        ble_module.car_api_charge.assert_called_once()
        tesla_api.car_api_charge.assert_not_called()

    def test_stop_charge_only_api_loaded(self, priority):
        """Stop-charge command works when only TeslaAPI is loaded."""
        tesla_api = Mock()
        tesla_api.car_api_charge = Mock(return_value=True)

        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TeslaAPI", tesla_api, 10),
                (None, None, 0),
            ]
        )

        task = {"cmd": "charge", "charge": False}
        result = priority.car_api_charge(task)

        assert result is True
        tesla_api.car_api_charge.assert_called_once_with(task)


class TestCommandPolicy:
    """Test commandPolicy restrictions on state-changing methods."""

    def make_master(self, policy=None, configPolicy=None):
        master = Mock()
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {},
        }
        master.config = (
            {"vehicle": {"commandPolicy": configPolicy}} if configPolicy else {}
        )
        master.settings = {"commandPolicy": policy} if policy else {}
        return master

    def make_modules(self, master, ble_result=False, api_result=True):
        ble = Mock()
        ble.startCharging = Mock(return_value=ble_result)
        ble.updateChargeAtHome = Mock(return_value=ble_result)
        api = Mock()
        api.startCharging = Mock(return_value=api_result)
        api.updateChargeAtHome = Mock(return_value=api_result)
        master.getModuleByPriority = Mock(
            side_effect=[
                ("TeslaBLE", ble, 20),
                ("TeslaAPI", api, 10),
                ("", None, 0),
            ]
        )
        return ble, api

    def test_default_policy_is_prefer_ble(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        priority = VehiclePriority(self.make_master())
        assert priority.commandPolicy == "prefer_ble"

    def test_invalid_policy_falls_back_to_prefer_ble(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        priority = VehiclePriority(self.make_master("banana"))
        assert priority.commandPolicy == "prefer_ble"

    def test_policy_from_settings(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        priority = VehiclePriority(self.make_master("ble_only"))
        assert priority.commandPolicy == "ble_only"
        assert priority.commandPolicyOverridden is False

    def test_settings_change_applies_at_runtime(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        master = self.make_master("prefer_ble")
        priority = VehiclePriority(master)
        assert priority.commandPolicy == "prefer_ble"

        master.settings["commandPolicy"] = "ble_only"
        assert priority.commandPolicy == "ble_only"

    def test_config_overrides_settings(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        priority = VehiclePriority(
            self.make_master(policy="ble_only", configPolicy="api_only")
        )
        assert priority.commandPolicy == "api_only"
        assert priority.commandPolicyOverridden is True

    def test_invalid_config_override_is_ignored(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        priority = VehiclePriority(
            self.make_master(policy="ble_only", configPolicy="banana")
        )
        assert priority.commandPolicy == "ble_only"
        assert priority.commandPolicyOverridden is False

    def test_prefer_ble_falls_back_to_api_on_command(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        master = self.make_master("prefer_ble")
        ble, api = self.make_modules(master, ble_result=False, api_result=True)
        priority = VehiclePriority(master)

        assert priority.startCharging("VIN123") is True
        assert ble.startCharging.called
        assert api.startCharging.called

    def test_ble_only_blocks_api_for_commands(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        master = self.make_master("ble_only")
        ble, api = self.make_modules(master, ble_result=False, api_result=True)
        priority = VehiclePriority(master)

        assert priority.startCharging("VIN123") is False
        assert ble.startCharging.called
        assert not api.startCharging.called

    def test_ble_only_allows_api_for_reads(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        master = self.make_master("ble_only")
        ble, api = self.make_modules(master, ble_result=False, api_result=True)
        priority = VehiclePriority(master)

        assert priority.updateChargeAtHome() is True
        assert api.updateChargeAtHome.called

    def test_api_only_blocks_ble_for_commands(self):
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority

        master = self.make_master("api_only")
        ble, api = self.make_modules(master, ble_result=False, api_result=True)
        priority = VehiclePriority(master)

        assert priority.startCharging("VIN123") is True
        assert not ble.startCharging.called
        assert api.startCharging.called
