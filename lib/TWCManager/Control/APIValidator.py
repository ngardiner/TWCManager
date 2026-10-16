"""
API Input Validation Helper Module

Provides centralized JSON and number validation for API endpoints.
"""

import json


class APIValidator:
    """Centralized validation for API endpoints"""

    @staticmethod
    def validate_json(data_bytes):
        """
        Validate and parse JSON data.

        Returns: (success: bool, data: dict or None, error_msg: str or None)
        """
        try:
            data = json.loads(data_bytes.decode("UTF-8"))
            # Ensure data is a dict, not a list or other type
            if not isinstance(data, dict):
                return False, None, "JSON must be an object, not an array or primitive"
            return True, data, None
        except (ValueError, UnicodeDecodeError, json.decoder.JSONDecodeError) as e:
            return False, None, f"Invalid JSON: {str(e)}"

    @staticmethod
    def validate_float(value, min_val=None, max_val=None):
        """
        Validate that value is a float within optional range.

        Returns: (success: bool, float_value: float or None, error_msg: str or None)
        """
        try:
            float_val = float(value)

            if min_val is not None and float_val < min_val:
                return False, None, f"Value must be >= {min_val}"
            if max_val is not None and float_val > max_val:
                return False, None, f"Value must be <= {max_val}"

            return True, float_val, None
        except (ValueError, TypeError):
            return False, None, "Value must be a number"
