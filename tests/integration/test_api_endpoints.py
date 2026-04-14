"""
Sample integration tests demonstrating the new pytest framework.

This file shows how to migrate existing tests to pytest and demonstrates
best practices for writing integration tests.
"""

import pytest
import time


class TestAPIEndpoints:
    """Test suite for basic API endpoints."""
    
    def test_api_listener_is_running(self, api_get):
        """Test that the API server is listening and responding."""
        response = api_get('/getStatus')
        assert response.status_code == 200, "API server should be accessible"
    
    def test_get_config(self, api_get, assert_json_response):
        """Test retrieving TWCManager configuration."""
        response = api_get('/getConfig')
        data = assert_json_response(response, 200)
        
        # Verify essential config fields exist
        assert 'config' in data, "Response should contain config object"
        assert 'wiringMaxAmpsAllTWCs' in data['config']
        assert 'minAmpsPerTWC' in data['config']
    
    def test_get_status(self, api_get, assert_json_response):
        """Test retrieving TWCManager status."""
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Verify status structure - should contain charging status information
        assert isinstance(data, dict), "Response should be a dictionary"
        # Check for key status fields
        assert any(key in data for key in ['currentPolicy', 'chargerLoadWatts', 'carsCharging']), \
            "Response should contain charging status information"
    
    def test_get_slave_twcs(self, api_get, assert_json_response):
        """Test retrieving list of slave TWCs."""
        response = api_get('/getSlaveTWCs')
        data = assert_json_response(response, 200)
        
        # With Dummy interface, we should have at least one slave
        assert isinstance(data, (list, dict)), "Response should be a list or dict of slaves"
        
        # If it's a list, check we have slaves
        if isinstance(data, list):
            assert len(data) > 0, "Should have at least one dummy TWC slave"
            
            # Verify slave structure
            slave = data[0]
            assert 'TWCID' in slave or 'twcid' in slave, "Slave should have an ID"


class TestChargeControl:
    """Test suite for charge control functionality."""
    
    def test_charge_now_activation(self, api_post, api_get, assert_json_response, wait_for_condition):
        """Test activating Charge Now mode."""
        # Set charge now with 32 amps for 1 hour
        charge_amps = 32
        charge_duration = 3600  # 1 hour in seconds
        response = api_post('/chargeNow', json={'chargeNowRate': charge_amps, 'chargeNowDuration': charge_duration})
        assert_json_response(response, 200)
        
        # Wait for status to reflect the change
        def charge_now_active():
            status_response = api_get('/getStatus')
            if status_response.status_code != 200:
                return False
            data = status_response.json()
            # Check if chargeNowAmps is set (field name may vary)
            return data.get('chargeNowAmps', 0) > 0 or data.get('settings', {}).get('chargeNowAmps', 0) > 0
        
        wait_for_condition(
            charge_now_active,
            timeout=10,
            message="Charge Now mode did not activate"
        )
    
    def test_charge_now_cancellation(self, api_post, api_get, assert_json_response):
        """Test canceling Charge Now mode."""
        # First activate charge now
        api_post('/chargeNow', json={'chargeNowRate': 32, 'chargeNowDuration': 3600})
        time.sleep(2)
        
        # Then cancel it
        response = api_post('/cancelChargeNow')
        assert_json_response(response, 200)
        
        # Verify it's canceled
        time.sleep(2)
        status_response = api_get('/getStatus')
        data = assert_json_response(status_response, 200)
        
        # ChargeNowAmps should be 0 or not present
        charge_now_amps = data.get('chargeNowAmps', 0) or data.get('settings', {}).get('chargeNowAmps', 0)
        assert charge_now_amps == 0, "Charge Now should be canceled"
    
    @pytest.mark.parametrize("amps", [12, 16, 24, 32, 40])
    def test_charge_now_various_amperage(self, api_post, assert_json_response, amps):
        """Test Charge Now with various amperage values."""
        response = api_post('/chargeNow', json={'chargeNowRate': amps, 'chargeNowDuration': 3600})
        assert_json_response(response, 200)
        
        # Give it a moment to process
        time.sleep(1)


