/*
 * timpoc — мост между интерфейсом установщика и главным процессом.
 *
 * contextIsolation включён, nodeIntegration выключен: страница не имеет
 * доступа к Node.js и не может вызвать произвольную команду. Она видит
 * только эти четыре метода. Это сознательно: установщик работает с реестром
 * и системными папками, давать странице прямой доступ нельзя.
 */

"use strict";

const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("timpoc", {
  detect: function () { return ipcRenderer.invoke("detect"); },
  check: function (arg) { return ipcRenderer.invoke("check", arg); },
  install: function (arg) { return ipcRenderer.invoke("install", arg); },
  uninstall: function (arg) { return ipcRenderer.invoke("uninstall", arg); },
  state: function () { return ipcRenderer.invoke("state"); },
  window: function (arg) { return ipcRenderer.invoke("window", arg); },
  onProgress: function (cb) {
    ipcRenderer.on("progress", function (event, data) { cb(data); });
  }
});
