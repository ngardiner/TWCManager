"""
Unit tests for TWCManager TWCMaster module.

Tests master control logic, amperage distribution, and slave management.
"""

import pytest
import time
from unittest.mock import Mock, MagicMock, patch
from datetime import datetime, timedelta


class TestTWCMasterInitialization:
    """Test TWCMaster initialization."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    def test_master_initialization(self, mock_config):
        """Test TWCMaster initializes correctly."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.TWCID == "AB"
        assert master.config == mock_config
        assert master.version == "1.4.0"
    
    def test_master_default_settings(self, mock_config):
        """Test master has default settings."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeStopMode"] == "1"
        assert master.settings["kWhDelivered"] == 119
    
    def test_master_protocol_version(self, mock_config):
        """Test master protocol version defaults to 2."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.protocolVersion == 2
    
    def test_master_sign_values(self, mock_config):
        """Test master and slave sign values."""
        from TWCManager.TWCMaster import TWCMaster
        
        master = TWCMaster("AB", mock_config)
        
        assert master.masterSign == bytearray(b"\x77")
        assert master.slaveSign == bytearray(b"\x77")


class TestTWCMasterAmperage:
    """Test amperage distribution logic."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_set_max_amps_to_divide(self, master):
        """Test setting max amps to divide among slaves."""
        master.setMaxAmpsToDivideAmongSlaves(32)
        
        assert master.maxAmpsToDivideAmongSlaves == 32
    
    def test_set_limit_amps_to_divide(self, master):
        """Test setting limit amps to divide among slaves."""
        master.setLimitAmpsToDivideAmongSlaves(16)
        
        assert master.limitAmpsToDivideAmongSlaves == 16
    
    def test_add_kwh_delivered(self, master):
        """Test adding kWh delivered."""
        initial = master.settings["kWhDelivered"]
        master.addkWhDelivered(5.5)
        
        assert master.settings["kWhDelivered"] == initial + 5.5
    
    def test_add_kwh_delivered_multiple_times(self, master):
        """Test adding kWh multiple times accumulates."""
        initial = master.settings["kWhDelivered"]
        master.addkWhDelivered(2.0)
        master.addkWhDelivered(3.0)
        
        assert master.settings["kWhDelivered"] == initial + 5.0


class TestTWCMasterSlaveManagement:
    """Test slave TWC management."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_add_slave_twc(self, master):
        """Test adding a slave TWC."""
        slave = Mock()
        slave.TWCID = "CD"
        
        master.addSlaveTWC(slave)
        
        assert slave in master.slaveTWCRoundRobin
    
    def test_get_slave_twcs(self, master):
        """Test getting slave TWCs."""
        slave1 = Mock()
        slave1.TWCID = "CD"
        slave2 = Mock()
        slave2.TWCID = "EF"
        
        master.addSlaveTWC(slave1)
        master.addSlaveTWC(slave2)
        
        slaves = master.getSlaveTWCs()
        
        assert len(slaves) == 2
        assert slave1 in slaves
        assert slave2 in slaves
    
    def test_get_slave_sign(self, master):
        """Test getting slave sign."""
        sign = master.getSlaveSign()
        
        assert sign == bytearray(b"\x77")


class TestTWCMasterSettings:
    """Test settings management."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_charge_now_amps(self, master):
        """Test chargeNowAmps setting."""
        master.setChargeNowAmps(16)
        
        assert master.settings["chargeNowAmps"] == 16
    
    def test_reset_charge_now_amps(self, master):
        """Test resetting chargeNowAmps."""
        master.setChargeNowAmps(16)
        master.resetChargeNowAmps()
        
        assert master.settings["chargeNowAmps"] == 0


class TestTWCMasterConfiguration:
    """Test configuration handling."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_subtract_charger_load_config(self, mock_config):
        """Test subtractChargerLoad configuration."""
        mock_config["config"]["subtractChargerLoad"] = True
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        
        assert master.subtractChargerLoad is True
    
    def test_treat_generation_as_grid_delivery_config(self, mock_config):
        """Test treatGenerationAsGridDelivery configuration."""
        mock_config["config"]["treatGenerationAsGridDelivery"] = True
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        
        assert master.treatGenerationAsGridDelivery is True
    
    def test_debug_output_to_file_config(self, mock_config):
        """Test debugOutputToFile configuration."""
        mock_config["config"]["debugOutputToFile"] = True
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        
        assert master.debugOutputToFile is True


