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
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {}
        }
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
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {}
        }
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
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {}
        }
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
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
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
                (None, None, 0)
            ]
        )
        
        result = priority.test_method()
        
        assert result is True
        module1.test_method.assert_called_once()
        module2.test_method.assert_called_once()
    
    def test_method_delegation_no_module(self, priority):
        """Test method delegation when no module available."""
        priority.master.getModuleByPriority = Mock(
            return_value=(None, None, 0)
        )
        
        result = priority.test_method()
        
        assert result is False
    
    def test_method_delegation_module_missing_method(self, priority):
        """Test method delegation when module doesn't have method."""
        module = Mock(spec=[])  # Empty spec means no methods
        
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
        )
        
        result = priority.test_method()
        
        assert result is False


class TestRetryLogic:
    """Test retry logic for failed attempts."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {}
        }
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
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
        )
        
        result = priority.test_method()
        
        assert result is True
        assert module.test_method.call_count == 3
    
    def test_exception_handling_with_retry(self, priority):
        """Test that exceptions trigger retries."""
        module = Mock()
        module.test_method = Mock(
            side_effect=[
                Exception("Test error"),
                Exception("Test error"),
                True
            ]
        )
        
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
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
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {}
        }
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
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
        )
        
        priority.test_method()
        
        assert priority.master.stats["moduleSuccess"]["TestModule"] == 1
    
    def test_failure_statistics_tracked(self, priority):
        """Test that failed calls are tracked."""
        module = Mock()
        module.test_method = Mock(return_value=False)
        
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
        )
        
        priority.test_method()
        
        assert priority.master.stats["moduleFailures"]["TestModule"] == 1
    
    def test_multiple_successes_tracked(self, priority):
        """Test that multiple successes are accumulated."""
        module = Mock()
        module.test_method = Mock(return_value=True)
        
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0),
                ("TestModule", module, 20),
                (None, None, 0)
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
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {}
        }
        return master
    
    @pytest.fixture
    def priority(self, mock_master):
        """Create a VehiclePriority instance."""
        from TWCManager.Vehicle.VehiclePriority import VehiclePriority
        return VehiclePriority(mock_master)
    
    def test_higher_priority_tried_first(self, priority):
        """Test that higher priority modules are tried first."""
        module_high = Mock()
        module_high.test_method = Mock(return_value=False)
        
        module_low = Mock()
        module_low.test_method = Mock(return_value=True)
        
        call_order = []
        
        def track_calls(*args):
            call_order.append(args[0])
            if args[0] == "HighPriority":
                return ("HighPriority", module_high, 20)
            elif args[0] == "LowPriority":
                return ("LowPriority", module_low, 10)
            else:
                return (None, None, 0)
        
        priority.master.getModuleByPriority = Mock(side_effect=track_calls)
        
        priority.test_method()
        
        # High priority should be tried before low priority
        assert call_order[0] == "Vehicle"
        assert call_order[1] == "Vehicle"


class TestMethodArguments:
    """Test that method arguments are passed correctly."""
    
    @pytest.fixture
    def mock_master(self):
        """Create a mock master object."""
        master = Mock()
        master.stats = {
            "moduleSuccess": {},
            "moduleFailures": {},
            "moduleDispatch": {}
        }
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
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
        )
        
        priority.test_method("arg1", "arg2")
        
        module.test_method.assert_called_with("arg1", "arg2")
    
    def test_keyword_arguments_passed(self, priority):
        """Test that keyword arguments are passed to module."""
        module = Mock()
        module.test_method = Mock(return_value=True)
        
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
        )
        
        priority.test_method(key1="value1", key2="value2")
        
        module.test_method.assert_called_with(key1="value1", key2="value2")
    
    def test_mixed_arguments_passed(self, priority):
        """Test that mixed arguments are passed to module."""
        module = Mock()
        module.test_method = Mock(return_value=True)
        
        priority.master.getModuleByPriority = Mock(
            side_effect=[
                ("TestModule", module, 20),
                (None, None, 0)
            ]
        )
        
        priority.test_method("arg1", key1="value1")
        
        module.test_method.assert_called_with("arg1", key1="value1")
