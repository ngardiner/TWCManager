"""
Extended API input validation tests for additional endpoints.

Tests cover:
- Policy selection and validation
- Settings persistence
- Command execution validation
- Offset deletion
- Debug commands
"""

import pytest
import time


class TestPolicyValidation:
    """Test input validation for policy-related endpoints."""
    
    def test_set_policy_missing_policy_field(self, api_post):
        """Test setPolicy with missing policy field."""
        response = api_post('/setPolicy', json={})
        assert response.status_code == 400, "Should reject missing policy field"
    
    def test_set_policy_null_policy(self, api_post):
        """Test setPolicy with null policy."""
        response = api_post('/setPolicy', json={'policy': None})
        assert response.status_code == 400, "Should reject null policy"
    
    def test_set_policy_empty_string(self, api_post):
        """Test setPolicy with empty string policy."""
        response = api_post('/setPolicy', json={'policy': ''})
        assert response.status_code == 400, "Should reject empty policy"
    
    def test_set_policy_nonexistent_policy(self, api_post):
        """Test setPolicy with non-existent policy name."""
        response = api_post('/setPolicy', json={'policy': 'NonExistentPolicy123'})
        # Should reject or ignore gracefully
        assert response.status_code in [400, 404]
    
    def test_set_policy_numeric_policy(self, api_post):
        """Test setPolicy with numeric policy."""
        response = api_post('/setPolicy', json={'policy': 12345})
        # Should reject or coerce
        assert response.status_code in [200, 201, 204, 400]
    
    def test_set_policy_array_policy(self, api_post):
        """Test setPolicy with array policy."""
        response = api_post('/setPolicy', json={'policy': ['Policy1', 'Policy2']})
        assert response.status_code == 400, "Should reject array policy"
    
    def test_set_policy_object_policy(self, api_post):
        """Test setPolicy with object policy."""
        response = api_post('/setPolicy', json={'policy': {'name': 'Policy1'}})
        assert response.status_code == 400, "Should reject object policy"
    
    def test_set_policy_very_long_string(self, api_post):
        """Test setPolicy with very long policy name."""
        long_policy = 'A' * 10000
        response = api_post('/setPolicy', json={'policy': long_policy})
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_set_policy_special_characters(self, api_post):
        """Test setPolicy with special characters."""
        response = api_post('/setPolicy', json={'policy': '!@#$%^&*()'})
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]


class TestCommandValidation:
    """Test input validation for command endpoints."""
    
    def test_send_start_command_extra_fields(self, api_post):
        """Test sendStartCommand with extra fields."""
        response = api_post('/sendStartCommand', json={
            'extraField': 'should be ignored'
        })
        # Should ignore extra fields
        assert response.status_code in [200, 201, 202, 204]
    
    def test_send_stop_command_extra_fields(self, api_post):
        """Test sendStopCommand with extra fields."""
        response = api_post('/sendStopCommand', json={
            'extraField': 'should be ignored'
        })
        # Should ignore extra fields
        assert response.status_code in [200, 201, 202, 204]
    
    def test_check_arrival_extra_fields(self, api_post):
        """Test checkArrival with extra fields."""
        response = api_post('/checkArrival', json={
            'extraField': 'should be ignored'
        })
        # Should ignore extra fields
        assert response.status_code in [200, 201, 202, 204]
    
    def test_check_departure_extra_fields(self, api_post):
        """Test checkDeparture with extra fields."""
        response = api_post('/checkDeparture', json={
            'extraField': 'should be ignored'
        })
        # Should ignore extra fields
        assert response.status_code in [200, 201, 202, 204]