class TestTWCMasterModuleManagement:
    """Test module registration and management."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }

    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        # Do NOT mock getModuleByName here — these tests verify the real implementation
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_register_module(self, master):
        """Test registering a module."""
        module_info = {
            "name": "TestModule",
            "ref": Mock(),
            "type": "Test"
        }
        
        master.registerModule(module_info)
        
        assert "TestModule" in master.modules
    
    def test_get_module_by_name(self, master):
        """Test getting module by name."""
        module_ref = Mock()
        module_info = {
            "name": "TestModule",
            "ref": module_ref,
            "type": "Test"
        }
        master.registerModule(module_info)
        
        result = master.getModuleByName("TestModule")
        
        assert result == module_ref
    
    def test_get_module_by_name_not_found(self, master):
        """Test getting non-existent module returns None."""
        result = master.getModuleByName("NonExistent")
        
        assert result is None


class TestTWCMasterHistory:
    """Test history snapshot management."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "maxAmpsAllowedFromGrid": None,
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_advance_history_snap(self, master):
        """Test advancing history snapshot."""
        # Simulate "snap time has arrived" by placing it in the past before re-advancing
        master.nextHistorySnap = datetime.now().astimezone() - timedelta(minutes=1)
        initial_snap = master.nextHistorySnap
        master.advanceHistorySnap()

        # Should have advanced to a future 5-minute boundary
        assert master.nextHistorySnap > initial_snap
    
    def test_history_snap_is_future(self, master):
        """Test history snap is in the future."""
        master.advanceHistorySnap()
        
        assert master.nextHistorySnap > datetime.now().astimezone()


