/*
 * Тесты установки и удаления.
 *
 * Здесь проверяется главное для установщика: откат при сбое и то, что
 * удаление не трогает чужие файлы. Тестируем на реальной временной папке —
 * это честная проверка, а не имитация.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const {
  EXTENSION_ID,
  checkPrerequisites,
  install,
  uninstall,
  enableDebugMode,
  isDebugEnabled,
  walk
} = require("../src/install.js");

function tmp(name) {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), "timpoc-test-" + name + "-"));
  return dir;
}

/* Создаём «исходники расширения» — как в собранном установщике. */
function makeSource() {
  const src = tmp("src");
  fs.mkdirSync(path.join(src, "CSXS"), { recursive: true });
  fs.mkdirSync(path.join(src, "host"), { recursive: true });
  fs.writeFileSync(path.join(src, "index.html"), "<html></html>");
  fs.writeFileSync(path.join(src, "CSXS", "manifest.xml"), "<ExtensionManifest/>");
  fs.writeFileSync(path.join(src, "host", "timpoc_host.jsx"), "// host");
  fs.writeFileSync(path.join(src, ".debug"), "<ExtensionList/>");
  return src;
}

function depsFor(platform) {
  const calls = [];
  return {
    platform: platform || "win32",
    fs: fs,
    run: function (cmd) {
      calls.push(cmd);
      if (/reg add/i.test(cmd)) { return { status: 0, stdout: "" }; }
      if (/reg query/i.test(cmd)) { return { status: 0, stdout: "    PlayerDebugMode    REG_DWORD    0x1" }; }
      if (/tasklist/i.test(cmd)) { return { status: 0, stdout: "AfterFX.exe   1234" }; }
      return { status: 0, stdout: "" };
    },
    calls: calls
  };
}

/* --------------------------------------------------------------- установка */

test("установка копирует все файлы расширения, включая .debug", function () {
  const src = makeSource();
  const cep = tmp("cep");
  const res = install({
    deps: depsFor(),
    sourceDir: src,
    cepDir: cep,
    enableDebug: true
  });

  assert.strictEqual(path.basename(res.targetDir), EXTENSION_ID);
  assert.ok(fs.existsSync(path.join(res.targetDir, "index.html")));
  assert.ok(fs.existsSync(path.join(res.targetDir, "CSXS", "manifest.xml")));
  assert.ok(fs.existsSync(path.join(res.targetDir, "host", "timpoc_host.jsx")));
  assert.ok(fs.existsSync(path.join(res.targetDir, ".debug")), ".debug обязателен для режима разработчика");
});

test("установка создаёт папку CEP, если её ещё нет", function () {
  const src = makeSource();
  const base = tmp("base");
  const cep = path.join(base, "Adobe", "CEP", "extensions");
  assert.ok(!fs.existsSync(cep));
  install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });
  assert.ok(fs.existsSync(cep));
});

test("прогресс сообщается по каждому файлу", function () {
  const src = makeSource();
  const cep = tmp("cep2");
  const seen = [];
  install({
    deps: depsFor(),
    sourceDir: src,
    cepDir: cep,
    enableDebug: false,
    onProgress: function (done, total, label) { seen.push([done, total, label]); }
  });
  assert.ok(seen.length >= 4, "должно быть не меньше 4 файлов");
  assert.strictEqual(seen[seen.length - 1][0], seen[seen.length - 1][1], "последний шаг = 100%");
});

test("повторная установка делает резервную копию прежней версии", function () {
  const src = makeSource();
  const cep = tmp("cep3");
  const first = install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });
  assert.strictEqual(first.backup, null);

  fs.writeFileSync(path.join(first.targetDir, "index.html"), "СТАРАЯ ВЕРСИЯ");
  const second = install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });

  assert.ok(second.backup, "должна быть создана резервная копия");
  assert.ok(fs.existsSync(second.backup));
  const old = fs.readFileSync(path.join(second.backup, "index.html"), "utf8");
  assert.strictEqual(old, "СТАРАЯ ВЕРСИЯ");
  const now = fs.readFileSync(path.join(second.targetDir, "index.html"), "utf8");
  assert.strictEqual(now, "<html></html>");
});

/* ------------------------------------------------------------------ откат */

test("сбой копирования откатывает установку и возвращает прежнюю версию", function () {
  const src = makeSource();
  const cep = tmp("cep4");

  /* Ставим рабочую версию. */
  const first = install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });
  fs.writeFileSync(path.join(first.targetDir, "index.html"), "РАБОЧАЯ ВЕРСИЯ");

  /* Ломаем копирование на середине. */
  const brokenFs = Object.assign({}, fs, {
    copyFileSync: function (from, to) {
      if (/manifest\.xml$/.test(to)) {
        const e = new Error("нет прав");
        e.code = "EPERM";
        throw e;
      }
      return fs.copyFileSync(from, to);
    }
  });

  assert.throws(function () {
    install({
      deps: { platform: "win32", fs: brokenFs, run: function () { return { status: 0, stdout: "" }; } },
      sourceDir: src,
      cepDir: cep,
      enableDebug: false
    });
  }, /Не удалось скопировать файлы панели/);

  /* Прежняя рабочая версия должна вернуться на место. */
  assert.ok(fs.existsSync(first.targetDir), "папка расширения должна остаться");
  const restored = fs.readFileSync(path.join(first.targetDir, "index.html"), "utf8");
  assert.strictEqual(restored, "РАБОЧАЯ ВЕРСИЯ", "должна вернуться прежняя версия");

  /* Прежняя версия возвращается ЦЕЛИКОМ, включая manifest.xml.
     Это осознанно: CEP не загружает расширение без манифеста, поэтому
     «частично новая» установка была бы хуже, чем рабочая старая. */
  assert.ok(fs.existsSync(path.join(first.targetDir, "CSXS", "manifest.xml")));
});

