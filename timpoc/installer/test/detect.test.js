/*
 * Тесты поиска After Effects.
 *
 * Проверяются на Linux через подставные fs/run — это единственный способ
 * убедиться, что логика верна, не имея под рукой Windows и установленного AE.
 */

"use strict";

const test = require("node:test");
const assert = require("node:assert");
const path = require("node:path");

const {
  detectAfterEffects,
  pathFor,
  cepExtensionsDir,
  timpocDataDir,
  parseRegOutput,
  parseRegSubkeys,
  parseVersion,
  yearFor
} = require("../src/detect.js");

/* ------------------------------------------------------- разбор вывода reg */

test("parseRegSubkeys достаёт версии из вывода reg query", function () {
  const stdout = [
    "",
    "HKEY_LOCAL_MACHINE\\SOFTWARE\\Adobe\\After Effects\\25.0",
    "HKEY_LOCAL_MACHINE\\SOFTWARE\\Adobe\\After Effects\\26.0",
    "HKEY_LOCAL_MACHINE\\SOFTWARE\\Adobe\\Other\\1.0",
    ""
  ].join("\r\n");
  assert.deepStrictEqual(parseRegSubkeys(stdout), ["25.0", "26.0"]);
});

test("parseRegOutput достаёт InstallPath", function () {
  const stdout = "    InstallPath    REG_SZ    C:\\Program Files\\Adobe\\Adobe After Effects 2026\\\r\n";
  const values = parseRegOutput(stdout);
  assert.ok(values.InstallPath, "InstallPath должен найтись");
  assert.ok(values.InstallPath.indexOf("After Effects 2026") !== -1);
});

test("parseRegOutput не падает на мусоре", function () {
  assert.deepStrictEqual(parseRegOutput("какой-то мусор"), {});
  assert.deepStrictEqual(parseRegOutput(""), {});
  assert.deepStrictEqual(parseRegOutput(null), {});
});

/* ------------------------------------------------------------- версии */

test("parseVersion разбирает 26.0.1", function () {
  const v = parseVersion("26.0.1");
  assert.strictEqual(v.major, 26);
  assert.strictEqual(v.minor, 0);
  assert.strictEqual(v.patch, 1);
});

test("parseVersion возвращает null на мусоре", function () {
  assert.strictEqual(parseVersion("не версия"), null);
  assert.strictEqual(parseVersion(""), null);
});

test("yearFor сопоставляет внутреннюю версию и год", function () {
  assert.strictEqual(yearFor(25), "2025");
  assert.strictEqual(yearFor(26), "2026");
});

/* ------------------------------------------------- поиск в Windows (подставка) */

function winDeps(regVersions, existingPaths) {
  const paths = new Set(existingPaths);
  return {
    platform: "win32",
    fs: {
      existsSync: function (p) { return paths.has(p); },
      readdirSync: function (dir) {
        if (dir === "C:\\Program Files\\Adobe") {
          return ["Adobe After Effects 2026", "Adobe Photoshop 2026"];
        }
        return [];
      }
    },
    run: function (cmd) {
      if (cmd.indexOf("/f") !== -1 && cmd.indexOf("After Effects") !== -1) {
        return {
          status: 0,
          stdout: regVersions.map(function (v) {
            return "HKEY_LOCAL_MACHINE\\SOFTWARE\\Adobe\\After Effects\\" + v;
          }).join("\r\n")
        };
      }
      const m = /After Effects\\([0-9.]+)" \/v InstallPath/.exec(cmd);
      if (m) {
        const year = yearFor(parseInt(m[1], 10));
        return {
          status: 0,
          stdout: "    InstallPath    REG_SZ    C:\\Program Files\\Adobe\\Adobe After Effects " + year + "\\\r\n"
        };
      }
      return { status: 1, stdout: "" };
    }
  };
}

test("находит AE 2026 из реестра и помечает как поддерживаемый", function () {
  const exe = path.win32.join("C:\\Program Files\\Adobe\\Adobe After Effects 2026", "Support Files", "AfterFX.exe");
  const res = detectAfterEffects(winDeps(["26.0"], [exe]));
  assert.strictEqual(res.error, null);
  assert.strictEqual(res.found.length, 1);
  assert.strictEqual(res.found[0].major, 26);
  assert.strictEqual(res.found[0].year, "2026");
  assert.strictEqual(res.found[0].supported, true);
});

test("находит AE по папке Program Files, если реестр пуст", function () {
  const exe = path.win32.join("C:\\Program Files\\Adobe\\Adobe After Effects 2026", "Support Files", "AfterFX.exe");
  const res = detectAfterEffects(winDeps([], [exe]));
  assert.strictEqual(res.found.length, 1);
  assert.strictEqual(res.found[0].year, "2026");
});

