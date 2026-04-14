"""
API Input Validation Helper Module

Provides centralized validation for API endpoints to ensure:
- Required fields are present
- Values are of correct types
- Values are within acceptable ranges
- Strings are safe (no injection attacks)
"""

import json
import re


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
            return True, data, None
        except (ValueError, UnicodeDecodeError, json.decoder.JSONDecodeError) as e:
            return False, None, f"Invalid JSON: {str(e)}"

    @staticmethod
    def validate_required_fields(data, required_fields):
        """
        Validate that all required fields are present and not None/empty.

        Args:
            data: dict to validate
            required_fields: list of field names that must be present

        Returns: (success: bool, missing_fields: list or None)
        """
        missing = []
        for field in required_fields:
            if field not in data or data[field] is None:
                missing.append(field)

        return len(missing) == 0, missing if missing else None

    @staticmethod
    def validate_integer(value, min_val=None, max_val=None):
        """
        Validate that value is an integer within optional range.

        Returns: (success: bool, int_value: int or None, error_msg: str or None)
        """
        try:
            int_val = int(value)

            if min_val is not None and int_val < min_val:
                return False, None, f"Value must be >= {min_val}"
            if max_val is not None and int_val > max_val:
                return False, None, f"Value must be <= {max_val}"

            return True, int_val, None
        except (ValueError, TypeError):
            return False, None, "Value must be an integer"

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

    @staticmethod
    def validate_string(value, min_length=None, max_length=None, pattern=None):
        """
        Validate that value is a string with optional length and pattern constraints.

        Args:
            value: value to validate
            min_length: minimum string length
            max_length: maximum string length
            pattern: regex pattern to match

        Returns: (success: bool, str_value: str or None, error_msg: str or None)
        """
        if value is None:
            return False, None, "Value cannot be None"

        str_val = str(value)

        if min_length is not None and len(str_val) < min_length:
            return False, None, f"String must be at least {min_length} characters"
        if max_length is not None and len(str_val) > max_length:
            return False, None, f"String must be at most {max_length} characters"
        if pattern is not None and not re.match(pattern, str_val):
            return False, None, f"String does not match required pattern"

        return True, str_val, None

    @staticmethod
    def validate_choice(value, allowed_values):
        """
        Validate that value is one of allowed choices.

        Returns: (success: bool, value: any or None, error_msg: str or None)
        """
        if value not in allowed_values:
            return (
                False,
                None,
                f"Value must be one of: {', '.join(str(v) for v in allowed_values)}",
            )
        return True, value, None

    @staticmethod
    def is_safe_string(value):
        """
        Check if string is safe (no injection attempts).

        Returns: bool
        """
        if not isinstance(value, str):
            return False

        # Check for common injection patterns
        dangerous_patterns = [
            r"['\";].*(?:DROP|DELETE|INSERT|UPDATE|SELECT)",  # SQL injection
            r"<script|javascript:|onerror=|onclick=",  # XSS
            r"\.\./|\.\.\\",  # Path traversal
        ]

        for pattern in dangerous_patterns:
            if re.search(pattern, value, re.IGNORECASE):
                return False

        return True
