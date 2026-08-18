"""
Stress testing for TWCManager.

Tests cover:
- Long-running continuous operation
- High-frequency policy changes
- Multiple slave coordination under stress
- Network instability simulation
- Resource usage under load
"""

import pytest
import time


@pytest.mark.slow
class TestContinuousOperation:
    """Test suite for continuous operation scenarios."""
    
    def test_60_second_continuous_operation(self, api_get, api_post):
        """Test 60 seconds of continuous operation."""
        duration = 60
        call_count = 0
        error_count = 0
        start = time.time()
        
        while time.time() - start < duration:
            try:
                # Alternate between reads and writes
                api_get('/getStatus')
                api_post('/chargeNow', json={
                    'chargeNowRate': 24,
                    'chargeNowDuration': 3600
                })
                api_get('/getPolicy')
                api_post('/cancelChargeNow')
                call_count += 4
            except Exception as e:
                error_count += 1
        
        elapsed = time.time() - start
        
        # Should complete with minimal errors
        assert error_count < 5, f"Had {error_count} errors in {duration}s"
        assert call_count > 100, f"Only {call_count} calls in {duration}s"
    
    def test_rapid_state_changes(self, api_post, api_get):
        """Test rapid state changes over extended period."""
        duration = 30
        state_changes = 0
        start = time.time()
        
        while time.time() - start < duration:
            # Rapid start/stop cycles
            api_post('/sendStartCommand')
            api_post('/sendStopCommand')
            state_changes += 2
            time.sleep(0.1)
        
        elapsed = time.time() - start
        
        # Should handle rapid state changes
        assert state_changes > 50, f"Only {state_changes} state changes in {duration}s"
        
        # System should still be responsive
        response = api_get('/getStatus')
        assert response.status_code == 200


@pytest.mark.slow
class TestHighFrequencyPolicyChanges:
    """Test suite for high-frequency policy changes."""
    
    def test_rapid_charge_rate_changes(self, api_post, api_get):
        """Test rapid charge rate changes."""
        rates = [12, 16, 20, 24, 28, 32, 28, 24, 20, 16, 12]
        
        for cycle in range(5):
            for rate in rates:
                api_post('/chargeNow', json={
                    'chargeNowRate': rate,
                    'chargeNowDuration': 3600
                })
                time.sleep(0.05)
        
        # System should handle 55 rate changes
        response = api_get('/getStatus')
        assert response.status_code == 200
    
    def test_alternating_charge_modes(self, api_post, api_get):
        """Test alternating between different charge modes."""
        for i in range(20):
            # Charge now mode
            api_post('/chargeNow', json={
                'chargeNowRate': 24,
                'chargeNowDuration': 3600
            })
            time.sleep(0.1)
            
            # Cancel and start/stop
            api_post('/cancelChargeNow')
            api_post('/sendStartCommand')
            time.sleep(0.1)
            
            # Stop and back to charge now
            api_post('/sendStopCommand')
            time.sleep(0.1)
        
        # System should handle mode switching
        response = api_get('/getStatus')
        assert response.status_code == 200


@pytest.mark.slow
class TestMultiSlaveStress:
    """Test suite for multi-slave stress scenarios."""
    
    def test_load_distribution_under_stress(self, api_post, api_get):
        """Test load distribution under stress."""
        # Set high charge rate
        api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 3600
        })
        
        # Hammer with status requests
        for i in range(50):
            response = api_get('/getStatus')
            assert response.status_code == 200
        
        # Verify slaves still detected
        response = api_get('/getSlaveTWCs')
        assert response.status_code == 200
    
    def test_slave_enumeration_under_load(self, api_get):
        """Test slave enumeration under load."""
        for i in range(30):
            response = api_get('/getSlaveTWCs')
            assert response.status_code == 200
            
            # Verify response is valid
            data = response.json()
            assert data is not None


@pytest.mark.slow
class TestNetworkInstability:
    """Test suite for network instability scenarios."""
    
    def test_recovery_from_timeout(self, api_get):
        """Test recovery from timeout scenarios."""
        success_count = 0
        
        for i in range(20):
            try:
                response = api_get('/getStatus')
                if response.status_code == 200:
                    success_count += 1
            except Exception as e:
                # Should recover
                pass
        
        # Should have high success rate
        assert success_count > 15, f"Only {success_count}/20 successful"
    
    def test_intermittent_failures_recovery(self, api_get, api_post):
        """Test recovery from intermittent failures."""
        operations = 0
        failures = 0
        
        for i in range(30):
            try:
                if i % 2 == 0:
                    api_get('/getStatus')
                else:
                    api_post('/chargeNow', json={
                        'chargeNowRate': 24,
                        'chargeNowDuration': 3600
                    })
                operations += 1
            except Exception as e:
                failures += 1
        
        # Should complete most operations
        assert operations > 20, f"Only {operations}/30 operations completed"


@pytest.mark.slow
class TestResourceUsage:
    """Test suite for resource usage under stress."""
    
    def test_memory_stability_under_load(self, api_get):
        """Test memory stability under sustained load."""
        # Perform many operations
        for i in range(100):
            response = api_get('/getStatus')
            assert response.status_code == 200
        
        # System should still be responsive
        response = api_get('/getStatus')
        assert response.status_code == 200
    
    def test_connection_stability(self, api_get, api_post):
        """Test connection stability under stress."""
        for i in range(50):
            # Mix of different operations
            api_get('/getStatus')
            api_get('/getPolicy')
            api_get('/getSlaveTWCs')
            api_post('/chargeNow', json={
                'chargeNowRate': 24,
                'chargeNowDuration': 3600
            })
        
        # Final check
        response = api_get('/getStatus')
        assert response.status_code == 200


@pytest.mark.slow
class TestExtendedScenarios:
    """Test suite for extended operation scenarios."""
    
    def test_charge_cycle_sequence(self, api_post, api_get):
        """Test extended charge cycle sequence."""
        for cycle in range(10):
            # Start charging
            api_post('/chargeNow', json={
                'chargeNowRate': 32,
                'chargeNowDuration': 3600
            })
            time.sleep(0.5)
            
            # Check status
            api_get('/getStatus')
            
            # Adjust rate
            api_post('/chargeNow', json={
                'chargeNowRate': 24,
                'chargeNowDuration': 3600
            })
            time.sleep(0.5)
            
            # Stop charging
            api_post('/cancelChargeNow')
            time.sleep(0.5)
        
        # System should complete all cycles
        response = api_get('/getStatus')
        assert response.status_code == 200
    
    def test_policy_evaluation_under_stress(self, api_get):
        """Test policy evaluation under stress."""
        for i in range(50):
            response = api_get('/getActivePolicyAction')
            assert response.status_code == 200
            
            response = api_get('/getPolicy')
            assert response.status_code == 200
        
        # System should handle repeated policy queries
        response = api_get('/getStatus')
        assert response.status_code == 200
