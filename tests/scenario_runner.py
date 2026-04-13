"""
Scenario runner — pytest fixtures for step-driven scenario-based testing.

Usage in a test:

    def test_green_energy_basic(run_scenario):
        result = run_scenario("tests/scenarios/green_energy/basic_surplus.json")
        result.assert_all()

The run_scenario fixture:
  - Loads the scenario JSON
  - Writes the step signal file to set the initial step
  - For each timeline step, waits for TWCManager to reflect the new EMS state
  - Evaluates all assertions declared for that step via the HTTP API
  - Returns a ScenarioResult with full evidence for reporting

Step advancement protocol:
  ScenarioEMS reads /tmp/twcm_scenario_step on every poll cycle.
  This file contains the integer step index (written by advance_step below).
  When the index changes, ScenarioEMS applies the new generation/consumption values.
"""

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pytest
import requests

STEP_FILE = "/tmp/twcm_scenario_step"
DEFAULT_ASSERTION_TIMEOUT = 20  # seconds
DEFAULT_SETTLE_POLL_INTERVAL = 0.5  # seconds between API polls


# ---------------------------------------------------------------------------
# Result data structures
# ---------------------------------------------------------------------------


@dataclass
class AssertionResult:
    step: int
    description: str
    passed: bool
    detail: str = ""
    api_responses: dict = field(default_factory=dict)


@dataclass
class ScenarioResult:
    scenario: dict
    assertion_results: List[AssertionResult] = field(default_factory=list)

    @property
    def metadata(self):
        return self.scenario.get("metadata", {})

    @property
    def known_failure(self):
        return self.metadata.get("known_failure", False)

    @property
    def github_issue(self):
        return self.metadata.get("github_issue")

    def record(self, result: AssertionResult):
        self.assertion_results.append(result)

    def passed_count(self):
        return sum(1 for r in self.assertion_results if r.passed)

    def failed_count(self):
        return sum(1 for r in self.assertion_results if not r.passed)

    def assert_all(self):
        """
        Call at the end of a test to raise failures.

        For known_failure scenarios:
          - If ALL assertions pass → the bug is fixed; raise to flag it.
          - If any assertion fails → expected; mark test as xfail.
        For normal scenarios:
          - Any failure raises AssertionError.
        """
        failures = [r for r in self.assertion_results if not r.passed]

        if self.known_failure:
            if not failures:
                issue_ref = f" (issue #{self.github_issue})" if self.github_issue else ""
                pytest.fail(
                    f"Known-failure scenario '{self.metadata.get('id')}' passed "
                    f"unexpectedly{issue_ref} — the bug appears to be fixed. "
                    "Remove known_failure flag and promote to a regression test."
                )
            else:
                issue_ref = f"#{self.github_issue}" if self.github_issue else "no issue ref"
                pytest.xfail(
                    f"Known failure ({issue_ref}): "
                    + "; ".join(r.description for r in failures)
                )
        else:
            if failures:
                lines = [f"Scenario '{self.metadata.get('id')}' — {len(failures)} assertion(s) failed:"]
                for r in failures:
                    lines.append(f"  step {r.step}: {r.description} — {r.detail}")
                raise AssertionError("\n".join(lines))


# ---------------------------------------------------------------------------
# Step signal helpers
# ---------------------------------------------------------------------------


def advance_step(step_index: int):
    """Write the step index to the signal file so ScenarioEMS picks it up."""
    Path(STEP_FILE).write_text(str(step_index))


def clear_step_file():
    try:
        Path(STEP_FILE).unlink(missing_ok=True)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Assertion evaluation
# ---------------------------------------------------------------------------


def _resolve_path(data: Any, path: str) -> Any:
    """
    Traverse a dot/bracket-notation path into a nested dict/list.

    Supported syntax:
      "foo.bar"       → data["foo"]["bar"]
      "[0].lastAmps"  → data[0]["lastAmps"]
      "foo[1].bar"    → data["foo"][1]["bar"]
    """
    import re

    current = data
    # Tokenise: split on dots, then handle [N] brackets within each token
    tokens = re.split(r"\.", path)
    for token in tokens:
        # Handle leading bracket like "[0]" or combined like "slaves[1]"
        parts = re.split(r"(\[\d+\])", token)
        for part in parts:
            if not part:
                continue
            m = re.match(r"\[(\d+)\]", part)
            if m:
                current = current[int(m.group(1))]
            else:
                current = current[part]
    return current


