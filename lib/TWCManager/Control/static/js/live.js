// Keeps a page's live values current: polls JSON APIs and applies the
// data-* hooks the templates carry (see the hook vocabulary in the
// live-pages plan). Text only: every value goes through textContent.
var Live = (function () {
  var L = {};

  function formatters() {
    if (typeof Format !== "undefined") {
      return Format;
    }
    if (typeof require === "function") {
      return require("./format.js");
    }
    return {};
  }

  // ---- Pure helpers (tested under Node) ----

  L._itemKey = function (item) {
    if (item == null) {
      return undefined;
    }
    if (item.key != null) {
      return String(item.key);
    }
    if (item.id != null) {
      return String(item.id);
    }
    return undefined;
  };

  function step(node, segment) {
    if (node == null) {
      return undefined;
    }
    if (Array.isArray(node)) {
      for (var i = 0; i < node.length; i++) {
        if (L._itemKey(node[i]) === segment) {
          return node[i];
        }
      }
      return /^\d+$/.test(segment) ? node[Number(segment)] : undefined;
    }
    if (typeof node === "object") {
      return node[segment];
    }
    return undefined;
  }

  // "a.b.c" walks data; ".b.c" walks item (the enclosing keyed-list entry).
  L._lookup = function (data, path, item) {
    var node = data;
    if (path.charAt(0) === ".") {
      node = item;
      path = path.slice(1);
    }
    if (path === "") {
      return node;
    }
    var segments = path.split(".");
    for (var i = 0; i < segments.length; i++) {
      node = step(node, segments[i]);
    }
    return node;
  };

  L._truthy = function (v) {
    return !(!v || (Array.isArray(v) && v.length === 0));
  };

  // Reload when any URL is gone, or when a response that carries a
  // fingerprint no longer matches the one the page was rendered with.
  L._mustReload = function (pageFingerprint, responses) {
    for (var i = 0; i < responses.length; i++) {
      var r = responses[i];
      if (r.status === 404) {
        return true;
      }
      var fp = r.body && r.body.fingerprint;
      if (pageFingerprint != null && fp != null && fp !== pageFingerprint) {
        return true;
      }
    }
    return false;
  };

  var warnedUnknown = {};
  var loggedThrow = {};
  var logged = {};

  // console.error once per key until L._clearLog(key) re-arms it.
  L._logOnce = function (key, message, err) {
    if (!logged[key]) {
      logged[key] = true;
      console.error("Live: " + message, err);
    }
  };

  L._clearLog = function (key) {
    delete logged[key];
  };

  // Runs fn; an exception is logged once per key and swallowed, so one bad
  // element or entry never stops the rest of the page from updating.
  L._guard = function (key, fn) {
    try {
      fn();
      return true;
    } catch (e) {
      L._logOnce(key, key + " failed", e);
      return false;
    }
  };

  // {ok: true, formatted: false}  no formatter (empty or unknown name)
  // {ok: true, formatted: true, value}
  // {ok: false}                   the formatter threw; leave the element as is
  L._callFormat = function (name, value, ctx) {
    if (!name) {
      return { ok: true, formatted: false };
    }
    var fn = formatters()[name];
    if (typeof fn !== "function") {
      if (!warnedUnknown[name]) {
        warnedUnknown[name] = true;
        console.warn("Live: unknown formatter " + name);
      }
      return { ok: true, formatted: false };
    }
    try {
      return { ok: true, formatted: true, value: fn(value, ctx) };
    } catch (e) {
      if (!loggedThrow[name]) {
        loggedThrow[name] = true;
        console.error("Live: formatter " + name + " failed", e);
      }
      return { ok: false };
    }
  };

  // The cell strings for data-list rows, or null to leave the rows as they
  // are (unknown formatter, or one that threw for any item).
  L._rowCells = function (name, items, ctx) {
    var fn = formatters()[name];
    if (typeof fn !== "function") {
      L._callFormat(name, undefined, ctx); // warns once
      return null;
    }
    var rows = [];
    for (var i = 0; i < items.length; i++) {
      var r = L._callFormat(name, items[i], { data: ctx.data, el: ctx.el, item: items[i] });
      if (!r.ok) {
        return null;
      }
      rows.push(Array.isArray(r.value) ? r.value.map(String) : []);
    }
    return rows;
  };

  // ---- DOM binding ----

  // A hook belongs to the innermost keyed entry around it; bind(root) only
  // handles hooks whose entry is root itself (or none, for the page).
  function ownedBy(root, el) {
    var owner = el.parentElement && el.parentElement.closest("[data-key]");
    if (el.hasAttribute("data-key")) {
      owner = el;
    }
    return owner === null || owner === root || !root.contains(owner);
  }

  function hooks(root, selector) {
    var out = [];
    if (root.matches && root.matches(selector)) {
      out.push(root);
    }
    var found = root.querySelectorAll(selector);
    for (var i = 0; i < found.length; i++) {
      if (ownedBy(root, found[i])) {
        out.push(found[i]);
      }
    }
    return out;
  }

  function setText(el, text) {
    if (el.textContent !== text) {
      el.textContent = text;
    }
  }

  function bindField(el, data, item) {
    var value = L._lookup(data, el.dataset.field, item);
    var r = L._callFormat(el.dataset.format, value, { data: data, el: el, item: item });
    if (!r.ok) {
      return;
    }
    if (r.formatted) {
      setText(el, r.value == null ? "" : String(r.value));
    } else {
      setText(el, value == null ? "" : String(value));
    }
  }

  function bindShow(el, data, item) {
    var value = L._lookup(data, el.dataset.show, item);
    var r = L._callFormat(el.dataset.showIf, value, { data: data, el: el, item: item });
    if (!r.ok) {
      return;
    }
    var shown = r.formatted ? !!r.value : L._truthy(value);
    if (el.hidden === shown) {
      el.hidden = !shown;
    }
  }

  function bindClass(el, data, item) {
    var on = L._truthy(L._lookup(data, el.dataset.classIf, item));
    if (el.dataset.classTrue) {
      el.classList.toggle(el.dataset.classTrue, on);
    }
    if (el.dataset.classFalse) {
      el.classList.toggle(el.dataset.classFalse, !on);
    }
  }

  function bindList(el, data, item) {
    var items = L._lookup(data, el.dataset.list, item);
    items = Array.isArray(items) ? items : [];
    var rows = L._rowCells(el.dataset.row, items, { data: data, el: el, item: item });
    if (rows === null) {
      return;
    }
    var signature = JSON.stringify(rows);
    if (el.liveRows === signature) {
      return;
    }
    el.liveRows = signature;
    var trs = rows.map(function (cells) {
      var tr = document.createElement("tr");
      cells.forEach(function (text) {
        var td = document.createElement("td");
        td.textContent = text;
        tr.appendChild(td);
      });
      return tr;
    });
    el.replaceChildren.apply(el, trs);
  }

  function newEntry(templateId, item) {
    var tpl = document.getElementById(templateId);
    var entry = tpl.content.firstElementChild.cloneNode(true);
    entry.setAttribute("data-key", L._itemKey(item));
    var imgs = entry.querySelectorAll("img[data-image]");
    for (var i = 0; i < imgs.length; i++) {
      imgs[i].dataset.vin = item.vin || "";
      imgs[i].dataset.appearance = JSON.stringify(item.appearance || {});
      if (typeof window.drawVehicleImage === "function") {
        L._guard("drawVehicleImage", window.drawVehicleImage.bind(window, imgs[i]));
      }
    }
    return entry;
  }

  function nextEntry(node) {
    while (node && !(node.nodeType === 1 && node.hasAttribute("data-key"))) {
      node = node.nextSibling;
    }
    return node;
  }

  function bindKeyedList(el, data, item) {
    var items = L._lookup(data, el.dataset.keyedList, item);
    items = Array.isArray(items) ? items : [];
    var existing = {};
    for (var c = el.firstElementChild; c; c = c.nextElementSibling) {
      if (c.hasAttribute("data-key")) {
        existing[c.getAttribute("data-key")] = c;
      }
    }
    // Walk items in order; every placed entry ends up before `cursor`.
    var cursor = nextEntry(el.firstChild);
    var kept = {};
    items.forEach(function (it) {
      var key = L._itemKey(it);
      if (key === undefined || kept[key]) {
        return;
      }
      var entry = existing[key];
      if (!entry && !L._guard("template " + el.dataset.template, function () {
        entry = newEntry(el.dataset.template, it);
      })) {
        return; // could not create it; try again next poll
      }
      kept[key] = true;
      if (entry === cursor) {
        cursor = nextEntry(cursor.nextSibling);
      } else {
        el.insertBefore(entry, cursor);
      }
      L.bind(entry, data, it);
    });
    Object.keys(existing).forEach(function (key) {
      if (!kept[key]) {
        existing[key].remove();
      }
    });
  }

  var BINDERS = [
    ["[data-keyed-list]", bindKeyedList],
    ["[data-list]", bindList],
    ["[data-field]", bindField],
    ["[data-show]", bindShow],
    ["[data-class-if]", bindClass],
  ];

  L.bind = function (root, data, item) {
    BINDERS.forEach(function (b) {
      hooks(root, b[0]).forEach(function (el) {
        L._guard(b[0], function () { b[1](el, data, item); });
      });
    });
  };

  // ---- Polling ----

  var listeners = [];

  L.onData = function (fn) {
    listeners.push(fn);
  };

  var notice = null;

  function showOffline(show) {
    if (!notice) {
      if (!show) {
        return;
      }
      notice = document.createElement("div");
      notice.className = "live-offline";
      notice.textContent = "Can't reach TWCManager";
      // Styled here: the two themes load different stylesheets.
      notice.style.padding = "0.75em 1em";
      notice.style.margin = "0.5em";
      notice.style.background = "#f8d7da";
      notice.style.color = "#721c24";
      notice.style.border = "1px solid #f5c6cb";
      notice.style.borderRadius = "4px";
      document.body.insertBefore(notice, document.body.firstChild);
    }
    notice.hidden = !show;
  }

  function fetchOne(url, signal) {
    return fetch(url, { cache: "no-store", signal: signal }).then(function (res) {
      if (res.status === 404) {
        return { status: 404, body: {} };
      }
      if (!res.ok) {
        throw new Error(url + ": HTTP " + res.status);
      }
      return res.json().then(function (body) {
        return { status: res.status, body: body };
      });
    });
  }

  L.start = function (opts) {
    var urls = opts.urls.map(function (u) { return typeof u === "string" ? [u, null] : u; });
    var timer = null;
    var inFlight = false;
    var reloading = false;

    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(tick, opts.interval);
    }

    function apply(responses) {
      if (L._mustReload(opts.fingerprint, responses)) {
        reloading = true;
        location.reload();
        return;
      }
      var data = {};
      responses.forEach(function (r, i) {
        if (urls[i][1]) {
          data[urls[i][1]] = r.body;
        } else {
          Object.assign(data, r.body);
        }
      });
      L.bind(document.body, data);
      showOffline(false);
      listeners.forEach(function (fn) {
        try {
          fn(data);
        } catch (e) {
          console.error("Live: onData handler failed", e);
        }
      });
    }

    function tick() {
      timer = null;
      if (inFlight || document.visibilityState === "hidden") {
        return; // a hidden page resumes on visibilitychange
      }
      inFlight = true;
      // A request that hangs counts as unreachable, like one that fails.
      var controller = new AbortController();
      var deadline = setTimeout(function () { controller.abort(); }, 2 * opts.interval);
      Promise.all(urls.map(function (u) { return fetchOne(u[0], controller.signal); }))
        .then(function (responses) {
          apply(responses);
          L._clearLog("update");
        }, function () { showOffline(true); })
        .catch(function (e) { L._logOnce("update", "update failed", e); })
        .finally(function () {
          clearTimeout(deadline);
          inFlight = false;
          if (!reloading) {
            schedule();
          }
        });
    }

    document.addEventListener("visibilitychange", function () {
      if (document.visibilityState === "visible") {
        clearTimeout(timer);
        tick();
      }
    });

    if (document.readyState === "loading") {
      document.addEventListener("DOMContentLoaded", tick);
    } else {
      tick();
    }
  };

  return L;
})();
if (typeof module !== "undefined") {
  module.exports = Live;
}
