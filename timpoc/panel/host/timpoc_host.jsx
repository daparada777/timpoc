/*
 * timpoc — хост-слой интеграции с After Effects (ExtendScript / CEP).
 *
 * ЧТО ЭТО
 *   Единственное место, где код трогает проект After Effects. Панель (panel/)
 *   вызывает отсюда функции через CSInterface.evalScript и получает JSON-строку.
 *
 * ПОЧЕМУ ТАК
 *   ExtendScript — это JavaScript версии ES3: нет JSON, нет Array.forEach в ряде
 *   версий, нет let/const, нет шаблонных строк. Поэтому:
 *     - ответ всегда формируется вручную через jsonEscape/jsonObj;
 *     - панель парсит JSON у себя (в браузере JSON есть);
 *     - код пишется в стиле ES3 намеренно, иначе панель просто не загрузится.
 *
 * ГЛАВНЫЕ ПРАВИЛА (из требований)
 *   1. Субтитры остаются редактируемыми средствами AE: это обычный текстовый
 *      слой с ключами Source Text. Никакого «вшивания» в видео.
 *   2. Один слой на всю дорожку субтитров, а не по слою на фразу: длинный
 *      проект с 800 субтитрами не превращается в 800 слоёв.
 *   3. Все операции обёрнуты в app.beginUndoGroup — пользователь может отменить
 *      применение обычным Ctrl+Z.
 *   4. Ничего не удаляем, кроме слоёв с нашей меткой. Пользовательские слои
 *      не трогаем никогда.
 *   5. Если что-то не учли (например, сложное растяжение времени) — возвращаем
 *      предупреждение, а не делаем вид, что всё хорошо.
 */

#include "json2.jsx"

var TIMPOC_TAG = "timpoc";
var TIMPOC_VERSION = "0.1.0";
var TIMPOC_PREFIX = "timpoc — ";

/* ------------------------------------------------------------------ утилиты */

function timpocError(message, hint, code) {
    return {
        ok: false,
        error: {
            code: code || "ae_error",
            message: message || "Неизвестная ошибка в After Effects.",
            hint: hint || ""
        }
    };
}

function timpocOk(payload) {
    var out = { ok: true };
    if (payload) {
        for (var k in payload) {
            if (payload.hasOwnProperty(k)) { out[k] = payload[k]; }
        }
    }
    return out;
}

/* Безопасный доступ к свойству: в AE обращение к отсутствующему свойству
   бросает исключение, а не возвращает undefined. */
function hasProp(obj, name) {
    try {
        return obj && obj.property(name) !== null && obj.property(name) !== undefined;
    } catch (e) {
        return false;
    }
}

function propOrNull(layer, name) {
    try {
        var p = layer.property(name);
        return (p === null || p === undefined) ? null : p;
    } catch (e) {
        return null;
    }
}

/* Маркер «это слой timpoc». Храним в comment слоя:
   это поддерживаемое свойство, оно переживает сохранение проекта и видно
   пользователю — можно понять, что за слой, без скрытых данных. */
function isTimpocLayer(layer) {
    try {
        return layer && layer.comment && layer.comment.indexOf(TIMPOC_TAG) === 0;
    } catch (e) {
        return false;
    }
}

function markLayer(layer, kind) {
    layer.comment = TIMPOC_TAG + "|" + kind + "|v" + TIMPOC_VERSION;
}

function layerKind(layer) {
    try {
        var parts = String(layer.comment).split("|");
        return parts.length > 1 ? parts[1] : "";
    } catch (e) {
        return "";
    }
}

/* ------------------------------------------------------------------ проект */

