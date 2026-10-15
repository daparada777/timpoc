/*
 * timpoc — поиск установленных версий After Effects.
 *
 * Зачем это отдельный модуль: установщик должен сам найти AE, а не заставлять
 * пользователя копаться в папках. И этот же модуль должен отвечать честно:
 * если AE не найден или версия слишком старая — сказать об этом прямо.
 *
 * Проверяем два источника, потому что по отдельности они ненадёжны:
 *   1) реестр Windows (HKLM\SOFTWARE\Adobe\After Effects\<ver>) — там лежит
 *      точный путь установки;
 *   2) папки "C:\Program Files\Adobe\Adobe After Effects <год>" — на случай,
 *      если реестр повреждён или AE поставлен не через Creative Cloud.
 *
 * На macOS реестра нет, поэтому смотрим /Applications и читаем версию из
 * Info.plist приложения.
 *
 * Модуль написан с внедрением зависимостей (fs/registry передаются снаружи),
 * чтобы его можно было протестировать на любой ОС — это не «красивая
 * абстракция», а единственный способ проверить логику без Windows.
 */

"use strict";

const nodePath = require("path");

/*
 * Важно: используем path той платформы, ДЛЯ КОТОРОЙ ищем AE, а не той,
 * на которой запущен установщик. Иначе при тестах (или при запуске на macOS
 * с целью Windows) получились бы пути со смешанными разделителями
 * вида "C:\Program Files\Adobe/After Effects 2026/Support Files/AfterFX.exe".
 */
function pathFor(platform) {
  if (platform === "win32") { return nodePath.win32; }
  if (platform === "darwin") { return nodePath.posix; }
  return nodePath;
}

/* Соответствие внутренней версии AE и года в названии.
   25.x = AE 2025, 26.x = AE 2026. Нужен минимум 25.0 — это CEP 12. */
const AE_YEAR_BY_MAJOR = {
  16: "2019",
  17: "2020",
  18: "2021",
  22: "2022",
  23: "2023",
  24: "2024",
  25: "2025",
  26: "2026",
  27: "2027"
};

/* Минимальная версия, которую мы поддерживаем.
   25.0 = AE 2025 = CEP 12. Более старые технически могут работать,
   но мы их не заявляем, пока не проверим. */
const MIN_MAJOR = 25;

function parseVersion(raw) {
  const m = /^(\d+)(?:\.(\d+))?(?:\.(\d+))?/.exec(String(raw || "").trim());
  if (!m) { return null; }
  return {
    major: parseInt(m[1], 10),
    minor: parseInt(m[2] || "0", 10),
    patch: parseInt(m[3] || "0", 10),
    text: String(raw).trim()
  };
}

function yearFor(major) {
  return AE_YEAR_BY_MAJOR[major] || String(2000 + major);
}

/*
 * Разобрать вывод `reg query`.
 * Формат строк: "    InstallPath    REG_SZ    C:\Program Files\Adobe\..."
 * Возвращаем значение InstallPath, если оно есть.
 */
function parseRegOutput(stdout) {
  const out = {};
  String(stdout || "").split(/\r?\n/).forEach(function (line) {
    const m = /^\s{4}([A-Za-z0-9_ ]+?)\s{4,}(REG_[A-Z_]+)\s{4,}(.+?)\s*$/.exec(line);
    if (m) { out[m[1].trim()] = m[3]; }
  });
  return out;
}

/* Список версий из вывода `reg query ... /f` или перечисления подобразделов. */
function parseRegSubkeys(stdout) {
  const keys = [];
  String(stdout || "").split(/\r?\n/).forEach(function (line) {
    const m = /\\Adobe\\After Effects\\([0-9]+\.[0-9]+)\s*$/.exec(line.trim());
    if (m) { keys.push(m[1]); }
  });
  return keys;
}