def _evaluate_check(check: dict, api_responses: dict, api_get_fn) -> Tuple[bool, str]:
    """
    Evaluate a single check dict.  Returns (passed, detail_string).

    Check formats:
      Simple:  {"api_endpoint": "/getSlaveTWCs", "path": "[0].lastAmpsOffered", "op": "gte", "value": 6}
      Logical: {"op": "or",  "checks": [...]}
               {"op": "and", "checks": [...]}
    """
    op = check.get("op", "eq")

    if op in ("or", "and"):
        sub_results = [_evaluate_check(c, api_responses, api_get_fn) for c in check.get("checks", [])]
        if op == "or":
            passed = any(r[0] for r in sub_results)
        else:
            passed = all(r[0] for r in sub_results)
        details = " | ".join(r[1] for r in sub_results)
        return passed, details

    endpoint = check["api_endpoint"]
    if endpoint not in api_responses:
        try:
            resp = api_get_fn(endpoint)
            api_responses[endpoint] = resp.json() if resp.status_code == 200 else None
        except Exception as e:
            api_responses[endpoint] = None
            return False, f"API error on {endpoint}: {e}"

    data = api_responses[endpoint]
    if data is None:
        return False, f"No data from {endpoint}"

    try:
        actual = _resolve_path(data, check["path"])
    except (KeyError, IndexError, TypeError) as e:
        return False, f"Path '{check['path']}' not found in {endpoint} response: {e}"

    expected = check["value"]
    ops = {
        "eq":  lambda a, e: a == e,
        "ne":  lambda a, e: a != e,
        "gt":  lambda a, e: a > e,
        "gte": lambda a, e: a >= e,
        "lt":  lambda a, e: a < e,
        "lte": lambda a, e: a <= e,
    }
    if op not in ops:
        return False, f"Unknown op '{op}'"

    passed = ops[op](actual, expected)
    detail = f"{check['path']} = {actual!r} (expected {op} {expected!r})"
    return passed, detail


def _wait_for_assertion(assertion: dict, api_get_fn, timeout: float) -> Tuple[bool, str, dict]:
    """
    Poll the API until all checks in the assertion pass or timeout expires.
    Returns (passed, detail, api_responses_snapshot).
    """
    deadline = time.monotonic() + timeout
    last_detail = ""
    last_responses = {}

    while time.monotonic() < deadline:
        api_responses = {}
        all_passed = True
        details = []

        for check in assertion.get("checks", []):
            passed, detail = _evaluate_check(check, api_responses, api_get_fn)
            details.append(detail)
            if not passed:
                all_passed = False

        last_detail = "; ".join(details)
        last_responses = api_responses

        if all_passed:
            return True, last_detail, last_responses

        time.sleep(DEFAULT_SETTLE_POLL_INTERVAL)

    return False, last_detail, last_responses


# ---------------------------------------------------------------------------
# Main fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def run_scenario(api_get, wait_for_twcmanager):
    """
    Run a named scenario file end-to-end and return a ScenarioResult.

    Usage:
        def test_something(run_scenario):
            result = run_scenario("tests/scenarios/green_energy/basic_surplus.json")
            result.assert_all()
    """

    def _run(scenario_path: str) -> ScenarioResult:
        path = Path(scenario_path)
        assert path.exists(), f"Scenario file not found: {scenario_path}"

        with open(path) as f:
            scenario = json.load(f)

        result = ScenarioResult(scenario=scenario)
        timeline = scenario.get("timeline", [])
        assertions_by_step = {}
        for a in scenario.get("assertions", []):
            step = a["after_step"]
            assertions_by_step.setdefault(step, []).append(a)

        # Index assertions to assertions_by_step
        for step_data in timeline:
            step_idx = step_data["step"]

            # Signal ScenarioEMS to apply this step's EMS values
            advance_step(step_idx)

            # Give TWCManager at least one poll cycle to pick up the new values
            # before we start asserting.  The assertions themselves have their
            # own timeout loop, so this is just a short grace period.
            time.sleep(1.0)

            for assertion in assertions_by_step.get(step_idx, []):
                timeout = assertion.get("timeout_seconds", DEFAULT_ASSERTION_TIMEOUT)
                passed, detail, api_snapshot = _wait_for_assertion(
                    assertion, api_get, timeout
                )
                result.record(AssertionResult(
                    step=step_idx,
                    description=assertion.get("description", f"step {step_idx} assertion"),
                    passed=passed,
                    detail=detail,
                    api_responses=api_snapshot,
                ))

        clear_step_file()
        return result

    yield _run
    clear_step_file()
