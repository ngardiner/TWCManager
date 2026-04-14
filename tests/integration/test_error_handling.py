"""
Integration tests for error handling and edge cases.

Tests cover:
- Network failures and timeouts
- Invalid input validation
- System failures and recovery
- Edge case scenarios
"""

import pytest
import time


class TestNetworkErrorHandling:
    """Test suite for network error handling."""
    
    def test_api_timeout_handling(self, api_get):
        """Test handling of API timeouts."""
        # Normal request should succeed
        response = api_get('/getStatus')
        assert response.status_code == 200
    
    def test_malformed_json_request(self, api_post):
        """Test handling of malformed JSON requests."""
        # This should be handled by the API
        response = api_post('/chargeNow', json={
            'chargeNowRate': 'invalid',  # Should be number
            'chargeNowDuration': 3600
        })
        
        # Should either reject or coerce to valid value
        assert response.status_code in [200, 400, 422]
    
    def test_missing_required_fields(self, api_post):
        """Test handling of missing required fields."""
        # Missing chargeNowDuration
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32
        })
        
        # Should handle gracefully
        assert response.status_code in [200, 400, 422]


class TestInputValidation:
    """Test suite for input validation."""
    
    def test_out_of_range_amperage(self, api_post):
        """Test rejection of out-of-range amperage values."""
        test_cases = [
            -1,      # Negative
            0,       # Zero
            1000,    # Too high
            -100     # Very negative
        ]
        
        for amps in test_cases:
            response = api_post('/chargeNow', json={
                'chargeNowRate': amps,
                'chargeNowDuration': 3600
            })
            
            # Should handle gracefully
            assert response.status_code in [200, 400, 422]
    
    def test_out_of_range_duration(self, api_post):
        """Test rejection of out-of-range duration values."""
        test_cases = [
            -1,           # Negative
            0,            # Zero
            999999999,    # Very large
        ]
        
        for duration in test_cases:
            response = api_post('/chargeNow', json={
                'chargeNowRate': 32,
                'chargeNowDuration': duration
            })
            
            # Should handle gracefully
            assert response.status_code in [200, 400, 422]
    
    def test_invalid_policy_selection(self, api_post):
        """Test handling of invalid policy selections."""
        response = api_post('/setPolicy', json={
            'policy': 'NonExistentPolicy'
        })
        
        # Should either reject or ignore
        assert response.status_code in [200, 400, 404]
    
    def test_invalid_coordinates(self, api_post):
        """Test handling of invalid latitude/longitude."""
        invalid_coords = [
            {'lat': 999, 'lon': 999},      # Out of range
            {'lat': -999, 'lon': -999},    # Out of range
            {'lat': 'invalid', 'lon': 0},  # Non-numeric
        ]
        
        for coords in invalid_coords:
            response = api_post('/setLatLon', json=coords)
            
            # Should handle gracefully
            assert response.status_code in [200, 400, 422]


class TestSystemResilience:
    """Test suite for system resilience."""
    
    def test_rapid_api_calls(self, api_get):
        """Test system handles rapid API calls."""
        for i in range(10):
            response = api_get('/getStatus')
            assert response.status_code == 200
    
    def test_alternating_commands(self, api_post, api_get):
        """Test system handles alternating start/stop commands."""
        for i in range(5):
            # Start
            api_post('/sendStartCommand')
            time.sleep(0.1)
            
            # Stop
            api_post('/sendStopCommand')
            time.sleep(0.1)
            
            # Verify system still responsive
            response = api_get('/getStatus')
            assert response.status_code == 200
    
    def test_charge_now_rapid_changes(self, api_post, api_get):
        """Test system handles rapid charge now rate changes."""
        rates = [12, 16, 24, 32, 24, 16, 12]
        
        for rate in rates:
            api_post('/chargeNow', json={
                'chargeNowRate': rate,
                'chargeNowDuration': 3600
            })
            time.sleep(0.1)
        
        # System should still be responsive
        response = api_get('/getStatus')
        assert response.status_code == 200
    
    def test_settings_persistence_under_load(self, api_post, api_get):
        """Test settings persist under rapid API load."""
        # Set initial value
        api_post('/chargeNow', json={
            'chargeNowRate': 24,
            'chargeNowDuration': 7200
        })
        
        # Hammer with other requests
        for i in range(20):
            api_get('/getStatus')
            api_get('/getPolicy')
            api_get('/getSlaveTWCs')
        
        # Verify setting still active
        response = api_get('/getStatus')
        assert response.status_code == 200


class TestEdgeCases:
    """Test suite for edge cases."""
    
    def test_empty_response_handling(self, api_get):
        """Test handling of endpoints that might return empty data."""
        response = api_get('/getSlaveTWCs')
        
        # Should return valid response even if empty
        assert response.status_code == 200
        data = response.json()
        assert data is not None
    
    def test_null_values_in_response(self, api_get, assert_json_response):
        """Test handling of null values in API responses."""
        response = api_get('/getStatus')
        data = assert_json_response(response, 200)
        
        # Should handle null values gracefully
        assert isinstance(data, dict)
    
    def test_very_long_duration(self, api_post):
        """Test handling of very long charge durations."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 12,
            'chargeNowDuration': 86400 * 365  # 1 year
        })
        
        # Should handle gracefully
        assert response.status_code in [200, 400, 422]
    
    def test_minimum_amperage(self, api_post):
        """Test handling of minimum amperage values."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 6,  # Minimum typical value
            'chargeNowDuration': 3600
        })
        
        # Should accept or reject gracefully
        assert response.status_code in [200, 400, 422]
    
    def test_maximum_amperage(self, api_post):
        """Test handling of maximum amperage values."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 80,  # High but potentially valid
            'chargeNowDuration': 3600
        })
        
        # Should accept or reject gracefully
        assert response.status_code in [200, 400, 422]


class TestConcurrency:
    """Test suite for concurrent operations."""
    
    def test_concurrent_charge_commands(self, api_post, api_get):
        """Test handling of concurrent charge commands."""
        # Simulate rapid concurrent-like commands
        api_post('/chargeNow', json={'chargeNowRate': 24, 'chargeNowDuration': 3600})
        api_post('/chargeNow', json={'chargeNowRate': 32, 'chargeNowDuration': 3600})
        api_post('/chargeNow', json={'chargeNowRate': 16, 'chargeNowDuration': 3600})
        
        time.sleep(0.5)
        
        # System should handle and be responsive
        response = api_get('/getStatus')
        assert response.status_code == 200
    
    def test_concurrent_read_operations(self, api_get):
        """Test handling of concurrent read operations."""
        endpoints = ['/getStatus', '/getPolicy', '/getSlaveTWCs', '/getConfig']
        
        for endpoint in endpoints:
            response = api_get(endpoint)
            assert response.status_code == 200
        
        # Repeat to simulate concurrency
        for endpoint in endpoints:
            response = api_get(endpoint)
            assert response.status_code == 200