/*
 * Главная функция.
 *
 * deps = { platform, fs, run } — внедряемые зависимости:
 *   platform : "win32" | "darwin" | ...
 *   fs       : объект с existsSync / readdirSync / readFileSync / statSync
 *   run      : (cmd) => { status, stdout } — выполнение команды синхронно
 *
 * Возвращает массив найденных версий:
 *   { version, major, year, path, supported, reason }
 */
function detectAfterEffects(deps) {
  const platform = deps.platform || process.platform;
  if (platform === "win32") { return detectWindows(deps); }
  if (platform === "darwin") { return detectMac(deps); }
  return {
    found: [],
    error: {
      code: "unsupported_os",
      message: "Эта операционная система не поддерживается.",
      hint: "timpoc работает на Windows 10/11 (64-бит) и macOS 12+."
    }
  };
}

function detectWindows(deps) {
  const fs = deps.fs;
  const run = deps.run;
  const path = pathFor("win32");
  const found = new Map();

  /* 1) Реестр — самый надёжный источник пути. */
  try {
    const res = run(
      'reg query "HKLM\\SOFTWARE\\Adobe\\After Effects" /f "*" /k'
    );
    if (res && res.status === 0) {
      parseRegSubkeys(res.stdout).forEach(function (ver) {
        addFromRegistry(found, ver, deps);
      });
    }
  } catch (e) {
    /* reg может быть недоступен в редких конфигурациях — не падаем,
       дальше попробуем по папкам. */
  }

  /* 2) Папки Program Files — запасной путь. */
  const roots = [
    process.env["ProgramFiles"] || "C:\\Program Files",
    process.env["ProgramFiles(x86)"] || "C:\\Program Files (x86)"
  ];
  roots.forEach(function (root) {
    const adobeDir = path.join(root, "Adobe");
    let entries = [];
    try {
      entries = fs.readdirSync(adobeDir);
    } catch (e) {
      return;
    }
    entries.forEach(function (name) {
      const m = /^Adobe After Effects\s*(\d{4})$/i.exec(name);
      if (!m) { return; }
      const year = m[1];
      const candidate = path.join(adobeDir, name);
      const exe = path.join(candidate, "Support Files", "AfterFX.exe");
      if (!safeExists(fs, exe)) { return; }
      const major = majorFromYear(year);
      addFound(found, major + ".0", major, year, candidate, "win32");
    });
  });

  return summarize(found);
}

function addFromRegistry(found, versionText, deps) {
  const path = pathFor("win32");
  const parsed = parseVersion(versionText);
  if (!parsed) { return; }
  let installPath = "";
  try {
    const res = deps.run(
      'reg query "HKLM\\SOFTWARE\\Adobe\\After Effects\\' + versionText + '" /v InstallPath'
    );
    if (res && res.status === 0) {
      const values = parseRegOutput(res.stdout);
      installPath = values.InstallPath || "";
    }
  } catch (e) {
    installPath = "";
  }
  if (!installPath) { return; }
  const exe = path.join(installPath, "Support Files", "AfterFX.exe");
  if (!safeExists(deps.fs, exe)) { return; }
  addFound(found, versionText, parsed.major, yearFor(parsed.major), installPath, "win32");
}

function detectMac(deps) {
  const fs = deps.fs;
  const path = pathFor("darwin");
  const found = new Map();
  const roots = ["/Applications", "/Applications/Adobe"];

  roots.forEach(function (root) {
    let entries = [];
    try {
      entries = fs.readdirSync(root);
    } catch (e) {
      return;
    }
    entries.forEach(function (name) {
      const m = /^Adobe After Effects\s*(\d{4})$/i.exec(name);
      if (!m) { return; }
      const year = m[1];
      const appPath = path.join(root, name, "Adobe After Effects " + year + ".app");
      if (!safeExists(fs, appPath)) { return; }
      const major = majorFromYear(year);
      addFound(found, major + ".0", major, year, appPath, "darwin");
    });
  });

  return summarize(found);
}

