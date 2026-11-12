// Display rules for live pages. Mirrors the wording the Python side used.
var Format = (function () {
  var F = {};

  var SOURCE_NAMES = {
    TeslaMateVehicle: "TeslaMate",
    FleetTelemetryMQTT: "Fleet Telemetry",
    TeslaBLE: "BLE",
    OwnerAPI: "Owner API",
    FleetAPI: "Fleet API",
    FleetAPICached: "Fleet API (cached)",
    HomeAssistant: "Home Assistant",
    Gen2TWC: "TWC",
    Gen3TWC: "Gen3 Wall Connector",
  };

  F.age = function (seconds) {
    if (seconds == null) {
      return "never";
    }
    seconds = Math.trunc(seconds);
    if (seconds < 60) {
      return seconds + " s ago";
    }
    if (seconds < 3600) {
      return Math.floor(seconds / 60) + " min ago";
    }
    if (seconds < 86400) {
      return Math.floor(seconds / 3600) + " h ago";
    }
    return Math.floor(seconds / 86400) + " d ago";
  };

  F.ageAt = function (at, ctx) {
    return F.age(at == null ? null : ctx.data.now - at);
  };

  F.sourceName = function (raw) {
    return Object.prototype.hasOwnProperty.call(SOURCE_NAMES, raw)
      ? SOURCE_NAMES[raw]
      : raw;
  };

  F.usd = function (amount) {
    // Small amounts keep a third decimal so a few hundred signals aren't $0.00
    return "$" + amount.toFixed(amount > 0 && amount < 0.01 ? 3 : 2);
  };

  F.thousands = function (n) {
    return n.toLocaleString("en-US");
  };

  function repr(v) {
    return typeof v === "string" ? "'" + v + "'" : F.pyStr(v);
  }

  F.pyStr = function (v) {
    if (v === true) {
      return "True";
    }
    if (v === false) {
      return "False";
    }
    if (v == null) {
      return "None";
    }
    if (Array.isArray(v)) {
      return "[" + v.map(repr).join(", ") + "]";
    }
    return String(v);
  };

  function pad(n) {
    return (n < 10 ? "0" : "") + n;
  }

  F.duration = function (seconds) {
    var total = Math.floor(seconds);
    var days = Math.floor(total / 86400);
    var rest = total % 86400;
    var clock =
      Math.floor(rest / 3600) +
      ":" +
      pad(Math.floor((rest % 3600) / 60)) +
      ":" +
      pad(rest % 60);
    if (days === 0) {
      return clock;
    }
    return days + (days === 1 ? " day, " : " days, ") + clock;
  };

  // ---- Page formatters: plain text only, never HTML. (value, ctx) ----
  var COVERED = "covered by ";
  var RULES = "rules: ";
  var PRESENCE_TEXT = { home: "Home", presumed: "Probably home", notHome: "Away" };
  var ASPECT_LABEL = { chargeLimit: "Charge limit", charging: "Charging", amps: "Charge rate" };

  F.presence = function (p) {
    return Object.prototype.hasOwnProperty.call(PRESENCE_TEXT, p) ? PRESENCE_TEXT[p] : p;
  };

  F.watchLine = function (s, ctx) {
    if (!s.watched || s.via == null) {
      var why = s.why;
      if (why.indexOf(COVERED) === 0) {
        why = COVERED + F.sourceName(why.slice(COVERED.length));
      }
      return "Not watched: " + why;
    }
    var now = ctx.data.now;
    var due =
      s.nextAt == null
        ? ""
        : ", next in " + Math.max(0, Math.ceil((s.nextAt - now) / 60)) + " min";
    return (
      "Watched: " + (s.last || "no answer") + " " + F.ageAt(s.lastAt, ctx) +
      " via " + s.via + due + "; budget $" + Number(ctx.data.budget).toFixed(2)
    );
  };

  F.providerLine = function (claim) {
    var can = claim.commands;
    var cap = can
      ? "commands"
      : "read-only" + (claim.readOnlyReason ? " (" + claim.readOnlyReason + ")" : "");
    if ((claim.status === "paired" || claim.status === "unproven") && can === (claim.status === "paired")) {
      cap = ""; // the status already says it
    }
    var status = claim.status === "listed" ? "" : claim.status;
    return F.sourceName(claim.provider) + ": " + [status, cap].filter(Boolean).join(", ");
  };

  F.providerLines = function (claims) {
    return (claims || []).map(F.providerLine).join("\n");
  };

  F.lastSeen = function (entry, ctx) {
    return F.ageAt(entry.lastSeenAt, ctx) + " via " + F.sourceName(entry.lastSeenSource);
  };

  F.heldLimit = function (l) {
    return l == null ? "—" : l.held + "% (" + l.outside + "%)";
  };

  function lastMonth(amount) {
    return amount ? " · Last month: " + F.usd(amount) : "";
  }

  function teleText(signals, usd) {
    // Counts what reached TWCManager; Tesla bills what the car sent
    return "Fleet Telemetry this month: approx. " + F.thousands(signals) + " signals, " + F.usd(usd);
  }

  F.fleetLine = function (spend) {
    if (!spend || !spend.fleet) {
      return "";
    }
    var m = spend.month;
    var by = m.pace ? " · on pace for " + F.usd(m.pace.api) + " by " + m.pace.by : "";
    return "Fleet API this month: " + F.usd(m.api) + by + lastMonth(m.last.api);
  };

  F.telemetryLine = function (spend) {
    if (!spend || !spend.telemetry) {
      return "";
    }
    var m = spend.month;
    var by = m.pace ? " · on pace for " + F.usd(m.pace.telemetry) : "";
    return teleText(m.signals, m.telemetry) + by + lastMonth(m.last.telemetry);
  };

  function carSpend(entry, ctx) {
    var sp = ctx.data.spend;
    return entry && entry.spend && sp ? { c: entry.spend, fleet: sp.fleet, tele: sp.telemetry } : null;
  }

  F.spendCell = function (entry, ctx) {
    var s = carSpend(entry, ctx);
    if (!s) {
      return "";
    }
    var tele = s.tele && s.c.signals ? "approx. " + F.usd(s.c.telemetry) + " telemetry" : "";
    return [s.fleet ? F.usd(s.c.api) : "", tele].filter(Boolean).join(" + ");
  };

  F.spendHeading = function (entry, ctx) {
    var s = carSpend(entry, ctx);
    if (!s || !s.fleet) {
      return "";
    }
    var n = s.c.requests;
    return (
      "Fleet API this month: " + F.usd(s.c.api) + " in " + n + " " +
      (n === 1 ? "request" : "requests") + lastMonth(s.c.last.api)
    );
  };

  F.spendTelemetry = function (entry, ctx) {
    var s = carSpend(entry, ctx);
    if (!s || !(s.tele && (s.c.signals || s.c.last.telemetry))) {
      return "";
    }
    return teleText(s.c.signals, s.c.telemetry) + lastMonth(s.c.last.telemetry);
  };

  F.spendTypeRow = function (t, ctx) {
    return [t.kind, String(t.count), F.usd(t.cost), F.ageAt(t.last, ctx)];
  };

  F.fieldRow = function (f) {
    return [f.field, F.thousands(f.count)];
  };

  F.pendingRow = function (p, ctx) {
    var v;
    if (p.aspect === "chargeLimit") {
      v = p.value === -1 ? "car's own" : p.value + "%";
    } else if (p.aspect === "charging") {
      v = p.value ? "start" : "stop";
    } else {
      v = p.value + " A";
    }
    var label = ASPECT_LABEL[p.aspect] + " → " + v;
    var why = p.said;
    if (p.inFlight) {
      why = "sending via " + F.sourceName(p.inFlight.provider) + ", started " +
        F.age(ctx.data.now - p.inFlight.since);
    } else if (!why && p.lastOutcome) {
      var o = p.lastOutcome;
      why = (o.detail.split(RULES).join("") || o.kind) + " via " +
        F.sourceName(o.provider) + ", " + F.ageAt(o.at, ctx);
    }
    return [label, why || "not tried yet"];
  };

  F.bleLabel = function (b) {
    if (!b) {
      return "";
    }
    return b.label + (!b.canScan && b.label === "Range unknown" ? " (scanning unavailable)" : "");
  };

  F.bleSince = function (b, ctx) {
    return !b || b.since == null ? "" : "since " + F.ageAt(b.since, ctx);
  };

  F.bleAttempt = function (b, ctx) {
    var a = b && b.attempt;
    if (!a) {
      return "";
    }
    return a.message + (typeof a.at === "number" ? " (" + F.ageAt(a.at, ctx) + ")" : "");
  };

  F.bleLastScan = function (b, ctx) {
    return b ? F.ageAt(b.scannedAt, ctx) : "";
  };

  F.bleRepair = function (b) {
    return !!b && (b.status === "paired" || b.status === "refused");
  };

  F.bleScanBlocked = function (b) {
    // Either the scan button or the reason there is none, never both
    return b && !b.canScan && b.scanBlocked ? "No range scan: " + b.scanBlocked : "";
  };

  F.resolved = function (v, ctx) {
    var literal = JSON.parse(ctx.el.dataset.literal);
    return JSON.stringify(v) === JSON.stringify(literal) ? "" : " (" + F.pyStr(v) + ")";
  };

  F.pointKind = function (controllers) {
    var list = controllers || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] === "Gen2TWCs") {
        return "TWC";
      }
      if (list[i] === "Gen3TWCs") {
        return "Gen3 Wall Connector";
      }
    }
    return "EVSE via car";
  };

  F.blank = function (v) {
    return v == null ? "" : String(v);
  };

  F.fixed2 = function (v) {
    return Number(v || 0).toFixed(2);
  };

  F.round2 = function (v) {
    return v == null ? "" : String(Math.round(v * 100) / 100);
  };

  F.whole = function (v) {
    return v == null ? "" : String(Math.round(v));
  };

  F.volts = function (list) {
    return list.map(Math.round).join(" / ");
  };

  F.secondsSince = function (at, ctx) {
    return String(Math.round((ctx.data.now - at) * 100) / 100);
  };

  F.chargeTime = function (since, ctx) {
    return since == null ? "--:--:--" : F.duration(ctx.data.now - since);
  };

  F.scheduledStart = function (_, ctx) {
    var d = ctx.data;
    return d.scheduledChargingStartHour != d.scheduledChargingFlexStart
      ? d.scheduledChargingStartHour + "(Flex " + d.scheduledChargingFlexStart + ")"
      : String(d.scheduledChargingStartHour);
  };

  F.powerValue = function (w) {
    return w > 1000 ? (w * 0.001).toFixed(2) : String(Math.round(w));
  };

  F.powerUnit = function (w) {
    return w > 1000 ? "kW" : "Watt";
  };

  function surplus(ctx) {
    var gen = parseFloat(ctx.data.generationWatts);
    var con = parseFloat(ctx.data.consumptionWatts);
    return gen > con ? gen - con : 0;
  }

  F.surplusWatts = function (_, ctx) {
    return F.powerValue(surplus(ctx));
  };

  F.surplusUnit = function (_, ctx) {
    return F.powerUnit(surplus(ctx));
  };

  F.surplusAmps = function (_, ctx) {
    var d = ctx.data;
    var amps = surplus(ctx) > 0 ? parseFloat(d.generationAmps) - parseFloat(d.consumptionAmps) : 0;
    return amps.toFixed(2);
  };

  F.pluralS = function (list) {
    return list && list.length === 1 ? "" : "s";
  };

  // ---- Predicates for data-show-if ----
  F.isEmpty = function (list) {
    return !list || !list.length;
  };

  F.not = function (v) {
    return !v || (Array.isArray(v) && v.length === 0);
  };

  F.bleNotRepair = function (b) {
    return !!b && !F.bleRepair(b);
  };

  return F;
})();
if (typeof module !== "undefined") {
  module.exports = Format;
}
