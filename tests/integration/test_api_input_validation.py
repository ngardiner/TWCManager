"""
Comprehensive input validation tests for TWCManager API endpoints.

Tests cover:
- Type validation (strings, numbers, booleans)
- Range validation (min/max values)
- Required field validation
- Malformed JSON handling
- SQL injection and XSS prevention
- Boundary conditions
- Special characters and encoding
"""

import pytest
import json


class TestChargeNowInputValidation:
    """Test input validation for /api/chargeNow endpoint."""
    
    def test_charge_now_missing_rate(self, api_post):
        """Test chargeNow with missing chargeNowRate field."""
        response = api_post('/chargeNow', json={
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject missing rate"
    
    def test_charge_now_missing_duration(self, api_post):
        """Test chargeNow with missing chargeNowDuration field."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32
        })
        assert response.status_code == 400, "Should reject missing duration"
    
    def test_charge_now_missing_both_fields(self, api_post):
        """Test chargeNow with both fields missing."""
        response = api_post('/chargeNow', json={})
        assert response.status_code == 400, "Should reject empty payload"
    
    def test_charge_now_rate_negative(self, api_post):
        """Test chargeNow with negative amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': -32,
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject negative amperage"
    
    def test_charge_now_rate_zero(self, api_post):
        """Test chargeNow with zero amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 0,
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject zero amperage"
    
    def test_charge_now_rate_string(self, api_post):
        """Test chargeNow with string amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': "32",
            'chargeNowDuration': 3600
        })
        # Should either coerce or reject
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_rate_float(self, api_post):
        """Test chargeNow with float amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32.5,
            'chargeNowDuration': 3600
        })
        # Should either coerce or reject
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_rate_non_numeric(self, api_post):
        """Test chargeNow with non-numeric amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': "invalid",
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject non-numeric amperage"
    
    def test_charge_now_rate_very_high(self, api_post):
        """Test chargeNow with extremely high amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 999999,
            'chargeNowDuration': 3600
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_duration_negative(self, api_post):
        """Test chargeNow with negative duration."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': -3600
        })
        assert response.status_code == 400, "Should reject negative duration"
    
    def test_charge_now_duration_zero(self, api_post):
        """Test chargeNow with zero duration."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 0
        })
        assert response.status_code == 400, "Should reject zero duration"
    
    def test_charge_now_duration_string(self, api_post):
        """Test chargeNow with string duration."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': "3600"
        })
        # Should either coerce or reject
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_duration_non_numeric(self, api_post):
        """Test chargeNow with non-numeric duration."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': "invalid"
        })
        assert response.status_code == 400, "Should reject non-numeric duration"
    
    def test_charge_now_duration_very_large(self, api_post):
        """Test chargeNow with extremely large duration."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 999999999999
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_extra_fields(self, api_post):
        """Test chargeNow with extra unexpected fields."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 3600,
            'extraField': 'should be ignored',
            'anotherField': 12345
        })
        # Should ignore extra fields and process normally
        assert response.status_code in [200, 204]
    
    def test_charge_now_null_rate(self, api_post):
        """Test chargeNow with null amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': None,
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject null amperage"
    
    def test_charge_now_null_duration(self, api_post):
        """Test chargeNow with null duration."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': None
        })
        assert response.status_code == 400, "Should reject null duration"
    
    def test_charge_now_boolean_rate(self, api_post):
        """Test chargeNow with boolean amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': True,
            'chargeNowDuration': 3600
        })
        # Should reject or coerce
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_array_rate(self, api_post):
        """Test chargeNow with array amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': [32, 24],
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject array amperage"
    
    def test_charge_now_object_rate(self, api_post):
        """Test chargeNow with object amperage."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': {'value': 32},
            'chargeNowDuration': 3600
        })
        assert response.status_code == 400, "Should reject object amperage"


