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
TEST_CONFIG_PATH = Path(__file__).parent.parent / "etc" / "twcmanager" / ".testconfig.json"
TEST_SCENARIOS_PATH = Path(__file__).parent / "fixtures" / "twc_scenarios.json"


@pytest.fixture(scope="session")
def test_scenarios():
    """Load TWC test scenarios from configuration file."""
    with open(TEST_SCENARIOS_PATH, 'r') as f:
        return json.load(f)


@pytest.fixture(scope="session")
def twcmanager_config():
    """Load TWCManager test configuration."""
    with open(TEST_CONFIG_PATH, 'r') as f:
        # Remove comments from JSON (not standard but used in config)
        content = '\n'.join(line for line in f if not line.strip().startswith('#'))
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
    
    This replaces the 120-second sleep with intelligent health checking.
    Assumes TWCManager is already running (e.g., in a Docker container).
    """
    max_wait = 60  # Maximum 60 seconds
    start_time = time.time()
    
    while time.time() - start_time < max_wait:
        try:
            response = api_client.get(f"{TEST_API_BASE_URL}/getStatus", timeout=5)
            if response.status_code == 200:
                print(f"\nTWCManager ready after {time.time() - start_time:.1f} seconds")
                return True
        except (requests.ConnectionError, requests.Timeout):
            time.sleep(1)
    
    pytest.fail(f"TWCManager did not become ready within {max_wait} seconds")


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
        return api_client.get(url, timeout=kwargs.pop('timeout', 30), **kwargs)
    
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
        return api_client.post(url, timeout=kwargs.pop('timeout', 30), **kwargs)
    
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
        if scenario_name not in test_scenarios['scenarios']:
            pytest.fail(f"Unknown scenario: {scenario_name}")
        return test_scenarios['scenarios'][scenario_name]
    
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
        assert response.status_code == expected_status, \
            f"Expected status {expected_status}, got {response.status_code}: {response.text}"
        
        try:
            return response.json()
        except json.JSONDecodeError as e:
            pytest.fail(f"Failed to parse JSON response: {e}\nResponse text: {response.text}")
    
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
    config.addinivalue_line(
        "markers", "integration: marks tests as integration tests"
    )
    config.addinivalue_line(
        "markers", "unit: marks tests as unit tests"
    )
    config.addinivalue_line(
        "markers", "e2e: marks tests as end-to-end tests"
    )


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