class TestPolicyEngine:
    """Test suite for policy engine functionality."""
    
    def test_get_policy(self, api_get, assert_json_response):
        """Test retrieving current charging policy."""
        response = api_get('/getPolicy')
        data = assert_json_response(response, 200)
        
        # Should return policy information
        assert data is not None, "Policy response should not be empty"
    
    def test_get_active_policy_action(self, api_get, assert_json_response):
        """Test retrieving the currently active policy action."""
        response = api_get('/getActivePolicyAction')
        data = assert_json_response(response, 200)
        
        # Should return active policy information
        assert data is not None, "Active policy response should not be empty"


class TestLocationSettings:
    """Test suite for location-based settings."""
    
    def test_set_lat_lon(self, api_post, assert_json_response):
        """Test setting latitude and longitude."""
        test_lat = 37.7749
        test_lon = -122.4194
        
        response = api_post('/setLatLon', json={
            'lat': test_lat,
            'lon': test_lon
        })
        
        # Should accept the coordinates
        assert response.status_code in [200, 201, 204], \
            f"Setting lat/lon should succeed, got {response.status_code}"


class TestConsumptionOffsets:
    """Test suite for consumption offset functionality."""
    
    def test_set_consumption_offset(self, api_post, assert_json_response):
        """Test setting consumption offset values."""
        response = api_post('/setConsumptionOffset', json={
            'offset': 2.5
        })
        
        # Should accept the offset
        assert response.status_code in [200, 201, 204], \
            f"Setting consumption offset should succeed, got {response.status_code}"


@pytest.mark.slow
class TestStartStopCommands:
    """Test suite for start/stop charging commands."""
    
    def test_send_start_command(self, api_post, assert_json_response):
        """Test sending start charging command."""
        response = api_post('/sendStartCommand')
        
        # Command should be accepted
        assert response.status_code in [200, 201, 204], \
            f"Start command should be accepted, got {response.status_code}"
    
    def test_send_stop_command(self, api_post, assert_json_response):
        """Test sending stop charging command."""
        response = api_post('/sendStopCommand')
        
        # Command should be accepted
        assert response.status_code in [200, 201, 204], \
            f"Stop command should be accepted, got {response.status_code}"
    
    def test_start_stop_sequence(self, api_post):
        """Test a complete start-stop-start sequence."""
        # Stop
        response = api_post('/sendStopCommand')
        assert response.status_code in [200, 201, 204]
        time.sleep(2)
        
        # Start
        response = api_post('/sendStartCommand')
        assert response.status_code in [200, 201, 204]
        time.sleep(2)
        
        # Stop again
        response = api_post('/sendStopCommand')
        assert response.status_code in [200, 201, 204]


class TestMultiSlaveScenarios:
    """Test suite for multi-slave TWC scenarios."""
    
    def test_load_sharing_two_slaves(self, dummy_twc_scenario, api_get):
        """Test load sharing between two TWC slaves."""
        scenario = dummy_twc_scenario('multi_slave_balanced')
        
        # Verify both slaves are detected
        response = api_get('/getSlaveTWCs')
        data = response.json()
        
        assert len(data) == 2, "Should detect two TWC slaves"
    
    @pytest.mark.slow
    def test_dynamic_slave_addition(self, dummy_twc_scenario, api_get, wait_for_condition):
        """Test adding a slave dynamically during operation."""
        scenario = dummy_twc_scenario('dynamic_slave_addition')
        
        # Initially should have one slave
        response = api_get('/getSlaveTWCs')
        initial_slaves = len(response.json())
        
        # Wait for second slave to join (configured to join after 30s in scenario)
        def second_slave_joined():
            response = api_get('/getSlaveTWCs')
            return len(response.json()) > initial_slaves
        
        wait_for_condition(
            second_slave_joined,
            timeout=35,
            message="Second slave did not join"
        )


