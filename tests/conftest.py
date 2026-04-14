"""
Pytest configuration and shared fixtures for TWCManager tests.

This file provides common fixtures and configuration for all tests.
"""

import json
import os
import pytest
import requests
import subprocess
import time
from pathlib import Path

# Scenario runner fixtures — imported so pytest auto-discovers them
from tests.scenario_runner import run_scenario  # noqa: F401

# Test configuration
TEST_API_BASE_URL = "http://127.0.0.1:8088/api"
TEST_CONFIG_PATH = (
    Path(__file__).parent.parent / "etc" / "twcmanager" / ".testconfig.json"
)
TEST_SCENARIOS_PATH = Path(__file__).parent / "fixtures" / "twc_scenarios.json"


@pytest.fixture(scope="session")
def test_scenarios():
    """Load TWC test scenarios from configuration file."""
    with open(TEST_SCENARIOS_PATH, "r") as f:
        return json.load(f)


@pytest.fixture(scope="session")
def twcmanager_config():
    """Load TWCManager test configuration."""
    with open(TEST_CONFIG_PATH, "r") as f:
        # Remove comments from JSON (not standard but used in config)
        content = "\n".join(line for line in f if not line.strip().startswith("#"))
        return json.loads(content)


@pytest.fixture(scope="session")
def api_client():
    """
    HTTP client for API testing with automatic retry logic.

    Returns a requests.Session configured for testing.
    """
    session = requests.Session()
    session.trust_env = False  # Disable proxy settings

    # Add retry logic
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry

    retry_strategy = Retry(
        total=3,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    return session


@pytest.fixture(scope="session")
def wait_for_twcmanager(api_client):
    """
    Wait for TWCManager to be ready before running tests.

    Polls the /api/getStatus endpoint with detailed logging.
    Provides comprehensive diagnostics on failure.
    """
    max_wait = 120
    start_time = time.time()
    attempt = 0
    last_error = None
    last_response = None

    print(f"\n{'='*70}")
    print(f"Health Check: Waiting for TWCManager at {TEST_API_BASE_URL}")
    print(f"{'='*70}")

    while time.time() - start_time < max_wait:
        attempt += 1
        elapsed = time.time() - start_time
        
        try:
            response = api_client.get(f"{TEST_API_BASE_URL}/getStatus", timeout=5)
            last_response = response
            
            if response.status_code == 200:
                print(f"\n✓ SUCCESS: TWCManager ready after {elapsed:.1f}s (attempt {attempt})")
                print(f"Response preview: {str(response.json())[:100]}...")
                print(f"{'='*70}\n")
                return True
            else:
                last_error = f"HTTP {response.status_code}"
                if attempt % 5 == 0:
                    print(f"  [{elapsed:6.1f}s] Attempt {attempt:3d}: {last_error}")
                    
        except requests.ConnectionError as e:
            last_error = f"Connection refused"
            if attempt % 5 == 0:
                print(f"  [{elapsed:6.1f}s] Attempt {attempt:3d}: {last_error} - {str(e)[:40]}")
                
        except requests.Timeout:
            last_error = "Request timeout (5s)"
            if attempt % 5 == 0:
                print(f"  [{elapsed:6.1f}s] Attempt {attempt:3d}: {last_error}")
                
        except Exception as e:
            last_error = f"{type(e).__name__}: {str(e)[:40]}"
            if attempt % 5 == 0:
                print(f"  [{elapsed:6.1f}s] Attempt {attempt:3d}: {last_error}")
        
        # Log progress every 10 attempts with system state
        if attempt % 10 == 0:
            print(f"  [{elapsed:6.1f}s] Attempt {attempt:3d}: Still waiting... (last error: {last_error})")
            import subprocess
            try:
                result = subprocess.run(['ps', 'aux'], capture_output=True, text=True, timeout=2)
                twcm_lines = [l for l in result.stdout.split('\n') if 'twcmanager' in l.lower() or 'python' in l.lower()]
                if twcm_lines:
                    print(f"             Processes: {len(twcm_lines)} matching processes found")
                else:
                    print(f"             Processes: NO matching processes found!")
            except Exception as e:
                print(f"             Processes: Could not check - {e}")
        
        time.sleep(1)

    # Provide detailed failure diagnostics
    elapsed = time.time() - start_time
    error_msg = (
        f"\n{'='*70}\n"
        f"FAILURE: TWCManager did not become ready within {max_wait}s\n"
        f"{'='*70}\n"
        f"Total attempts: {attempt}\n"
        f"Elapsed time: {elapsed:.1f}s\n"
        f"Last error: {last_error}\n"
        f"Last response status: {last_response.status_code if last_response else 'None'}\n"
        f"Endpoint: {TEST_API_BASE_URL}\n"
        f"\nDiagnostics:\n"
        f"  - Check if TWCManager process is running\n"
        f"  - Check if port 8088 is listening\n"
        f"  - Check TWCManager startup logs in /tmp/twcmanager/twcmanager.log\n"
        f"  - Check if MariaDB is running on port 3306\n"
        f"  - Check if all required modules loaded successfully\n"
        f"{'='*70}\n"
    )
    pytest.fail(error_msg)


@pytest.fixture
def api_get(api_client, wait_for_twcmanager):
    """
    Fixture for making GET requests to the API.

    Usage:
        def test_something(api_get):
            response = api_get('/getStatus')
            assert response.status_code == 200
    """

    def _get(endpoint, **kwargs):
        url = f"{TEST_API_BASE_URL}{endpoint}"
        return api_client.get(url, timeout=kwargs.pop("timeout", 30), **kwargs)

    return _get


@pytest.fixture
def api_post(api_client, wait_for_twcmanager):
    """
    Fixture for making POST requests to the API.

    Usage:
        def test_something(api_post):
            response = api_post('/chargeNow', json={'amps': 32})
            assert response.status_code == 200
    """

    def _post(endpoint, **kwargs):
        url = f"{TEST_API_BASE_URL}{endpoint}"
        return api_client.post(url, timeout=kwargs.pop("timeout", 30), **kwargs)

    return _post


@pytest.fixture
def dummy_twc_scenario(test_scenarios):
    """
    Configure the Dummy TWC interface with a specific scenario.

    This fixture allows tests to specify which TWC scenario to use.

    Usage:
        def test_multi_slave(dummy_twc_scenario):
            scenario = dummy_twc_scenario('multi_slave_balanced')
            # Test with multiple slaves
    """

    def _set_scenario(scenario_name):
        if scenario_name not in test_scenarios["scenarios"]:
            pytest.fail(f"Unknown scenario: {scenario_name}")
        return test_scenarios["scenarios"][scenario_name]

    return _set_scenario


@pytest.fixture
def assert_json_response():
    """
    Helper fixture for asserting JSON responses.

    Usage:
        def test_api(api_get, assert_json_response):
            response = api_get('/getStatus')
            data = assert_json_response(response, 200)
            assert 'status' in data
    """

    def _assert(response, expected_status=200):
        # Accept both 200 (OK) and 204 (No Content) as valid success responses
        valid_statuses = [expected_status]
        if expected_status == 200:
            valid_statuses.append(204)
        
        assert (
            response.status_code in valid_statuses
        ), f"Expected status {expected_status}, got {response.status_code}: {response.text}"

        # 204 No Content responses don't have a body
        if response.status_code == 204:
            return {}
        
        try:
            return response.json()
        except json.JSONDecodeError as e:
            pytest.fail(
                f"Failed to parse JSON response: {e}\nResponse text: {response.text}"
            )

    return _assert


@pytest.fixture
def wait_for_condition():
    """
    Helper fixture for waiting for a condition to become true.

    Usage:
        def test_charging(api_get, wait_for_condition):
            def is_charging():
                response = api_get('/getStatus')
                data = response.json()
                return data['charging'] == True

            wait_for_condition(is_charging, timeout=30, message="Vehicle did not start charging")
    """

    def _wait(condition_func, timeout=30, interval=1, message="Condition not met"):
        start_time = time.time()

        while time.time() - start_time < timeout:
            if condition_func():
                return True
            time.sleep(interval)

        pytest.fail(f"{message} (waited {timeout}s)")

    return _wait


# Pytest configuration
def pytest_configure(config):
    """Configure pytest with custom markers."""
    config.addinivalue_line(
        "markers", "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line("markers", "integration: marks tests as integration tests")
    config.addinivalue_line("markers", "unit: marks tests as unit tests")
    config.addinivalue_line("markers", "e2e: marks tests as end-to-end tests")


def pytest_collection_modifyitems(config, items):
    """Automatically mark tests based on their location."""
    for item in items:
        # Add markers based on test path
        if "integration" in str(item.fspath):
            item.add_marker(pytest.mark.integration)
        elif "unit" in str(item.fspath):
            item.add_marker(pytest.mark.unit)
        elif "e2e" in str(item.fspath):
            item.add_marker(pytest.mark.e2e)
            item.add_marker(pytest.mark.slow)