function getProjectInfo() {
    try {
        var proj = app.project;
        var item = proj.activeItem;
        if (!item || !(item instanceof CompItem)) {
            return timpocError(
                "Нет открытой композиции.",
                "Откройте или создайте композицию и сделайте её активной, затем нажмите «Обновить».",
                "no_comp"
            );
        }
        var audioLayers = [];
        var i;
        for (i = 1; i <= item.numLayers; i++) {
            var lyr = item.layer(i);
            if (lyr.hasAudio) {
                audioLayers.push({
                    index: lyr.index,
                    name: lyr.name,
                    inPoint: lyr.inPoint,
                    outPoint: lyr.outPoint,
                    startTime: lyr.startTime,
                    stretch: lyr.stretch,
                    hasTimeRemap: lyr.timeRemapEnabled === true
                });
            }
        }
        return timpocOk({
            name: item.name,
            width: item.width,
            height: item.height,
            frameRate: item.frameRate,
            duration: item.duration,
            workAreaStart: item.workAreaStart,
            workAreaDuration: item.workAreaDuration,
            displayStartTime: item.displayStartTime,
            time: item.time,
            audioLayers: audioLayers,
            aeVersion: app.version
        });
    } catch (e) {
        return timpocError("Не удалось прочитать проект.", "Закройте и снова откройте проект.", "read_project", e.toString());
    }
}

/* Список шрифтов, которые ВИДИТ AFTER EFFECTS. Именно этот список важен:
   шрифт может лежать в системе, но не подхватиться до перезапуска AE. */
function getFonts() {
    try {
        var families = [];
        var styles = {};
        var n = app.fonts.length;
        for (var i = 0; i < n; i++) {
            var f = app.fonts[i];
            var fam = f.family;
            if (families.indexOf(fam) === -1) { families.push(fam); }
            if (!styles[fam]) { styles[fam] = []; }
            if (f.style && styles[fam].indexOf(f.style) === -1) { styles[fam].push(f.style); }
        }
        return timpocOk({ families: families, styles: styles, count: families.length });
    } catch (e) {
        return timpocError(
            "Не удалось получить список шрифтов.",
            "Перезапустите After Effects. Пока будет использоваться системный список.",
            "fonts",
            e.toString()
        );
    }
}

/* ------------------------------------------------------------------ аудио */

/*
 * ЧЕСТНО ПРО «РАСТЯЖЕНИЕ ВРЕМЕНИ И СЛОЖНЫЙ МИКС»
 *
 * Мы НЕ пытаемся вычислить итоговый звук композиции по параметрам слоёв:
 * time remap, экспрешены и эффекты на звуке делают это недостоверным.
 * Вместо этого:
 *   1) если у слоя включён time remap — возвращаем предупреждение и предлагаем
 *      предварительный рендер звука;
 *   2) простое растяжение (stretch) учитываем точно — передаём его в движок;
 *   3) для «звука композиции» предлагаем сделать RAM-превью/экспорт в WAV,
 *      то есть распознавать ровно то, что слышно.
 */
