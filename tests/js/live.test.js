const test = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const L = require("../../lib/TWCManager/Control/static/js/live.js");

test("lookup by key, id, index and relative path", () => {
  const d = { vehicles: [{ key: "anon:8244", name: "" }, { key: "5YJ", name: "Car" }], points: [{ id: "Point-5YJ", amps: 16 }], policies: [{ nodes: { "g-0": { truth: true } } }] };
  assert.equal(L._lookup(d, "vehicles.5YJ.name"), "Car");
  assert.equal(L._lookup(d, "vehicles.anon:8244.name"), "");
  assert.equal(L._lookup(d, "points.Point-5YJ.amps"), 16);
  assert.equal(L._lookup(d, "policies.0.nodes.g-0.truth"), true);
  assert.equal(L._lookup(d, ".amps", d.points[0]), 16);
  assert.equal(L._lookup(d, "vehicles.nope.name"), undefined);
});

test("lookup edge cases", () => {
  const d = { a: null, list: [{ key: "1", v: "by key" }, { v: "by index" }] };
  assert.equal(L._lookup(d, "a.b"), undefined);
  assert.equal(L._lookup(d, "missing.b"), undefined);
  assert.equal(L._lookup(d, "list.1.v"), "by key"); // key wins over index
  assert.equal(L._lookup(d, ".v", undefined), undefined);
  assert.deepEqual(L._lookup(d, ".", d.list[0]), d.list[0]);
});

test("truthy", () => {
  for (const v of [null, undefined, false, 0, "", []]) assert.equal(L._truthy(v), false);
  assert.equal(L._truthy([1]), true);
  assert.equal(L._truthy("x"), true);
  assert.equal(L._truthy({}), true);
});

test("reload rules", () => {
  assert.equal(L._mustReload("abc", [{ status: 200, body: { fingerprint: "abc" } }]), false);
  assert.equal(L._mustReload("abc", [{ status: 200, body: { fingerprint: "def" } }]), true);
  assert.equal(L._mustReload("abc", [{ status: 200, body: { carsCharging: 1 } }, { status: 200, body: { fingerprint: "abc" } }]), false);
  assert.equal(L._mustReload("abc", [{ status: 404, body: {} }]), true);
  assert.equal(L._mustReload("abc", [{ status: 200, body: null }]), false);
});

test("item key prefers key, then id", () => {
  assert.equal(L._itemKey({ key: "k", id: "i" }), "k");
  assert.equal(L._itemKey({ id: 7 }), "7");
  assert.equal(L._itemKey({ key: null, id: "i" }), "i");
  assert.equal(L._itemKey({}), undefined);
});

test("callFormat: no name, unknown name and throwing formatter", () => {
  const warn = console.warn, error = console.error;
  const logged = [];
  console.warn = (...a) => logged.push(["warn", a.join(" ")]);
  console.error = (...a) => logged.push(["error", a.join(" ")]);
  try {
    assert.deepEqual(L._callFormat("", 5, {}), { ok: true, formatted: false });
    assert.deepEqual(L._callFormat("thousands", 41800, {}), { ok: true, formatted: true, value: "41,800" });
    assert.deepEqual(L._callFormat("noSuchFormatter", 5, {}), { ok: true, formatted: false });
    assert.deepEqual(L._callFormat("noSuchFormatter", 5, {}), { ok: true, formatted: false });
    assert.equal(logged.filter((l) => l[0] === "warn").length, 1); // warned once
    const F = require("../../lib/TWCManager/Control/static/js/format.js");
    F.__testThrows = () => { throw new Error("boom"); };
    try {
      assert.deepEqual(L._callFormat("__testThrows", 5, {}), { ok: false });
      assert.deepEqual(L._callFormat("__testThrows", 5, {}), { ok: false });
    } finally {
      delete F.__testThrows;
    }
    assert.equal(logged.filter((l) => l[0] === "error").length, 1); // logged once
  } finally {
    console.warn = warn;
    console.error = error;
  }
});

test("live.js never assigns innerHTML", () => {
  const src = fs.readFileSync(require.resolve("../../lib/TWCManager/Control/static/js/live.js"), "utf8");
  assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|document\.write/);
});

test("live.js never writes form control state", () => {
  const src = fs.readFileSync(require.resolve("../../lib/TWCManager/Control/static/js/live.js"), "utf8");
  assert.doesNotMatch(src, /\.(value|checked|disabled)\s*=[^=]/);
});

function captureErrors(fn) {
  const error = console.error, warn = console.warn;
  const logged = [];
  console.error = (...a) => logged.push(["error", String(a[0])]);
  console.warn = (...a) => logged.push(["warn", String(a[0])]);
  try {
    fn();
  } finally {
    console.error = error;
    console.warn = warn;
  }
  return logged;
}

test("guard swallows an exception and logs once per key until cleared", () => {
  const logged = captureErrors(() => {
    assert.equal(L._guard("g1", () => {}), true);
    assert.equal(L._guard("g1", () => { throw new Error("x"); }), false);
    assert.equal(L._guard("g1", () => { throw new Error("x"); }), false);
    L._clearLog("g1");
    assert.equal(L._guard("g1", () => { throw new Error("x"); }), false);
  });
  assert.equal(logged.length, 2);
});

test("logOnce re-arms only after clearLog", () => {
  const logged = captureErrors(() => {
    L._logOnce("update-test", "update failed");
    L._logOnce("update-test", "update failed");
    L._clearLog("update-test");
    L._logOnce("update-test", "update failed");
  });
  assert.equal(logged.length, 2);
});

test("rowCells: rows, and null (leave rows alone) for unknown or throwing formatter even when empty", () => {
  const F = require("../../lib/TWCManager/Control/static/js/format.js");
  F.__testRow = (it) => [it.a, it.b];
  F.__testRowThrows = () => { throw new Error("boom"); };
  try {
    captureErrors(() => {
      assert.deepEqual(L._rowCells("__testRow", [{ a: "x", b: 1 }], {}), [["x", "1"]]);
      assert.deepEqual(L._rowCells("__testRow", [], {}), []);
      assert.equal(L._rowCells("__noSuchRow", [{ a: 1 }], {}), null);
      assert.equal(L._rowCells("__noSuchRow", [], {}), null);
      assert.equal(L._rowCells("__testRowThrows", [{ a: 1 }], {}), null);
    });
  } finally {
    delete F.__testRow;
    delete F.__testRowThrows;
  }
});
