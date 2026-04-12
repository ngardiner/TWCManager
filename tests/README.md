# Pytest Integration Testing Guide

## Running Tests

### Run all tests
```bash
pytest tests/
```

### Run specific test categories
```bash
# Integration tests only
pytest tests/integration/

# Unit tests only
pytest tests/unit/

# Exclude slow tests
pytest -m "not slow"
```

### Run with coverage
```bash
pytest tests/ --cov=lib/TWCManager --cov-report=html
```

### Run tests in parallel
```bash
pytest tests/ -n auto
```

### Run specific test file
```bash
pytest tests/integration/test_api_endpoints.py
```

### Run specific test
```bash
pytest tests/integration/test_api_endpoints.py::TestAPIEndpoints::test_get_config
```

## Test Structure

```
tests/
├── conftest.py                      # Shared fixtures and configuration
├── fixtures/
│   └── twc_scenarios.json           # TWC test scenarios
├── integration/
│   └── test_api_endpoints.py        # API integration tests
├── unit/                            # Unit tests (to be added)
└── e2e/                             # End-to-end tests (to be added)
```

## Writing Tests

### Basic API Test
```python
def test_get_status(api_get, assert_json_response):
    """Test retrieving TWCManager status."""
    response = api_get('/getStatus')
    data = assert_json_response(response, 200)
    assert 'status' in data
```

### Test with Wait Condition
```python
def test_charge_activation(api_post, wait_for_condition):
    """Test that charging activates."""
    api_post('/chargeNow', json={'amps': 32})
    
    def is_charging():
        response = api_get('/getStatus')
        return response.json().get('charging') == True
    
    wait_for_condition(is_charging, timeout=30)
```

### Parametrized Test
```python
@pytest.mark.parametrize("amps", [12, 16, 24, 32])
def test_various_amperage(api_post, amps):
    """Test different amperage values."""
    response = api_post('/chargeNow', json={'amps': amps})
    assert response.status_code == 200
```

## Available Fixtures

### API Fixtures
- `api_client` - HTTP session for API calls
- `api_get(endpoint)` - Make GET request to API
- `api_post(endpoint, **kwargs)` - Make POST request to API
- `wait_for_twcmanager` - Ensures TWCManager is ready

### Assertion Fixtures
- `assert_json_response(response, status=200)` - Assert response status and parse JSON
- `wait_for_condition(func, timeout=30)` - Wait for condition to become true

### Configuration Fixtures
- `twcmanager_config` - Loaded test configuration
- `test_scenarios` - Available TWC test scenarios
- `dummy_twc_scenario(name)` - Load specific TWC scenario

## Docker Testing

### Build test container
```bash
docker build -f tests/docker/Dockerfile -t twcmanager-test .
```

### Run tests in container
```bash
docker run --rm twcmanager-test pytest tests/ -v
```

### Run tests with coverage
```bash
docker run --rm -v $(pwd)/htmlcov:/app/htmlcov twcmanager-test \
    pytest tests/ --cov=lib/TWCManager --cov-report=html
```

### Interactive testing
```bash
docker run --rm -it twcmanager-test bash
# Inside container:
pytest tests/integration/test_api_endpoints.py -v
```

## CI Integration

Tests run automatically in GitHub Actions on:
- Push to any branch
- Pull requests
- Multiple Python versions (3.8, 3.9, 3.10, 3.11)

See `.github/workflows/test_suite.yml` for CI configuration.

## Troubleshooting

### Tests timing out
- Increase timeout: `pytest --timeout=600`
- Check if TWCManager started: Look for "TWCManager ready" message

### Connection refused errors
- Ensure TWCManager is running
- Check API is listening on port 8088
- Verify no firewall blocking localhost

### Import errors
- Install test dependencies: `pip install -r requirements-test.txt`
- Ensure TWCManager is in PYTHONPATH

### Flaky tests
- Run with retries: `pytest --reruns 3`
- Check for race conditions in test
- Increase wait timeouts
