"""
Unit tests for TWCManager Policy module.

Tests core policy matching, condition evaluation, and policy selection logic.
"""

import pytest
import time
from unittest.mock import Mock, MagicMock, patch


class TestPolicyConditionMatching:
    """Test individual condition matching logic."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "chargeNowLimit": 100,
                "scheduledLimit": 80,
                "greenEnergyLimit": 60,
                "nonScheduledLimit": 50,
            },
            "policy": {}
        }
        master.settings = {
            "chargeNowAmps": 0,
            "chargeNowTimeEnd": 0,
            "scheduledAmpsMax": 16,
            "nonScheduledAmpsMax": 8,
            "nonScheduledAction": 1,
            "sunrise": 6,
            "sunset": 20,
            "hourResumeTrackGreenEnergy": 6,
        }
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    @pytest.fixture
    def policy(self, mock_master):
        """Create a Policy instance with mock master."""
        from TWCManager.Policy.Policy import Policy
        return Policy(mock_master)
    
    def test_condition_gt_true(self, policy):
        """Test greater than condition when true."""
        assert policy.doesConditionMatch("10", "gt", "5", False) is True
    
    def test_condition_gt_false(self, policy):
        """Test greater than condition when false."""
        assert policy.doesConditionMatch("5", "gt", "10", False) is False
    
    def test_condition_gte_equal(self, policy):
        """Test greater than or equal when values are equal."""
        assert policy.doesConditionMatch("10", "gte", "10", False) is True
    
    def test_condition_gte_greater(self, policy):
        """Test greater than or equal when first is greater."""
        assert policy.doesConditionMatch("15", "gte", "10", False) is True
    
    def test_condition_gte_less(self, policy):
        """Test greater than or equal when first is less."""
        assert policy.doesConditionMatch("5", "gte", "10", False) is False
    
    def test_condition_lt_true(self, policy):
        """Test less than condition when true."""
        assert policy.doesConditionMatch("5", "lt", "10", False) is True
    
    def test_condition_lt_false(self, policy):
        """Test less than condition when false."""
        assert policy.doesConditionMatch("10", "lt", "5", False) is False
    
    def test_condition_lte_equal(self, policy):
        """Test less than or equal when values are equal."""
        assert policy.doesConditionMatch("10", "lte", "10", False) is True
    
    def test_condition_lte_less(self, policy):
        """Test less than or equal when first is less."""
        assert policy.doesConditionMatch("5", "lte", "10", False) is True
    
    def test_condition_lte_greater(self, policy):
        """Test less than or equal when first is greater."""
        assert policy.doesConditionMatch("15", "lte", "10", False) is False
    
    def test_condition_eq_true(self, policy):
        """Test equality condition when true."""
        assert policy.doesConditionMatch("10", "eq", "10", False) is True
    
    def test_condition_eq_false(self, policy):
        """Test equality condition when false."""
        assert policy.doesConditionMatch("10", "eq", "5", False) is False
    
    def test_condition_ne_true(self, policy):
        """Test not equal condition when true."""
        assert policy.doesConditionMatch("10", "ne", "5", False) is True
    
    def test_condition_ne_false(self, policy):
        """Test not equal condition when false."""
        assert policy.doesConditionMatch("10", "ne", "10", False) is False
    
    def test_condition_false(self, policy):
        """Test false condition always returns false."""
        assert policy.doesConditionMatch("anything", "false", "anything", False) is False
    
    def test_condition_none(self, policy):
        """Test none condition always returns true."""
        assert policy.doesConditionMatch("anything", "none", "anything", False) is True
    
    def test_condition_invalid(self, policy):
        """Test invalid condition raises ValueError."""
        with pytest.raises(ValueError, match="Unknown condition"):
            policy.doesConditionMatch("10", "invalid", "5", False)


class TestPolicyValueMacros:
    """Test policy value macro substitution."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "chargeNowLimit": 100,
                "testValue": 42,
            },
            "policy": {}
        }
        master.settings = {
            "chargeNowAmps": 32,
            "scheduledAmpsMax": 16,
        }
        master.modules = {}
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    @pytest.fixture
    def policy(self, mock_master):
        """Create a Policy instance with mock master."""
        from TWCManager.Policy.Policy import Policy
        return Policy(mock_master)
    
    def test_literal_integer(self, policy):
        """Test literal integer values pass through unchanged."""
        assert policy.policyValue(42) == 42
    
    def test_literal_float(self, policy):
        """Test literal float values pass through unchanged."""
        assert policy.policyValue(3.14) == 3.14
    
    def test_literal_zero(self, policy):
        """Test literal zero passes through unchanged."""
        assert policy.policyValue(0) == 0
    
    def test_now_macro(self, policy):
        """Test 'now' macro returns current timestamp."""
        before = time.time()
        result = policy.policyValue("now")
        after = time.time()
        assert before <= result <= after
    
    def test_settings_macro(self, policy):
        """Test settings macro retrieves setting value."""
        assert policy.policyValue("settings.chargeNowAmps") == 32
    
    def test_settings_macro_default(self, policy):
        """Test settings macro returns 0 for missing setting."""
        assert policy.policyValue("settings.nonexistent") == 0
    
    def test_config_macro(self, policy):
        """Test config macro retrieves config value."""
        assert policy.policyValue("config.testValue") == 42
    
    def test_config_macro_default(self, policy):
        """Test config macro returns 0 for missing config."""
        assert policy.policyValue("config.nonexistent") == 0
    
    def test_tm_hour_macro(self, policy):
        """Test tm_hour macro returns current hour."""
        result = policy.policyValue("tm_hour")
        assert 0 <= result <= 23
    
    def test_tm_min_macro(self, policy):
        """Test tm_min macro returns current minute."""
        result = policy.policyValue("tm_min")
        assert 0 <= result <= 59
    
    def test_scheduled_charging_function(self, policy):
        """Test checkScheduledCharging() function call."""
        policy.master.checkScheduledCharging = Mock(return_value=1)
        assert policy.policyValue("checkScheduledCharging()") == 1
        policy.master.checkScheduledCharging.assert_called_once()


