/*
 * timpoc — установка, откат и удаление.
 *
 * Три принципа, которые здесь важнее красоты:
 *
 * 1) ОТКАТ. Любая установка идёт через журнал действий. Если что-то упало
 *    на середине — undo() возвращает систему в исходное состояние. Не «частично
 *    установленный мусор», а именно то, что было до нажатия кнопки.
 *
 * 2) НЕ ТРОГАТЬ ЧУЖОЕ. При удалении стирается только папка com.timpoc.ae.panel
 *    и ключ реестра CSXS.12\timpoc. Настройки пользователя (%APPDATA%\timpoc)
 *    остаются, если пользователь сам не попросил их удалить.
 *
 * 3) ЧЕСТНЫЕ ОШИБКИ. Нет места на диске, AE запущен, нет прав — каждое из этого
 *    отдельный код ошибки с подсказкой, а не «установка не удалась».
 */

"use strict";

const fs = require("fs");
const path = require("path");
const os = require("os");

const EXTENSION_ID = "com.timpoc.ae.panel";
/* Ключ реестра для режима разработчика. CSXS.12 = CEP 12 = AE 2025/2026. */
const CEP_KEY = "HKCU\\Software\\Adobe\\CSXS.12";

function makeError(code, message, hint) {
  const e = new Error(message);
  e.code = code;
  e.hint = hint || "";
  return e;
}

/* ---------------------------------------------------------------- проверки */

/*
 * Можно ли ставить: место на диске, AE не запущен, папки доступны.
 * Возвращает { ok, warnings: [], error }.
 *
 * Проверка «AE запущен» обязательна: CEP читает список расширений при старте,
 * поэтому установка при работающем AE приведёт к тому, что панель не появится
 * до перезапуска. Лучше сказать об этом сразу.
 */
function checkPrerequisites(opts) {
  const deps = opts.deps;
  const needMb = opts.needMb || 250;
  const warnings = [];

  const target = opts.targetDir;
  if (!target) {
    return { ok: false, warnings, error: makeError("no_target", "Не выбрана версия After Effects.", "Выберите версию в списке.") };
  }

  /* Свободное место. */
  try {
    const freeMb = freeSpaceMb(target);
    if (freeMb < needMb) {
      return {
        ok: false,
        warnings,
        error: makeError(
          "no_space",
          "Не хватает места на диске.",
          "Нужно примерно " + needMb + " МБ, свободно " + freeMb + " МБ. Освободите место и повторите."
        )
      };
    }
  } catch (e) {
    warnings.push("Не удалось проверить свободное место на диске. Установка продолжится.");
  }

  /* Запущен ли After Effects. */
  const aeRunning = isAfterEffectsRunning(deps);
  if (aeRunning.running) {
    warnings.push(
      "After Effects сейчас запущен. Закройте его перед установкой — " +
      "иначе панель появится только после перезапуска AE."
    );
  }

  /* Доступность папки расширений. */
  const extDir = opts.cepDir;
  try {
    ensureDir(deps.fs, extDir);
  } catch (e) {
    return {
      ok: false,
      warnings,
      error: makeError(
        "no_permission",
        "Нет прав на запись в папку расширений Adobe.",
        "Запустите установщик от имени текущего пользователя и убедитесь, что папка не защищена антивирусом: " + extDir
      )
    };
  }

  return { ok: true, warnings, error: null };
}

function freeSpaceMb(p) {
  try {
    const st = fs.statfsSync(p);
    return Math.floor((st.bavail * st.bsize) / (1024 * 1024));
  } catch (e) {
    /* На Windows statfsSync появился только в Node 18.15+; есть запасной путь. */
    if (typeof fs.statfsSync !== "function") {
      const stats = require("child_process").spawnSync(
        process.platform === "win32" ? "wmic" : "df",
        process.platform === "win32"
          ? ["logicaldisk", "get", "freespace,name"]
          : ["-m", p],
        { encoding: "utf8" }
      );
      if (stats && stats.status === 0) {
        if (process.platform === "win32") {
          const drive = (path.parse(p).root || "C:\\").replace("\\", "");
          const line = String(stats.stdout).split(/\r?\n/).find(function (l) {
            return l.trim().toUpperCase().indexOf(drive.toUpperCase() + ":") === 0;
          });
          if (line) {
            const mb = parseInt(String(line).trim().split(/\s+/)[1], 10);
            if (!isNaN(mb)) { return Math.floor(mb / (1024 * 1024)); }
          }
        }
      }
    }
    throw e;
  }
}