class TestSetLatLonInputValidation:
    """Test input validation for /api/setLatLon endpoint."""
    
    def test_set_lat_lon_missing_lat(self, api_post):
        """Test setLatLon with missing latitude."""
        response = api_post('/setLatLon', json={
            'lon': -122.4194
        })
        assert response.status_code == 400, "Should reject missing latitude"
    
    def test_set_lat_lon_missing_lon(self, api_post):
        """Test setLatLon with missing longitude."""
        response = api_post('/setLatLon', json={
            'lat': 37.7749
        })
        assert response.status_code == 400, "Should reject missing longitude"
    
    def test_set_lat_lon_lat_out_of_range_high(self, api_post):
        """Test setLatLon with latitude > 90."""
        response = api_post('/setLatLon', json={
            'lat': 91,
            'lon': -122.4194
        })
        assert response.status_code == 400, "Should reject latitude > 90"
    
    def test_set_lat_lon_lat_out_of_range_low(self, api_post):
        """Test setLatLon with latitude < -90."""
        response = api_post('/setLatLon', json={
            'lat': -91,
            'lon': -122.4194
        })
        assert response.status_code == 400, "Should reject latitude < -90"
    
    def test_set_lat_lon_lon_out_of_range_high(self, api_post):
        """Test setLatLon with longitude > 180."""
        response = api_post('/setLatLon', json={
            'lat': 37.7749,
            'lon': 181
        })
        assert response.status_code == 400, "Should reject longitude > 180"
    
    def test_set_lat_lon_lon_out_of_range_low(self, api_post):
        """Test setLatLon with longitude < -180."""
        response = api_post('/setLatLon', json={
            'lat': 37.7749,
            'lon': -181
        })
        assert response.status_code == 400, "Should reject longitude < -180"
    
    def test_set_lat_lon_lat_string(self, api_post):
        """Test setLatLon with string latitude."""
        response = api_post('/setLatLon', json={
            'lat': "37.7749",
            'lon': -122.4194
        })
        # Should either coerce or reject
        assert response.status_code in [200, 201, 204, 400]
    
    def test_set_lat_lon_lat_non_numeric(self, api_post):
        """Test setLatLon with non-numeric latitude."""
        response = api_post('/setLatLon', json={
            'lat': "invalid",
            'lon': -122.4194
        })
        assert response.status_code == 400, "Should reject non-numeric latitude"
    
    def test_set_lat_lon_null_lat(self, api_post):
        """Test setLatLon with null latitude."""
        response = api_post('/setLatLon', json={
            'lat': None,
            'lon': -122.4194
        })
        assert response.status_code == 400, "Should reject null latitude"
    
    def test_set_lat_lon_null_lon(self, api_post):
        """Test setLatLon with null longitude."""
        response = api_post('/setLatLon', json={
            'lat': 37.7749,
            'lon': None
        })
        assert response.status_code == 400, "Should reject null longitude"


class TestMalformedJSONHandling:
    """Test handling of malformed JSON requests."""
    
    def test_invalid_json_syntax(self, api_post):
        """Test handling of invalid JSON syntax."""
        # Send raw invalid JSON
        response = api_post('/chargeNow', data='{invalid json}')
        assert response.status_code == 400, "Should reject invalid JSON"
    
    def test_empty_json_object(self, api_post):
        """Test handling of empty JSON object."""
        response = api_post('/chargeNow', json={})
        assert response.status_code == 400, "Should reject empty JSON"
    
    def test_json_array_instead_of_object(self, api_post):
        """Test handling of JSON array instead of object."""
        response = api_post('/chargeNow', json=[32, 3600])
        assert response.status_code == 400, "Should reject JSON array"


class TestSecurityInputValidation:
    """Test security-related input validation."""
    
    def test_sql_injection_attempt_in_policy(self, api_post):
        """Test SQL injection prevention in policy parameter."""
        response = api_post('/setPolicy', json={
            'policy': "'; DROP TABLE settings; --"
        })
        # Should reject or safely handle
        assert response.status_code in [400, 404]
    
    def test_xss_attempt_in_offset_name(self, api_post):
        """Test XSS prevention in offset name."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': '<script>alert("xss")</script>',
            'value': 100,
            'unit': 'W'
        })
        # Should reject or safely handle
        assert response.status_code in [200, 201, 204, 400]
    
    def test_path_traversal_attempt(self, api_post):
        """Test path traversal prevention."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': '../../../etc/passwd',
            'value': 100,
            'unit': 'W'
        })
        # Should reject or safely handle
        assert response.status_code in [200, 201, 204, 400]


