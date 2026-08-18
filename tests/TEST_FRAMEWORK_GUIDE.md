# TWCManager Test Framework Guide

## Overview

This guide documents the TWCManager test framework, including the regression test system, EMS module tests, integration tests, and performance benchmarks.

## Directory Structure

```
tests/
├── unit/                          # Unit tests for individual modules
│   ├── test_enphase_ems.py       # Enphase EMS module tests
│   ├── test_solaredge_ems.py     # SolarEdge EMS module tests
│   ├── test_teslapowerwall2_ems.py
│   ├── test_growatt_ems.py
│   ├── test_kostal_ems.py
│   ├── test_remaining_ems_modules.py
│   └── ... (other unit tests)
│
├── integration/                   # Integration tests
│   ├── test_api_endpoints.py     # API endpoint tests
│   ├── test_multislave_scenarios.py
│   ├── test_error_handling.py
│   ├── test_vehicle_integration.py
│   ├── test_performance_benchmarks.py
│   ├── test_stress_scenarios.py
│   └── conftest.py               # Shared fixtures
│
├── scenarios/                     # Test scenarios
│   ├── bugs/                      # Regression test scenarios
│   │   ├── __init__.py
│   │   ├── conftest.py           # Regression test fixtures
│   │   ├── test_regression_template.py
│   │   └── (issue-specific tests)
│   ├── green_energy/             # Green energy scenarios
│   ├── load_sharing/             # Load sharing scenarios
│   └── policy/                   # Policy scenarios
│
└── conftest.py                   # Root pytest configuration
```

## Running Tests

### All Tests
```bash
pytest tests/
```

### Unit Tests Only
```bash
pytest tests/unit/
```

### Integration Tests Only
```bash
pytest tests/integration/
```

### Specific Test File
```bash
pytest tests/unit/test_enphase_ems.py
```

### Specific Test Class
```bash
pytest tests/unit/test_enphase_ems.py::TestEnphaseInitialization
```

### Specific Test
```bash
pytest tests/unit/test_enphase_ems.py::TestEnphaseInitialization::test_init_disabled_module
```

### With Coverage
```bash
pytest tests/ --cov=lib/TWCManager --cov-report=html
```

### Parallel Execution
```bash
pytest tests/ -n auto
```

### Skip Slow Tests
```bash
pytest tests/ -m "not slow"
```

### Only Slow Tests
```bash
pytest tests/ -m slow
```

## Regression Test Framework

### Purpose
Track and prevent regressions for user-reported issues.

### Location
`tests/scenarios/bugs/`

### Adding a Regression Test

1. **Create scenario file** (optional):
```json
{
  "name": "Issue #123: Description",
  "description": "What the bug was",
  "steps": ["Step 1", "Step 2"],
  "expected": "Expected behavior",
  "actual_before_fix": "What was broken"
}
```

2. **Create test file**:
```python
# tests/scenarios/bugs/test_issue_123.py
import pytest

class TestIssue123:
    """Regression test for Issue #123: Description."""
    
    def test_issue_123_charge_now_timeout(self, api_post, issue_tracker):
        """Test that chargeNow doesn't timeout."""
        issue_tracker.register(
            issue_number=123,
            title="Charge Now times out after 30 seconds",
            severity="high"
        )
        
        response = api_post('/chargeNow', json={
            'chargeNowRate': 32,
            'chargeNowDuration': 3600
        })
        
        assert response.status_code == 200
        assert response.elapsed.total_seconds() < 5
```

3. **Run the test**:
```bash
pytest tests/scenarios/bugs/test_issue_123.py -v
```

## EMS Module Tests

### Coverage
- 24/24 EMS modules tested
- Initialization and configuration validation
- API connection handling
- Error scenarios

### Test Structure
Each EMS module test includes:
- `TestInitialization`: Module enable/disable, credential validation
- `TestAPIConnection`: Connection success/failure, timeouts
- `TestDataParsing`: Response parsing, error handling
- `TestCaching`: Cache expiration, data freshness
- `TestGetters`: Return value validation

### Example: Adding Tests for a New EMS Module

```python
# tests/unit/test_new_ems.py
import pytest
from unittest.mock import Mock, patch

class TestNewEMSInitialization:
    """Test NewEMS module initialization."""
    
    def test_init_disabled(self):
        """Test module when disabled."""
        master = Mock()
        master.config = {
            "config": {},
            "sources": {"NewEMS": {"enabled": False}}
        }
        
        with patch('TWCManager.EMS.NewEMS.logger'):
            from TWCManager.EMS.NewEMS import NewEMS
            ems = NewEMS(master)
            master.releaseModule.assert_called_once()
```

## Integration Tests

### Categories

1. **API Endpoints** (`test_api_endpoints.py`)
   - Endpoint availability
   - Response format validation
   - Parameter handling

