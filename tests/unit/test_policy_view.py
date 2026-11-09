import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from TWCManager.Control.Views import (
    pageFingerprint,
    policyLayout,
    policyState,
    policyStateView,
    policyTree,
)
from TWCManager.Policy.Policy import Policy
from tests.unit.live_helpers import callAPI

NESTED = {
    "name": "N",
    "match": ["a", ["b", "c"]],
    "condition": ["eq", ["gt", "lt"]],
    "value": [1, [2, 3]],
}


@pytest.fixture
def policy_mod():
    # Built as tests/unit/test_policy.py builds it; the policy list is the
    # instance's own copy so a test's edits never reach the class's
    master = Mock()
    master.config = {"config": {}, "policy": {}}
    master.settings = {"nonScheduledAction": 1, "sunrise": 6, "sunset": 20}
    master.modules = {}
    master.getModulesByType = Mock(return_value=[])
    master.checkScheduledCharging = Mock(return_value=0)
    mod = Policy(master)
    mod.charge_policy = copy.deepcopy(Policy.default_policy)
    master.getModuleByName = lambda name: mod if name == "Policy" else None
    return mod


def test_tree_ids_and_ops():
    t = policyTree(NESTED)
    assert t["id"] == "g" and t["kind"] == "group" and t["op"] == "AND"
    assert [c["id"] for c in t["children"]] == ["g-0", "g-1"]
    assert t["children"][0] == {
        "id": "g-0",
        "kind": "leaf",
        "match": "a",
        "condition": "eq",
        "value": 1,
    }
    assert t["children"][1]["op"] == "OR"
    assert t["children"][1]["children"][0]["id"] == "g-1-0"


def test_single_item_group_keeps_its_slot():
    t = policyTree({"name": "S", "match": [["x"]], "condition": [["eq"]], "value": [[1]]})
    assert t["children"][0]["kind"] == "group"
    assert t["children"][0]["children"][0]["id"] == "g-0-0"


def test_state_resolves_both_sides_and_groups(policy_mod):
    policy_mod.master.settings["x"] = 5
    p = {
        "name": "P",
        "match": ["settings.x", ["settings.x", "tm_hour"]],
        "condition": ["gt", ["eq", "lt"]],
        "value": [3, [4, -1]],
    }
    s = policyState(policy_mod, policyTree(p))
    assert s["g-0"] == {"match": 5, "value": 3, "truth": True}
    assert s["g-1"]["truth"] is False and s["g"]["truth"] is False
    assert s["g-1-0"] == {"match": 5, "value": 4, "truth": False}


def test_state_or_group_true_when_any_child_is(policy_mod):
    policy_mod.master.settings["x"] = 4
    p = {"name": "P", "match": [["settings.x", "settings.x"]],
         "condition": [["eq", "eq"]], "value": [[1, 4]]}
    s = policyState(policy_mod, policyTree(p))
    assert s["g-0"]["truth"] is True and s["g"]["truth"] is True


def test_layout_default_set(policy_mod):
    blocks = policyLayout(policy_mod)
    assert [b["name"] for b in blocks if b["type"] == "ext"] == [
        "Emergency",
        "Before",
        "After",
    ]
    assert all(b["items"] == [] for b in blocks if b["type"] == "ext")
    assert all(b["category"] == "Default" for b in blocks if b["type"] == "policy")
    assert [b["index"] for b in blocks if b["type"] == "policy"] == [0, 1, 2, 3, 4]
    # Emergency precedes Charge Now, Before follows it, After precedes
    # Non Scheduled Charging
    assert [b.get("name", b.get("index")) for b in blocks] == [
        "Emergency", 0, "Before", 1, 2, "After", 3, 4,
    ]


def test_layout_custom_bucket_and_modified(policy_mod):
    custom = {"name": "Mine", "match": ["none"], "condition": ["none"], "value": [0]}
    policy_mod.charge_policy.insert(1, custom)
    policy_mod.charge_policy[3]["condition"] = ["gt", "lt", "lte", "eq"]  # Track Green Energy
    blocks = policyLayout(policy_mod)
    before = next(b for b in blocks if b.get("name") == "Before")
    assert before["items"] == [{"type": "policy", "index": 1, "category": None}]
    tge = next(b for b in blocks if b.get("index") == 3)
    assert tge["category"] == "Default, Modified"
    assert next(b for b in blocks if b.get("index") == 0)["category"] == "Default"


def test_layout_trailing_extensions_need_no_marker(policy_mod):
    # Past the last insertion point an extension policy is listed plainly
    extra = {"name": "Late", "match": ["none"], "condition": ["none"], "value": [0]}
    policy_mod.charge_policy.append(extra)
    blocks = policyLayout(policy_mod)
    assert blocks[-1] == {"type": "policy", "index": 5, "category": None}


def test_layout_replaced(policy_mod):
    policy_mod.charge_policy = [
        {"name": "A", "match": ["none"], "condition": ["none"], "value": [0]},
        {"name": "B", "match": ["none"], "condition": ["none"], "value": [0]},
    ]
    assert policyLayout(policy_mod) == [
        {"type": "policy", "index": 0, "category": "Custom"},
        {"type": "policy", "index": 1, "category": "Custom"},
    ]


def test_policy_state_view(policy_mod):
    policy_mod.active_policy = "Charge Now"
    status, headers, body = callAPI(policy_mod.master, "/api/getPolicyState")
    data = json.loads(body)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert data["active"] == "Charge Now" and isinstance(data["now"], float)
    assert [p["name"] for p in data["policies"]] == [
        p["name"] for p in policy_mod.charge_policy
    ]
    assert set(data["policies"][0]["nodes"]) == {"g", "g-0", "g-1", "g-2"}


def test_policy_state_survives_odd_values(policy_mod):  # Review Focus 1
    odd = SimpleNamespace(nan=float("nan"), obj=object())
    policy_mod.master.modules = {"Odd": {}}
    policy_mod.master.getModuleByName = (
        lambda n: policy_mod if n == "Policy" else odd
    )
    policy_mod.charge_policy = [
        {
            "name": "O",
            "match": ["modules.Odd.nan", "modules.Odd.obj"],
            "condition": ["eq", "eq"],
            "value": [1, 1],
        }
    ]
    status, _, body = callAPI(policy_mod.master, "/api/getPolicyState")
    nodes = json.loads(body)["policies"][0]["nodes"]
    assert status == 200 and nodes["g-0"]["match"] is None
    assert nodes["g-1"]["match"].startswith("<object")


def test_policy_fingerprint_follows_names(policy_mod):
    m = policy_mod.master
    before = policyStateView(m)["fingerprint"]
    assert before == pageFingerprint(m, "policy")
    policy_mod.charge_policy[0]["condition"] = ["eq", "eq", "eq"]
    assert policyStateView(m)["fingerprint"] == before
    policy_mod.charge_policy[0]["name"] = "Charge Right Now"
    after = policyStateView(m)["fingerprint"]
    assert after != before and after == pageFingerprint(m, "policy")