class TestCheckConditions:
    """Test multiple condition checking (AND/OR logic)."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {"config": {}, "policy": {}}
        master.settings = {}
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        return master
    
    @pytest.fixture
    def policy(self, mock_master):
        """Create a Policy instance with mock master."""
        from TWCManager.Policy.Policy import Policy
        return Policy(mock_master)
    
    def test_all_conditions_true_and_logic(self, policy):
        """Test AND logic: all conditions must be true."""
        matches = ["10", "20", "30"]
        conditions = ["gt", "gt", "gt"]
        values = ["5", "10", "20"]
        assert policy.checkConditions(matches, conditions, values, exitOn=False) is True
    
    def test_one_condition_false_and_logic(self, policy):
        """Test AND logic: one false condition fails."""
        matches = ["10", "5", "30"]
        conditions = ["gt", "gt", "gt"]
        values = ["5", "10", "20"]
        assert policy.checkConditions(matches, conditions, values, exitOn=False) is False
    
    def test_all_conditions_false_or_logic(self, policy):
        """Test OR logic: all false conditions fail."""
        matches = ["5", "5", "5"]
        conditions = ["gt", "gt", "gt"]
        values = ["10", "10", "10"]
        assert policy.checkConditions(matches, conditions, values, exitOn=True) is False
    
    def test_one_condition_true_or_logic(self, policy):
        """Test OR logic: one true condition succeeds."""
        matches = ["5", "20", "5"]
        conditions = ["gt", "gt", "gt"]
        values = ["10", "10", "10"]
        assert policy.checkConditions(matches, conditions, values, exitOn=True) is True


class TestPolicySelection:
    """Test policy selection and enforcement."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {
            "config": {
                "chargeNowLimit": 100,
                "scheduledLimit": 80,
                "greenEnergyLimit": 60,
                "nonScheduledLimit": 50,
            },
            "policy": {}
        }
        master.settings = {
            "chargeNowAmps": 0,
            "chargeNowTimeEnd": 0,
            "scheduledAmpsMax": 16,
            "nonScheduledAmpsMax": 8,
            "nonScheduledAction": 1,
        }
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        master.setMaxAmpsToDivideAmongSlaves = Mock()
        master.setAllowedFlex = Mock()
        master.queue_background_task = Mock()
        return master
    
    @pytest.fixture
    def policy(self, mock_master):
        """Create a Policy instance with mock master."""
        from TWCManager.Policy.Policy import Policy
        return Policy(mock_master)
    
    def test_get_policy_by_name_found(self, policy):
        """Test getPolicyByName returns correct policy."""
        result = policy.getPolicyByName("Charge Now")
        assert result is not None
        assert result["name"] == "Charge Now"
    
    def test_get_policy_by_name_not_found(self, policy):
        """Test getPolicyByName returns None for missing policy."""
        result = policy.getPolicyByName("Nonexistent Policy")
        assert result is None
    
    def test_policy_is_green_true(self, policy):
        """Test policyIsGreen returns true for green energy policy."""
        policy.active_policy = "Track Green Energy"
        assert policy.policyIsGreen() is True
    
    def test_policy_is_green_false(self, policy):
        """Test policyIsGreen returns false for non-green policy."""
        policy.active_policy = "Charge Now"
        assert policy.policyIsGreen() is False
    
    def test_policy_is_green_no_active(self, policy):
        """Test policyIsGreen returns false when no policy active."""
        policy.active_policy = None
        assert policy.policyIsGreen() is False
    
    def test_get_active_policy_action_green(self, policy):
        """Test getActivePolicyAction returns 3 for green energy."""
        policy.active_policy = "Track Green Energy"
        assert policy.getActivePolicyAction() == 3
    
    def test_get_active_policy_action_charging(self, policy):
        """Test getActivePolicyAction returns 1 for charging."""
        policy.active_policy = "Charge Now"
        assert policy.getActivePolicyAction() == 1
    
    def test_get_active_policy_action_no_charge(self, policy):
        """Test getActivePolicyAction returns 2 for no charge."""
        policy.active_policy = "Non Scheduled Charging"
        policy.master.settings["nonScheduledAmpsMax"] = 0
        assert policy.getActivePolicyAction() == 2
    
    def test_get_active_policy_action_none(self, policy):
        """Test getActivePolicyAction returns None when no policy active."""
        policy.active_policy = None
        assert policy.getActivePolicyAction() is None
    
    def test_override_limit(self, policy):
        """Test overrideLimit sets flag."""
        policy.limitOverride = False
        policy.overrideLimit()
        assert policy.limitOverride is True
    
    def test_clear_override(self, policy):
        """Test clearOverride clears flag."""
        policy.limitOverride = True
        policy.clearOverride()
        assert policy.limitOverride is False