test("несколько версий сортируются от новой к старой", function () {
  const exe26 = path.win32.join("C:\\Program Files\\Adobe\\Adobe After Effects 2026", "Support Files", "AfterFX.exe");
  const exe25 = path.win32.join("C:\\Program Files\\Adobe\\Adobe After Effects 2025", "Support Files", "AfterFX.exe");
  const deps = winDeps(["25.0", "26.0"], [exe25, exe26]);
  const res = detectAfterEffects(deps);
  assert.strictEqual(res.found.length, 2);
  assert.strictEqual(res.found[0].major, 26);
  assert.strictEqual(res.found[1].major, 25);
});

test("старая версия находится, но помечается неподдерживаемой с объяснением", function () {
  const exe = path.win32.join("C:\\Program Files\\Adobe\\Adobe After Effects 2024", "Support Files", "AfterFX.exe");
  const res = detectAfterEffects(winDeps(["24.0"], [exe]));
  assert.strictEqual(res.found.length, 1);
  assert.strictEqual(res.found[0].supported, false);
  assert.ok(res.found[0].reason.indexOf("2025") !== -1, "должно быть сказано, какая версия нужна");
  assert.strictEqual(res.error.code, "ae_too_old");
});

test("если AE не найден — понятная ошибка, а не пустой список", function () {
  const res = detectAfterEffects(winDeps([], []));
  assert.strictEqual(res.found.length, 0);
  assert.strictEqual(res.error.code, "ae_not_found");
  assert.ok(res.error.hint.length > 10, "должна быть подсказка, что делать");
});

test("битый реестр не роняет поиск (падает reg — ищем по папкам)", function () {
  const exe = path.win32.join("C:\\Program Files\\Adobe\\Adobe After Effects 2026", "Support Files", "AfterFX.exe");
  const deps = winDeps([], [exe]);
  deps.run = function () { throw new Error("reg недоступен"); };
  const res = detectAfterEffects(deps);
  assert.strictEqual(res.found.length, 1);
});

test("запись в реестре без AfterFX.exe игнорируется", function () {
  const res = detectAfterEffects(winDeps(["26.0"], []));
  assert.strictEqual(res.found.length, 0);
});

/* -------------------------------------------------------------- macOS */

test("находит AE на macOS в /Applications", function () {
  const deps = {
    platform: "darwin",
    fs: {
      existsSync: function (p) {
        return p === "/Applications/Adobe After Effects 2026/Adobe After Effects 2026.app";
      },
      readdirSync: function (dir) {
        return dir === "/Applications" ? ["Adobe After Effects 2026"] : [];
      }
    },
    run: function () { return { status: 1, stdout: "" }; }
  };
  const res = detectAfterEffects(deps);
  assert.strictEqual(res.found.length, 1);
  assert.strictEqual(res.found[0].year, "2026");
});

test("неподдерживаемая ОС даёт явную ошибку", function () {
  const res = detectAfterEffects({ platform: "freebsd", fs: {}, run: function () { return {}; } });
  assert.strictEqual(res.error.code, "unsupported_os");
});

/* --------------------------------------------------------------- пути */

test("папка расширений CEP — в области пользователя, не в Program Files", function () {
  process.env.APPDATA = "C:\\Users\\Иван\\AppData\\Roaming";
  const dir = cepExtensionsDir({ platform: "win32" });
  const sep = pathFor("win32").sep;
  const expect = ["AppData", "Roaming", "Adobe", "CEP", "extensions"].join(sep);
  assert.ok(dir.indexOf(expect) !== -1, "получено: " + dir);
  assert.ok(dir.indexOf("Program Files") === -1, "не должны требоваться права администратора");
});

test("папка данных timpoc отдельна от папки расширения", function () {
  process.env.APPDATA = "C:\\Users\\Иван\\AppData\\Roaming";
  const cep = cepExtensionsDir({ platform: "win32" });
  const data = timpocDataDir({ platform: "win32" });
  assert.notStrictEqual(cep, data);
  assert.ok(data.indexOf("timpoc") !== -1);
});

test("пути на macOS корректны", function () {
  process.env.HOME = "/Users/ivan";
  const dir = cepExtensionsDir({ platform: "darwin" });
  assert.ok(dir.indexOf("/Library/Application Support/Adobe/CEP/extensions") !== -1, "получено: " + dir);
  assert.ok(dir.indexOf("\\") === -1, "в macOS-пути не должно быть обратных слешей");
});

test("кириллица в пути пользователя не ломает поиск папок", function () {
  process.env.APPDATA = "C:\\Users\\Иван Петров\\AppData\\Roaming";
  const dir = cepExtensionsDir({ platform: "win32" });
  assert.ok(dir.indexOf("Иван Петров") !== -1);
});