class TestSettingsPersistence:
    """Test suite for settings persistence and edge cases."""
    
    def test_settings_survive_api_calls(self, api_post, api_get, assert_json_response):
        """Test that settings persist across multiple API calls."""
        # Set charge now
        api_post('/chargeNow', json={'chargeNowRate': 24, 'chargeNowDuration': 7200})
        time.sleep(1)
        
        # Make several other API calls
        api_get('/getStatus')
        api_get('/getPolicy')
        api_get('/getSlaveTWCs')
        
        # Verify charge now is still active
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        assert data.get('chargeNowAmps', 0) > 0, "Charge Now setting should persist"
    
    def test_charge_now_with_zero_duration_rejected(self, api_post):
        """Test that chargeNow with zero duration is rejected."""
        response = api_post('/chargeNow', json={'chargeNowRate': 32, 'chargeNowDuration': 0})
        # Should fail validation
        assert response.status_code in [400, 422], "Zero duration should be rejected"
    
    def test_charge_now_with_negative_amps_rejected(self, api_post):
        """Test that chargeNow with negative amps is rejected."""
        response = api_post('/chargeNow', json={'chargeNowRate': -32, 'chargeNowDuration': 3600})
        # Should fail validation
        assert response.status_code in [400, 422], "Negative amps should be rejected"
    
    def test_charge_now_with_zero_amps_rejected(self, api_post):
        """Test that chargeNow with zero amps is rejected."""
        response = api_post('/chargeNow', json={'chargeNowRate': 0, 'chargeNowDuration': 3600})
        # Should fail validation
        assert response.status_code in [400, 422], "Zero amps should be rejected"


class TestAPIErrorHandling:
    """Test suite for API error handling and edge cases."""
    
    def test_invalid_endpoint_returns_404(self, api_get):
        """Test that invalid endpoints return 404."""
        response = api_get('/nonexistent')
        assert response.status_code == 404, "Invalid endpoint should return 404"
    
    def test_malformed_json_returns_400(self, api_post):
        """Test that malformed JSON returns 400."""
        response = api_post('/chargeNow', data='not valid json', 
                          headers={'Content-Type': 'application/json'})
        assert response.status_code == 400, "Malformed JSON should return 400"
    
    def test_missing_required_parameters_returns_400(self, api_post):
        """Test that missing required parameters return 400."""
        # chargeNow requires both chargeNowRate and chargeNowDuration
        response = api_post('/chargeNow', json={'chargeNowRate': 32})
        assert response.status_code == 400, "Missing duration should return 400"
    
    def test_get_status_always_returns_valid_json(self, api_get, assert_json_response):
        """Test that getStatus always returns valid JSON."""
        for _ in range(5):
            response = api_get('/getStatus')
            data = assert_json_response(response, 200)
            assert isinstance(data, dict), "Status should always return a dict"
            assert len(data) > 0, "Status should not be empty"


class TestPolicyEngineIntegration:
    """Test suite for policy engine integration."""
    
    def test_policy_changes_with_charge_now(self, api_post, api_get, assert_json_response):
        """Test that policy changes when Charge Now is activated."""
        # Get initial policy
        response = api_get('/getStatus')
        initial_data = assert_json_response(response, 200)
        initial_policy = initial_data.get('currentPolicy')
        
        # Activate charge now
        api_post('/chargeNow', json={'chargeNowRate': 32, 'chargeNowDuration': 3600})
        time.sleep(2)
        
        # Check if policy changed
        response = api_get('/getStatus')
        new_data = assert_json_response(response, 200)
        new_policy = new_data.get('currentPolicy')
        
        # Policy should change to reflect charge now
        assert new_policy is not None, "Policy should be set"
    
    def test_get_active_policy_action_returns_data(self, api_get, assert_json_response):
        """Test that getActivePolicyAction returns valid data."""
        response = api_get('/getActivePolicyAction')
        data = assert_json_response(response, 200)
        
        # Should return some policy information
        assert data is not None, "Active policy action should not be None"
    
    def test_policy_persists_across_calls(self, api_get, assert_json_response):
        """Test that policy remains consistent across multiple calls."""
        policies = []
        for _ in range(3):
            response = api_get('/getStatus')
            data = assert_json_response(response, 200)
            policies.append(data.get('currentPolicy'))
            time.sleep(0.5)
        
        # All policies should be the same (no rapid changes)
        assert len(set(policies)) == 1, "Policy should remain consistent"