2. **Multi-Slave Scenarios** (`test_multislave_scenarios.py`)
   - Slave detection
   - Load sharing
   - State management

3. **Error Handling** (`test_error_handling.py`)
   - Network errors
   - Input validation
   - System resilience

4. **Vehicle Integration** (`test_vehicle_integration.py`)
   - Vehicle detection
   - State transitions
   - Charge control

5. **Performance Benchmarks** (`test_performance_benchmarks.py`)
   - Response times
   - High-frequency calls
   - Concurrent operations

6. **Stress Scenarios** (`test_stress_scenarios.py`)
   - Continuous operation
   - Rapid state changes
   - Network instability

### Fixtures

Available fixtures in `conftest.py`:
- `api_get(endpoint)`: Make GET request
- `api_post(endpoint, json=...)`: Make POST request
- `assert_json_response(response, status_code)`: Validate JSON response
- `wait_for_condition(func, timeout=10)`: Wait for condition

## Performance Benchmarks

### Running Performance Tests
```bash
pytest tests/integration/test_performance_benchmarks.py -v
```

### Performance Targets
- API response: <100ms average
- Commands: <200ms
- High-frequency: 10+ calls/second
- Concurrent ops: 5+ ops/second
- Sustained load: 30+ seconds without errors

### Interpreting Results
```
test_get_status_response_time PASSED
Average response time 45.2ms exceeds 100ms: PASS
Max response time 89.5ms exceeds 500ms: PASS
```

## Stress Testing

### Running Stress Tests
```bash
pytest tests/integration/test_stress_scenarios.py -m slow -v
```

### Stress Test Scenarios
- 60-second continuous operation
- Rapid state changes (start/stop cycles)
- High-frequency policy changes (55+ rate changes)
- Multi-slave stress scenarios
- Network instability simulation
- Resource usage monitoring

## Best Practices

### Writing Tests

1. **Use descriptive names**
   ```python
   def test_charge_now_with_valid_amperage(self):  # Good
   def test_charge(self):  # Bad
   ```

2. **Follow Arrange-Act-Assert pattern**
   ```python
   def test_example(self, api_post):
       # Arrange
       expected = 200
       
       # Act
       response = api_post('/chargeNow', json={...})
       
       # Assert
       assert response.status_code == expected
   ```

3. **Use parametrize for multiple cases**
   ```python
   @pytest.mark.parametrize("rate,expected", [
       (12, 200),
       (32, 200),
       (999, 400),
   ])
   def test_charge_rates(self, api_post, rate, expected):
       response = api_post('/chargeNow', json={'chargeNowRate': rate})
       assert response.status_code == expected
   ```

4. **Mock external dependencies**
   ```python
   with patch('TWCManager.EMS.Module.logger'):
       from TWCManager.EMS.Module import Module
       module = Module(master)
   ```

5. **Add docstrings**
   ```python
   def test_charge_now_activation(self, api_post):
       """Test activating Charge Now mode with valid parameters."""
   ```

### Test Organization

1. Group related tests in classes
2. Use descriptive class names
3. Keep tests focused and independent
4. Avoid test interdependencies
5. Clean up after tests (use fixtures)

### Debugging Tests

```bash
# Verbose output
pytest tests/ -v

# Show print statements
pytest tests/ -s

# Stop on first failure
pytest tests/ -x

# Show local variables on failure
pytest tests/ -l

# Drop into debugger on failure
pytest tests/ --pdb

# Run last failed tests
pytest tests/ --lf

# Run failed tests first
pytest tests/ --ff
```

## CI/CD Integration

### GitHub Actions
Tests run automatically on:
- Push to main branch
- Pull requests
- Scheduled daily runs

### Test Matrix
- Python 3.8, 3.9, 3.10, 3.11
- Ubuntu 24.04
- MariaDB service

### Coverage Reports
- Generated after each run
- Uploaded to Codecov
- Available in GitHub Actions artifacts

## Maintenance

### Quarterly Review
1. Review test coverage metrics
2. Identify untested code paths
3. Update performance baselines
4. Review and update test patterns

### Adding Tests for New Features
1. Write tests before implementation (TDD)
2. Ensure tests pass with new code
3. Add regression tests for any bugs found
4. Update documentation

### Updating Tests for Code Changes
1. Run full test suite after changes
2. Update tests if behavior changes
3. Add new tests for new functionality
4. Verify no regressions

## Resources

- Pytest documentation: https://docs.pytest.org/
- Unittest.mock documentation: https://docs.python.org/3/library/unittest.mock.html
- TWCManager documentation: See CLAUDE.md

## Support

For questions about the test framework:
1. Check this guide
2. Review existing test examples
3. Check pytest documentation
4. Ask in project discussions