function getAudioSource(request) {
    try {
        var proj = app.project;
        var comp = proj.activeItem;
        if (!comp || !(comp instanceof CompItem)) {
            return timpocError("Нет открытой композиции.", "Сделайте композицию активной.", "no_comp");
        }

        var kind = (request && request.kind) || "comp";
        var warnings = [];
        var result = {
            kind: kind,
            frameRate: comp.frameRate,
            compStart: comp.displayStartTime,
            compEnd: comp.displayStartTime + comp.duration
        };

        if (kind === "comp") {
            result.rangeStart = comp.workAreaStart;
            result.rangeEnd = comp.workAreaStart + comp.workAreaDuration;
            result.offsetSeconds = comp.workAreaStart;
            result.needsRender = true;
            result.label = "Звук композиции (рабочая область)";
            warnings.push(
                "Для звука композиции нужен предварительный рендер в WAV. " +
                "Это единственный достоверный способ учесть эффекты, time remap и микс."
            );
            if (!comp.hasAudio) {
                return timpocError(
                    "В композиции нет звука.",
                    "Добавьте слой со звуком или выберите отдельный аудиофайл.",
                    "no_audio"
                );
            }
        } else if (kind === "layer") {
            var idx = request.index;
            if (!idx || idx < 1 || idx > comp.numLayers) {
                return timpocError("Слой не найден.", "Обновите список слоёв в панели.", "no_layer");
            }
            var lyr = comp.layer(idx);
            if (!lyr.hasAudio) {
                return timpocError(
                    "У выбранного слоя нет звука.",
                    "Выберите слой со звуковой дорожкой или используйте отдельный аудиофайл.",
                    "no_audio"
                );
            }
            var src = lyr.source;
            if (!src || !src.file) {
                return timpocError(
                    "Не удалось определить файл звука для этого слоя.",
                    "Если слой создан синтезом или является частью препомпа — выведите звук в файл и выберите его вручную.",
                    "no_source_file"
                );
            }
            var stretch = lyr.stretch || 1;
            if (lyr.timeRemapEnabled === true) {
                warnings.push(
                    "У слоя включено растяжение времени (time remap). " +
                    "Точный учёт в этом случае невозможен без рендера. " +
                    "Рекомендуется распознать звук композиции после рендера в WAV."
                );
            }
            result.label = lyr.name;
            result.path = src.file.fsName;
            result.rangeStart = lyr.inPoint;
            result.rangeEnd = lyr.outPoint;
            result.offsetSeconds = lyr.startTime;
            result.stretch = stretch;
            result.needsRender = false;
        } else {
            return timpocError(
                "Неизвестный источник звука.",
                "Выберите «звук композиции», «слой» или «файл».",
                "bad_source"
            );
        }

        result.warnings = warnings;
        return timpocOk(result);
    } catch (e) {
        return timpocError("Не удалось определить источник звука.", "", "audio_source", e.toString());
    }
}

/*
 * Экспорт звука рабочей области композиции в WAV — тот самый «поддерживаемый
 * способ получить результирующее аудио», когда напрямую учесть эффекты нельзя.
 *
 * Реализация: добавляем временную композицию с нужным фрагментом и выводим
 * через очередь рендера. Пользователь видит процесс в AE и может его отменить.
 */
function renderCompAudio(request) {
    try {
        var comp = app.project.activeItem;
        if (!comp || !(comp instanceof CompItem)) {
            return timpocError("Нет открытой композиции.", "Сделайте композицию активной.", "no_comp");
        }
        var outPath = request && request.path;
        if (!outPath) {
            return timpocError("Не указан путь для WAV.", "Панель должна передать путь во временную папку.", "bad_path");
        }
        var start = (request && typeof request.start === "number") ? request.start : comp.workAreaStart;
        var duration = (request && typeof request.duration === "number") ? request.duration : comp.workAreaDuration;

        app.beginUndoGroup("timpoc: экспорт звука");
        try {
            var tmpName = "timpoc_audio_render";
            var tmpComp = comp.duplicate();
            tmpComp.name = tmpName;
            tmpComp.workAreaStart = start;
            tmpComp.workAreaDuration = duration;

            var templateName = "WAV 48 kHz";
            var templateFound = false;
            var i;
            for (i = 0; i < app.project.renderQueue.numItems; i++) { /* no-op */ }
            var rqItem = app.project.renderQueue.items.add(tmpComp);
            var om = rqItem.outputModule(1);
            var templates = om.templates;
            for (i = 0; i < templates.length; i++) {
                if (templates[i].indexOf("WAV") === 0) {
                    om.applyTemplate(templates[i]);
                    templateFound = true;
                    templateName = templates[i];
                    break;
                }
            }
            if (!templateFound) {
                om.applyTemplate(om.templates[0]);
                templateName = om.templates[0];
            }
            om.file = new File(outPath);
            app.project.renderQueue.render();

            tmpComp.remove();
        } finally {
            app.endUndoGroup();
        }

        var f = new File(outPath);
        if (!f.exists) {
            return timpocError(
                "Рендер завершился, но файл не создан.",
                "Проверьте, что в очереди рендера есть свободное место на диске и права на папку.",
                "render_failed"
            );
        }
        return timpocOk({
            path: outPath,
            start: start,
            duration: duration,
            template: templateName,
            offsetSeconds: start
        });
    } catch (e) {
        return timpocError(
            "Не удалось вывести звук композиции.",
            "Попробуйте вывести звук вручную: Композиция → Добавить в очередь рендера, формат WAV.",
            "render",
            e.toString()
        );
    }
}

