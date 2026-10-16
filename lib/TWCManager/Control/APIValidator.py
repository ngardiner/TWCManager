"""
API Input Validation Helper Module

Provides centralized JSON validation for API endpoints.
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
