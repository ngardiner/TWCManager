const test = require("node:test");
const assert = require("node:assert/strict");
const F = require("../../lib/TWCManager/Control/static/js/format.js");

test("age", () => {
  assert.equal(F.age(null), "never");
  assert.equal(F.age(30), "30 s ago");
  assert.equal(F.age(90), "1 min ago");
  assert.equal(F.age(3600 * 5), "5 h ago");
  assert.equal(F.age(86400 * 3), "3 d ago");
  assert.equal(F.age(59.9), "59 s ago"); // int() truncation
  assert.equal(F.ageAt(100, { data: { now: 130 } }), "30 s ago");
  assert.equal(F.ageAt(null, { data: { now: 130 } }), "never");
});

test("sourceName", () => {
  assert.equal(F.sourceName("Gen2TWC"), "TWC");
  assert.equal(F.sourceName("TeslaBLE"), "BLE");
  assert.equal(F.sourceName("FleetAPICached"), "Fleet API (cached)");
  assert.equal(F.sourceName("Gen3TWC"), "Gen3 Wall Connector");
  assert.equal(F.sourceName("SomethingNew"), "SomethingNew");
});

test("usd keeps a third decimal below one cent", () => {
  assert.equal(F.usd(0), "$0.00");
  assert.equal(F.usd(0.002), "$0.002");
  assert.equal(F.usd(0.31), "$0.31");
});

test("thousands", () => assert.equal(F.thousands(41800), "41,800"));

test("pyStr matches Python str() for JSON values", () => {
  assert.equal(F.pyStr(true), "True");
  assert.equal(F.pyStr(null), "None");
  assert.equal(F.pyStr(5), "5");
  assert.equal(F.pyStr(0.5), "0.5");
  assert.equal(F.pyStr("tm_hour"), "tm_hour");
  assert.equal(F.pyStr(["a", 1, false]), "['a', 1, False]");
});

test("duration matches str(timedelta) without microseconds", () => {
  assert.equal(F.duration(3723.9), "1:02:03");
  assert.equal(F.duration(86400 + 5), "1 day, 0:00:05");
  assert.equal(F.duration(2 * 86400), "2 days, 0:00:00");
});
