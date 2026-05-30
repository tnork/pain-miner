// Pain Miner frontend error reporter.
// Captures uncaught errors + unhandled promise rejections + explicit
// `painminerError(err)` calls and posts them to Supabase via the
// log_painminer_error RPC. The cron job pain_miner.py --mode error-digest
// emails new fingerprints to tylerdnorkus@gmail.com via Brevo.
//
// Hardened: every code path is wrapped so the reporter itself never
// crashes the page. Server-side rate limit (max 20/fingerprint/hour)
// prevents email storms from runaway browser loops.
//
// Loaded with <script src="error-shim.js"></script> AFTER config.js +
// the Supabase JS client. window.painminerError(err, ctx?) becomes the
// public API.

(function () {
  function fingerprint(message, stack) {
    try {
      var firstFrame = (stack || "").split("\n").slice(1, 2).join("");
      var raw = (message || "") + "|" + firstFrame;
      // btoa-safe ASCII; map any non-ASCII to escape sequences first
      var ascii = unescape(encodeURIComponent(raw));
      return btoa(ascii).replace(/[^A-Za-z0-9]/g, "").slice(0, 64);
    } catch (_) {
      return "unknown";
    }
  }

  function getClient() {
    try {
      if (!window.PAINMINER_CONFIG) return null;
      var cfg = window.PAINMINER_CONFIG;
      if (!cfg.supabaseUrl || !cfg.supabaseAnonKey || !cfg.rpcSecret) return null;
      if (!window.supabase || typeof window.supabase.createClient !== "function") return null;
      // Reuse the page's existing client if it created one — otherwise make our own.
      // Using a separate instance is fine; the RPC call doesn't share state.
      return window.supabase.createClient(cfg.supabaseUrl, cfg.supabaseAnonKey);
    } catch (_) {
      return null;
    }
  }

  // Per-page in-memory dedup so the same exception fired 50x in a render loop
  // doesn't generate 50 RPC calls. Server still has its own rate limit.
  var seenFingerprints = Object.create(null);
  var SEEN_TTL_MS = 60 * 1000; // 1 min — long enough to suppress a burst, short enough that recurring bugs still report

  function painminerError(err, ctx) {
    try {
      var message = "";
      var stack = "";
      if (err instanceof Error) {
        message = String(err.message || err.name || "Error");
        stack = String(err.stack || "");
      } else if (typeof err === "string") {
        message = err;
      } else if (err && typeof err === "object") {
        message = String(err.message || err.reason || JSON.stringify(err).slice(0, 200));
        stack = String(err.stack || "");
      } else {
        message = "Unknown error";
      }
      if (!message) return;

      var fp = fingerprint(message, stack);
      var now = Date.now();
      if (seenFingerprints[fp] && (now - seenFingerprints[fp]) < SEEN_TTL_MS) return;
      seenFingerprints[fp] = now;

      var sb = getClient();
      if (!sb) return;

      var cfg = window.PAINMINER_CONFIG || {};
      var pageUrl = "";
      try { pageUrl = String(location.href || ""); } catch (_) {}
      var ua = "";
      try { ua = String(navigator.userAgent || ""); } catch (_) {}
      var ctxStr = "";
      try { ctxStr = ctx ? " :: " + JSON.stringify(ctx).slice(0, 200) : ""; } catch (_) {}

      var rpcPromise = sb.rpc("log_painminer_error", {
        p_message:     (message + ctxStr).slice(0, 1000),
        p_stack:       stack.slice(0, 4000),
        p_page_url:    pageUrl.slice(0, 500),
        p_user_agent:  ua.slice(0, 500),
        p_fingerprint: fp,
        p_secret:      cfg.rpcSecret || "",
      });
      // Fire-and-forget. Never let the reporter's own failures escape.
      if (rpcPromise && typeof rpcPromise.then === "function") {
        rpcPromise.then(function () {}, function () {});
      }
    } catch (_) { /* never throw from the reporter */ }
  }

  window.painminerError = painminerError;

  window.addEventListener("error", function (e) {
    try {
      painminerError(e.error || new Error(e.message || "uncaught error"),
                     { source: "window.error", file: e.filename, line: e.lineno });
    } catch (_) {}
  });
  window.addEventListener("unhandledrejection", function (e) {
    try {
      var reason = e.reason;
      if (!(reason instanceof Error)) reason = new Error(String(reason || "unhandled rejection"));
      painminerError(reason, { source: "unhandledrejection" });
    } catch (_) {}
  });
})();