/* ------------------------------------------------------- создание субтитров */

/* Ищем слой, созданный timpoc ранее, чтобы обновить его без дублей. */
function findTimpocTextLayer(comp) {
    for (var i = 1; i <= comp.numLayers; i++) {
        var lyr = comp.layer(i);
        if (isTimpocLayer(lyr) && layerKind(lyr) === "subtitles") {
            return lyr;
        }
    }
    return null;
}

function findTimpocBoxLayer(comp) {
    for (var i = 1; i <= comp.numLayers; i++) {
        var lyr = comp.layer(i);
        if (isTimpocLayer(lyr) && layerKind(lyr) === "box") {
            return lyr;
        }
    }
    return null;
}

function ensureTimpocFolder(comp) {
    var i;
    for (i = 1; i <= comp.numLayers; i++) {
        var lyr = comp.layer(i);
        if (isTimpocLayer(lyr) && layerKind(lyr) === "folder") {
            return lyr;
        }
    }
    var nullLayer = comp.layers.addNull();
    nullLayer.name = TIMPOC_PREFIX + "группа";
    markLayer(nullLayer, "folder");
    nullLayer.enabled = false;
    nullLayer.shy = true;
    return nullLayer;
}

/* Цвет из 0..255 в формат AE 0..1 */
function toAeColor(rgb) {
    return [
        (rgb[0] || 0) / 255.0,
        (rgb[1] || 0) / 255.0,
        (rgb[2] || 0) / 255.0
    ];
}

/* Применяем оформление к текстовому слою. Каждое свойство — реальное свойство
   текстового слоя AE, а не выдуманное API. */
