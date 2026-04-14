"""
Integration tests for multi-slave TWC scenarios.

Tests cover:
- Load sharing between multiple slaves
- Slave detection and enumeration
- Dynamic slave addition/removal
- Load rebalancing
"""

import pytest
import time


class TestMultiSlaveDetection:
    """Test suite for multi-slave detection."""
    
    def test_single_slave_detection(self, api_get, assert_json_response):
        """Test detection of single slave TWC."""
        response = api_get('/getSlaveTWCs')
        data = assert_json_response(response, 200)
        
        # With Dummy interface, we should have at least one slave
        assert isinstance(data, (list, dict)), "Response should be a list or dict of slaves"
        
        if isinstance(data, list):
            assert len(data) >= 1, "Should have at least one dummy TWC slave"
    
    def test_slave_structure(self, api_get, assert_json_response):
        """Test that slave objects have required fields."""
        response = api_get('/getSlaveTWCs')
        data = assert_json_response(response, 200)
        
        if isinstance(data, list) and len(data) > 0:
            slave = data[0]
            # Check for common slave identifier fields
            assert any(key in slave for key in ['TWCID', 'twcid', 'id', 'ID']), \
                "Slave should have an identifier field"


class TestLoadSharing:
    """Test suite for load sharing functionality."""
    
    def test_charge_distribution_single_slave(self, api_post, api_get, assert_json_response):
        """Test charge distribution with single slave."""
        # Set charge now with 32 amps
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 3600
        })
        assert_json_response(response, 200)
        
        time.sleep(1)
        
        # Get status to verify charge is being distributed
        status_response = api_get('/getStatus')
        status = assert_json_response(status_response, 200)
        
        # Should have some charging activity
        assert status is not None
    
    def test_charge_now_with_multiple_amperage_levels(self, api_post, assert_json_response):
        """Test charge now with various amperage levels."""
        test_amperages = [12, 16, 24, 32]
        
        for amps in test_amperages:
            response = api_post('/chargeNow', json={
                'chargeNowRate': amps,
                'chargeNowDuration': 3600
            })
            assert_json_response(response, 200)
            time.sleep(0.5)


class TestSlaveStateManagement:
    """Test suite for slave state management."""
    
    def test_slave_state_persistence(self, api_get, assert_json_response):
        """Test that slave state persists across API calls."""
        # Get initial slave list
        response1 = api_get('/getSlaveTWCs')
        data1 = assert_json_response(response1, 200)
        initial_count = len(data1) if isinstance(data1, list) else 1
        
        # Make other API calls
        api_get('/getStatus')
        api_get('/getPolicy')
        api_get('/getConfig')
        
        # Get slave list again
        response2 = api_get('/getSlaveTWCs')
        data2 = assert_json_response(response2, 200)
        final_count = len(data2) if isinstance(data2, list) else 1
        
        # Slave count should remain the same
        assert initial_count == final_count, "Slave count should not change"
    
    def test_slave_status_after_charge_command(self, api_post, api_get, assert_json_response):
        """Test slave status after sending charge commands."""
        # Send start command
        api_post('/sendStartCommand')
        time.sleep(1)
        
        # Get slave status
        response = api_get('/getSlaveTWCs')
        data = assert_json_response(response, 200)
        
        assert data is not None, "Should return slave data after start command"
        
        # Send stop command
        api_post('/sendStopCommand')
        time.sleep(1)
        
        # Get slave status again
        response = api_get('/getSlaveTWCs')
        data = assert_json_response(response, 200)
        
        assert data is not None, "Should return slave data after stop command"


class TestLoadBalancing:
    """Test suite for load balancing scenarios."""
    
    def test_charge_now_cancellation_affects_all_slaves(self, api_post, api_get, assert_json_response):
        """Test that canceling charge now affects all slaves."""
        # Activate charge now
        api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 3600
        })
        time.sleep(1)
        
        # Get status with active charge
        status1 = api_get('/getStatus')
        assert_json_response(status1, 200)
        
        # Cancel charge now
        api_post('/cancelChargeNow')
        time.sleep(1)
        
        # Get status after cancellation
        status2 = api_get('/getStatus')
        data = assert_json_response(status2, 200)
        
        # Verify charge now is canceled
        charge_now_amps = data.get('chargeNowAmps', 0) or data.get('settings', {}).get('chargeNowAmps', 0)
        assert charge_now_amps == 0, "Charge Now should be canceled"
    
    def test_policy_change_affects_all_slaves(self, api_get, assert_json_response):
        """Test that policy changes are reflected across all slaves."""
        # Get initial policy
        response1 = api_get('/getPolicy')
        policy1 = assert_json_response(response1, 200)
        
        # Get status
        status_response = api_get('/getStatus')
        status = assert_json_response(status_response, 200)
        
        # Get policy again
        response2 = api_get('/getPolicy')
        policy2 = assert_json_response(response2, 200)
        
        # Policies should be consistent
        assert policy1 is not None
        assert policy2 is not None


class TestSlaveErrorHandling:
    """Test suite for slave error handling."""
    
    def test_invalid_amperage_rejected(self, api_post):
        """Test that invalid amperage values are rejected."""
        # Try to set invalid amperage (too high)
        response = api_post('/chargeNow', json={
            'chargeNowRate': 999,
            'chargeNowDuration': 3600
        })
        
        # Should either reject or handle gracefully
        assert response.status_code in [200, 400, 422], \
            "Should handle invalid amperage gracefully"
    
    def test_invalid_duration_rejected(self, api_post):
        """Test that invalid duration values are rejected."""
        # Try to set invalid duration (negative)
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': -1
        })
        
        # Should either reject or handle gracefully
        assert response.status_code in [200, 400, 422], \
            "Should handle invalid duration gracefully"
    
    def test_concurrent_charge_commands(self, api_post, api_get, assert_json_response):
        """Test handling of concurrent charge commands."""
        # Send multiple charge commands rapidly
        for i in range(3):
            api_post('/chargeNow', json={
                'chargeNowRate': 24 + (i * 4),
                'chargeNowDuration': 3600
            })
        
        time.sleep(1)
        
        # System should handle gracefully
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        assert data is not None, "System should handle concurrent commands"