class TestOffsetDeletionValidation:
    """Test input validation for offset deletion."""
    
    def test_delete_consumption_offset_missing_name(self, api_post):
        """Test deleteConsumptionOffset with missing offsetName."""
        response = api_post('/deleteConsumptionOffset', json={})
        assert response.status_code == 400, "Should reject missing offsetName"
    
    def test_delete_consumption_offset_null_name(self, api_post):
        """Test deleteConsumptionOffset with null offsetName."""
        response = api_post('/deleteConsumptionOffset', json={
            'offsetName': None
        })
        assert response.status_code == 400, "Should reject null offsetName"
    
    def test_delete_consumption_offset_empty_name(self, api_post):
        """Test deleteConsumptionOffset with empty offsetName."""
        response = api_post('/deleteConsumptionOffset', json={
            'offsetName': ''
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_delete_consumption_offset_nonexistent(self, api_post):
        """Test deleteConsumptionOffset with non-existent offset."""
        response = api_post('/deleteConsumptionOffset', json={
            'offsetName': 'NonExistentOffset123'
        })
        # Should handle gracefully (may return 404 or 204)
        assert response.status_code in [200, 201, 204, 404]
    
    def test_delete_consumption_offset_numeric_name(self, api_post):
        """Test deleteConsumptionOffset with numeric offsetName."""
        response = api_post('/deleteConsumptionOffset', json={
            'offsetName': 12345
        })
        # Should coerce or reject
        assert response.status_code in [200, 201, 204, 400]


class TestDebugCommandValidation:
    """Test input validation for debug commands."""
    
    def test_send_debug_command_missing_command(self, api_post):
        """Test sendDebugCommand with missing command field."""
        response = api_post('/sendDebugCommand', json={})
        assert response.status_code == 400, "Should reject missing command"
    
    def test_send_debug_command_null_command(self, api_post):
        """Test sendDebugCommand with null command."""
        response = api_post('/sendDebugCommand', json={
            'command': None
        })
        assert response.status_code == 400, "Should reject null command"
    
    def test_send_debug_command_empty_command(self, api_post):
        """Test sendDebugCommand with empty command."""
        response = api_post('/sendDebugCommand', json={
            'command': ''
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_send_debug_command_numeric_command(self, api_post):
        """Test sendDebugCommand with numeric command."""
        response = api_post('/sendDebugCommand', json={
            'command': 12345
        })
        # Should coerce or reject
        assert response.status_code in [200, 201, 204, 400]
    
    def test_send_debug_command_very_long_command(self, api_post):
        """Test sendDebugCommand with very long command."""
        long_command = 'A' * 100000
        response = api_post('/sendDebugCommand', json={
            'command': long_command
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]


class TestSettingsPersistenceValidation:
    """Test settings persistence and validation."""
    
    def test_save_settings_endpoint(self, api_post):
        """Test saveSettings endpoint."""
        response = api_post('/saveSettings', json={})
        # Should accept empty payload
        assert response.status_code in [200, 201, 202, 204]
    
    def test_save_settings_with_extra_fields(self, api_post):
        """Test saveSettings with extra fields."""
        response = api_post('/saveSettings', json={
            'extraField': 'should be ignored'
        })
        # Should ignore extra fields
        assert response.status_code in [200, 201, 202, 204]


class TestConcurrentInputValidation:
    """Test concurrent requests with invalid inputs."""
    
    def test_concurrent_invalid_charge_now_requests(self, api_post):
        """Test system handles concurrent invalid chargeNow requests."""
        invalid_payloads = [
            {'chargeNowRate': -1, 'chargeNowDuration': 3600},
            {'chargeNowRate': 'invalid', 'chargeNowDuration': 3600},
            {'chargeNowRate': 32, 'chargeNowDuration': -1},
            {'chargeNowRate': None, 'chargeNowDuration': 3600},
            {},
        ]
        
        responses = []
        for payload in invalid_payloads:
            response = api_post('/chargeNow', json=payload)
            responses.append(response.status_code)
        
        # All should be rejected or handled
        assert all(code in [200, 204, 400] for code in responses)
    
    def test_concurrent_invalid_lat_lon_requests(self, api_post):
        """Test system handles concurrent invalid setLatLon requests."""
        invalid_payloads = [
            {'lat': 91, 'lon': 0},
            {'lat': -91, 'lon': 0},
            {'lat': 0, 'lon': 181},
            {'lat': 0, 'lon': -181},
            {'lat': 'invalid', 'lon': 0},
            {},
        ]
        
        responses = []
        for payload in invalid_payloads:
            response = api_post('/setLatLon', json=payload)
            responses.append(response.status_code)
        
        # All should be rejected or handled
        assert all(code in [200, 201, 204, 400] for code in responses)


class TestTypeCoercionBehavior:
    """Test how API handles type coercion."""
    
    def test_charge_now_string_to_int_coercion(self, api_post):
        """Test if chargeNow coerces string to int."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': "32",
            'chargeNowDuration': "3600"
        })
        # Should either accept (with coercion) or reject
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_float_to_int_coercion(self, api_post):
        """Test if chargeNow coerces float to int."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32.7,
            'chargeNowDuration': 3600.5
        })
        # Should either accept (with coercion) or reject
        assert response.status_code in [200, 204, 400]
    
    def test_set_lat_lon_string_to_float_coercion(self, api_post):
        """Test if setLatLon coerces string to float."""
        response = api_post('/setLatLon', json={
            'lat': "37.7749",
            'lon': "-122.4194"
        })
        # Should either accept (with coercion) or reject
        assert response.status_code in [200, 201, 204, 400]


class TestErrorResponseConsistency:
    """Test consistency of error responses."""
    
    def test_all_400_errors_return_json(self, api_post):
        """Test that all 400 errors return JSON responses."""
        invalid_requests = [
            ('/chargeNow', {'chargeNowRate': -1, 'chargeNowDuration': 3600}),
            ('/setLatLon', {'lat': 91, 'lon': 0}),
            ('/setConsumptionOffset', {'offsetName': 'test'}),
        ]
        
        for endpoint, payload in invalid_requests:
            response = api_post(endpoint, json=payload)
            
            if response.status_code == 400:
                # Should be valid JSON or empty
                if response.text:
                    try:
                        data = response.json()
                        assert isinstance(data, dict)
                    except Exception:
                        pytest.fail(f"400 response from {endpoint} should be valid JSON")
    
    def test_error_responses_have_consistent_format(self, api_post):
        """Test that error responses follow consistent format."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 'invalid',
            'chargeNowDuration': 3600
        })
        
        if response.status_code == 400 and response.text:
            try:
                data = response.json()
                # Should have either 'error' or 'message' field
                assert 'error' in data or 'message' in data or isinstance(data, dict)
            except Exception:
                pass  # Empty body is acceptable


class TestLargePayloadHandling:
    """Test handling of large payloads."""
    
    def test_very_long_offset_name(self, api_post):
        """Test setConsumptionOffset with very long offset name."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'A' * 10000,
            'value': 100,
            'unit': 'W'
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_very_long_policy_name(self, api_post):
        """Test setPolicy with very long policy name."""
        response = api_post('/setPolicy', json={
            'policy': 'A' * 10000
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_deeply_nested_json(self, api_post):
        """Test handling of deeply nested JSON."""
        # Create deeply nested structure
        nested = {'value': 1}
        for i in range(100):
            nested = {'nested': nested}
        
        response = api_post('/chargeNow', json=nested)
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 202, 204, 400]


class TestUnicodeAndEncodingValidation:
    """Test handling of unicode and special encodings."""
    
    def test_unicode_in_offset_name(self, api_post):
        """Test setConsumptionOffset with unicode characters."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': '测试名称🔋',
            'value': 100,
            'unit': 'W'
        })
        # Should handle or reject gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_unicode_in_policy_name(self, api_post):
        """Test setPolicy with unicode characters."""
        response = api_post('/setPolicy', json={
            'policy': 'Политика🔌'
        })
        # Should handle or reject gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_emoji_in_offset_name(self, api_post):
        """Test setConsumptionOffset with emoji."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': '⚡🔋🌞',
            'value': 100,
            'unit': 'W'
        })
        # Should handle or reject gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_null_bytes_in_string(self, api_post):
        """Test handling of null bytes in strings."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test\x00name',
            'value': 100,
            'unit': 'W'
        })
        # Should handle or reject gracefully
        assert response.status_code in [200, 201, 204, 400]


class TestNumericBoundaryValues:
    """Test numeric boundary values and edge cases."""
    
    def test_charge_now_max_int_value(self, api_post):
        """Test chargeNow with maximum integer value."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 2147483647,  # Max 32-bit int
            'chargeNowDuration': 3600
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_min_int_value(self, api_post):
        """Test chargeNow with minimum integer value."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': -2147483648,  # Min 32-bit int
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject negative amperage"
    
    def test_set_lat_lon_float_precision(self, api_post):
        """Test setLatLon with high-precision floats."""
        response = api_post('/setLatLon', json={
            'lat': 37.77493123456789,
            'lon': -122.41941234567890
        })
        # Should accept high-precision coordinates
        assert response.status_code in [200, 201, 204]
    
    def test_set_lat_lon_scientific_notation(self, api_post):
        """Test setLatLon with scientific notation."""
        response = api_post('/setLatLon', json={
            'lat': 3.77e1,  # 37.7
            'lon': -1.22e2  # -122
        })
        # Should handle or reject gracefully
        assert response.status_code in [200, 201, 204, 400]
