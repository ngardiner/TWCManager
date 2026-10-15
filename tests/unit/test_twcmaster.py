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
        assert master.version == "1.3.4"
    
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
    
    def test_set_allowed_flex(self, master):
        """Test setting allowed flex amperage."""
        master.setAllowedFlex(8)
        
        assert master.allowed_flex == 8
    
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
    
    def test_get_master_sign(self, master):
        """Test getting master sign."""
        sign = master.getMasterSign()
        
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
    
    def test_get_setting(self, master):
        """Test getting a setting."""
        value = master.getSetting("chargeNowAmps")
        
        assert value == 0
    
    def test_get_setting_default(self, master):
        """Test getting a non-existent setting returns default."""
        value = master.getSetting("nonexistent", 42)
        
        assert value == 42
    
    def test_set_setting(self, master):
        """Test setting a value."""
        master.setSetting("chargeNowAmps", 32)
        
        assert master.settings["chargeNowAmps"] == 32
    
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
    """Vehicle commands go to the VehicleCommandQueue, not the background queue."""

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
        m.vehicleCommandQueue = Mock()
        # The background queue state is class-level; isolate it per test.
        m.backgroundTasksQueue = Mock()
        m.backgroundTasksCmds = {}
        m.backgroundTasksDelayed = []
        return m

    def test_master_creates_unstarted_queue(self):
        from TWCManager.TWCMaster import TWCMaster
        from TWCManager.VehicleCommandQueue import VehicleCommandQueue

        m = TWCMaster(
            "AB",
            {
                "config": {
                    "wiringMaxAmpsAllTWCs": 80,
                    "maxAmpsAllowedFromGrid": None,
                }
            },
        )
        assert isinstance(m.vehicleCommandQueue, VehicleCommandQueue)
        assert m.vehicleCommandQueue._thread is None

    @pytest.mark.parametrize(
        "task",
        [
            {"cmd": "charge", "charge": True},
            {"cmd": "charge", "charge": False, "vin": "V1"},
        ],
    )
    def test_vehicle_commands_routed(self, master, task):
        master.queue_background_task(task)
        master.vehicleCommandQueue.put.assert_called_once_with(task, None)
        master.backgroundTasksQueue.put.assert_not_called()
        assert task["cmd"] not in master.backgroundTasksCmds

    def test_other_tasks_stay_on_background_queue(self, master):
        task = {"cmd": "saveSettings"}
        master.queue_background_task(task)
        master.backgroundTasksQueue.put.assert_called_once_with(task)
        master.vehicleCommandQueue.put.assert_not_called()

    def test_delayed_charge_routed_when_due(self, master):
        task = {"cmd": "charge", "charge": True}
        master.queue_background_task(task, delay=60)
        master.vehicleCommandQueue.put.assert_not_called()
        assert len(master.backgroundTasksDelayed) == 1

        master.backgroundTasksDelayed[0] = (datetime.now() - timedelta(seconds=1), task)
        master.getBackgroundTask()
        master.vehicleCommandQueue.put.assert_called_once_with(task, None)

    def test_queue_vehicle_command_forwards_callback(self, master):
        task = {"cmd": "setChargeRate", "amps": 16, "vehicle": None}
        callback = Mock()
        master.queue_vehicle_command(task, callback)
        master.vehicleCommandQueue.put.assert_called_once_with(task, callback)

    @pytest.mark.parametrize(
        "loaded, expected",
        [
            (["VehiclePriority", "HomeAssistant", "TeslaBLE", "TeslaAPI"], "VehiclePriority"),
            (["HomeAssistant", "TeslaBLE", "TeslaAPI"], "HomeAssistant"),
            (["TeslaBLE", "TeslaAPI"], "TeslaBLE"),
            (["TeslaAPI"], "TeslaAPI"),
            ([], None),
        ],
    )
    def test_resolve_vehicle_command_module(self, master, loaded, expected):
        modules = {name: Mock(name=name) for name in loaded}
        master.getModuleByName = Mock(side_effect=lambda n: modules.get(n))
        resolved = master._resolveVehicleCommandModule()
        assert resolved is (modules[expected] if expected else None)


class TestChargeStartStopCancelRules:
    """startCarsCharging / stopCarsCharging / cancelStopCarsCharging keep the
    vehicle queue consistent: a stop cancels pending starts for that car, and
    cancelStopCarsCharging really removes pending stops (#667)."""

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
        m.getModuleByName = Mock(return_value=Mock())
        m.settings["chargeStopMode"] = "1"
        # The queue is never started, so put() only records entries.
        return m

    @staticmethod
    def pending(master):
        q = master.vehicleCommandQueue
        return [task for task, _ in list(q._high.values()) + list(q._low.values())]

    def test_start_passes_vin_in_stop_mode_1(self, master):
        master.startCarsCharging("V1")
        assert self.pending(master) == [{"cmd": "charge", "charge": True, "vin": "V1"}]

    def test_stop_cancels_pending_start_for_that_vin_and_vehicleless(self, master):
        master.startCarsCharging("V1")
        master.startCarsCharging(None)
        master.startCarsCharging("V2")
        master.stopCarsCharging("V1")
        assert self.pending(master) == [
            {"cmd": "charge", "charge": False, "vin": "V1"},
            {"cmd": "charge", "charge": True, "vin": "V2"},
        ]

    def test_stop_for_all_cancels_every_pending_start(self, master):
        master.startCarsCharging("V1")
        master.startCarsCharging("V2")
        master.stopCarsCharging(None)
        assert self.pending(master) == [{"cmd": "charge", "charge": False, "vin": None}]

    def test_cancel_stop_for_vin_leaves_other_stops(self, master):
        master.stopCarsCharging("V1")
        master.stopCarsCharging("V2")
        assert master.cancelStopCarsCharging("V1") == 1
        assert self.pending(master) == [{"cmd": "charge", "charge": False, "vin": "V2"}]

    def test_cancel_stop_for_all(self, master):
        master.stopCarsCharging("V1")
        master.stopCarsCharging("V2")
        assert master.cancelStopCarsCharging() == 2
        assert self.pending(master) == []

    def test_empty_vin_is_treated_as_none(self, master):
        master.startCarsCharging("")
        assert self.pending(master) == [{"cmd": "charge", "charge": True, "vin": None}]
        master.stopCarsCharging("")
        assert self.pending(master) == [{"cmd": "charge", "charge": False, "vin": None}]
        assert master.cancelStopCarsCharging("") == 1
        assert self.pending(master) == []