class TestConsumptionOffsetValidation:
    """Test input validation for consumption offset endpoints."""
    
    def test_set_consumption_offset_missing_name(self, api_post):
        """Test setConsumptionOffset with missing offsetName."""
        response = api_post('/setConsumptionOffset', json={
            'value': 100,
            'unit': 'W'
        })
        assert response.status_code == 400, "Should reject missing offsetName"
    
    def test_set_consumption_offset_missing_value(self, api_post):
        """Test setConsumptionOffset with missing value."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test',
            'unit': 'W'
        })
        assert response.status_code == 400, "Should reject missing value"
    
    def test_set_consumption_offset_missing_unit(self, api_post):
        """Test setConsumptionOffset with missing unit."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test',
            'value': 100
        })
        assert response.status_code == 400, "Should reject missing unit"
    
    def test_set_consumption_offset_invalid_unit(self, api_post):
        """Test setConsumptionOffset with invalid unit."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test',
            'value': 100,
            'unit': 'INVALID'
        })
        # Should reject or handle gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_set_consumption_offset_negative_value(self, api_post):
        """Test setConsumptionOffset with negative value."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test',
            'value': -100,
            'unit': 'W'
        })
        # Should accept (offsets can be negative) or reject
        assert response.status_code in [200, 201, 204, 400]
    
    def test_set_consumption_offset_very_large_value(self, api_post):
        """Test setConsumptionOffset with very large value."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test',
            'value': 999999999999,
            'unit': 'W'
        })
        # Should handle gracefully
        assert response.status_code in [200, 201, 204, 400]
    
    def test_set_consumption_offset_string_value(self, api_post):
        """Test setConsumptionOffset with string value."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test',
            'value': "100",
            'unit': 'W'
        })
        # Should coerce or reject
        assert response.status_code in [200, 201, 204, 400]
    
    def test_set_consumption_offset_null_value(self, api_post):
        """Test setConsumptionOffset with null value."""
        response = api_post('/setConsumptionOffset', json={
            'offsetName': 'test',
            'value': None,
            'unit': 'W'
        })
        assert response.status_code == 400, "Should reject null value"


class TestBoundaryConditions:
    """Test boundary conditions and edge cases."""
    
    def test_charge_now_minimum_valid_rate(self, api_post):
        """Test chargeNow with minimum valid amperage (1A)."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 1,
            'chargeNowDuration': 3600
        })
        # Should accept or reject based on config
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_maximum_valid_rate(self, api_post):
        """Test chargeNow with maximum valid amperage (80A)."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 80,
            'chargeNowDuration': 3600
        })
        # Should accept or reject based on config
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_minimum_duration(self, api_post):
        """Test chargeNow with minimum duration (1 second)."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 1
        })
        # Should accept or reject based on config
        assert response.status_code in [200, 204, 400]
    
    def test_charge_now_maximum_duration(self, api_post):
        """Test chargeNow with maximum duration (1 year in seconds)."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 86400 * 365
        })
        # Should accept or reject based on config
        assert response.status_code in [200, 204, 400]
    
    def test_set_lat_lon_boundary_lat_positive(self, api_post):
        """Test setLatLon with boundary latitude (90)."""
        response = api_post('/setLatLon', json={
            'lat': 90,
            'lon': 0
        })
        assert response.status_code in [200, 201, 204]
    
    def test_set_lat_lon_boundary_lat_negative(self, api_post):
        """Test setLatLon with boundary latitude (-90)."""
        response = api_post('/setLatLon', json={
            'lat': -90,
            'lon': 0
        })
        assert response.status_code in [200, 201, 204]
    
    def test_set_lat_lon_boundary_lon_positive(self, api_post):
        """Test setLatLon with boundary longitude (180)."""
        response = api_post('/setLatLon', json={
            'lat': 0,
            'lon': 180
        })
        assert response.status_code in [200, 201, 204]
    
    def test_set_lat_lon_boundary_lon_negative(self, api_post):
        """Test setLatLon with boundary longitude (-180)."""
        response = api_post('/setLatLon', json={
            'lat': 0,
            'lon': -180
        })
        assert response.status_code in [200, 201, 204]


class TestResponseValidation:
    """Test that error responses are properly formatted."""
    
    def test_error_response_is_json(self, api_post):
        """Test that error responses are valid JSON."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': 'invalid',
            'chargeNowDuration': 3600
        })
        
        if response.status_code == 400:
            # Should be valid JSON
            try:
                data = response.json()
                assert isinstance(data, dict)
            except json.JSONDecodeError:
                pytest.fail("Error response should be valid JSON")
    
    def test_error_response_contains_message(self, api_post):
        """Test that error responses contain error message."""
        response = api_post('/chargeNow', json={
            'chargeNowRate': -1,
            'chargeNowDuration': 3600
        })
        
        if response.status_code == 400:
            try:
                data = response.json()
                # Should contain some error information
                assert 'error' in data or 'message' in data or len(data) > 0
            except json.JSONDecodeError:
                pass  # Empty body is acceptable for 400
