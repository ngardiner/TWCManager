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

  return F;
})();
if (typeof module !== "undefined") {
  module.exports = Format;
}