class TestPolicyInitialization:
    """Test policy initialization and configuration."""
    
    def test_default_policy_loaded(self):
        """Test default policy is loaded when no config provided."""
        from TWCManager.Policy.Policy import Policy
        master = Mock()
        master.config = {"config": {}, "policy": {}}
        master.settings = {}
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        
        policy = Policy(master)
        
        assert len(policy.charge_policy) > 0
        assert policy.charge_policy[0]["name"] == "Charge Now"
    
    def test_policy_check_interval_default(self):
        """Test default policy check interval."""
        from TWCManager.Policy.Policy import Policy
        master = Mock()
        master.config = {"config": {}, "policy": {}}
        master.settings = {}
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        
        policy = Policy(master)
        
        assert policy.policyCheckInterval == 30
    
    def test_policy_check_interval_custom(self):
        """Test custom policy check interval from config."""
        from TWCManager.Policy.Policy import Policy
        master = Mock()
        master.config = {
            "config": {},
            "policy": {
                "engine": {
                    "policyCheckInterval": 60
                }
            }
        }
        master.settings = {}
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        
        policy = Policy(master)
        
        assert policy.policyCheckInterval == 60


class TestPolicyThrottling:
    """Test policy check throttling."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.config = {"config": {}, "policy": {}}
        master.settings = {}
        master.getModuleByName = Mock(return_value=Mock())
        master.getModulesByType = Mock(return_value=[])
        master.setMaxAmpsToDivideAmongSlaves = Mock()
        master.setAllowedFlex = Mock()
        master.queue_background_task = Mock()
        return master
    
    @pytest.fixture
    def policy(self, mock_master):
        """Create a Policy instance with mock master."""
        from TWCManager.Policy.Policy import Policy
        return Policy(mock_master)
    
    def test_apply_policy_immediately(self, policy):
        """Test applyPolicyImmediately resets throttle."""
        policy.lastPolicyCheck = time.time()
        policy.applyPolicyImmediately()
        assert policy.lastPolicyCheck == 0
    
    def test_policy_check_throttled(self, policy):
        """Test policy check is throttled within interval."""
        policy.policyCheckInterval = 30
        policy.lastPolicyCheck = time.time()
        policy.active_policy = "Charge Now"
        
        # Should return early without checking
        policy.setChargingPerPolicy()
        
        # setMaxAmpsToDivideAmongSlaves should not be called
        policy.master.setMaxAmpsToDivideAmongSlaves.assert_not_called()
    
    def test_policy_check_not_throttled(self, policy):
        """Test policy check runs after throttle interval."""
        policy.policyCheckInterval = 0
        policy.lastPolicyCheck = 0
        policy.active_policy = None
        
        policy.setChargingPerPolicy()
        
        # Should have called setMaxAmpsToDivideAmongSlaves
        assert policy.master.setMaxAmpsToDivideAmongSlaves.called