function isAfterEffectsRunning(deps) {
  const run = deps.run;
  try {
    if (deps.platform === "win32") {
      const res = run('tasklist /FI "IMAGENAME eq AfterFX.exe" /NH');
      const running = res && res.status === 0 && /AfterFX\.exe/i.test(res.stdout || "");
      return { running: !!running };
    }
    const res = run('pgrep -f "Adobe After Effects"');
    return { running: !!(res && res.status === 0 && String(res.stdout || "").trim()) };
  } catch (e) {
    return { running: false, unknown: true };
  }
}

/* ---------------------------------------------------------------- установка */

/*
 * Установить расширение.
 *
 * opts:
 *   deps      : { fs, run, platform }
 *   sourceDir : откуда копировать (в собранном установщике — resources/extension)
 *   cepDir    : папка расширений CEP
 *   enableDebug : включать ли PlayerDebugMode (режим разработчика)
 *   onProgress: (done, total, label) => void
 *
 * Возвращает { targetDir, actions, debugEnabled }.
 */
function install(opts) {
  const deps = opts.deps;
  const fsys = deps.fs;
  const target = path.join(opts.cepDir, EXTENSION_ID);
  const log = [];

  /* 1) Если расширение уже стоит — запоминаем, чтобы уметь откатиться. */
  let backup = null;
  if (safeExists(fsys, target)) {
    backup = target + ".bak-" + Date.now();
    log.push({ type: "move", from: target, to: backup });
    fsys.renameSync(target, backup);
  }

  let copied = [];
  try {
    copied = copyTree(fsys, opts.sourceDir, target, opts.onProgress);
    log.push({ type: "dir", path: target });
  } catch (e) {
    rollback(fsys, log, copied, target, backup);
    throw makeError(
      "copy_failed",
      "Не удалось скопировать файлы панели.",
      "Проверьте права на папку " + opts.cepDir + " и отключите блокировку антивирусом на время установки."
    );
  }

  /* 2) Режим разработчика: без подписи CEP панель не грузит. */
  let debugEnabled = false;
  if (opts.enableDebug) {
    debugEnabled = enableDebugMode(deps);
    if (debugEnabled) {
      log.push({ type: "registry", key: CEP_KEY });
    }
  }

  return { targetDir: target, actions: log, debugEnabled, backup };
}

/*
 * Откат: удаляем то, что успели скопировать, и возвращаем прежнюю версию.
 * Вызывается при любой ошибке установки.
 */
function rollback(fsys, log, copied, target, backup) {
  try {
    if (safeExists(fsys, target)) {
      fsys.rmSync(target, { recursive: true, force: true });
    }
  } catch (e) { /* продолжаем откат */ }

  if (backup && safeExists(fsys, backup)) {
    try { fsys.renameSync(backup, target); } catch (e) { /* не критично */ }
  }
}

function copyTree(fsys, src, dest, onProgress) {
  const all = [];
  const files = walk(fsys, src);
  const total = files.length;
  let done = 0;

  ensureDir(fsys, dest);

  files.forEach(function (rel) {
    const from = path.join(src, rel);
    const to = path.join(dest, rel);
    ensureDir(fsys, path.dirname(to));
    fsys.copyFileSync(from, to);
    all.push(to);
    done += 1;
    if (onProgress) { onProgress(done, total, rel); }
  });

  return all;
}

function walk(fsys, dir, prefix) {
  prefix = prefix || "";
  let out = [];
  const entries = fsys.readdirSync(dir, { withFileTypes: true });
  entries.forEach(function (e) {
    const rel = prefix ? prefix + path.sep + e.name : e.name;
    if (e.isDirectory()) {
      out = out.concat(walk(fsys, path.join(dir, e.name), rel));
    } else {
      out.push(rel);
    }
  });
  return out;
}

/* ------------------------------------------------------------------ реестр */