/* --------------------------------------------------------------- удаление */

test("удаление стирает расширение, но оставляет данные пользователя", function () {
  const src = makeSource();
  const cep = tmp("cep5");
  const data = tmp("data");
  fs.writeFileSync(path.join(data, "project_state.json"), "{}");

  const res = install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });
  assert.ok(fs.existsSync(res.targetDir));

  const out = uninstall({
    deps: depsFor(),
    cepDir: cep,
    dataDir: data,
    removeUserData: false
  });

  assert.ok(!fs.existsSync(res.targetDir), "расширение удалено");
  assert.ok(fs.existsSync(path.join(data, "project_state.json")), "данные пользователя сохранены");
  assert.strictEqual(out.keptUserData, true);
});

test("удаление с removeUserData стирает и данные", function () {
  const src = makeSource();
  const cep = tmp("cep6");
  const data = tmp("data2");
  install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });

  const out = uninstall({
    deps: depsFor(),
    cepDir: cep,
    dataDir: data,
    removeUserData: true
  });

  assert.ok(!fs.existsSync(data));
  assert.strictEqual(out.keptUserData, false);
});

test("удаление не трогает чужие расширения", function () {
  const src = makeSource();
  const cep = tmp("cep7");
  install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });

  const other = path.join(cep, "com.someone.else");
  fs.mkdirSync(other, { recursive: true });
  fs.writeFileSync(path.join(other, "index.html"), "чужое");

  uninstall({ deps: depsFor(), cepDir: cep, dataDir: tmp("d3"), removeUserData: false });

  assert.ok(fs.existsSync(path.join(other, "index.html")), "чужое расширение не должно пострадать");
  assert.ok(!fs.existsSync(path.join(cep, EXTENSION_ID)));
});

test("удаление подчищает резервные копии от прошлых установок", function () {
  const src = makeSource();
  const cep = tmp("cep8");
  install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });
  install({ deps: depsFor(), sourceDir: src, cepDir: cep, enableDebug: false });

  const backups = fs.readdirSync(cep).filter(function (n) { return /\.bak-/.test(n); });
  assert.ok(backups.length >= 1, "резервная копия должна появиться");

  uninstall({ deps: depsFor(), cepDir: cep, dataDir: tmp("d4"), removeUserData: false });

  const left = fs.readdirSync(cep).filter(function (n) { return /\.bak-/.test(n); });
  assert.strictEqual(left.length, 0, "резервные копии должны быть убраны");
});

test("удаление, когда ничего не установлено, не падает", function () {
  const cep = tmp("cep9");
  const out = uninstall({ deps: depsFor(), cepDir: cep, dataDir: tmp("d5"), removeUserData: false });
  assert.strictEqual(out.removed.length, 0);
});

/* ------------------------------------------------------------- реестр */

test("режим разработчика включается и обнаруживается", function () {
  const deps = depsFor();
  assert.strictEqual(enableDebugMode(deps), true);
  assert.ok(deps.calls.some(function (c) { return /PlayerDebugMode/.test(c); }));
  assert.strictEqual(isDebugEnabled(deps), true);
});

test("на macOS режим разработчика не требует реестра", function () {
  const deps = depsFor("darwin");
  assert.strictEqual(enableDebugMode(deps), true);
  assert.strictEqual(deps.calls.length, 0, "никаких системных команд на macOS");
});

/* --------------------------------------------------------------- проверки */

test("проверки требуют выбранную версию AE", function () {
  const res = checkPrerequisites({
    deps: depsFor(),
    targetDir: null,
    cepDir: tmp("cepA")
  });
  assert.strictEqual(res.ok, false);
  assert.strictEqual(res.error.code, "no_target");
});

test("проверки предупреждают о запущенном After Effects", function () {
  const res = checkPrerequisites({
    deps: depsFor(),
    targetDir: tmp("ae"),
    cepDir: tmp("cepB"),
    needMb: 1
  });
  assert.strictEqual(res.ok, true);
  assert.ok(res.warnings.some(function (w) { return /After Effects сейчас запущен/.test(w); }));
});

test("проверки не падают, если место определить не удалось", function () {
  const deps = depsFor();
  const res = checkPrerequisites({
    deps: deps,
    targetDir: "/несуществующий/путь/которого/нет",
    cepDir: tmp("cepC"),
    needMb: 1
  });
  /* Либо ок с предупреждением, либо ошибка про место — но не исключение. */
  assert.ok(res.ok === true || res.error.code === "no_space");
});

test("walk обходит дерево файлов", function () {
  const src = makeSource();
  const files = walk(fs, src).sort();
  assert.ok(files.indexOf("index.html") !== -1);
  assert.ok(files.indexOf(path.join("CSXS", "manifest.xml")) !== -1);
  assert.ok(files.indexOf(path.join("host", "timpoc_host.jsx")) !== -1);
  assert.ok(files.indexOf(".debug") !== -1);
});
