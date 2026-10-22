/* Qwen Code bridge — console page (host plugin frontend bundle).
 *
 * Registers a "Qwen Code" route in the QwenPaw console and embeds the local
 * Qwen Code web console through the bridge's reverse proxy. The Qwen Code web
 * shell natively accepts ?theme=dark|light, so the iframe follows QwenPaw's
 * theme (preference key "qwenpaw-theme" + prefers-color-scheme fallback).
 */
(function () {
  var H = window.QwenPaw || {};
  var HOST = H.host || {};
  var React = HOST.React || H.React;
  if (!React || typeof H.registerRoutes !== "function") {
    console.warn("[qwen-code-bridge] host API unavailable; route not registered");
    return;
  }

  var THEME_KEY = "qwenpaw-theme";
  var BASE = "/api/qwen-code-bridge/web/";

  function isDark() {
    var pref = "system";
    try {
      pref = localStorage.getItem(THEME_KEY) || "system";
    } catch (e) {
      pref = "system";
    }
    if (pref === "dark") return true;
    if (pref === "light") return false;
    return !!(
      window.matchMedia &&
      window.matchMedia("(prefers-color-scheme: dark)").matches
    );
  }

  function QwenCodePage() {
    var st = React.useState(isDark());
    var dark = st[0];
    var setDark = st[1];

    React.useEffect(function () {
      function sync() {
        var next = isDark();
        setDark(function (prev) {
          return prev === next ? prev : next;
        });
      }
      var mq = window.matchMedia
        ? window.matchMedia("(prefers-color-scheme: dark)")
        : null;
      window.addEventListener("storage", sync);
      if (mq && mq.addEventListener) mq.addEventListener("change", sync);
      // The host applies theme switches via React context (no DOM event);
      // a cheap poll keeps the iframe in sync without touching the host.
      var timer = setInterval(sync, 1500);
      return function () {
        window.removeEventListener("storage", sync);
        if (mq && mq.removeEventListener) mq.removeEventListener("change", sync);
        clearInterval(timer);
      };
    }, [setDark]);

    var src = BASE + "?theme=" + (dark ? "dark" : "light");
    return React.createElement(
      "div",
      {
        style: {
          height: "100%",
          minHeight: 480,
          display: "flex",
          flexDirection: "column",
          gap: 6,
          background: dark ? "#141414" : "#ffffff",
        },
      },
      React.createElement(
        "div",
        {
          style: {
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "6px 4px",
          },
        },
        React.createElement("strong", null, "Qwen Code"),
        React.createElement(
          "span",
          { style: { opacity: 0.6, fontSize: 12 } },
          dark ? "深色" : "浅色",
        ),
        React.createElement(
          "a",
          { href: src, target: "_blank", rel: "noreferrer" },
          "新标签打开",
        ),
      ),
      React.createElement("iframe", {
        key: dark ? "dark" : "light",
        src: src,
        title: "Qwen Code",
        style: {
          flex: 1,
          width: "100%",
          minHeight: 420,
          border: 0,
          borderRadius: 8,
        },
        allow: "clipboard-read; clipboard-write",
      }),
    );
  }

  try {
    H.registerRoutes("qwen-code-bridge", [
      {
        path: "/qwen-code",
        label: "Qwen Code",
        icon: "\u{1F4BB}",
        priority: 20,
        component: QwenCodePage,
      },
    ]);
    console.info("[plugin:qwen-code-bridge] route /qwen-code registered");
  } catch (err) {
    console.warn("[qwen-code-bridge] registerRoutes failed", err);
  }
})();