class TestSettingsInitialization:
    """Test suite for settings initialization and persistence."""

    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "minAmpsPerTWC": 6,
                "settingsPath": "/tmp/test_settings",
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance for testing."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_settings_has_default_values(self, master):
        """Test that settings contain all default values."""
        required_defaults = [
            "chargeNowAmps",
            "chargeStopMode",
            "chargeNowTimeEnd",
            "homeLat",
            "homeLon",
            "hourResumeTrackGreenEnergy",
            "kWhDelivered",
            "nonScheduledAmpsMax",
            "respondToSlaves",
            "scheduledAmpsDaysBitmap",
            "scheduledAmpsEndHour",
            "scheduledAmpsMax",
            "scheduledAmpsStartHour",
            "sendServerTime",
        ]
        
        for key in required_defaults:
            assert key in master.settings, f"Settings should contain {key}"
    
    def test_settings_default_values_correct(self, master):
        """Test that default settings have correct values."""
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeStopMode"] == "1"
        assert master.settings["chargeNowTimeEnd"] == 0
        assert master.settings["homeLat"] == 10000
        assert master.settings["homeLon"] == 10000
        assert master.settings["hourResumeTrackGreenEnergy"] == -1
        assert master.settings["kWhDelivered"] == 119
        assert master.settings["nonScheduledAmpsMax"] == 0
        assert master.settings["respondToSlaves"] == 1
        assert master.settings["scheduledAmpsDaysBitmap"] == 0x7F
        assert master.settings["scheduledAmpsEndHour"] == -1
        assert master.settings["scheduledAmpsMax"] == 0
        assert master.settings["scheduledAmpsStartHour"] == -1
        assert master.settings["sendServerTime"] == 0
    
    def test_load_settings_with_missing_file(self, master, tmp_path):
        """Test loadSettings when settings file doesn't exist."""
        # Update config to use temp directory
        master.config["config"]["settingsPath"] = str(tmp_path)
        
        # Call loadSettings
        master.loadSettings()
        
        # Should have default settings
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeNowTimeEnd"] == 0
        assert "chargeStopMode" in master.settings
    
    def test_load_settings_merges_with_defaults(self, master, tmp_path):
        """Test that loaded settings are merged with defaults."""
        import json
        import os
        
        # Create a settings file with partial data
        settings_dir = tmp_path / "settings"
        settings_dir.mkdir()
        settings_file = settings_dir / "settings.json"
        
        partial_settings = {
            "chargeNowAmps": 32,
            "customField": "custom_value"
        }
        
        with open(settings_file, "w") as f:
            json.dump(partial_settings, f)
        
        # Update config
        master.config["config"]["settingsPath"] = str(settings_dir)
        
        # Load settings
        master.loadSettings()
        
        # Should have loaded value
        assert master.settings["chargeNowAmps"] == 32
        # Should have custom field
        assert master.settings.get("customField") == "custom_value"
        # Should have defaults for missing keys
        assert master.settings["chargeNowTimeEnd"] == 0
        assert master.settings["chargeStopMode"] == "1"
    
    def test_get_status_with_charge_now_active(self, master):
        """Test getStatus when chargeNow is active."""
        master.settings["chargeNowAmps"] = 32
        master.settings["chargeNowTimeEnd"] = int(time.time()) + 3600
        
        # Mock required methods
        master.getScheduledAmpsBatterySize = Mock(return_value=100)
        master.getInterfaceModule = Mock(return_value=Mock(timeLastTx=time.time()))
        
        status = master.getStatus()
        
        assert status["chargeNowAmps"] == 32
        assert "chargeNowTimeEnd" in status
    
    def test_get_status_without_charge_now(self, master):
        """Test getStatus when chargeNow is not active."""
        master.settings["chargeNowAmps"] = 0
        master.settings["chargeNowTimeEnd"] = 0
        
        # Mock required methods
        master.getScheduledAmpsBatterySize = Mock(return_value=100)
        master.getInterfaceModule = Mock(return_value=Mock(timeLastTx=time.time()))
        
        status = master.getStatus()
        
        assert status.get("chargeNowAmps", 0) == 0
        assert "chargeNowTimeEnd" not in status or status.get("chargeNowTimeEnd", 0) == 0
    
    def test_charge_now_sets_correct_values(self, master):
        """Test that chargeNow sets correct settings values."""
        master.setChargeNowAmps(32)
        master.setChargeNowTimeEnd(3600)
        
        assert master.settings["chargeNowAmps"] == 32
        assert master.settings["chargeNowTimeEnd"] > time.time()
    
    def test_cancel_charge_now_clears_values(self, master):
        """Test that cancelChargeNow clears settings."""
        # First set charge now
        master.setChargeNowAmps(32)
        master.setChargeNowTimeEnd(3600)
        assert master.settings["chargeNowAmps"] == 32
        
        # Then cancel
        master.resetChargeNowAmps()
        
        assert master.settings["chargeNowAmps"] == 0
        assert master.settings["chargeNowTimeEnd"] == 0


class TestSettingsEdgeCases:
    """Test edge cases in settings handling."""
    
    @pytest.fixture
    def mock_config(self):
        """Create a mock configuration."""
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": False,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "settingsPath": "/tmp/test_settings",
            }
        }
    
    @pytest.fixture
    def master(self, mock_config):
        """Create a TWCMaster instance for testing."""
        from TWCManager.TWCMaster import TWCMaster
        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    def test_charge_now_with_very_high_amps(self, master):
        """Test chargeNow with very high amperage."""
        master.setChargeNowAmps(200)
        master.setChargeNowTimeEnd(3600)
        # Should be rejected as it's above wiringMaxAmpsAllTWCs (default 100 in mock)
        assert master.settings["chargeNowAmps"] == 0
    
    def test_charge_now_with_very_long_duration(self, master):
        """Test chargeNow with very long duration."""
        master.setChargeNowAmps(32)
        master.setChargeNowTimeEnd(86400 * 7)
        assert master.settings["chargeNowTimeEnd"] > time.time() + 86400 * 6
    
    def test_charge_now_with_minimum_amps(self, master):
        """Test chargeNow with minimum amperage."""
        master.setChargeNowAmps(1)
        master.setChargeNowTimeEnd(3600)
        assert master.settings["chargeNowAmps"] == 1
    
    def test_charge_now_with_minimum_duration(self, master):
        """Test chargeNow with minimum duration."""
        master.setChargeNowAmps(32)
        master.setChargeNowTimeEnd(60)
        assert master.settings["chargeNowTimeEnd"] > time.time()
    
    def test_multiple_charge_now_calls_override(self, master):
        """Test that multiple chargeNow calls override previous values."""
        master.setChargeNowAmps(32)
        master.setChargeNowTimeEnd(3600)
        first_end_time = master.settings["chargeNowTimeEnd"]
        
        time.sleep(0.1)
        
        master.setChargeNowAmps(24)
        master.setChargeNowTimeEnd(7200)
        second_end_time = master.settings["chargeNowTimeEnd"]
        
        assert master.settings["chargeNowAmps"] == 24
        assert second_end_time > first_end_time