function applyStyleToTextLayer(textLayer, style, frameRate) {
    var warnings = [];
    var props = textLayer.property("ADBE Text Properties");

    /* --- Шрифт --- */
    var animators = props.property("ADBE Text Animators");
    var options = props.property("ADBE Text More Options");
    var sourceText = textLayer.property("ADBE Text Properties").property("ADBE Text Document");

    /* Шрифт и начертание задаются внутри TextDocument, поэтому их применяем
       вместе с текстом (см. makeTextDocument). */

    /* --- Цвет заливки и размер --- */
    try {
        var fill = props.property("ADBE Text Fill Color");
        if (fill) { fill.setValue(toAeColor(style.color || [255, 255, 255])); }
    } catch (e) { warnings.push("Не удалось задать цвет текста."); }

    try {
        var fillOpacity = props.property("ADBE Text Fill Opacity");
        if (fillOpacity) { fillOpacity.setValue(Math.round((style.opacity === undefined ? 1 : style.opacity) * 100)); }
    } catch (e2) { /* свойство может отсутствовать в старых версиях */ }

    /* --- Обводка --- */
    try {
        var strokeEnabled = props.property("ADBE Text Stroke");
        if (style.stroke_enabled) {
            if (hasProp(props, "ADBE Text Stroke Color")) {
                props.property("ADBE Text Stroke Color").setValue(toAeColor(style.stroke_color || [0, 0, 0]));
            }
            if (hasProp(props, "ADBE Text Stroke Width")) {
                props.property("ADBE Text Stroke Width").setValue(style.stroke_width || 0);
            }
            if (hasProp(props, "ADBE Text Stroke Opacity")) {
                props.property("ADBE Text Stroke Opacity").setValue(Math.round((style.stroke_opacity === undefined ? 1 : style.stroke_opacity) * 100));
            }
        } else if (hasProp(props, "ADBE Text Stroke Width")) {
            props.property("ADBE Text Stroke Width").setValue(0);
        }
    } catch (e3) { warnings.push("Не удалось применить обводку."); }

    /* --- Трекинг и интерлиньяж --- */
    try {
        if (hasProp(props, "ADBE Text Tracking Amount")) {
            props.property("ADBE Text Tracking Amount").setValue(style.tracking || 0);
        }
    } catch (e4) { warnings.push("Не удалось применить межбуквенное расстояние."); }

    /* --- Тень: эффект Drop Shadow (штатный эффект AE) --- */
    try {
        var shadow = null;
        var i;
        for (i = 1; i <= textLayer.numProperties; i++) {
            var p = textLayer.property(i);
            if (p.matchName === "ADBE Drop Shadow") { shadow = p; break; }
        }
        if (style.shadow_enabled) {
            if (!shadow) { shadow = textLayer.property("ADBE Effect Parade").addProperty("ADBE Drop Shadow"); }
            if (hasProp(shadow, "ADBE Drop Shadow-0001")) { shadow.property("ADBE Drop Shadow-0001").setValue(toAeColor(style.shadow_color || [0, 0, 0])); }
            if (hasProp(shadow, "ADBE Drop Shadow-0002")) { shadow.property("ADBE Drop Shadow-0002").setValue(Math.round((style.shadow_opacity === undefined ? 0.5 : style.shadow_opacity) * 255)); }
            if (hasProp(shadow, "ADBE Drop Shadow-0003")) { shadow.property("ADBE Drop Shadow-0003").setValue(style.shadow_direction || 135); }
            if (hasProp(shadow, "ADBE Drop Shadow-0004")) { shadow.property("ADBE Drop Shadow-0004").setValue(style.shadow_distance || 0); }
            if (hasProp(shadow, "ADBE Drop Shadow-0005")) { shadow.property("ADBE Drop Shadow-0005").setValue(style.shadow_softness || 0); }
        } else if (shadow) {
            shadow.remove();
        }
    } catch (e5) { warnings.push("Не удалось применить тень."); }

    return warnings;
}

/* Создаём TextDocument: здесь задаются шрифт, размер, выравнивание и ширина блока. */
function makeTextDocument(style, text) {
    var doc = new TextDocument("");
    try { doc.text = text; } catch (e) { doc.text = String(text); }
    doc.font = style.font_family || "Arial";
    if (style.font_style) {
        try { doc.fontStyle = style.font_style; } catch (e2) { /* начертания может не быть */ }
    }
    doc.fontSize = style.font_size || 64;
    doc.fillColor = toAeColor(style.color || [255, 255, 255]);
    doc.applyFill = true;
    doc.applyStroke = style.stroke_enabled ? true : false;
    if (style.stroke_enabled) {
        doc.strokeColor = toAeColor(style.stroke_color || [0, 0, 0]);
        doc.strokeWidth = style.stroke_width || 0;
    }
    doc.tracking = (style.case_mode === "caps")
        ? (style.tracking || 0) + 30
        : (style.tracking || 0);
    doc.leading = (style.font_size || 64) * (style.line_height || 1.25);

    var align = style.text_align || "center";
    doc.justification = (align === "left") ? ParagraphJustification.LEFT_JUSTIFY
        : (align === "right") ? ParagraphJustification.RIGHT_JUSTIFY
        : ParagraphJustification.CENTER_JUSTIFY;

    /* Ширина блока: paragraph (box) text, а не point text.
       Только так AE сам переносит строки по заданной ширине. */
    try {
        doc.boxText = true;
        doc.boxTextPos = [0, 0];
        doc.boxTextSize = [style.box_width || 900, Math.max(100, (style.font_size || 64) * (style.line_height || 1.25) * (style.max_lines || 2) * 1.5)];
    } catch (e3) { /* boxText доступен не во всех версиях — тогда point text */ }

    return doc;
}