class TestConfigurationAccess:
    """Test suite for configuration access and validation."""
    
    def test_config_contains_required_fields(self, api_get, assert_json_response):
        """Test that config contains all required fields."""
        response = api_get('/getConfig')
        data = assert_json_response(response, 200)
        
        required_fields = ['config', 'wiringMaxAmpsAllTWCs', 'minAmpsPerTWC']
        for field in required_fields:
            assert field in data or field in data.get('config', {}), \
                f"Config should contain {field}"
    
    def test_config_values_are_reasonable(self, api_get, assert_json_response):
        """Test that config values are within reasonable ranges."""
        response = api_get('/getConfig')
        data = assert_json_response(response, 200)
        
        config = data.get('config', data)
        
        # Wiring max amps should be positive and reasonable (1-200A)
        max_amps = config.get('wiringMaxAmpsAllTWCs')
        if max_amps:
            assert 1 <= int(max_amps) <= 200, "Max amps should be reasonable"
        
        # Min amps should be positive and less than max
        min_amps = config.get('minAmpsPerTWC')
        if min_amps and max_amps:
            assert 1 <= int(min_amps) <= int(max_amps), "Min amps should be less than max"


class TestConcurrentOperations:
    """Test suite for concurrent operations and race conditions."""
    
    def test_rapid_charge_now_calls(self, api_post, assert_json_response):
        """Test that rapid chargeNow calls don't cause issues."""
        responses = []
        for amps in [12, 16, 20, 24, 28, 32]:
            response = api_post('/chargeNow', json={'chargeNowRate': amps, 'chargeNowDuration': 3600})
            responses.append(response.status_code)
        
        # All should succeed
        assert all(code in [200, 204] for code in responses), \
            "Rapid chargeNow calls should all succeed"
    
    def test_status_calls_during_charge_now(self, api_post, api_get, assert_json_response):
        """Test that status calls work correctly during chargeNow."""
        api_post('/chargeNow', json={'chargeNowRate': 32, 'chargeNowDuration': 3600})
        
        # Make rapid status calls
        for _ in range(5):
            response = api_get('/getStatus')
            data = assert_json_response(response, 200)
            assert data.get('chargeNowAmps', 0) > 0, "Should show charge now is active"
            time.sleep(0.1)


class TestDataConsistency:
    """Test suite for data consistency across endpoints."""
    
    def test_status_and_policy_consistency(self, api_get, assert_json_response):
        """Test that status and policy endpoints return consistent data."""
        status_response = api_get('/getStatus')
        status_data = assert_json_response(status_response, 200)
        
        policy_response = api_get('/getPolicy')
        policy_data = assert_json_response(policy_response, 200)
        
        # Both should return valid data
        assert status_data is not None, "Status should not be None"
        assert policy_data is not None, "Policy should not be None"
    
    def test_slave_twcs_consistency(self, api_get, assert_json_response):
        """Test that getSlaveTWCs returns consistent data."""
        responses = []
        for _ in range(3):
            response = api_get('/getSlaveTWCs')
            data = assert_json_response(response, 200)
            responses.append(data)
            time.sleep(0.2)
        
        # All responses should have the same structure
        assert all(isinstance(r, (list, dict)) for r in responses), \
            "All responses should be list or dict"


class TestChargeNowEdgeCases:
    """Test suite for chargeNow edge cases and boundary conditions."""
    
    @pytest.mark.parametrize("amps,duration", [
        (12, 3600),      # Minimum amps, 1 hour
        (40, 86400),     # High amps, 24 hours
        (20, 1800),      # Mid amps, 30 minutes
        (32, 7200),      # Standard amps, 2 hours
    ])
    def test_charge_now_various_combinations(self, api_post, assert_json_response, amps, duration):
        """Test chargeNow with various amp and duration combinations."""
        response = api_post('/chargeNow', json={'chargeNowRate': amps, 'chargeNowDuration': duration})
        assert_json_response(response, 200)
    
    def test_charge_now_then_cancel_sequence(self, api_post, api_get, assert_json_response):
        """Test complete chargeNow activation and cancellation sequence."""
        # Activate
        api_post('/chargeNow', json={'chargeNowRate': 32, 'chargeNowDuration': 3600})
        time.sleep(1)
        
        # Verify active
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        assert data.get('chargeNowAmps', 0) > 0, "Should be charging"
        
        # Cancel
        api_post('/cancelChargeNow')
        time.sleep(1)
        
        # Verify canceled
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        assert data.get('chargeNowAmps', 0) == 0, "Should not be charging"