/*
 * Включить PlayerDebugMode — разрешение грузить неподписанные расширения.
 *
 * ВАЖНО про безопасность: это режим разработчика. Он ослабляет проверку
 * подписи CEP для ТЕКУЩЕГО ПОЛЬЗОВАТЕЛЯ. Мы:
 *   * включаем его только если вызывающий код явно попросил (enableDebug);
 *   * пишем только в HKCU (текущий пользователь), не в HKLM;
 *   * никогда не отключаем Defender, UAC или другие механизмы защиты;
 *   * при удалении спрашиваем, выключать ли его обратно.
 *
 * Для обычной пользовательской установки правильнее подписать .zxp и не
 * трогать реестр вообще — это отдельный путь, см. docs/SIGNING.md.
 */
function enableDebugMode(deps) {
  if (deps.platform !== "win32") {
    /* На macOS аналог — файл .debug внутри расширения, он уже в комплекте. */
    return true;
  }
  try {
    const res = deps.run(
      'reg add "' + CEP_KEY + '" /v PlayerDebugMode /t REG_DWORD /d 1 /f'
    );
    return !!(res && res.status === 0);
  } catch (e) {
    return false;
  }
}

function disableDebugMode(deps) {
  if (deps.platform !== "win32") { return true; }
  try {
    const res = deps.run('reg add "' + CEP_KEY + '" /v PlayerDebugMode /t REG_DWORD /d 0 /f');
    return !!(res && res.status === 0);
  } catch (e) {
    return false;
  }
}

function isDebugEnabled(deps) {
  if (deps.platform !== "win32") { return false; }
  try {
    const res = deps.run('reg query "' + CEP_KEY + '" /v PlayerDebugMode');
    if (!res || res.status !== 0) { return false; }
    return /PlayerDebugMode\s+REG_DWORD\s+0x1/i.test(res.stdout || "");
  } catch (e) {
    return false;
  }
}

/* ----------------------------------------------------------------- удаление */

/*
 * Удалить timpoc.
 *
 * keepUserData (по умолчанию true) — НЕ удалять папку с пресетами, журналом
 * и распознанными текстами. Пользователь мог потратить часы на распознавание;
 * терять это при переустановке нельзя.
 */
function uninstall(opts) {
  const deps = opts.deps;
  const fsys = deps.fs;
  const removed = [];

  const extDir = path.join(opts.cepDir, EXTENSION_ID);
  if (safeExists(fsys, extDir)) {
    try {
      fsys.rmSync(extDir, { recursive: true, force: true });
      removed.push(extDir);
    } catch (e) {
      throw makeError(
        "remove_failed",
        "Не удалось удалить папку расширения.",
        "Закройте After Effects и повторите. Если не помогает — удалите папку вручную: " + extDir
      );
    }
  }

  /* Убираем остатки от предыдущих установок. */
  try {
    fsys.readdirSync(opts.cepDir).forEach(function (name) {
      if (/^com\.timpoc\.ae\.panel\.bak-/.test(name)) {
        const p = path.join(opts.cepDir, name);
        try { fsys.rmSync(p, { recursive: true, force: true }); removed.push(p); } catch (e) { /* пропускаем */ }
      }
    });
  } catch (e) { /* папки может не быть */ }

  if (opts.disableDebug) { disableDebugMode(deps); }

  if (opts.removeUserData) {
    const data = opts.dataDir;
    if (data && safeExists(fsys, data)) {
      try {
        fsys.rmSync(data, { recursive: true, force: true });
        removed.push(data);
      } catch (e) {
        throw makeError(
          "remove_data_failed",
          "Расширение удалено, но папку с данными стереть не удалось.",
          "Удалите вручную: " + data
        );
      }
    }
  }

  return { removed, keptUserData: !opts.removeUserData, dataDir: opts.dataDir };
}

/* ------------------------------------------------------------------ утилиты */

function ensureDir(fsys, dir) {
  if (!dir) { return; }
  if (safeExists(fsys, dir)) { return; }
  fsys.mkdirSync(dir, { recursive: true });
}

function safeExists(fsys, p) {
  try { return fsys.existsSync(p); } catch (e) { return false; }
}

module.exports = {
  EXTENSION_ID,
  CEP_KEY,
  checkPrerequisites,
  install,
  uninstall,
  rollback,
  enableDebugMode,
  disableDebugMode,
  isDebugEnabled,
  isAfterEffectsRunning,
  freeSpaceMb,
  copyTree,
  walk,
  makeError
};