class TestSubtractChargerLoadGetMaxAmps:
    """Regression tests for subtractChargerLoad in getMaxAmpsForTargetGridUsage.

    Without the fix, when the spike-to-cancel-6A-limit temporarily drives
    charger draw far above generation, the incremental step produced a deeply
    negative newOffer which clamped the final result to 0 A even though net
    available solar was still positive.
    """

    @pytest.fixture
    def mock_config(self):
        return {
            "config": {
                "debugOutputToFile": False,
                "subtractChargerLoad": True,
                "treatGenerationAsGridDelivery": False,
                "wiringMaxAmpsAllTWCs": 80,
                "defaultVoltage": 240,
                "numberOfPhases": 1,
                "minAmpsPerTWC": 6,
                "realPowerFactorMinAmps": 1,
                "realPowerFactorMaxAmps": 1,
            }
        }

    @pytest.fixture
    def master(self, mock_config):
        from TWCManager.TWCMaster import TWCMaster

        master = TWCMaster("AB", mock_config)
        master.registerModule = Mock()
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master

    def _add_fake_slave(self, master, reported_amps_actual):
        """Add a mock slave with the given actual amp draw."""
        slave = Mock()
        slave.reportedAmpsActual = reported_amps_actual
        slave.voltsPhaseA = 0
        slave.voltsPhaseB = 0
        slave.voltsPhaseC = 0
        slave.isCharging = 1
        master.slaveTWCRoundRobin.append(slave)
        return slave

    def test_returns_positive_when_charger_spiked(self, master):
        """After a spike to ~20 A, available power must stay positive, not drop to 0."""
        # Solar: 2838 W (~11.83 A @ 240 V)
        master.setGeneration("test", 2838)
        # Total consumption including 20 A charger draw: 5836 W
        master.setConsumption("test", 5836)
        # Charger is currently drawing 19.83 A (spike in progress)
        self._add_fake_slave(master, 19.83)
        # Previous offer was 7 A (before spike)
        master.maxAmpsToDivideAmongSlaves = 7.0
        master.limitAmpsToDivideAmongSlaves = 80.0

        result = master.getMaxAmpsForTargetGridUsage()

        # Net available = generation - other_load = ~11.83 - ~4.49 = ~7.3 A
        # Must be clearly positive, not clamped to 0
        assert result > 0, (
            f"Expected positive amps available but got {result}; "
            "subtractChargerLoad spike bug likely regressed"
        )

    def test_stable_at_min_charge_returns_correct_offer(self, master):
        """At steady 6 A charging, offer should reflect net solar surplus."""
        # Solar: 2757 W (~11.49 A), consumption 2691 W (charger 1554 W + other 1137 W)
        master.setGeneration("test", 2757)
        master.setConsumption("test", 2691)
        # Charger drawing 6.475 A
        self._add_fake_slave(master, 6.475)
        master.maxAmpsToDivideAmongSlaves = 6.28
        master.limitAmpsToDivideAmongSlaves = 80.0

        result = master.getMaxAmpsForTargetGridUsage()

        # Net: (2757 - 1137) / 240 = 6.75 A
        assert result > 0
        assert result < 12, f"Offer {result} A seems too high for available solar"



