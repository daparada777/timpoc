/*
 * timpoc — клиент локального движка распознавания.
 *
 * Панель и движок — разные процессы. Движок слушает 127.0.0.1 на случайном
 * порту и требует токен. Порт и токен движок пишет в runtime-файл, а читает
 * его Node.js внутри панели (поэтому в манифесте включён --enable-nodejs).
 *
 * Почему не «жёстко зашитый порт»: случайный порт + токен означают, что
 * другая программа на этом компьютере не сможет ни занять наш порт и сломать
 * работу, ни отправить запрос к нашему движку.
 *
 * Приватность: все запросы идут на 127.0.0.1. Наружу не уходит ничего.
 */

(function (global) {
  "use strict";

  var POLL_MS = 300;
  var DEFAULT_PORT = 8731;

  function dataDir() {
    var home = global.process && global.process.env
      ? (global.process.env.APPDATA ||
         (global.process.platform === "darwin"
           ? global.process.env.HOME + "/Library/Application Support"
           : (global.process.env.XDG_CONFIG_HOME || global.process.env.HOME + "/.config")))
      : "";
    return home + "/timpoc";
  }

  function readRuntimeFile() {
    /* Читаем runtime.json: { port, token, pid, version }. */
    try {
      if (!global.require) { return null; }
      var fs = require("fs");
      var path = dataDir() + "/runtime.json";
      if (!fs.existsSync(path)) { return null; }
      var raw = fs.readFileSync(path, "utf8");
      var data = JSON.parse(raw);
      if (!data || !data.port) { return null; }
      return data;
    } catch (e) {
      return null;
    }
  }

  function Engine() {
    this.port = DEFAULT_PORT;
    this.token = "";
    this.connected = false;
    this.version = "";
    this._job = null;
    this._timer = null;
  }

  Engine.prototype.refreshRuntime = function () {
    var rt = readRuntimeFile();
    if (rt) {
      this.port = rt.port;
      this.token = rt.token || "";
    }
    return rt;
  };

  Engine.prototype.url = function (path) {
    return "http://127.0.0.1:" + this.port + path;
  };

  Engine.prototype.request = function (method, path, body) {
    var self = this;
    return new Promise(function (resolve) {
      var xhr = new XMLHttpRequest();
      xhr.open(method, self.url(path), true);
      xhr.timeout = 30000;
      if (self.token) { xhr.setRequestHeader("X-Timpoc-Token", self.token); }
      xhr.setRequestHeader("Content-Type", "application/json; charset=utf-8");

      xhr.onload = function () {
        var data;
        try {
          data = JSON.parse(xhr.responseText);
        } catch (e) {
          resolve({ ok: false, error: { code: "bad_response", message: "Движок вернул непонятный ответ.", hint: "Перезапустите timpoc через установщик." } });
          return;
        }
        resolve(data);
      };
      xhr.onerror = function () {
        resolve({
          ok: false,
          error: {
            code: "engine_offline",
            message: "Движок timpoc не отвечает.",
            hint: "Запустите timpoc из меню Пуск (или установщика) — он поднимает движок распознавания. After Effects сам движок не запускает."
          }
        });
      };
      xhr.ontimeout = function () {
        resolve({ ok: false, error: { code: "timeout", message: "Движок не ответил вовремя.", hint: "Повторите действие." } });
      };
      xhr.send(body ? JSON.stringify(body) : null);
    });
  };

  /* Проверка связи. Вызывается при открытии панели и по кнопке «Обновить». */
  Engine.prototype.health = function () {
    var self = this;
    this.refreshRuntime();
    return new Promise(function (resolve) {
      var xhr = new XMLHttpRequest();
      xhr.open("GET", self.url("/health"), true);
      xhr.timeout = 3000;
      xhr.onload = function () {
        try {
          var data = JSON.parse(xhr.responseText);
          self.connected = !!(data && data.ok);
          self.version = (data && data.version) || "";
        } catch (e) {
          self.connected = false;
        }
        resolve(self.connected);
      };
      xhr.onerror = function () { self.connected = false; resolve(false); };
      xhr.ontimeout = function () { self.connected = false; resolve(false); };
      xhr.send();
    });
  };

  Engine.prototype.system = function () { return this.request("GET", "/system"); };
  Engine.prototype.state = function () { return this.request("GET", "/state"); };
  Engine.prototype.settings = function () { return this.request("GET", "/settings"); };
  Engine.prototype.presets = function () { return this.request("GET", "/presets"); };
  Engine.prototype.diagnostics = function () { return this.request("GET", "/diagnostics"); };

  Engine.prototype.transcribe = function (payload) { return this.request("POST", "/transcribe", payload); };
  Engine.prototype.cancelJob = function (id) { return this.request("POST", "/job/cancel", { id: id }); };
  Engine.prototype.downloadModel = function (model) { return this.request("POST", "/models/download", { model: model }); };
  Engine.prototype.resegment = function (payload) { return this.request("POST", "/resegment", payload); };
  Engine.prototype.updateBlock = function (payload) { return this.request("POST", "/blocks/update", payload); };
  Engine.prototype.deleteBlock = function (payload) { return this.request("POST", "/blocks/delete", payload); };
  Engine.prototype.mergeBlock = function (payload) { return this.request("POST", "/blocks/merge", payload); };
  Engine.prototype.shift = function (payload) { return this.request("POST", "/shift", payload); };
  Engine.prototype.exportSrt = function (payload) { return this.request("POST", "/srt/export", payload); };
  Engine.prototype.importSrt = function (payload) { return this.request("POST", "/srt/import", payload); };
  Engine.prototype.checkFont = function (payload) { return this.request("POST", "/fonts/check", payload); };
  Engine.prototype.savePreset = function (payload) { return this.request("POST", "/presets/save", payload); };
  Engine.prototype.deletePreset = function (payload) { return this.request("POST", "/presets/delete", payload); };
  Engine.prototype.layoutCheck = function (payload) { return this.request("POST", "/layout/check", payload); };
  Engine.prototype.updateStyle = function (payload) { return this.request("POST", "/style", payload); };
  Engine.prototype.updateLayout = function (payload) { return this.request("POST", "/layout", payload); };
  Engine.prototype.updateSettings = function (payload) { return this.request("POST", "/settings", payload); };

  /*
   * Запуск задачи с опросом прогресса.
   *
   * onProgress({progress, message}) — для полосы и подписи;
   * Возвращает Promise с результатом или ошибкой.
   * Отмена: вызовите engine.cancelCurrent().
   */
  Engine.prototype.runJob = function (starter, onProgress) {
    var self = this;
    return new Promise(function (resolve) {
      starter().then(function (startRes) {
        if (!startRes || !startRes.ok || !startRes.job) {
          resolve(startRes || { ok: false, error: { code: "no_job", message: "Не удалось запустить задачу." } });
          return;
        }
        var jobId = startRes.job.id;
        self._job = jobId;

        function poll() {
          self.request("GET", "/job/" + jobId).then(function (res) {
            if (!res || !res.ok || !res.job) {
              self._job = null;
              resolve(res || { ok: false, error: { code: "job_lost", message: "Задача потеряна." } });
              return;
            }
            var job = res.job;
            if (onProgress) { onProgress({ progress: job.progress, message: job.message, logs: job.logs }); }

            if (job.status === "done") {
              self._job = null;
              resolve({ ok: true, result: job.result });
            } else if (job.status === "error") {
              self._job = null;
              resolve({ ok: false, error: job.error || { code: "job_error", message: "Задача завершилась с ошибкой." } });
            } else if (job.status === "cancelled") {
              self._job = null;
              resolve({ ok: false, cancelled: true, error: { code: "cancelled", message: "Отменено пользователем.", hint: "Можно запустить заново." } });
            } else {
              self._timer = setTimeout(poll, POLL_MS);
            }
          });
        }
        poll();
      });
    });
  };

  Engine.prototype.cancelCurrent = function () {
    if (!this._job) { return Promise.resolve({ ok: true, nothing: true }); }
    return this.cancelJob(this._job);
  };

  /* Путь к папке данных — показываем пользователю в диагностике. */
  Engine.prototype.getDataDir = function () { return dataDir(); };

  global.TimpocEngine = new Engine();
})(typeof window !== "undefined" ? window : this);
