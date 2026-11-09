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

const now = 1_000_000;
const ctx = (data, el) => ({ data: Object.assign({ now, budget: 2.5 }, data), el: el || { dataset: {} } });

test("presence", () => {
  assert.equal(F.presence("home"), "Home");
  assert.equal(F.presence("presumed"), "Probably home");
  assert.equal(F.presence("notHome"), "Away");
});
test("watch line", () => {
  assert.equal(F.watchLine({ watched: false, why: "off" }, ctx()), "Not watched: off");
  assert.equal(F.watchLine({ watched: false, why: "covered by TeslaMateVehicle" }, ctx()), "Not watched: covered by TeslaMate");
  const s = { watched: true, why: "", via: "ble", last: "asleep", lastAt: now - 120, nextAt: null };
  assert.equal(F.watchLine(s, ctx()), "Watched: asleep 2 min ago via ble; budget $2.50");
  assert.equal(F.watchLine({ ...s, via: null, why: "no status source" }, ctx()), "Not watched: no status source");
  assert.match(F.watchLine({ ...s, nextAt: now + 150 }, ctx()), /, next in 3 min;/);
});
test("provider lines", () => {
  const c = (provider, status, commands, readOnlyReason = null) => ({ provider, status, commands, readOnlyReason });
  assert.equal(F.providerLines([c("TeslaBLE", "paired", true), c("TeslaMateVehicle", "reports", false), c("Gen3TWC", "", true)]),
    "BLE: paired\nTeslaMate: reports, read-only\nGen3 Wall Connector: commands");
  assert.equal(F.providerLine(c("FleetAPI", "listed", false, "token lacks command scope")), "Fleet API: read-only (token lacks command scope)");
  assert.equal(F.providerLine(c("TeslaBLE", "unproven", false)), "BLE: unproven");
  assert.equal(F.providerLine(c("HomeAssistant", "entities", true)), "Home Assistant: entities, commands");
  assert.equal(F.providerLine(c("TeslaBLE", "paired", false)), "BLE: paired, read-only");
  assert.equal(F.providerLine(c("OwnerAPI", "listed", true)), "Owner API: commands");
});
test("held limit", () => { assert.equal(F.heldLimit({ held: 80, outside: 90 }), "80% (90%)"); assert.equal(F.heldLimit(null), "—"); });
test("last seen", () => {
  assert.equal(F.lastSeen({ lastSeenAt: now - 30, lastSeenSource: "TeslaBLE" }, ctx()), "30 s ago via BLE");
});
test("fleet summary line with pace and last month", () => {
  const m = { api: 0.31, telemetry: 0, signals: 0, last: { api: 0, telemetry: 0 }, pace: { api: 0.62, telemetry: 0, by: "Dec 1" } };
  assert.equal(F.fleetLine({ fleet: true, month: m }), "Fleet API this month: $0.31 · on pace for $0.62 by Dec 1");
  assert.equal(F.fleetLine({ fleet: true, month: { ...m, api: 0, pace: null } }), "Fleet API this month: $0.00");
  assert.equal(F.fleetLine({ fleet: true, month: { ...m, pace: null, last: { api: 0.02, telemetry: 0 } } }), "Fleet API this month: $0.31 · Last month: $0.02");
  assert.equal(F.fleetLine({ fleet: false, month: m }), "");
});
test("telemetry line is approximate", () => {
  const m = { api: 0, telemetry: 0.28, signals: 41800, last: { api: 0, telemetry: 0 }, pace: null };
  assert.equal(F.telemetryLine({ telemetry: true, month: m }), "Fleet Telemetry this month: approx. 41,800 signals, $0.28");
  assert.equal(F.telemetryLine({ telemetry: true, month: { ...m, telemetry: 0.002, signals: 300 } }), "Fleet Telemetry this month: approx. 300 signals, $0.002");
  assert.equal(F.telemetryLine({ telemetry: false, month: m }), "");
});
test("car spend cell, heading and telemetry", () => {
  const car = { api: 0.02, telemetry: 0.002, requests: 1, signals: 300, types: [], fields: [], last: { api: 0, telemetry: 0 } };
  const both = ctx({ spend: { fleet: true, telemetry: true } });
  assert.equal(F.spendCell({ spend: car }, both), "$0.02 + approx. $0.002 telemetry");
  assert.equal(F.spendHeading({ spend: car }, both), "Fleet API this month: $0.02 in 1 request");
  assert.equal(F.spendHeading({ spend: { ...car, requests: 2, api: 0.04 } }, both), "Fleet API this month: $0.04 in 2 requests");
  assert.equal(F.spendTelemetry({ spend: { ...car, signals: 3 } }, both).startsWith("Fleet Telemetry this month: approx. 3 signals"), true);
  assert.deepEqual(F.spendTypeRow({ kind: "wake_up", count: 2, cost: 0.04, last: now - 30 }, both), ["wake_up", "2", "$0.04", "30 s ago"]);
  assert.deepEqual(F.fieldRow({ field: "Soc", count: 41800 }), ["Soc", "41,800"]);
  const fleetOnly = ctx({ spend: { fleet: true, telemetry: false } });
  assert.equal(F.spendCell({ spend: car }, fleetOnly), "$0.02");
  assert.equal(F.spendTelemetry({ spend: car }, fleetOnly), "");
});
test("pending rows", () => {
  const p = (o) => Object.assign({ aspect: "chargeLimit", value: 50, inFlight: null, said: null, lastOutcome: null }, o);
  assert.deepEqual(F.pendingRow(p({ inFlight: { provider: "TeslaBLE", since: now - 12 } }), ctx()), ["Charge limit → 50%", "sending via BLE, started 12 s ago"]);
  assert.deepEqual(F.pendingRow(p({ value: -1 }), ctx()), ["Charge limit → car's own", "not tried yet"]);
  assert.equal(F.pendingRow(p({ aspect: "charging", value: true }), ctx())[0], "Charging → start");
  assert.equal(F.pendingRow(p({ aspect: "charging", value: false }), ctx())[0], "Charging → stop");
  assert.equal(F.pendingRow(p({ aspect: "amps", value: 16 }), ctx())[0], "Charge rate → 16 A");
  const last = { kind: "unreachable", detail: "", provider: "TeslaBLE", at: now - 120 };
  assert.equal(F.pendingRow(p({ lastOutcome: last }), ctx())[1], "unreachable via BLE, 2 min ago");
  assert.equal(F.pendingRow(p({ lastOutcome: { ...last, detail: "backoff" } }), ctx())[1], "backoff via BLE, 2 min ago");
  assert.equal(F.pendingRow(p({ lastOutcome: last, said: "no provider can deliver" }), ctx())[1], "no provider can deliver");
  assert.match(F.pendingRow(p({ lastOutcome: { ...last, detail: "held: driving", provider: "Gen2TWC" } }), ctx())[1], /^held: driving via TWC, /);
  assert.match(F.pendingRow(p({ lastOutcome: { ...last, detail: "rules: held" } }), ctx())[1], /^held via BLE/);
});
test("ble", () => {
  const b = (o) => Object.assign({ status: "unpaired", label: "In range (not paired)", canScan: true, since: null, scannedAt: now - 42, scanPending: false, scanBlocked: null, attempt: null }, o);
  assert.equal(F.bleLabel(b({ label: "Range unknown", canScan: false })), "Range unknown (scanning unavailable)");
  assert.equal(F.bleLabel(b()), "In range (not paired)");
  assert.equal(F.bleSince(b({ since: now - 120 }), ctx()), "since 2 min ago");
  assert.equal(F.bleSince(b(), ctx()), "");
  assert.equal(F.bleAttempt(b({ attempt: { ok: false, at: now - 120, message: "<b>timed out</b>" } }), ctx()), "<b>timed out</b> (2 min ago)");
  assert.equal(F.bleLastScan(b(), ctx()), "42 s ago");
  assert.equal(F.bleLastScan(b({ scannedAt: null }), ctx()), "never");
  assert.equal(F.bleRepair(b({ status: "refused" })), true);
  assert.equal(F.bleRepair(b()), false);
  assert.equal(F.bleNotRepair(b()), true);
  const why = "Bluetooth adapter is powered off or blocked (rfkill)";
  assert.equal(F.bleScanBlocked(b({ canScan: false, scanBlocked: why })), "No range scan: " + why);
  assert.equal(F.bleScanBlocked(b()), "");
  // the scan button shows instead: never both
  assert.equal(F.bleScanBlocked(b({ canScan: true, scanBlocked: "bluetoothctl not installed (install the bluez package)" })), "");
});
test("ble formatters tolerate no BLE status", () => {
  for (const name of ["bleLabel", "bleSince", "bleAttempt", "bleLastScan", "bleScanBlocked"]) {
    assert.equal(F[name](null, ctx()), "", name);
  }
  assert.equal(F.bleRepair(null), false);
  assert.equal(F.bleNotRepair(null), false);
});
test("predicates", () => {
  assert.equal(F.isEmpty([]), true); assert.equal(F.isEmpty([1]), false);
  assert.equal(F.not(false), true); assert.equal(F.not(true), false);
  for (const v of [null, undefined, 0, "", []]) assert.equal(F.not(v), true);
  for (const v of [1, "x", [0]]) assert.equal(F.not(v), false);
});
test("policy resolved suffix", () => {
  const el = (lit) => ({ dataset: { literal: JSON.stringify(lit) } });
  assert.equal(F.resolved(5, ctx({}, el("settings.x"))), " (5)");
  assert.equal(F.resolved(5, ctx({}, el(5))), "");
  assert.equal(F.resolved(true, ctx({}, el("x"))), " (True)");
  assert.equal(F.resolved("a<b", ctx({}, el("a<b"))), "");
});
test("home", () => {
  assert.equal(F.pointKind(["Gen2TWCs", "VehicleEndpoints"]), "TWC");
  assert.equal(F.pointKind(["Gen3TWCs"]), "Gen3 Wall Connector");
  assert.equal(F.pointKind(["VehicleEndpoints"]), "EVSE via car");
  assert.equal(F.blank(null), "");
  assert.equal(F.blank(0), "0");
  assert.equal(F.chargeTime(null, ctx()), "--:--:--");
  assert.equal(F.chargeTime(now - 3661, ctx()), "1:01:01");
  assert.equal(F.volts([240, 0, 0]), "240 / 0 / 0");
  assert.equal(F.fixed2(null), "0.00");
  assert.equal(F.secondsSince(now - 1.234, ctx()), "1.23");
  assert.equal(F.pluralS([1, 2]), "s"); assert.equal(F.pluralS([1]), "");
  assert.equal(F.scheduledStart(null, ctx({ scheduledChargingStartHour: 22, scheduledChargingFlexStart: 21 })), "22(Flex 21)");
  assert.equal(F.scheduledStart(null, ctx({ scheduledChargingStartHour: 22, scheduledChargingFlexStart: 22 })), "22");
  assert.equal(F.powerValue(1500) + " " + F.powerUnit(1500), "1.50 kW");
  assert.equal(F.powerValue(800) + " " + F.powerUnit(800), "800 Watt");
  const s = ctx({ generationWatts: "3000", consumptionWatts: "500", generationAmps: "12.5", consumptionAmps: "2", });
  assert.equal(F.surplusWatts(null, s) + " " + F.surplusUnit(null, s), "2.50 kW");
  assert.equal(F.surplusAmps(null, s), "10.50");
  const n = ctx({ generationWatts: "100", consumptionWatts: "500", generationAmps: "1", consumptionAmps: "2" });
  assert.equal(F.surplusWatts(null, n) + " " + F.surplusUnit(null, n), "0 Watt");
  assert.equal(F.surplusAmps(null, n), "0.00");
});
