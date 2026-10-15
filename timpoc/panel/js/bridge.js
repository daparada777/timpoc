/*
 * timpoc — тонкая обёртка над CSInterface.
 *
 * Зачем своя обёртка, а не «просто CSInterface.js»:
 *   1. CSInterface.evalScript отдаёт результат СТРОКОЙ и при ошибке возвращает
 *      литерал "EvalScript error." — по нему невозможно понять, что случилось.
 *      Мы разбираем JSON, различаем «ошибка AE» и «не ответило», и всегда
 *      возвращаем объект вида {ok, error}.
 *   2. ExtendScript не имеет JSON, поэтому хост отдаёт строку, собранную
 *      вручную; парсим её здесь, в браузере, где JSON есть.
 *   3. Нужен единый таймаут: если AE завис, панель не должна висеть вместе с ним.
 *
 * Если в окружении нет CEP (например, панель открыли в обычном браузере для
 * вёрстки), включается режим заглушки: вызовы AE возвращают понятную ошибку,
 * а движок при этом работает — так можно разрабатывать интерфейс без AE.
 */

(function (global) {
  "use strict";

  var HAS_CSINTERFACE = false;
  var cs = null;

  try {
    if (typeof global.CSInterface === "function") {
      cs = new global.CSInterface();
      HAS_CSINTERFACE = true;
    }
  } catch (e) {
    HAS_CSINTERFACE = false;
  }

  var EVAL_ERROR = "EvalScript error.";
  var DEFAULT_TIMEOUT_MS = 20000;

  function isPlainObject(v) {
    return v !== null && typeof v === "object" && !Array.isArray(v);
  }

  /* Безопасный разбор ответа хоста. */
  function parseHostResult(raw) {
    if (raw === null || raw === undefined || raw === "") {
      return { ok: false, error: { code: "empty_response", message: "After Effects вернул пустой ответ.", hint: "Повторите действие. Если повторяется — перезапустите After Effects." } };
    }
    if (raw === EVAL_ERROR) {
      return { ok: false, error: { code: "eval_error", message: "After Effects не смог выполнить запрос.", hint: "Возможно, композиция закрыта или проект повреждён. Проверьте состояние проекта." } };
    }
    if (typeof raw !== "string") {
      return isPlainObject(raw) ? raw : { ok: true, value: raw };
    }
    try {
      return JSON.parse(raw);
    } catch (e) {
      return { ok: false, error: { code: "bad_json", message: "Не удалось разобрать ответ After Effects.", hint: "Повторите действие.", details: String(raw).slice(0, 400) } };
    }
  }

  /*
   * Вызвать функцию хоста.
   *   fnName — имя функции в timpoc_host.jsx;
   *   arg    — объект-аргумент (будет сериализован в литерал ExtendScript);
   *   timeoutMs — сколько ждать ответа.
   *
   * Возвращает Promise<{ok, ...}> — никогда не бросает исключение наружу:
   * панель обязана показывать понятное сообщение, а не падать.
   */
  function callHost(fnName, arg, timeoutMs) {
    return new Promise(function (resolve) {
      if (!HAS_CSINTERFACE) {
        resolve({
          ok: false,
          error: {
            code: "no_ae",
            message: "Панель открыта вне After Effects.",
            hint: "Откройте панель через Window → Extensions → timpoc. В браузере доступна только вёрстка."
          }
        });
        return;
      }

      var script;
      try {
        script = arg === undefined
          ? fnName + "()"
          : fnName + "(" + toExtendScriptLiteral(arg) + ")";
      } catch (e) {
        resolve({ ok: false, error: { code: "serialize", message: "Не удалось передать данные в After Effects.", hint: "", details: String(e) } });
        return;
      }

      var settled = false;
      var timer = setTimeout(function () {
        if (settled) { return; }
        settled = true;
        resolve({
          ok: false,
          error: {
            code: "timeout",
            message: "After Effects не ответил вовремя.",
            hint: "Возможно, AE занят рендером. Дождитесь окончания и повторите."
          }
        });
      }, timeoutMs || DEFAULT_TIMEOUT_MS);

      try {
        cs.evalScript(script, function (raw) {
          if (settled) { return; }
          settled = true;
          clearTimeout(timer);
          resolve(parseHostResult(raw));
        });
      } catch (e) {
        if (settled) { return; }
        settled = true;
        clearTimeout(timer);
        resolve({ ok: false, error: { code: "exception", message: "Сбой обращения к After Effects.", hint: "", details: String(e) } });
      }
    });
  }

  /*
   * Сериализация в литерал ExtendScript (ES3).
   * JSON.stringify даёт почти то же самое, но ExtendScript не понимает
   * некоторые конструкции; поэтому сериализуем сами и экранируем аккуратно.
   */
  function toExtendScriptLiteral(value) {
    if (value === null || value === undefined) { return "null"; }
    var t = typeof value;
    if (t === "number") { return isFinite(value) ? String(value) : "0"; }
    if (t === "boolean") { return value ? "true" : "false"; }
    if (t === "string") { return quoteString(value); }
    if (Array.isArray(value)) {
      return "[" + value.map(toExtendScriptLiteral).join(",") + "]";
    }
    if (isPlainObject(value)) {
      var parts = [];
      for (var key in value) {
        if (!Object.prototype.hasOwnProperty.call(value, key)) { continue; }
        parts.push(safeKey(key) + ":" + toExtendScriptLiteral(value[key]));
      }
      return "{" + parts.join(",") + "}";
    }
    return "null";
  }

  function quoteString(s) {
    var out = '"';
    for (var i = 0; i < s.length; i++) {
      var c = s.charAt(i);
      var code = s.charCodeAt(i);
      if (c === '"') { out += '\\"'; }
      else if (c === "\\") { out += "\\\\"; }
      else if (c === "\n") { out += "\\n"; }
      else if (c === "\r") { out += "\\r"; }
      else if (c === "\t") { out += "\\t"; }
      else if (code < 32 || code > 126) {
        /* Кириллица и любые не-ASCII — через \uXXXX: надёжнее всего,
           потому что кодировка файла и настройки AE могут отличаться. */
        out += "\\u" + ("0000" + code.toString(16)).slice(-4);
      } else { out += c; }
    }
    return out + '"';
  }

  function safeKey(key) {
    return /^[A-Za-z_$][A-Za-z0-9_$]*$/.test(key) ? key : quoteString(key);
  }

  /* ------------------------------------------------------- специфичные вызовы */

  var Bridge = {
    available: function () { return HAS_CSINTERFACE; },

    ping: function () { return callHost("ping"); },
    getProjectInfo: function () { return callHost("getProjectInfo"); },
    getFonts: function () { return callHost("getFonts"); },
    getAudioSource: function (req) { return callHost("getAudioSource", req); },
    renderCompAudio: function (req) { return callHost("renderCompAudio", req, 600000); },
    applySubtitles: function (req) { return callHost("applySubtitles", req, 120000); },
    updateStyleOnly: function (req) { return callHost("updateStyleOnly", req, 30000); },
    updatePosition: function (req) { return callHost("updatePosition", req, 15000); },
    removeTimpocLayers: function () { return callHost("removeTimpocLayers"); },
    measureText: function (req) { return callHost("measureText", req); },

    /* Открыть композицию в AE — удобно после применения. */
    openInAe: function () {
      if (!HAS_CSINTERFACE) { return Promise.resolve({ ok: false }); }
      return new Promise(function (resolve) {
        try {
          cs.evalScript("app.project.activeItem.openInViewer ? app.project.activeItem.openInViewer() : null", function () {
            resolve({ ok: true });
          });
        } catch (e) { resolve({ ok: false }); }
      });
    },

    /* Диалог выбора файла. Возвращает путь или null. */
    chooseFile: function (title, filter) {
      if (!HAS_CSINTERFACE) { return Promise.resolve(null); }
      return new Promise(function (resolve) {
        try {
          var path = cs.openURLInDefaultBrowser ? null : null;
          /* openDialog доступен в CEP через CSInterface.requestOpenExtension
             только для расширений; надёжнее — нативный input[type=file] в панели
             или путь, сохранённый движком. Возвращаем null, панель покажет
             поле ввода пути. */
          resolve(path);
        } catch (e) { resolve(null); }
      });
    },

    theme: function () {
      if (!HAS_CSINTERFACE || !cs.getHostEnvironment) { return null; }
      try {
        var env = cs.getHostEnvironment();
        var skin = env && env.appSkinInfo && env.appSkinInfo.panelBackgroundColor;
        return skin || null;
      } catch (e) { return null; }
    },

    onThemeChange: function (cb) {
      if (!HAS_CSINTERFACE || !cs.addEventListener) { return; }
      try {
        cs.addEventListener("com.adobe.csxs.events.ThemeColorChanged", function () { cb(Bridge.theme()); });
      } catch (e) { /* не критично */ }
    }
  };

  global.TimpocBridge = Bridge;
})(typeof window !== "undefined" ? window : this);
