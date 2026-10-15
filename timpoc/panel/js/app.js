/*
 * timpoc — логика панели.
 *
 * Связывает три стороны:
 *   интерфейс (index.html)  <->  движок распознавания (engine.js)
 *                             <->  After Effects (bridge.js -> timpoc_host.jsx)
 *
 * Главные правила, которые здесь реализованы:
 *   1. Смена оформления НЕ запускает распознавание: стиль уходит в AE
 *      отдельным дешёвым вызовом updateStyleOnly.
 *   2. Распознавание идёт в движке, панель только спрашивает прогресс
 *      и может отменить — After Effects при этом не блокируется.
 *   3. Любая ошибка показывается как «что случилось + что сделать».
 *   4. Результат распознавания сохраняется движком на диск, поэтому сбой
 *      применения не требует повторного распознавания.
 *   5. Предпросмотр — приближение. Итог всегда сверяется в композиции.
 */

(function () {
  "use strict";

  var engine = window.TimpocEngine;
  var ae = window.TimpocBridge;

  var S = {
    project: null,
    fonts: { families: [], styles: {} },
    style: {},
    layout: { v_slot: 5, h_slot: "center", safe_zone: {} },
    transcript: null,
    segmentation: { max_words: 8, max_chars: 42, max_lines: 2 },
    busy: false
  };

  var el = {};
  function $(id) { return document.getElementById(id); }
  function cacheDom() {
    var ids = [
      "engineDot", "engineStatus", "alerts",
      "sourceKind", "sourceLayer", "sourcePath", "layerField", "fileField",
      "rangeStart", "rangeEnd", "sourceMeta", "language", "model", "modelHint",
      "progressWrap", "progressFill", "progressText", "btnCreate", "btnCancel",
      "blocksCard", "blocksCount", "blocksList", "maxWords", "maxChars", "maxLines",
      "btnResegment", "shiftSeconds", "btnShift",
      "previewFrame", "previewText", "previewBox", "previewSafe", "previewAspect",
      "previewHint", "previewWarn",
      "fontFamily", "fontStyle", "fontSize", "fontColor", "opacity", "opacityVal",
      "caseMode", "tracking", "trackingVal", "lineHeight", "lineHeightVal",
      "textAlign", "boxWidth", "fontWarn",
      "strokeEnabled", "strokeColor", "strokeWidth", "strokeWidthVal",
      "strokeOpacity", "strokeOpacityVal",
      "shadowEnabled", "shadowColor", "shadowOpacity", "shadowOpacityVal",
      "shadowDistance", "shadowDistanceVal", "shadowSoftness", "shadowSoftnessVal",
      "shadowDirection", "shadowDirectionVal",
      "boxEnabled", "boxColor", "boxOpacity", "boxOpacityVal",
      "boxPadX", "boxPadXVal", "boxPadY", "boxPadYVal", "boxRadius", "boxRadiusVal",
      "boxUseExpression",
      "btnApply", "btnRestyle",
      "marginTop", "marginBottom", "marginLeft", "marginRight",
      "useExact", "exactX", "exactY", "safeEnabled", "safeTB", "safeLR",
      "btnFit", "layoutWarn",
      "presetList", "presetName", "btnSavePreset",
      "systemInfo", "device", "modelsList", "offlineMode",
      "effectsLevel", "reduceMotion",
      "btnExportSrt", "btnImportSrt", "btnDiagnostics", "logBox", "btnRemoveLayers"
    ];
    ids.forEach(function (id) { el[id] = $(id); });
  }

  /* ------------------------------------------------------- сообщения */

  function clearAlerts() {
    el.alerts.innerHTML = "";
    el.alerts.hidden = true;
  }

  function alert(kind, title, hint) {
    var box = document.createElement("div");
    box.className = "tp-alert tp-alert--" + kind;
    var t = document.createElement("div");
    t.className = "tp-alert-title";
    t.textContent = title || "";
    box.appendChild(t);
    if (hint) {
      var h = document.createElement("div");
      h.className = "tp-alert-hint";
      h.textContent = hint;
      box.appendChild(h);
    }
    el.alerts.appendChild(box);
    el.alerts.hidden = false;
    return box;
  }

  function showError(err) {
    if (!err) { return; }
    alert("err", err.message || "Ошибка", err.hint || "");
  }

  function showWarnings(list) {
    (list || []).forEach(function (w) { alert("warn", w); });
  }

  function setEngineStatus(state, text) {
    el.engineDot.className = "tp-dot" + (state ? " is-" + state : "");
    el.engineStatus.textContent = text;
  }

  /* ------------------------------------------------------- вкладки */

  function initTabs() {
    var tabs = document.querySelectorAll(".tp-tab");
    Array.prototype.forEach.call(tabs, function (tab) {
      tab.addEventListener("click", function () {
        Array.prototype.forEach.call(tabs, function (t) {
          t.classList.remove("is-active");
          t.setAttribute("aria-selected", "false");
        });
        tab.classList.add("is-active");
        tab.setAttribute("aria-selected", "true");
        document.querySelectorAll(".tp-panel").forEach(function (p) {
          p.classList.remove("is-active");
        });
        $("tab-" + tab.dataset.tab).classList.add("is-active");
        if (tab.dataset.tab === "style") { renderPreview(); }
        if (tab.dataset.tab === "presets") { loadPresets(); }
      });
    });
  }

  /* ------------------------------------------------------- проект AE */

  function refreshProject() {
    return ae.getProjectInfo().then(function (res) {
      if (!res.ok) {
        S.project = null;
        el.sourceMeta.textContent = res.error ? res.error.message : "Нет композиции.";
        return res;
      }
      S.project = res;
      el.rangeStart.value = Number(res.workAreaStart || 0).toFixed(2);
      el.rangeEnd.value = Number((res.workAreaStart || 0) + (res.workAreaDuration || 0)).toFixed(2);
      el.sourceMeta.textContent =
        res.name + "\n" + res.width + "×" + res.height + "  ·  " +
        Number(res.frameRate).toFixed(3) + " fps  ·  " +
        Number(res.duration).toFixed(2) + " с";
      fillLayerList(res.audioLayers || []);
      return res;
    });
  }

  function fillLayerList(layers) {
    el.sourceLayer.innerHTML = "";
    if (!layers.length) {
      var none = document.createElement("option");
      none.value = "";
      none.textContent = "В композиции нет слоёв со звуком";
      el.sourceLayer.appendChild(none);
      return;
    }
    layers.forEach(function (l) {
      var o = document.createElement("option");
      o.value = String(l.index);
      var extra = [];
      if (l.timeRemap) { extra.push("time remap"); }
      if (l.stretch && Math.abs(l.stretch - 1) > 0.001) { extra.push("растяжение " + Number(l.stretch).toFixed(2) + "×"); }
      o.textContent = l.name + (extra.length ? "  (" + extra.join(", ") + ")" : "");
      el.sourceLayer.appendChild(o);
    });
  }

  function refreshFonts() {
    return ae.getFonts().then(function (res) {
      if (!res.ok) { return; }
      S.fonts = { families: res.families || [], styles: res.styles || {} };
      fillFontList();
      checkFont();
    });
  }

  function fillFontList() {
    var current = S.style.font_family || "Arial";
    el.fontFamily.innerHTML = "";
    var list = S.fonts.families.slice().sort(function (a, b) {
      return a.toLowerCase() < b.toLowerCase() ? -1 : 1;
    });
    if (list.indexOf(current) === -1) { list.unshift(current); }
    list.forEach(function (fam) {
      var o = document.createElement("option");
      o.value = fam;
      o.textContent = fam;
      if (fam === current) { o.selected = true; }
      el.fontFamily.appendChild(o);
    });
    fillStyleList(current);
  }

  function fillStyleList(family) {
    var styles = S.fonts.styles[family] || ["Regular", "Bold"];
    var current = S.style.font_style || "Regular";
    el.fontStyle.innerHTML = "";
    styles.forEach(function (st) {
      var o = document.createElement("option");
      o.value = st;
      o.textContent = st;
      if (st === current) { o.selected = true; }
      el.fontStyle.appendChild(o);
    });
  }

  /* Проверка шрифта: честное предупреждение и предложение замены. */
  function checkFont() {
    return engine.checkFont({
      family: S.style.font_family,
      style: S.style.font_style,
      ae_families: S.fonts.families,
      ae_styles: S.fonts.styles
    }).then(function (res) {
      if (!res || !res.ok) { return; }
      if (res.warnings && res.warnings.length) {
        el.fontWarn.hidden = false;
        el.fontWarn.textContent = res.warnings.join(" ");
      } else {
        el.fontWarn.hidden = true;
        el.fontWarn.textContent = "";
      }
      if (!res.available && res.family && res.family !== S.style.font_family) {
        /* Предлагаем замену, но не подменяем молча: ставим её в список,
           чтобы пользователь видел, что будет применено. */
        S.style.font_family = res.family;
        fillFontList();
      }
    });
  }

  /* ------------------------------------------------------- стиль */

  function rgbToHex(rgb) {
    if (!rgb || rgb.length < 3) { return "#ffffff"; }
    return "#" + rgb.slice(0, 3).map(function (v) {
      var h = Math.max(0, Math.min(255, Math.round(v))).toString(16);
      return h.length === 1 ? "0" + h : h;
    }).join("");
  }

  function hexToRgb(hex) {
    var h = String(hex || "").replace("#", "");
    if (h.length === 3) { h = h[0] + h[0] + h[1] + h[1] + h[2] + h[2]; }
    var n = parseInt(h, 16);
    if (isNaN(n)) { return [255, 255, 255]; }
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }

  function readStyleFromForm() {
    S.style = {
      font_family: el.fontFamily.value,
      font_style: el.fontStyle.value,
      font_size: parseFloat(el.fontSize.value) || 64,
      line_height: (parseFloat(el.lineHeight.value) || 125) / 100,
      tracking: parseFloat(el.tracking.value) || 0,
      case_mode: el.caseMode.value,
      text_align: el.textAlign.value,
      box_width: parseFloat(el.boxWidth.value) || 900,
      max_lines: parseInt(el.maxLines.value, 10) || 2,
      color: hexToRgb(el.fontColor.value),
      opacity: (parseFloat(el.opacity.value) || 0) / 100,
      stroke_enabled: el.strokeEnabled.checked,
      stroke_color: hexToRgb(el.strokeColor.value),
      stroke_width: parseFloat(el.strokeWidth.value) || 0,
      stroke_opacity: (parseFloat(el.strokeOpacity.value) || 0) / 100,
      shadow_enabled: el.shadowEnabled.checked,
      shadow_color: hexToRgb(el.shadowColor.value),
      shadow_opacity: (parseFloat(el.shadowOpacity.value) || 0) / 100,
      shadow_distance: parseFloat(el.shadowDistance.value) || 0,
      shadow_direction: parseFloat(el.shadowDirection.value) || 0,
      shadow_softness: parseFloat(el.shadowSoftness.value) || 0,
      box_enabled: el.boxEnabled.checked,
      box_color: hexToRgb(el.boxColor.value),
      box_opacity: (parseFloat(el.boxOpacity.value) || 0) / 100,
      box_padding_x: parseFloat(el.boxPadX.value) || 0,
      box_padding_y: parseFloat(el.boxPadY.value) || 0,
      box_corner_radius: parseFloat(el.boxRadius.value) || 0,
      box_use_expression: el.boxUseExpression.checked
    };
    return S.style;
  }

  function writeStyleToForm(st) {
    S.style = st || {};
    el.fontFamily.value = st.font_family || "Arial";
    fillStyleList(st.font_family || "Arial");
    el.fontStyle.value = st.font_style || "Regular";
    el.fontSize.value = st.font_size || 64;
    el.lineHeight.value = Math.round((st.line_height || 1.25) * 100);
    el.tracking.value = st.tracking || 0;
    el.caseMode.value = st.case_mode || "none";
    el.textAlign.value = st.text_align || "center";
    el.boxWidth.value = st.box_width || 900;
    el.maxLines.value = st.max_lines || 2;
    el.fontColor.value = rgbToHex(st.color);
    el.opacity.value = Math.round((st.opacity === undefined ? 1 : st.opacity) * 100);
    el.strokeEnabled.checked = st.stroke_enabled !== false;
    el.strokeColor.value = rgbToHex(st.stroke_color || [0, 0, 0]);
    el.strokeWidth.value = st.stroke_width === undefined ? 6 : st.stroke_width;
    el.strokeOpacity.value = Math.round((st.stroke_opacity === undefined ? 1 : st.stroke_opacity) * 100);
    el.shadowEnabled.checked = st.shadow_enabled !== false;
    el.shadowColor.value = rgbToHex(st.shadow_color || [0, 0, 0]);
    el.shadowOpacity.value = Math.round((st.shadow_opacity === undefined ? 0.55 : st.shadow_opacity) * 100);
    el.shadowDistance.value = st.shadow_distance === undefined ? 6 : st.shadow_distance;
    el.shadowDirection.value = st.shadow_direction === undefined ? 135 : st.shadow_direction;
    el.shadowSoftness.value = st.shadow_softness === undefined ? 8 : st.shadow_softness;
    el.boxEnabled.checked = !!st.box_enabled;
    el.boxColor.value = rgbToHex(st.box_color || [0, 0, 0]);
    el.boxOpacity.value = Math.round((st.box_opacity === undefined ? 0.55 : st.box_opacity) * 100);
    el.boxPadX.value = st.box_padding_x === undefined ? 24 : st.box_padding_x;
    el.boxPadY.value = st.box_padding_y === undefined ? 12 : st.box_padding_y;
    el.boxRadius.value = st.box_corner_radius === undefined ? 14 : st.box_corner_radius;
    el.boxUseExpression.checked = st.box_use_expression !== false;
    updateSliderLabels();
  }

  function updateSliderLabels() {
    el.opacityVal.textContent = el.opacity.value + "%";
    el.trackingVal.textContent = el.tracking.value;
    el.lineHeightVal.textContent = ((parseFloat(el.lineHeight.value) || 125) / 100).toFixed(2);
    el.strokeWidthVal.textContent = el.strokeWidth.value;
    el.strokeOpacityVal.textContent = el.strokeOpacity.value + "%";
    el.shadowOpacityVal.textContent = el.shadowOpacity.value + "%";
    el.shadowDistanceVal.textContent = el.shadowDistance.value;
    el.shadowSoftnessVal.textContent = el.shadowSoftness.value;
    el.shadowDirectionVal.textContent = el.shadowDirection.value + "°";
    el.boxOpacityVal.textContent = el.boxOpacity.value + "%";
    el.boxPadXVal.textContent = el.boxPadX.value;
    el.boxPadYVal.textContent = el.boxPadY.value;
    el.boxRadiusVal.textContent = el.boxRadius.value;
  }

  function readLayoutFromForm() {
    S.layout = {
      v_slot: parseInt(document.querySelector(".tp-vslot.is-active").dataset.vslot, 10) || 5,
      h_slot: (document.querySelector(".tp-hslot.is-active") || {}).dataset
        ? document.querySelector(".tp-hslot.is-active").dataset.hslot
        : "center",
      margin_top: parseFloat(el.marginTop.value) || 0,
      margin_bottom: parseFloat(el.marginBottom.value) || 0,
      margin_left: parseFloat(el.marginLeft.value) || 0,
      margin_right: parseFloat(el.marginRight.value) || 0,
      use_exact: el.useExact.checked,
      exact_x: parseFloat(el.exactX.value) || 0,
      exact_y: parseFloat(el.exactY.value) || 0,
      safe_zone: {
        enabled: el.safeEnabled.checked,
        title_top: (parseFloat(el.safeTB.value) || 0) / 100,
        title_bottom: (parseFloat(el.safeTB.value) || 0) / 100,
        title_left: (parseFloat(el.safeLR.value) || 0) / 100,
        title_right: (parseFloat(el.safeLR.value) || 0) / 100
      }
    };
    return S.layout;
  }

  function writeLayoutToForm(lay) {
    if (!lay) { return; }
    document.querySelectorAll(".tp-vslot").forEach(function (b) {
      b.classList.toggle("is-active", Number(b.dataset.vslot) === Number(lay.v_slot));
    });
    document.querySelectorAll(".tp-hslot").forEach(function (b) {
      b.classList.toggle("is-active", b.dataset.hslot === lay.h_slot);
    });
    el.marginTop.value = lay.margin_top === undefined ? 60 : lay.margin_top;
    el.marginBottom.value = lay.margin_bottom === undefined ? 90 : lay.margin_bottom;
    el.marginLeft.value = lay.margin_left === undefined ? 60 : lay.margin_left;
    el.marginRight.value = lay.margin_right === undefined ? 60 : lay.margin_right;
    el.useExact.checked = !!lay.use_exact;
    el.exactX.value = lay.exact_x || 0;
    el.exactY.value = lay.exact_y || 0;
    var sz = lay.safe_zone || {};
    el.safeEnabled.checked = sz.enabled !== false;
    el.safeTB.value = Math.round((sz.title_top === undefined ? 0.1 : sz.title_top) * 100);
    el.safeLR.value = Math.round((sz.title_left === undefined ? 0.05 : sz.title_left) * 100);
  }

  /* ------------------------------------------------------- предпросмотр */

  var PREVIEW_MAX_W = 340;
  var PREVIEW_MAX_H = 260;

  function previewDims() {
    var aspect = el.previewAspect.value;
    var cw = S.project ? S.project.width : 1920;
    var ch = S.project ? S.project.height : 1080;
    var w, h, label;
    if (aspect === "comp") { w = cw; h = ch; label = "Композиция"; }
    else if (aspect === "custom") {
      w = parseFloat(window.prompt("Ширина", "9")) || 9;
      h = parseFloat(window.prompt("Высота", "16")) || 16;
      label = w + ":" + h;
    } else {
      var parts = aspect.split(":");
      w = parseFloat(parts[0]); h = parseFloat(parts[1]); label = aspect;
    }
    return { w: w, h: h, label: label };
  }

  /*
   * Предпросмотр — ПРИБЛИЖЕНИЕ.
   * Метрики шрифта в браузере и в After Effects различаются, поэтому здесь
   * мы показываем замысел, а не итог. Об этом честно написано под предпросмотром,
   * и точные размеры можно снять из AE (measureText).
   */
  function renderPreview() {
    if (!el.previewFrame) { return; }
    var st = readStyleFromForm();
    var lay = readLayoutFromForm();
    var dims = previewDims();

    var scale = Math.min(PREVIEW_MAX_W / dims.w, PREVIEW_MAX_H / dims.h);
    el.previewFrame.style.width = Math.round(dims.w * scale) + "px";
    el.previewFrame.style.height = Math.round(dims.h * scale) + "px";
    el.previewHint.textContent = dims.label + " · масштаб " + (scale * 100).toFixed(1) + "%";

    var text = el.previewText;
    text.textContent = "";
    var sample = "Пример субтитра\nвторая строка";
    if (st.case_mode === "upper" || st.case_mode === "caps") { sample = sample.toUpperCase(); }
    else if (st.case_mode === "lower") { sample = sample.toLowerCase(); }
    sample.split("\n").forEach(function (line, i) {
      if (i > 0) { text.appendChild(document.createElement("br")); }
      text.appendChild(document.createTextNode(line));
    });

    var fs = st.font_size * scale;
    text.style.fontFamily = '"' + (st.font_family || "Arial") + '", sans-serif';
    text.style.fontWeight = /bold/i.test(st.font_style || "") ? "700" : "400";
    text.style.fontSize = fs + "px";
    text.style.lineHeight = (st.line_height || 1.25);
    text.style.letterSpacing = (fs * ((st.tracking || 0) / 1000)) + "px";
    text.style.color = rgbCss(st.color, st.opacity);
    text.style.textAlign = st.text_align || "center";
    text.style.width = Math.min(st.box_width || 900, dims.w) * scale + "px";

    var shadow = st.shadow_enabled
      ? shadowOffset(st) + "px " + (st.shadow_softness || 0) * scale + "px 0 " + rgbCss(st.shadow_color, st.shadow_opacity)
      : "none";
    text.style.textShadow = shadow;

    var strokeW = st.stroke_enabled ? (st.stroke_width || 0) * scale : 0;
    text.style.webkitTextStroke = strokeW > 0
      ? strokeW + "px " + rgbCss(st.stroke_color, st.stroke_opacity)
      : "";

    var box = el.previewBox;
    if (st.box_enabled) {
      var bw = text.offsetWidth + 2 * st.box_padding_x * scale;
      var bh = text.offsetHeight + 2 * st.box_padding_y * scale;
      box.style.width = bw + "px";
      box.style.height = bh + "px";
      box.style.background = rgbCss(st.box_color, st.box_opacity);
      box.style.borderRadius = (st.box_corner_radius || 0) * scale + "px";
      box.hidden = false;
    } else {
      box.hidden = true;
    }

    /* Позиция блока — по слотам, как в AE. */
    var blockW = st.box_enabled ? box.offsetWidth : text.offsetWidth;
    var blockH = st.box_enabled ? box.offsetHeight : text.offsetHeight;
    var fw = dims.w * scale;
    var fh = dims.h * scale;
    var szOn = lay.safe_zone && lay.safe_zone.enabled;
    var sx = szOn ? fw * lay.safe_zone.title_left : 0;
    var sy = szOn ? fh * lay.safe_zone.title_top : 0;
    var sw = fw - sx * 2;
    var sh = fh - sy * 2;

    var top = sy + lay.margin_top * scale;
    var bottom = sy + sh - lay.margin_bottom * scale;
    var cy;
    if (bottom - top <= blockH) { cy = (top + bottom) / 2; }
    else { cy = top + blockH / 2 + (bottom - top - blockH) * ((lay.v_slot - 1) / 4); }

    var left = sx + lay.margin_left * scale;
    var right = sx + sw - lay.margin_right * scale;
    var cx;
    if (lay.h_slot === "left") { cx = left + blockW / 2; }
    else if (lay.h_slot === "right") { cx = right - blockW / 2; }
    else { cx = (left + right) / 2; }

    if (lay.use_exact) { cx = lay.exact_x * scale; cy = lay.exact_y * scale; }

    placeCentered(text, cx, cy);
    if (st.box_enabled) { placeCentered(box, cx, cy); }

    /* Безопасная зона */
    if (szOn) {
      el.previewSafe.hidden = false;
      el.previewSafe.style.left = sx + "px";
      el.previewSafe.style.top = sy + "px";
      el.previewSafe.style.width = sw + "px";
      el.previewSafe.style.height = sh + "px";
    } else {
      el.previewSafe.hidden = true;
    }

    checkOverflow(fw, fh, cx, cy, blockW, blockH, scale);
  }

  function placeCentered(node, cx, cy) {
    node.style.left = Math.round(cx - node.offsetWidth / 2) + "px";
    node.style.top = Math.round(cy - node.offsetHeight / 2) + "px";
  }

  function shadowOffset(st) {
    var rad = ((st.shadow_direction || 0) - 90) * Math.PI / 180;
    var d = st.shadow_distance || 0;
    return (Math.sin(rad) * d).toFixed(1) + "px " + (Math.cos(rad) * -d).toFixed(1);
  }

  function rgbCss(rgb, alpha) {
    var c = rgb && rgb.length >= 3 ? rgb : [255, 255, 255];
    var a = alpha === undefined ? 1 : alpha;
    return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")";
  }

  function checkOverflow(fw, fh, cx, cy, bw, bh, scale) {
    var left = cx - bw / 2, top = cy - bh / 2;
    var over = [];
    if (left < 0) { over.push("слева на " + Math.round(-left / scale) + " px"); }
    if (top < 0) { over.push("сверху на " + Math.round(-top / scale) + " px"); }
    if (left + bw > fw) { over.push("справа на " + Math.round((left + bw - fw) / scale) + " px"); }
    if (top + bh > fh) { over.push("снизу на " + Math.round((top + bh - fh) / scale) + " px"); }
    if (over.length) {
      el.previewWarn.hidden = false;
      el.previewWarn.textContent = "Текст выходит за границы кадра: " + over.join(", ") + ". Нажмите «Вписать в кадр».";
    } else {
      el.previewWarn.hidden = true;
    }
  }

  /* Перетаскивание блока мышью в предпросмотре. */
  function initDrag() {
    var dragging = false;
    var startX = 0, startY = 0, origX = 0, origY = 0;

    el.previewText.addEventListener("mousedown", function (e) {
      dragging = true;
      startX = e.clientX; startY = e.clientY;
      origX = parseFloat(el.previewText.style.left) || 0;
      origY = parseFloat(el.previewText.style.top) || 0;
      e.preventDefault();
    });

    document.addEventListener("mousemove", function (e) {
      if (!dragging) { return; }
      var dx = e.clientX - startX;
      var dy = e.clientY - startY;
      var fw = el.previewFrame.offsetWidth;
      var fh = el.previewFrame.offsetHeight;
      var scale = fw / (previewDims().w || fw);
      var nx = origX + dx;
      var ny = origY + dy;
      el.previewText.style.left = nx + "px";
      el.previewText.style.top = ny + "px";
      if (!el.previewBox.hidden) {
        el.previewBox.style.left = nx + "px";
        el.previewBox.style.top = ny + "px";
      }
      var cx = nx + el.previewText.offsetWidth / 2;
      var cy = ny + el.previewText.offsetHeight / 2;
      checkOverflow(fw, fh, cx, cy, el.previewText.offsetWidth, el.previewText.offsetHeight, scale);
      el.exactX.value = Math.round(cx / scale);
      el.exactY.value = Math.round(cy / scale);
      el.useExact.checked = true;
    });

    document.addEventListener("mouseup", function () {
      if (!dragging) { return; }
      dragging = false;
      pushPositionToAe();
    });
  }

  /* ------------------------------------------------------- распознавание */

  function buildTranscribePayload() {
    var kind = el.sourceKind.value;
    var payload = {
      frame_rate: S.project ? S.project.frameRate : 25,
      comp_start: S.project ? S.project.displayStartTime : 0,
      comp_end: S.project ? (S.project.displayStartTime + S.project.duration) : null,
      source_kind: kind,
      recognition: {
        language: el.language.value,
        model: el.model.value,
        device: el.device.value,
        beam_size: 5,
        vad_filter: true,
        word_timestamps: true
      },
      segmentation: {
        max_words: parseInt(el.maxWords.value, 10) || 8,
        max_chars: parseInt(el.maxChars.value, 10) || 42,
        max_lines: parseInt(el.maxLines.value, 10) || 2
      }
    };

    if (kind === "file") {
      payload.path = el.sourcePath.value.trim();
      payload.range_start = parseFloat(el.rangeStart.value) || 0;
      payload.range_end = parseFloat(el.rangeEnd.value) || null;
      payload.offset_seconds = parseFloat(el.rangeStart.value) || 0;
      payload.source_label = payload.path;
    } else {
      /* Для «звук композиции» и «слой» источник определяет After Effects. */
      var src = ae.getAudioSource(kind === "layer"
        ? { kind: "layer", index: parseInt(el.sourceLayer.value, 10) }
        : { kind: "comp" });
      return src.then(function (res) {
        if (!res.ok) { return { ok: false, error: res.error }; }
        showWarnings(res.warnings);
        payload.path = res.path || "";
        payload.range_start = res.rangeStart || 0;
        payload.range_end = res.rangeEnd || null;
        payload.offset_seconds = res.offsetSeconds || 0;
        payload.source_label = res.label || "";
        payload.source_kind = res.kind;
        return { ok: true, payload: payload };
      });
    }
    return Promise.resolve({ ok: true, payload: payload });
  }

  function startTranscribe() {
    if (S.busy) { return; }
    clearAlerts();
    S.busy = true;
    el.btnCreate.disabled = true;
    el.btnCancel.hidden = false;
    el.progressWrap.hidden = false;
    setProgress(0, "Подготовка…");

    buildTranscribePayload().then(function (prepared) {
      if (!prepared.ok) {
        finishTranscribe();
        showError(prepared.error);
        return;
      }
      return engine.runJob(
        function () { return engine.transcribe(prepared.payload); },
        function (p) { setProgress(p.progress, p.message); }
      ).then(function (res) {
        finishTranscribe();
        if (!res.ok) { showError(res.error); return; }
        S.transcript = res.result.transcript;
        renderBlocks();
        showWarnings(res.result.transcript.warnings);
        alert("ok", "Готово: " + S.transcript.blocks.length + " субтитров",
          "Проверьте текст и тайминги, затем нажмите «Применить».");
      });
    });
  }

  function finishTranscribe() {
    S.busy = false;
    el.btnCreate.disabled = false;
    el.btnCancel.hidden = true;
    el.progressWrap.hidden = true;
  }

  function setProgress(p, msg) {
    el.progressFill.style.width = Math.round((p || 0) * 100) + "%";
    el.progressText.textContent = (msg || "") + " · " + Math.round((p || 0) * 100) + "%";
  }

  /* ------------------------------------------------------- редактор блоков */

  function fmtTime(sec) {
    var s = Math.max(0, Number(sec) || 0);
    var m = Math.floor(s / 60);
    var rest = s - m * 60;
    return (m < 10 ? "0" : "") + m + ":" + (rest < 10 ? "0" : "") + rest.toFixed(2);
  }

  function parseTime(str) {
    var t = String(str || "").trim();
    if (t.indexOf(":") !== -1) {
      var p = t.split(":");
      return (parseFloat(p[0]) || 0) * 60 + (parseFloat(p[1]) || 0);
    }
    return parseFloat(t) || 0;
  }

  function renderBlocks() {
    el.blocksList.innerHTML = "";
    if (!S.transcript || !S.transcript.blocks) {
      el.blocksCard.hidden = true;
      return;
    }
    el.blocksCard.hidden = false;
    el.blocksCount.textContent = String(S.transcript.blocks.length);

    S.transcript.blocks.forEach(function (b, i) {
      var row = document.createElement("div");
      row.className = "tp-block" + (b.locked ? " is-locked" : "");

      var head = document.createElement("div");
      head.className = "tp-block-head";

      var idx = document.createElement("span");
      idx.className = "tp-block-idx";
      idx.textContent = "#" + (i + 1);
      head.appendChild(idx);

      var inEl = document.createElement("input");
      inEl.className = "tp-block-time";
      inEl.value = fmtTime(b["in"]);
      inEl.title = "Начало";
      head.appendChild(inEl);

      var outEl = document.createElement("input");
      outEl.className = "tp-block-time";
      outEl.value = fmtTime(b.out);
      outEl.title = "Конец";
      head.appendChild(outEl);

      row.appendChild(head);

      var text = document.createElement("textarea");
      text.className = "tp-block-text";
      text.value = b.text;
      row.appendChild(text);

      var actions = document.createElement("div");
      actions.className = "tp-block-actions";
      actions.appendChild(smallBtn("Сохранить", function () {
        engine.updateBlock({
          index: i,
          text: text.value,
          "in": parseTime(inEl.value),
          out: parseTime(outEl.value)
        }).then(function (res) {
          clearAlerts();
          if (!res.ok) { showError(res.error); return; }
          alert("ok", "Блок сохранён");
        });
      }));
      actions.appendChild(smallBtn(b.locked ? "Открепить" : "Закрепить", function () {
        engine.updateBlock({ index: i, locked: !b.locked }).then(function () {
          S.transcript.blocks[i].locked = !b.locked;
          renderBlocks();
        });
      }));
      actions.appendChild(smallBtn("Объединить с предыдущим", function () {
        engine.mergeBlock({ index: i }).then(function (res) {
          clearAlerts();
          if (!res.ok) { showError(res.error); return; }
          reloadState();
        });
      }));
      actions.appendChild(smallBtn("Удалить", function () {
        engine.deleteBlock({ index: i }).then(function (res) {
          clearAlerts();
          if (!res.ok) { showError(res.error); return; }
          reloadState();
        });
      }));
      row.appendChild(actions);

      el.blocksList.appendChild(row);
    });
  }

  function smallBtn(label, onClick) {
    var b = document.createElement("button");
    b.className = "tp-btn tp-btn--small";
    b.textContent = label;
    b.addEventListener("click", onClick);
    return b;
  }

  /* ------------------------------------------------------- применение к AE */

  function computeAePosition() {
    var lay = readLayoutFromForm();
    var st = readStyleFromForm();
    var cw = S.project ? S.project.width : 1920;
    var ch = S.project ? S.project.height : 1080;
    var scale = 1;
    var bw = Math.min(st.box_width || 900, cw);
    var bh = (st.font_size || 64) * (st.line_height || 1.25) * (st.max_lines || 2);
    if (st.box_enabled) {
      bw += 2 * (st.box_padding_x || 0);
      bh += 2 * (st.box_padding_y || 0);
    }
    var szOn = lay.safe_zone && lay.safe_zone.enabled;
    var sx = szOn ? cw * lay.safe_zone.title_left : 0;
    var sy = szOn ? ch * lay.safe_zone.title_top : 0;
    var sw = cw - sx * 2;
    var sh = ch - sy * 2;
    var top = sy + (lay.margin_top || 0);
    var bottom = sy + sh - (lay.margin_bottom || 0);
    var cy;
    if (bottom - top <= bh) { cy = (top + bottom) / 2; }
    else { cy = top + bh / 2 + (bottom - top - bh) * ((lay.v_slot - 1) / 4); }
    var left = sx + (lay.margin_left || 0);
    var right = sx + sw - (lay.margin_right || 0);
    var cx;
    if (lay.h_slot === "left") { cx = left + bw / 2; }
    else if (lay.h_slot === "right") { cx = right - bw / 2; }
    else { cx = (left + right) / 2; }
    if (lay.use_exact) { cx = lay.exact_x; cy = lay.exact_y; }
    return { cx: cx, cy: cy };
  }

  function applyToAe() {
    clearAlerts();
    if (!S.transcript || !S.transcript.blocks || !S.transcript.blocks.length) {
      alert("warn", "Нет субтитров для применения.", "Сначала создайте субтитры.");
      return;
    }
    if (!S.project) {
      alert("warn", "Нет открытой композиции.", "Сделайте композицию активной в After Effects.");
      return;
    }
    var payload = {
      blocks: S.transcript.blocks,
      style: readStyleFromForm(),
      layout: computeAePosition()
    };
    ae.applySubtitles(payload).then(function (res) {
      if (!res.ok) { showError(res.error); return; }
      showWarnings(res.warnings);
      alert("ok",
        "Применено: " + res.blocks + " субтитров в слое «" + res.layerName + "»",
        "Отменить можно обычным Ctrl+Z. Слои остаются редактируемыми в After Effects.");
    });
  }

  /* Быстрое обновление оформления: без пересоздания слоёв и ключей. */
  function restyleOnly() {
    clearAlerts();
    ae.updateStyleOnly({ style: readStyleFromForm() }).then(function (res) {
      if (!res.ok) { showError(res.error); return; }
      showWarnings(res.warnings);
    });
  }

  function pushPositionToAe() {
    var pos = computeAePosition();
    ae.updatePosition({ x: pos.cx, y: pos.cy }).then(function () { /* тихо */ });
  }

  /* ------------------------------------------------------- пресеты */

  function loadPresets() {
    return engine.presets().then(function (res) {
      if (!res || !res.ok) { return; }
      el.presetList.innerHTML = "";
      (res.builtin || []).concat(res.user || []).forEach(function (p) {
        var row = document.createElement("div");
        row.className = "tp-preset";
        var name = document.createElement("span");
        name.className = "tp-preset-name";
        name.textContent = p._name + (p._builtin === false ? "  (свой)" : "");
        row.appendChild(name);
        row.appendChild(smallBtn("Загрузить", function () {
          writeStyleToForm(p);
          renderPreview();
          clearAlerts();
          alert("ok", "Пресет «" + p._name + "» загружен", "Нажмите «Применить», чтобы перенести его в композицию.");
        }));
        if (p._builtin === false) {
          row.appendChild(smallBtn("Удалить", function () {
            engine.deletePreset({ name: p._name }).then(loadPresets);
          }));
        }
        el.presetList.appendChild(row);
      });
    });
  }

  /* ------------------------------------------------------- настройки */

  function loadSystem() {
    return engine.system().then(function (res) {
      if (!res || !res.ok) {
        el.systemInfo.textContent = "Движок недоступен.";
        return;
      }
      el.systemInfo.textContent =
        "timpoc " + res.version + "\n" +
        "Устройство: " + res.device.device + " / " + res.device.compute_type + "\n" +
        res.device.reason + "\n" +
        "Ядер CPU: " + res.cpu_count + "  ·  ОЗУ: " + res.ram_total_mb + " МБ\n" +
        "Свободно на диске: " + res.disk_free_mb + " МБ";

      el.model.innerHTML = "";
      el.modelsList.innerHTML = "";
      (res.models || []).forEach(function (m) {
        var o = document.createElement("option");
        o.value = m.name;
        o.textContent = m.name + " — " + m.mb + " МБ, русский: " + m.ru_quality;
        if (m.name === (res.models.find(function (x) { return x.installed; }) || {}).name) { o.selected = true; }
        el.model.appendChild(o);

        var row = document.createElement("div");
        row.className = "tp-model";
        var nm = document.createElement("span");
        nm.className = "tp-model-name";
        nm.textContent = m.name;
        row.appendChild(nm);
        var meta = document.createElement("span");
        meta.className = "tp-model-meta";
        meta.textContent = m.mb + " МБ · " + m.note;
        row.appendChild(meta);
        if (m.installed) {
          var ok = document.createElement("span");
          ok.className = "tp-model-meta";
          ok.textContent = "установлена";
          row.appendChild(ok);
        } else {
          row.appendChild(smallBtn("Скачать", function () {
            downloadModel(m.name);
          }));
        }
        el.modelsList.appendChild(row);
      });
      updateModelHint();
    });
  }

  function updateModelHint() {
    var name = el.model.value;
    el.modelHint.textContent =
      "Модель скачивается один раз и работает без интернета. " +
      "Для русского языка лучше medium или large-v3; turbo заточен под английский.";
  }

  function downloadModel(name) {
    clearAlerts();
    alert("warn", "Скачивание модели «" + name + "»", "Потребуется интернет. Аудио при этом никуда не отправляется.");
    engine.runJob(
      function () { return engine.downloadModel(name); },
      function (p) { setProgress(p.progress, p.message); el.progressWrap.hidden = false; }
    ).then(function (res) {
      el.progressWrap.hidden = true;
      clearAlerts();
      if (!res.ok) { showError(res.error); return; }
      alert("ok", "Модель установлена", "Теперь можно создавать субтитры без интернета.");
      loadSystem();
    });
  }

  function applyEffectsLevel(level, reduceMotion) {
    document.documentElement.classList.remove("fx-reduced", "fx-off");
    if (level === "reduced") { document.documentElement.classList.add("fx-reduced"); }
    if (level === "off") { document.documentElement.classList.add("fx-off"); }
    if (reduceMotion) { document.documentElement.classList.add("fx-reduced"); }
  }

  /* ------------------------------------------------------- состояние */

  function reloadState() {
    return engine.state().then(function (res) {
      if (!res || !res.ok) { return; }
      if (res.style) { writeStyleToForm(res.style); }
      if (res.layout) { writeLayoutToForm(res.layout); }
      S.transcript = res.transcript;
      renderBlocks();
      renderPreview();
    });
  }

  /* ------------------------------------------------------- события */

  function bindEvents() {
    el.btnCreate.addEventListener("click", startTranscribe);
    el.btnCancel.addEventListener("click", function () { engine.cancelCurrent(); });
    el.btnApply.addEventListener("click", applyToAe);
    el.btnRestyle.addEventListener("click", restyleOnly);
    el.btnResegment.addEventListener("click", function () {
      clearAlerts();
      engine.resegment({
        frame_rate: S.project ? S.project.frameRate : 25,
        segmentation: {
          max_words: parseInt(el.maxWords.value, 10) || 8,
          max_chars: parseInt(el.maxChars.value, 10) || 42,
          max_lines: parseInt(el.maxLines.value, 10) || 2
        }
      }).then(function (res) {
        if (!res.ok) { showError(res.error); return; }
        S.transcript = res.transcript;
        renderBlocks();
        alert("ok", "Фразы пересобраны", "Распознавание не повторялось.");
      });
    });
    el.btnShift.addEventListener("click", function () {
      clearAlerts();
      engine.shift({
        delta_seconds: parseFloat(el.shiftSeconds.value) || 0,
        frame_rate: S.project ? S.project.frameRate : 25
      }).then(function (res) {
        if (!res.ok) { showError(res.error); return; }
        S.transcript = res.transcript;
        renderBlocks();
      });
    });

    el.sourceKind.addEventListener("change", function () {
      el.layerField.hidden = el.sourceKind.value !== "layer";
      el.fileField.hidden = el.sourceKind.value !== "file";
    });

    el.model.addEventListener("change", updateModelHint);

    /* Оформление: мгновенный предпросмотр, но в AE — только по кнопке
       (иначе каждое движение ползунка пересоздавало бы слои). */
    var styleInputs = [
      "fontSize", "fontColor", "opacity", "caseMode", "tracking", "lineHeight",
      "textAlign", "boxWidth", "strokeColor", "strokeWidth", "strokeOpacity",
      "shadowColor", "shadowOpacity", "shadowDistance", "shadowSoftness", "shadowDirection",
      "boxColor", "boxOpacity", "boxPadX", "boxPadY", "boxRadius"
    ];
    styleInputs.forEach(function (id) {
      el[id].addEventListener("input", function () {
        updateSliderLabels();
        renderPreview();
      });
    });
    ["strokeEnabled", "shadowEnabled", "boxEnabled", "boxUseExpression"].forEach(function (id) {
      el[id].addEventListener("change", renderPreview);
    });
    el.fontFamily.addEventListener("change", function () {
      S.style.font_family = el.fontFamily.value;
      S.style.font_style = el.fontStyle.value;
      fillStyleList(el.fontFamily.value);
      checkFont();
      renderPreview();
    });
    el.fontStyle.addEventListener("change", renderPreview);
    el.previewAspect.addEventListener("change", renderPreview);

    /* Расположение */
    document.querySelectorAll(".tp-vslot").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll(".tp-vslot").forEach(function (x) { x.classList.remove("is-active"); });
        b.classList.add("is-active");
        renderPreview();
        pushPositionToAe();
      });
    });
    document.querySelectorAll(".tp-hslot").forEach(function (b) {
      b.addEventListener("click", function () {
        document.querySelectorAll(".tp-hslot").forEach(function (x) { x.classList.remove("is-active"); });
        b.classList.add("is-active");
        renderPreview();
        pushPositionToAe();
      });
    });
    ["marginTop", "marginBottom", "marginLeft", "marginRight", "exactX", "exactY", "safeTB", "safeLR"].forEach(function (id) {
      el[id].addEventListener("input", function () { renderPreview(); });
    });
    el.useExact.addEventListener("change", renderPreview);
    el.safeEnabled.addEventListener("change", renderPreview);
    el.btnFit.addEventListener("click", function () {
      el.useExact.checked = false;
      el.marginLeft.value = Math.max(20, parseFloat(el.marginLeft.value) || 60);
      el.marginRight.value = Math.max(20, parseFloat(el.marginRight.value) || 60);
      renderPreview();
      pushPositionToAe();
      clearAlerts();
      alert("ok", "Блок вписан в кадр");
    });

    el.btnSavePreset.addEventListener("click", function () {
      var name = el.presetName.value.trim() || "Мой пресет";
      engine.savePreset({ name: name, style: readStyleFromForm() }).then(function (res) {
        clearAlerts();
        if (!res.ok) { showError(res.error); return; }
        alert("ok", "Пресет сохранён");
        loadPresets();
      });
    });

    /* Настройки */
    el.effectsLevel.addEventListener("change", function () {
      applyEffectsLevel(el.effectsLevel.value, el.reduceMotion.checked);
      engine.updateSettings({ settings: { ui: { effects_level: el.effectsLevel.value } } });
    });
    el.reduceMotion.addEventListener("change", function () {
      applyEffectsLevel(el.effectsLevel.value, el.reduceMotion.checked);
      engine.updateSettings({ settings: { ui: { reduce_motion: el.reduceMotion.checked } } });
    });
    el.offlineMode.addEventListener("change", function () {
      engine.updateSettings({ settings: { privacy: { offline_mode: el.offlineMode.checked } } });
      clearAlerts();
      alert(el.offlineMode.checked ? "warn" : "ok",
        el.offlineMode.checked ? "Автономный режим включён" : "Автономный режим выключен",
        el.offlineMode.checked
          ? "Сеть полностью отключена: скачивание моделей недоступно."
          : "Скачивание моделей снова доступно.");
    });
    el.device.addEventListener("change", function () {
      engine.updateSettings({ settings: { recognition: { device: el.device.value } } });
    });

    el.btnExportSrt.addEventListener("click", function () {
      var name = (S.project ? S.project.name : "subtitles") + ".srt";
      var path = window.prompt("Куда сохранить SRT (полный путь):", name);
      if (!path) { return; }
      engine.exportSrt({ path: path }).then(function (res) {
        clearAlerts();
        if (!res.ok) { showError(res.error); return; }
        alert("ok", "SRT сохранён", path);
      });
    });
    el.btnImportSrt.addEventListener("click", function () {
      var path = window.prompt("Путь к файлу SRT:", "");
      if (!path) { return; }
      engine.importSrt({ path: path }).then(function (res) {
        clearAlerts();
        if (!res.ok) { showError(res.error); return; }
        S.transcript = res.transcript;
        renderBlocks();
        alert("ok", "SRT импортирован", "Времена слов приближённые — переразбивка будет менее точной.");
      });
    });
    el.btnDiagnostics.addEventListener("click", function () {
      engine.diagnostics().then(function (res) {
        el.logBox.hidden = false;
        el.logBox.textContent = res && res.ok
          ? JSON.stringify(res, null, 2)
          : "Движок недоступен. Запустите timpoc из меню Пуск.";
      });
    });
    el.btnRemoveLayers.addEventListener("click", function () {
      if (!window.confirm("Удалить слои, созданные timpoc? Ваши слои не затрагиваются.")) { return; }
      ae.removeTimpocLayers().then(function (res) {
        clearAlerts();
        if (!res.ok) { showError(res.error); return; }
        alert("ok", "Удалено слоёв: " + res.count, "Отменить можно через Ctrl+Z.");
      });
    });
  }

  /* ------------------------------------------------------- запуск */

  function init() {
    cacheDom();
    initTabs();
    initDrag();
    bindEvents();
    updateSliderLabels();

    setEngineStatus("", "подключение…");
    engine.health().then(function (ok) {
      if (ok) {
        setEngineStatus("ok", "движок готов");
        loadSystem();
        reloadState();
      } else {
        setEngineStatus("err", "движок не запущен");
        alert("err",
          "Движок распознавания не запущен.",
          "Запустите timpoc из меню Пуск (или из установщика). After Effects сам движок не поднимает.");
      }
    });

    refreshProject();
    refreshFonts();
    if (ae.available()) {
      setEngineStatus("ok", "AE подключён");
    } else {
      alert("warn",
        "Панель открыта вне After Effects.",
        "Доступны вёрстка и предпросмотр. Для работы с композицией откройте панель через Window → Extensions → timpoc.");
    }
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