class TestVehicleCommandRouting:
    """Vehicle commands are desired state for the reconciler; the master has
    no legacy command queue."""

    @pytest.fixture
    def master(self):
        from TWCManager.TWCMaster import TWCMaster

        m = TWCMaster(
            "AB",
            {
                "config": {
                    "debugOutputToFile": False,
                    "subtractChargerLoad": False,
                    "treatGenerationAsGridDelivery": False,
                    "wiringMaxAmpsAllTWCs": 80,
                    "maxAmpsAllowedFromGrid": None,
                }
            },
        )
        # The background queue state is class-level; isolate it per test.
        m.backgroundTasksQueue = Mock()
        m.backgroundTasksCmds = {}
        m.backgroundTasksDelayed = []
        return m

    def test_other_tasks_stay_on_background_queue(self, master):
        task = {"cmd": "saveSettings"}
        master.queue_background_task(task)
        master.backgroundTasksQueue.put.assert_called_once_with(task)
        assert not hasattr(master, "vehicleCommandQueue")
        assert not hasattr(master, "queue_vehicle_command")


class TestChargeStartStopCancelRules:
    """startCarsCharging / stopCarsCharging / cancelStopCarsCharging write
    desired state for the cars they name; cancelStopCarsCharging drops only
    a stop not yet delivered (#667)."""

    V1, V2 = "5YJ3E1EA1KF000001", "5YJ3E1EA1KF000002"

    @pytest.fixture
    def master(self):
        from TWCManager.TWCMaster import TWCMaster

        m = TWCMaster(
            "AB",
            {
                "config": {
                    "debugOutputToFile": False,
                    "subtractChargerLoad": False,
                    "treatGenerationAsGridDelivery": False,
                    "wiringMaxAmpsAllTWCs": 80,
                    "maxAmpsAllowedFromGrid": None,
                }
            },
        )
        # Policy.clearOverride()/overrideLimit() are called; a Mock is enough.
        # No Gen3 units: the cars' point is checked for a plug
        policy = Mock()
        m.getModuleByName = Mock(side_effect=lambda n: None if n == "Gen3TWCs" else policy)
        m.settings["chargeStopMode"] = "1"
        for vin in (self.V1, self.V2):
            m.vehicleRegistry.getOrCreate(vin, "t").observe("twcVin", "8244", "TWC")
        return m

    def wanted(self, master):
        from TWCManager.Vehicle.Reconcile.Desired import CHARGING

        return {
            vin: d.value
            for vin in (self.V1, self.V2)
            for d in [master.desired.get(vin, CHARGING)]
            if d is not None
        }

    def test_start_names_its_car_and_clears_the_override_in_mode_1(self, master):
        master.startCarsCharging(self.V1)
        assert self.wanted(master) == {self.V1: True}
        master.getModuleByName("Policy").clearOverride.assert_called_once_with()

    def test_stop_replaces_the_start_for_that_car_only(self, master):
        master.startCarsCharging(self.V1)
        master.startCarsCharging(self.V2)
        master.stopCarsCharging(self.V1)
        assert self.wanted(master) == {self.V1: False, self.V2: True}

    def test_cancel_stop_for_a_car_leaves_other_stops(self, master):
        master.stopCarsCharging(self.V1)
        master.stopCarsCharging(self.V2)
        assert master.cancelStopCarsCharging(self.V1) == 1
        assert self.wanted(master) == {self.V2: False}

    def test_empty_vin_is_every_present_car_with_a_stop_candidate(self, master):
        from tests.unit.reconcile_helpers import ScriptedProvider

        master.stopCarsCharging("")
        assert self.wanted(master) == {}  # nothing can stop them
        master.providerOrder.register(ScriptedProvider("TeslaBLE", 2, True, []))
        master.stopCarsCharging("")
        assert self.wanted(master) == {self.V1: False, self.V2: False}
        assert master.cancelStopCarsCharging("") == 2 and self.wanted(master) == {}

    def test_modes_2_and_3_never_touch_the_bus_here(self, master):
        master.sendStopCommand = Mock()
        for mode in ("2", "3"):
            master.settings["chargeStopMode"] = mode
            master.stopCarsCharging(self.V1)
        assert master.settings["respondToSlaves"] == 1
        master.sendStopCommand.assert_not_called()
        master.getModuleByName("Policy").overrideLimit.assert_not_called()