function majorFromYear(year) {
  const y = parseInt(year, 10);
  const entry = Object.keys(AE_YEAR_BY_MAJOR).find(function (k) {
    return AE_YEAR_BY_MAJOR[k] === String(y);
  });
  return entry ? parseInt(entry, 10) : y - 2000;
}

/*
 * Нормализуем путь для сравнения: реестр часто отдаёт путь со слешем на конце
 * ("...\Adobe After Effects 2026\"), а обход папок — без него. Без приведения
 * одна и та же установка попадала в список дважды.
 * На Windows регистр не важен, поэтому приводим к нижнему.
 */
function normalizeForCompare(p, platform) {
  let s = String(p || "").replace(/[\\/]+$/, "");
  if (platform === "win32") { s = s.toLowerCase(); }
  return s;
}

function addFound(map, versionText, major, year, installPath, platform) {
  const key = major + "|" + normalizeForCompare(installPath, platform);
  if (map.has(key)) { return; }
  map.set(key, {
    version: versionText,
    major: major,
    year: year,
    path: installPath,
    supported: major >= MIN_MAJOR,
    reason: major >= MIN_MAJOR
      ? ""
      : "Версия AE " + year + " старше поддерживаемой. Нужен After Effects 2025 (25.x) или новее."
  });
}

function summarize(map) {
  const list = Array.from(map.values()).sort(function (a, b) {
    return b.major - a.major;
  });
  const supported = list.filter(function (v) { return v.supported; });
  return {
    found: list,
    error: list.length === 0
      ? {
          code: "ae_not_found",
          message: "After Effects не найден на этом компьютере.",
          hint: "Установите After Effects 2025 или новее через Adobe Creative Cloud, затем запустите установщик timpoc заново."
        }
      : (supported.length === 0
          ? {
              code: "ae_too_old",
              message: "Найден After Effects, но версия слишком старая.",
              hint: "timpoc требует After Effects 2025 (25.x) или новее — это версия CEP 12."
            }
          : null)
  };
}

function safeExists(fs, p) {
  try { return fs.existsSync(p); } catch (e) { return false; }
}

/*
 * Папка расширений CEP.
 *
 * Выбираем пользовательскую область (%APPDATA%), а не ProgramData:
 *   * не нужны права администратора;
 *   * установка не трогает общесистемные папки;
 *   * удаление timpoc не оставляет следов в Program Files.
 */
function cepExtensionsDir(deps) {
  const platform = deps.platform || process.platform;
  const path = pathFor(platform);
  if (platform === "win32") {
    const base = process.env.APPDATA ||
      path.join(process.env.USERPROFILE || "C:\\Users\\Default", "AppData", "Roaming");
    return path.join(base, "Adobe", "CEP", "extensions");
  }
  if (platform === "darwin") {
    const home = process.env.HOME || "/Users/Shared";
    return path.join(home, "Library", "Application Support", "Adobe", "CEP", "extensions");
  }
  return "";
}

/* Папка данных timpoc (пресеты, журнал, состояние проекта). */
function timpocDataDir(deps) {
  const platform = deps.platform || process.platform;
  const path = pathFor(platform);
  if (platform === "win32") {
    const base = process.env.APPDATA ||
      path.join(process.env.USERPROFILE || "C:\\Users\\Default", "AppData", "Roaming");
    return path.join(base, "timpoc");
  }
  if (platform === "darwin") {
    const home = process.env.HOME || "/Users/Shared";
    return path.join(home, "Library", "Application Support", "timpoc");
  }
  return "";
}

module.exports = {
  detectAfterEffects,
  pathFor,
  normalizeForCompare,
  cepExtensionsDir,
  timpocDataDir,
  parseRegOutput,
  parseRegSubkeys,
  parseVersion,
  yearFor,
  MIN_MAJOR,
  AE_YEAR_BY_MAJOR
};
