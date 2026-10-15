/*
 * timpoc — главный процесс установщика (Electron).
 *
 * Здесь только «клей»: окно, IPC-вызовы и последовательность шагов.
 * Вся настоящая логика живёт в detect.js и install.js — они протестированы
 * отдельно и не знают про Electron, поэтому их можно проверить на любой ОС.
 *
 * Последовательность установки:
 *   1. найти версии After Effects;
 *   2. проверить место, права, не запущен ли AE;
 *   3. скопировать панель в папку CEP;
 *   4. включить режим разработчика (если нужна неподписанная установка);
 *   5. показать, где открыть панель.
 * При ошибке на любом шаге — откат.
 */

"use strict";

const { app, BrowserWindow, ipcMain } = require("electron");
const path = require("node:path");
const fs = require("node:fs");
const { execSync } = require("node:child_process");

const detect = require("./detect.js");
const installer = require("./install.js");

const deps = {
  platform: process.platform,
  fs: fs,
  run: function (cmd) {
    try {
      const stdout = execSync(cmd, { encoding: "utf8", timeout: 15000, windowsHide: true });
      return { status: 0, stdout: stdout };
    } catch (e) {
      return { status: e.status === undefined ? 1 : e.status, stdout: e.stdout || "" };
    }
  }
};

/* Где лежат ресурсы: в собранном приложении — рядом с exe, в разработке — в проекте. */
function resourcesDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "extension");
  }
  return path.resolve(__dirname, "..", "..", "panel");
}

function engineResourcesDir() {
  if (app.isPackaged) {
    return path.join(process.resourcesPath, "engine");
  }
  return path.resolve(__dirname, "..", "resources", "engine");
}

let win = null;

function createWindow() {
  win = new BrowserWindow({
    width: 720,
    height: 640,
    minWidth: 560,
    minHeight: 520,
    resizable: true,
    frame: false,
    transparent: false,
    backgroundColor: "#14101d",
    title: "timpoc",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: false
    }
  });

  win.loadFile(path.join(__dirname, "ui", "index.html"));
}

/* ------------------------------------------------------------------- IPC */

function handle(channel, fn) {
  ipcMain.handle(channel, async function (event, arg) {
    try {
      return { ok: true, data: await fn(arg) };
    } catch (e) {
      return {
        ok: false,
        error: {
          code: e.code || "internal",
          message: e.message || "Непредвиденная ошибка установщика.",
          hint: e.hint || ""
        }
      };
    }
  });
}

function registerIpc() {
  handle("detect", function () {
    const res = detect.detectAfterEffects(deps);
    return {
      found: res.found,
      error: res.error,
      platform: deps.platform,
      cepDir: detect.cepExtensionsDir(deps),
      dataDir: detect.timpocDataDir(deps),
      resources: resourcesDir(),
      engineBundled: fs.existsSync(path.join(engineResourcesDir(), "timpoc-engine.exe")) ||
                     fs.existsSync(path.join(engineResourcesDir(), "timpoc-engine"))
    };
  });

  handle("check", function (arg) {
    return installer.checkPrerequisites({
      deps: deps,
      targetDir: arg.targetDir,
      cepDir: detect.cepExtensionsDir(deps),
      needMb: arg.needMb || 250
    });
  });

  handle("install", function (arg) {
    const res = installer.install({
      deps: deps,
      sourceDir: resourcesDir(),
      cepDir: detect.cepExtensionsDir(deps),
      enableDebug: arg.enableDebug !== false,
      onProgress: function (done, total, label) {
        if (win && !win.isDestroyed()) {
          win.webContents.send("progress", { done: done, total: total, label: label });
        }
      }
    });
    return {
      targetDir: res.targetDir,
      debugEnabled: res.debugEnabled,
      menuPath: deps.platform === "win32"
        ? "Window → Extensions → timpoc — субтитры"
        : "Окно → Расширения → timpoc — субтитры"
    };
  });

  handle("uninstall", function (arg) {
    return installer.uninstall({
      deps: deps,
      cepDir: detect.cepExtensionsDir(deps),
      dataDir: detect.timpocDataDir(deps),
      removeUserData: !!arg.removeUserData,
      disableDebug: !!arg.disableDebug
    });
  });

  handle("state", function () {
    const cepDir = detect.cepExtensionsDir(deps);
    const installed = fs.existsSync(path.join(cepDir, installer.EXTENSION_ID));
    return {
      installed: installed,
      debugEnabled: installer.isDebugEnabled(deps),
      dataDir: detect.timpocDataDir(deps)
    };
  });

  handle("window", function (arg) {
    if (!win) { return; }
    if (arg.action === "minimize") { win.minimize(); }
    if (arg.action === "close") { app.quit(); }
  });
}

app.whenReady().then(function () {
  registerIpc();
  createWindow();
  app.on("activate", function () {
    if (BrowserWindow.getAllWindows().length === 0) { createWindow(); }
  });
});

app.on("window-all-closed", function () {
  if (process.platform !== "darwin") { app.quit(); }
});
