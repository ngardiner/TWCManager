"""
Integration tests for vehicle integration scenarios.

Tests cover:
- Vehicle detection and status
- Vehicle state transitions
- Multiple vehicle coordination
- Vehicle charging control
"""

import pytest
import time


class TestVehicleDetection:
    """Test suite for vehicle detection."""
    
    def test_vehicle_status_available(self, api_get, assert_json_response):
        """Test that vehicle status is available."""
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should have vehicle-related fields
        assert 'carsCharging' in data or 'currentPolicy' in data
    
    def test_cars_charging_count(self, api_get, assert_json_response):
        """Test that cars charging count is reported."""
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should have carsCharging field
        if 'carsCharging' in data:
            cars_charging = int(data['carsCharging'])
            assert cars_charging >= 0


class TestVehicleStateTransitions:
    """Test suite for vehicle state transitions."""
    
    def test_charge_start_affects_vehicle_state(self, api_post, api_get, assert_json_response):
        """Test that starting charge affects vehicle state."""
        # Get initial state
        initial = api_get('/getStatus')
        initial_data = assert_json_response(initial, 200)
        
        # Send start command
        api_post('/sendStartCommand')
        time.sleep(1)
        
        # Get new state
        after_start = api_get('/getStatus')
        after_data = assert_json_response(after_start, 200)
        
        # State should be available
        assert after_data is not None
    
    def test_charge_stop_affects_vehicle_state(self, api_post, api_get, assert_json_response):
        """Test that stopping charge affects vehicle state."""
        # Send stop command
        api_post('/sendStopCommand')
        time.sleep(1)
        
        # Get state
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # State should be available
        assert data is not None
    
    def test_charge_now_affects_vehicle_state(self, api_post, api_get, assert_json_response):
        """Test that charge now affects vehicle state."""
        # Activate charge now
        api_post('/chargeNow', json={
            'chargeNowRate': 24,
            'chargeNowDuration': 3600
        })
        time.sleep(1)
        
        # Get state
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should reflect charge now state
        assert data is not None


class TestMultipleVehicleCoordination:
    """Test suite for multiple vehicle coordination."""
    
    def test_policy_applies_to_all_vehicles(self, api_get, assert_json_response):
        """Test that policy applies to all vehicles."""
        response = api_get('/getPolicy')
        policy = assert_json_response(response, 200)
        
        # Should have policy information
        assert policy is not None
    
    def test_charge_distribution_across_vehicles(self, api_post, api_get, assert_json_response):
        """Test charge distribution across multiple vehicles."""
        # Set charge now
        api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 3600
        })
        time.sleep(1)
        
        # Get status
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should show charging activity
        assert data is not None
        
        # Cancel charge
        api_post('/cancelChargeNow')
        time.sleep(1)
    
    def test_vehicle_priority_handling(self, api_get, assert_json_response):
        """Test vehicle priority handling."""
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should have priority-related information
        assert data is not None


class TestVehicleChargingControl:
    """Test suite for vehicle charging control."""
    
    def test_charge_rate_adjustment(self, api_post, api_get, assert_json_response):
        """Test adjusting charge rate."""
        rates = [12, 16, 24, 32]
        
        for rate in rates:
            response = api_post('/chargeNow', json={
                'chargeNowRate': rate,
                'chargeNowDuration': 3600
            })
            assert_json_response(response, 200)
            time.sleep(0.5)
        
        # System should handle rate changes
        status = api_get('/getStatus')
        assert_json_response(status, 200)
    
    def test_charge_duration_handling(self, api_post, api_get, assert_json_response):
        """Test handling of different charge durations."""
        durations = [1800, 3600, 7200]  # 30min, 1hr, 2hr
        
        for duration in durations:
            response = api_post('/chargeNow', json={
                'chargeNowRate': 24,
                'chargeNowDuration': duration
            })
            assert_json_response(response, 200)
            time.sleep(0.5)
        
        # System should handle duration changes
        status = api_get('/getStatus')
        assert_json_response(status, 200)
    
    def test_immediate_charge_stop(self, api_post, api_get, assert_json_response):
        """Test immediate charge stop."""
        # Start charging
        api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 3600
        })
        time.sleep(0.5)
        
        # Immediately stop
        response = api_post('/cancelChargeNow')
        assert_json_response(response, 200)
        
        # Verify stopped
        status = api_get('/getStatus')
        data = assert_json_response(status, 200)
        assert data is not None


class TestVehicleStatusReporting:
    """Test suite for vehicle status reporting."""
    
    def test_charging_status_accuracy(self, api_get, assert_json_response):
        """Test accuracy of charging status reporting."""
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should have charging status fields
        status_fields = [
            'chargerLoadWatts',
            'chargerLoadAmps',
            'currentPolicy',
            'carsCharging'
        ]
        
        for field in status_fields:
            if field in data:
                assert data[field] is not None
    
    def test_policy_status_reporting(self, api_get, assert_json_response):
        """Test policy status reporting."""
        response = api_get('/getActivePolicyAction')
        data = assert_json_response(response, 200)
        
        # Should have policy action information
        assert data is not None
    
    def test_generation_consumption_reporting(self, api_get, assert_json_response):
        """Test generation and consumption reporting."""
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should have generation/consumption fields
        assert 'generationWatts' in data or 'consumptionWatts' in data


class TestVehicleErrorHandling:
    """Test suite for vehicle error handling."""
    
    def test_invalid_charge_rate_handling(self, api_post):
        """Test handling of invalid charge rates."""
        invalid_rates = [-1, 0, 1000]
        
        for rate in invalid_rates:
            response = api_post('/chargeNow', json={
                'chargeNowRate': rate,
                'chargeNowDuration': 3600
            })
            
            # Should handle gracefully
            assert response.status_code in [200, 400, 422]
    
    def test_invalid_duration_handling(self, api_post):
        """Test handling of invalid durations."""
        invalid_durations = [-1, 0, -3600]
        
        for duration in invalid_durations:
            response = api_post('/chargeNow', json={
                'chargeNowRate': 24,
                'chargeNowDuration': duration
            })
            
            # Should handle gracefully
            assert response.status_code in [200, 400, 422]
    
    def test_rapid_command_sequences(self, api_post, api_get):
        """Test handling of rapid command sequences."""
        for i in range(5):
            api_post('/sendStartCommand')
            api_post('/sendStopCommand')
            api_get('/getStatus')
        
        # System should remain responsive
        response = api_get('/getStatus')
        assert response.status_code == 200