/*
 * ГЛАВНАЯ ФУНКЦИЯ: применить субтитры к композиции.
 *
 * Схема: ОДИН текстовый слой с ключами Source Text.
 *   - на in каждого блока ставим его текст;
 *   - на out ставим пустую строку — текст исчезает;
 *   - между ключами значение держится, поэтому субтитр виден всё своё время.
 *
 * Почему не «слой на каждую фразу»: на длинном проекте это сотни слоёв,
 * AE начинает тормозить, а таймлайн становится нечитаемым.
 */
function applySubtitles(request) {
    try {
        var comp = app.project.activeItem;
        if (!comp || !(comp instanceof CompItem)) {
            return timpocError("Нет открытой композиции.", "Сделайте композицию активной.", "no_comp");
        }
        var blocks = request && request.blocks;
        if (!blocks || blocks.length === 0) {
            return timpocError("Нет субтитров для применения.", "Сначала создайте субтитры.", "no_blocks");
        }
        var style = request.style || {};
        var layout = request.layout || {};
        var frameRate = comp.frameRate || 25;
        var warnings = [];

        app.beginUndoGroup("timpoc: применить субтитры");
        try {
            var folder = ensureTimpocFolder(comp);
            var textLayer = findTimpocTextLayer(comp);
            var created = false;

            if (!textLayer) {
                textLayer = comp.layers.addText("");
                textLayer.name = TIMPOC_PREFIX + "субтитры";
                markLayer(textLayer, "subtitles");
                created = true;
            }

            /* Якорь в центр блока — иначе при смене длины текста блок «уезжает». */
            try {
                var anchor = textLayer.property("ADBE Transform Group").property("ADBE Anchor Point");
                var boxW = style.box_width || 900;
                anchor.setValue([boxW / 2.0, 0]);
            } catch (eAnchor) { /* не критично */ }

            warnings = warnings.concat(applyStyleToTextLayer(textLayer, style, frameRate));

            /* --- Ключи Source Text --- */
            var srcText = propOrNull(textLayer, "ADBE Text Properties");
            var docProp = srcText ? propOrNull(srcText, "ADBE Text Document") : null;
            if (!docProp) {
                return timpocError(
                    "Не удалось получить свойство Source Text.",
                    "Обновите After Effects: свойство текста недоступно в этой версии.",
                    "no_source_text"
                );
            }
            /* Чистим старые ключи — иначе при повторном применении останутся хвосты. */
            while (docProp.numKeys > 0) { docProp.removeKey(1); }

            var emptyDoc = makeTextDocument(style, "");
            var i;
            for (i = 0; i < blocks.length; i++) {
                var b = blocks[i];
                var text = applyCase(style.case_mode, b.text || "");
                docProp.setValueAtTime(b["in"], makeTextDocument(style, text));
                docProp.setValueAtTime(b.out, emptyDoc);
            }
            /* До первого субтитра и после последнего — пусто. */
            if (blocks[0]["in"] > comp.displayStartTime) {
                docProp.setValueAtTime(comp.displayStartTime, emptyDoc);
            }

            /* --- Позиция --- */
            var pos = textLayer.property("ADBE Transform Group").property("ADBE Position");
            pos.setValue([
                (layout.cx === undefined) ? comp.width / 2.0 : layout.cx,
                (layout.cy === undefined) ? comp.height - 120 : layout.cy
            ]);

            /* --- Подложка --- */
            if (style.box_enabled) {
                var boxLayer = findTimpocBoxLayer(comp);
                if (!boxLayer) {
                    boxLayer = comp.layers.addSolid(
                        toAeColor(style.box_color || [0, 0, 0]),
                        TIMPOC_PREFIX + "подложка",
                        Math.max(10, Math.round(style.box_width || 900)),
                        Math.max(10, Math.round((style.font_size || 64) * 2)),
                        1.0,
                        frameRate,
                        comp.duration
                    );
                    markLayer(boxLayer, "box");
                }
                boxLayer.opacity = Math.round((style.box_opacity === undefined ? 0.5 : style.box_opacity) * 100);
                try {
                    boxLayer.startTime = comp.displayStartTime;
                    boxLayer.outPoint = comp.displayStartTime + comp.duration;
                } catch (eBox) { /* не критично */ }

                /* Скругление углов — штатный эффект Rounded Corners. */
                try {
                    var rounded = null;
                    for (i = 1; i <= boxLayer.numProperties; i++) {
                        if (boxLayer.property(i).matchName === "ADBE Rounded Corners") { rounded = boxLayer.property(i); break; }
                    }
                    if (!rounded) { rounded = boxLayer.property("ADBE Effect Parade").addProperty("ADBE Rounded Corners"); }
                    rounded.property(1).setValue(style.box_corner_radius || 0);
                } catch (eRound) { warnings.push("Не удалось применить скругление углов подложки."); }

                /* Авто-размер по тексту через sourceRectAtTime — стандартный приём.
                   Если пользователь не хочет экспрешены, ставим фиксированный размер
                   и честно об этом говорим. */
                if (style.box_use_expression) {
                    try {
                        var scaleExpr =
                            "(function(){\n" +
                            "  try {\n" +
                            "    var t = thisComp.layer('" + textLayer.name.replace(/'/g, "\\'") + "');\n" +
                            "    var r = t.sourceRectAtTime(time, false);\n" +
                            "    var px = " + (style.box_padding_x || 0) + ";\n" +
                            "    var py = " + (style.box_padding_y || 0) + ";\n" +
                            "    var w = r.width + px * 2;\n" +
                            "    var h = r.height + py * 2;\n" +
                            "    [w / this.width * 100, h / this.height * 100];\n" +
                            "  } catch (e) { [100, 100]; }\n" +
                            "})()";
                        boxLayer.property("ADBE Transform Group").property("ADBE Scale").expression = scaleExpr;
                    } catch (eExpr) {
                        warnings.push(
                            "Не удалось включить авто-размер подложки. " +
                            "Подложка останется фиксированного размера."
                        );
                    }
                } else {
                    try {
                        boxLayer.property("ADBE Transform Group").property("ADBE Scale").expression = "";
                    } catch (eNoExpr) { /* ignore */ }
                    warnings.push(
                        "Авто-размер подложки выключен: она не будет повторять длину текста."
                    );
                }

                /* Подложка должна быть ПОД текстом. */
                try { boxLayer.moveAfter(textLayer); } catch (eMove) { /* ignore */ }
                var boxPos = boxLayer.property("ADBE Transform Group").property("ADBE Position");
                boxPos.setValue([
                    (layout.cx === undefined) ? comp.width / 2.0 : layout.cx,
                    (layout.cy === undefined) ? comp.height - 120 : layout.cy
                ]);
            } else {
                var existingBox = findTimpocBoxLayer(comp);
                if (existingBox) { existingBox.remove(); }
            }

            /* Организуем: кладём слои в группу timpoc. */
            try {
                textLayer.parent = null;
            } catch (eParent) { /* не критично */ }
        } finally {
            app.endUndoGroup();
        }

        return timpocOk({
            layerName: textLayer.name,
            created: created,
            blocks: blocks.length,
            frameRate: frameRate,
            warnings: warnings
        });
    } catch (e) {
        return timpocError(
            "Не удалось применить субтитры.",
            "Проверьте, что композиция активна. Распознанный текст сохранён — распознавание повторять не нужно.",
            "apply",
            e.toString()
        );
    }
}

function applyCase(mode, text) {
    if (!text) { return ""; }
    if (mode === "upper" || mode === "caps") { return text.toUpperCase(); }
    if (mode === "lower") { return text.toLowerCase(); }
    return text;
}

/* Быстрое обновление ТОЛЬКО оформления — без пересоздания слоёв и ключей.
   Именно это делает движение ползунка мгновенным. */
function updateStyleOnly(request) {
    try {
        var comp = app.project.activeItem;
        if (!comp || !(comp instanceof CompItem)) {
            return timpocError("Нет открытой композиции.", "Сделайте композицию активной.", "no_comp");
        }
        var textLayer = findTimpocTextLayer(comp);
        if (!textLayer) {
            return timpocError(
                "Субтитры timpoc ещё не созданы.",
                "Сначала нажмите «Применить», затем меняйте оформление.",
                "not_applied"
            );
        }
        var style = request.style || {};
        app.beginUndoGroup("timpoc: оформление");
        var warnings;
        try {
            warnings = applyStyleToTextLayer(textLayer, style, comp.frameRate);
        } finally {
            app.endUndoGroup();
        }
        return timpocOk({ layerName: textLayer.name, warnings: warnings || [] });
    } catch (e) {
        return timpocError("Не удалось обновить оформление.", "", "style", e.toString());
    }
}

/* Перемещение блока субтитров. Отдельная функция — вызывается при перетаскивании
   мышью в предпросмотре, поэтому должна быть максимально дешёвой. */
function updatePosition(request) {
    try {
        var comp = app.project.activeItem;
        if (!comp || !(comp instanceof CompItem)) {
            return timpocError("Нет открытой композиции.", "", "no_comp");
        }
        var textLayer = findTimpocTextLayer(comp);
        if (!textLayer) {
            return timpocError("Субтитры timpoc ещё не созданы.", "Сначала нажмите «Применить».", "not_applied");
        }
        var pos = [request.x, request.y];
        app.beginUndoGroup("timpoc: позиция");
        try {
            textLayer.property("ADBE Transform Group").property("ADBE Position").setValue(pos);
            var boxLayer = findTimpocBoxLayer(comp);
            if (boxLayer) {
                boxLayer.property("ADBE Transform Group").property("ADBE Position").setValue(pos);
            }
        } finally {
            app.endUndoGroup();
        }
        return timpocOk({ x: pos[0], y: pos[1] });
    } catch (e) {
        return timpocError("Не удалось изменить позицию.", "", "position", e.toString());
    }
}

/* Удаляем ТОЛЬКО слои timpoc. Пользовательские слои не трогаем. */
function removeTimpocLayers() {
    try {
        var comp = app.project.activeItem;
        if (!comp || !(comp instanceof CompItem)) {
            return timpocError("Нет открытой композиции.", "", "no_comp");
        }
        var removed = [];
        app.beginUndoGroup("timpoc: удалить субтитры");
        try {
            var i;
            /* Идём с конца: удаление меняет индексы. */
            for (i = comp.numLayers; i >= 1; i--) {
                var lyr = comp.layer(i);
                if (isTimpocLayer(lyr)) {
                    removed.push(lyr.name);
                    lyr.remove();
                }
            }
        } finally {
            app.endUndoGroup();
        }
        return timpocOk({ removed: removed, count: removed.length });
    } catch (e) {
        return timpocError("Не удалось удалить слои timpoc.", "", "remove", e.toString());
    }
}

/* Реальные метрики текстового блока — точнее любой оценки в предпросмотре. */
function measureText(request) {
    try {
        var comp = app.project.activeItem;
        if (!comp || !(comp instanceof CompItem)) {
            return timpocError("Нет открытой композиции.", "", "no_comp");
        }
        var textLayer = findTimpocTextLayer(comp);
        if (!textLayer) {
            return timpocError("Субтитры timpoc ещё не созданы.", "", "not_applied");
        }
        var t = (typeof request.time === "number") ? request.time : comp.time;
        var rect = textLayer.sourceRectAtTime(t, false);
        return timpocOk({
            width: rect.width,
            height: rect.height,
            left: rect.left,
            top: rect.top,
            time: t
        });
    } catch (e) {
        return timpocError(
            "Не удалось измерить текстовый блок.",
            "Измерение доступно только после применения субтитров.",
            "measure",
            e.toString()
        );
    }
}

function ping() {
    return timpocOk({ pong: true, version: TIMPOC_VERSION, aeVersion: app.version });
}
