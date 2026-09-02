"""The configuration GUI, rendered as a Rich UI card in the chat.

Spike S7 showed that OpenWebUI embeds HTML returned by a tool as an iframe, provided the
response carries ``Content-Disposition: inline``. The markup travels inside the tool
result and is never fetched separately, so there is no route and no signed link here.

Three constraints from OpenWebUI's sandbox shape everything below:

* **No forms.** ``allow-forms`` is off by default, so a ``<form>`` submission is blocked
  by the sandbox. The card uses a plain button and a click handler instead.
* **The theme is read from the page, never from the server.** The card mirrors the
  ``dark`` class OpenWebUI puts on its own ``<html>``, and falls back to
  ``prefers-color-scheme`` when the parent document is out of reach. Both live in
  :mod:`.theme_ui`, which explains why that order and not the other one.

  Whatever decides, the card must declare ``color-scheme: light dark`` on ``:root``.
  Declaring a single scheme is what breaks embedding: a document whose used scheme
  differs from its embedder is given an *opaque* canvas, so a card pinned to light
  becomes a white rectangle in a dark chat.

  Gone from earlier versions: a server-side guess from OpenWebUI's settings API, and an
  in-card toggle to correct it. The guess pinned the card at render time, which is
  precisely what stopped it following a theme switch, and the toggle only existed to fix
  answers the parent sync now gets right.
* **No callbacks.** Nothing inside can reach HiveMCP, so everything the card needs is
  inlined at render time and the only way out is ``postMessage``.

Template names are chosen by users and end up in this markup, so the environment
autoescapes, and values destined for the script block go through :func:`js_literal`.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from jinja2 import Environment, select_autoescape
from markupsafe import Markup

from ..core.preferences import UserPreferences
from ..core.render.theme import CJK_FONTS, SAFE_FONTS
from .theme_ui import PARENT_THEME_SYNC, dark_css

ConfigKind = Literal["pptx", "docx", "xlsx", "md"]
LanguageChoice = Literal["auto", "en", "de", "zh-CN", "zh-TW"]

# English is the default rather than the author's own language: this is a general tool,
# and a German form in an English instance is worse than an English one in a German
# instance. A user's OpenWebUI locale overrides it when it can be read.
#
# Chinese is keyed by script, not by primary subtag: Simplified and Traditional are two
# written forms, and serving one to a reader of the other is a visible error rather than
# a regional spelling difference.
STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "heading": "{kind} settings",
        "lead": "Choose your settings and submit — the values go back to the chat as a message.",
        "topic": "Topic / content",
        "topic_hint": "What is it about? The more concrete, the better.",
        "template": "Template",
        "no_template": "No template (default design)",
        "audience": "Audience",
        "audience_hint": "e.g. board, engineering team",
        "font": "Font",
        "font_note": "must be installed on the reader's machine",
        "font_inherit": "Take from the template",
        "size": "Font size (pt)",
        "length": "Length",
        "density": "Information density",
        "sparse": "Sparse",
        "normal": "Normal",
        "dense": "Dense",
        "page_size": "Page size",
        "orientation": "Orientation",
        "portrait": "Portrait",
        "landscape": "Landscape",
        "language": "Document language",
        "notes": "Speaker notes",
        "toc": "Table of contents",
        "submit": "Create {kind}",
        "confirm_note": "OpenWebUI will ask once whether this card may send a message.",
        "pptx": "presentation",
        "docx": "document",
        "xlsx": "workbook",
        "md": "Markdown file",
        "unit_md": "sections",
        "verb_md": "Create a Markdown file",
        "frontmatter": "YAML front matter",
        "frontmatter_note": "for Hugo, Jekyll and similar",
        "unit_pptx": "slides",
        "unit_docx": "pages",
        "verb_pptx": "Create a presentation",
        "verb_docx": "Create a document",
        "verb_xlsx": "Create a workbook",
        "about": " about: ",
        "use_options": "Use these options (RenderOptions):",
    },
    "zh-CN": {
        "heading": "{kind}设置",
        "lead": "选择设置并提交 — 这些值将作为消息返回到聊天中。",
        "topic": "主题 / 内容",
        "topic_hint": "内容是关于什么的？越具体越好。",
        "template": "模板",
        "no_template": "不使用模板（默认设计）",
        "audience": "目标受众",
        "audience_hint": "例如：管理层、技术团队",
        "font": "字体",
        "font_note": "必须已安装在阅读者的设备上",
        "font_inherit": "沿用模板中的字体",
        "size": "字号（磅）",
        "length": "篇幅",
        "density": "信息密度",
        "sparse": "稀疏",
        "normal": "适中",
        "dense": "密集",
        "page_size": "页面大小",
        "orientation": "方向",
        "portrait": "纵向",
        "landscape": "横向",
        "language": "文档语言",
        "notes": "演讲者备注",
        "toc": "目录",
        "submit": "创建{kind}",
        "confirm_note": "OpenWebUI 会询问一次是否允许此卡片发送消息。",
        "pptx": "演示文稿",
        "docx": "文档",
        "xlsx": "工作簿",
        "md": "Markdown 文件",
        "unit_md": "个章节",
        "verb_md": "创建一个 Markdown 文件",
        "frontmatter": "YAML 前置数据",
        "frontmatter_note": "用于 Hugo、Jekyll 等",
        "unit_pptx": "张幻灯片",
        "unit_docx": "页",
        "verb_pptx": "创建一个演示文稿",
        "verb_docx": "创建一个文档",
        "verb_xlsx": "创建一个工作簿",
        "about": "，主题：",
        "use_options": "使用以下选项（RenderOptions）：",
    },
    "zh-TW": {
        "heading": "{kind}設定",
        "lead": "選擇設定並送出 — 這些值會以訊息形式回到聊天中。",
        "topic": "主題 / 內容",
        "topic_hint": "內容是關於什麼的？越具體越好。",
        "template": "範本",
        "no_template": "不使用範本（預設設計）",
        "audience": "目標對象",
        "audience_hint": "例如：管理層、技術團隊",
        "font": "字型",
        "font_note": "必須已安裝在閱讀者的裝置上",
        "font_inherit": "沿用範本中的字型",
        "size": "字型大小（點）",
        "length": "篇幅",
        "density": "資訊密度",
        "sparse": "稀疏",
        "normal": "適中",
        "dense": "密集",
        "page_size": "頁面大小",
        "orientation": "方向",
        "portrait": "直向",
        "landscape": "橫向",
        "language": "文件語言",
        "notes": "備忘稿",
        "toc": "目錄",
        "submit": "建立{kind}",
        "confirm_note": "OpenWebUI 會詢問一次是否允許此卡片傳送訊息。",
        "pptx": "簡報",
        "docx": "文件",
        "xlsx": "活頁簿",
        "md": "Markdown 檔案",
        "unit_md": "個章節",
        "verb_md": "建立一個 Markdown 檔案",
        "frontmatter": "YAML 前置資料",
        "frontmatter_note": "用於 Hugo、Jekyll 等",
        "unit_pptx": "張投影片",
        "unit_docx": "頁",
        "verb_pptx": "建立一份簡報",
        "verb_docx": "建立一份文件",
        "verb_xlsx": "建立一個活頁簿",
        "about": "，主題：",
        "use_options": "使用以下選項（RenderOptions）：",
    },
    "de": {
        "heading": "{kind} konfigurieren",
        "lead": "Einstellungen wählen und absenden — die Werte gehen als Nachricht "
        "zurück in den Chat.",
        "topic": "Thema / Inhalt",
        "topic_hint": "Worum geht es? Je konkreter, desto besser.",
        "template": "Vorlage",
        "no_template": "Ohne Vorlage (Standarddesign)",
        "audience": "Zielgruppe",
        "audience_hint": "z.B. Vorstand, Fachteam",
        "font": "Schriftart",
        "font_note": "muss beim Leser installiert sein",
        "font_inherit": "Aus der Vorlage übernehmen",
        "size": "Schriftgröße (pt)",
        "length": "Umfang",
        "density": "Informationsdichte",
        "sparse": "Locker",
        "normal": "Normal",
        "dense": "Dicht",
        "page_size": "Seitenformat",
        "orientation": "Ausrichtung",
        "portrait": "Hochformat",
        "landscape": "Querformat",
        "language": "Dokumentsprache",
        "notes": "Sprechernotizen",
        "toc": "Inhaltsverzeichnis",
        "submit": "{kind} erstellen",
        "confirm_note": "OpenWebUI fragt einmal nach, ob diese Karte eine Nachricht "
        "senden darf.",
        "pptx": "Präsentation",
        "docx": "Dokument",
        "xlsx": "Arbeitsmappe",
        "md": "Markdown-Datei",
        "unit_md": "Abschnitte",
        "verb_md": "Markdown-Datei erstellen",
        "frontmatter": "YAML-Frontmatter",
        "frontmatter_note": "für Hugo, Jekyll und ähnliche",
        "unit_pptx": "Folien",
        "unit_docx": "Seiten",
        "verb_pptx": "Erstelle eine Präsentation",
        "verb_docx": "Erstelle ein Dokument",
        "verb_xlsx": "Erstelle eine Arbeitsmappe",
        "about": " zum Thema: ",
        "use_options": "Nutze diese Optionen (RenderOptions):",
    },
}

_env = Environment(autoescape=select_autoescape(default_for_string=True, default=True))

CONFIG_HTML = """<!DOCTYPE html>
<html lang="{{ lang }}"><head><meta charset="utf-8">
<style>
  /* Light values are the defaults; the dark block below wins when the card is told the
     instance is dark, or when it has to guess from the operating system. */
  :root {
    /* Both, deliberately. Declaring a single scheme is what breaks theme matching:
       CSS Color Adjust says an embedded document whose used colour scheme differs from
       the embedding element gets an *opaque* canvas, so a card pinned to light turns
       into a white rectangle inside a dark chat. Accepting both lets the embedder
       decide, keeps the canvas transparent, and makes prefers-color-scheme below report
       OpenWebUI's own theme rather than the operating system's. */
    color-scheme: light dark;
    --fg: #1a1a1a; --muted: #6b7280; --border: #d1d5db;
    --field-bg: #ffffff; --accent: #4f46e5; --accent-fg: #ffffff;
  }
  {{ dark_css }}
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 18px; color: var(--fg);
    /* Transparent rather than a colour: whatever the chat paints behind the iframe
       shows through, so the card sits on the real background even when the theme could
       not be determined. */
    background: transparent;
    /* CJK families are named explicitly: system-ui resolves to a Latin face on some
       platforms, which drops Chinese text to a fallback with visibly different metrics. */
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", "PingFang SC", "PingFang TC",
          "Microsoft YaHei", "Microsoft JhengHei", "Noto Sans CJK SC", sans-serif;
  }
  h1 { margin: 0 0 4px; font-size: 1.05rem; font-weight: 600; }
  p.lead { margin: 0 0 16px; color: var(--muted); font-size: .85rem; }
  .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; }
  .full { grid-column: 1 / -1; }
  label { display: block; font-size: .78rem; font-weight: 500; margin-bottom: 4px; }
  .hint { font-weight: 400; color: var(--muted); }
  input, select, textarea {
    width: 100%; padding: 7px 9px; border: 1px solid var(--border); border-radius: 7px;
    background: var(--field-bg); color: var(--fg); font: inherit; font-size: .85rem;
  }
  textarea { resize: vertical; min-height: 66px; }
  input:focus, select:focus, textarea:focus {
    outline: 2px solid var(--accent); outline-offset: -1px; border-color: transparent;
  }
  .checks { display: flex; gap: 18px; flex-wrap: wrap; }
  .check { display: flex; align-items: center; gap: 7px; font-size: .85rem; }
  .check input { width: auto; }
  button {
    margin-top: 16px; width: 100%; padding: 10px; border: 0; border-radius: 8px;
    background: var(--accent); color: var(--accent-fg); font: inherit; font-weight: 600;
    cursor: pointer;
  }
  button:hover { filter: brightness(1.08); }
  .note { margin-top: 10px; font-size: .74rem; color: var(--muted); text-align: center; }
</style></head>
<body>
  <h1>{{ t.heading }}</h1>
  <p class="lead">{{ t.lead }}</p>

  <!-- Deliberately plain fields rather than a form element: the sandbox blocks form
       submission unless the user turned on "iframe Sandbox Allow Forms", which is off
       by default, and the button would silently do nothing. -->
  <div class="grid">
    <div class="full">
      <label for="topic">{{ t.topic }}</label>
      <textarea id="topic" placeholder="{{ t.topic_hint }}">{{ prefill_topic }}</textarea>
    </div>

    <div>
      <label for="template_id">{{ t.template }}</label>
      <select id="template_id">
        <option value="">{{ t.no_template }}</option>
        {% for tpl in templates %}
        <option value="{{ tpl.template_id }}">{{ tpl.name }}</option>
        {% endfor %}
      </select>
    </div>

    <div>
      <label for="audience">{{ t.audience }}</label>
      <input id="audience" placeholder="{{ t.audience_hint }}" value="{{ prefill_audience }}">
    </div>

    {% if kind != "md" %}
    <div>
      <label for="font_family">{{ t.font }} <span class="hint">({{ t.font_note }})</span></label>
      <select id="font_family">
        <option value="">{{ t.font_inherit }}</option>
        {% for font in fonts %}
        <option value="{{ font }}"{% if font == default_font %} selected{% endif %}>{{ font }}</option>
        {% endfor %}
      </select>
    </div>
    {% endif %}

    {% if kind != "md" %}
    <div>
      <label for="font_size_base">{{ t.size }}</label>
      <input id="font_size_base" type="number" min="6" max="72" value="{{ default_size }}">
    </div>
    {% endif %}

    {% if kind != "xlsx" %}
    <div>
      <label for="target_length">{{ t.length }} <span class="hint">({{ unit }})</span></label>
      <input id="target_length" type="number" min="1" max="200" placeholder="8">
    </div>

    <div>
      <label for="density">{{ t.density }}</label>
      <select id="density">
        <option value="sparse">{{ t.sparse }}</option>
        <option value="normal" selected>{{ t.normal }}</option>
        <option value="dense">{{ t.dense }}</option>
      </select>
    </div>
    {% endif %}

    {% if kind == "md" %}
    <div class="full">
      <label class="check"><input type="checkbox" id="frontmatter">
        {{ t.frontmatter }} <span class="hint">({{ t.frontmatter_note }})</span></label>
    </div>
    {% endif %}

    {% if kind == "docx" %}
    <div>
      <label for="page_size">{{ t.page_size }}</label>
      <select id="page_size">
        <option value="A4" selected>A4</option>
        <option value="Letter">Letter</option>
      </select>
    </div>

    <div>
      <label for="orientation">{{ t.orientation }}</label>
      <select id="orientation">
        <option value="portrait" selected>{{ t.portrait }}</option>
        <option value="landscape">{{ t.landscape }}</option>
      </select>
    </div>
    {% endif %}

    <div>
      <label for="language">{{ t.language }}</label>
      <select id="language">
        <option value="en"{% if lang == "en" %} selected{% endif %}>English</option>
        <option value="de"{% if lang == "de" %} selected{% endif %}>Deutsch</option>
        <option value="zh-CN"{% if lang == "zh-CN" %} selected{% endif %}>简体中文</option>
        <option value="zh-TW"{% if lang == "zh-TW" %} selected{% endif %}>繁體中文</option>
      </select>
    </div>

    {% if kind == "pptx" %}
    <div class="full checks">
      <label class="check"><input type="checkbox" id="include_notes"> {{ t.notes }}</label>
    </div>
    {% endif %}
    {% if kind in ["docx", "md"] %}
    <div class="full checks">
      <label class="check"><input type="checkbox" id="include_toc"> {{ t.toc }}</label>
    </div>
    {% endif %}
  </div>

  <button type="button" id="submit">{{ t.submit }}</button>
  <p class="note">{{ t.confirm_note }}</p>

<script>
{{ theme_sync }}
  const VERB = {{ verb_json }};
  const ABOUT = {{ about_json }};
  const USE_OPTIONS = {{ use_options_json }};

  function reportHeight() {
    parent.postMessage(
      { type: 'iframe:height', height: document.documentElement.scrollHeight }, '*');
  }
  window.addEventListener('load', reportHeight);
  new ResizeObserver(reportHeight).observe(document.body);

  function value(id) {
    const el = document.getElementById(id);
    if (!el) return null;
    if (el.type === 'checkbox') return el.checked || null;
    const raw = (el.value || '').trim();
    if (!raw) return null;
    return el.type === 'number' ? Number(raw) : raw;
  }

  // No theme code here on purpose. The card follows OpenWebUI through a plain CSS media
  // query, and a media query is live by definition: when the embedder's colour scheme
  // changes, the browser re-evaluates and repaints. Anything written in JavaScript could
  // only listen for that same change and do by hand what the engine already did.

  document.getElementById('submit').addEventListener('click', function () {
    const topic = value('topic');
    const options = {};
    for (const key of ['template_id', 'font_family', 'font_size_base', 'audience',
                       'target_length', 'density', 'language', 'page_size',
                       'orientation', 'include_notes', 'include_toc', 'frontmatter']) {
      const v = value(key);
      if (v !== null) options[key] = v;
    }

    // The options travel as a chat message rather than a direct call, so the model sees
    // them, the chat keeps a record of what was asked for, and nothing has to escape
    // this sandbox.
    const text = VERB + (topic ? ABOUT + topic : '')
      + '\\n\\n' + USE_OPTIONS + '\\n'
      + '```json\\n' + JSON.stringify(options, null, 2) + '\\n```';

    parent.postMessage({ type: 'input:prompt:submit', text: text }, '*');
  });
</script>
</body></html>"""

def render_config_page(
    kind: ConfigKind,
    templates: list[dict[str, Any]],
    *,
    preferences: UserPreferences | None = None,
    language: LanguageChoice = "auto",
    prefill_topic: str = "",
    prefill_audience: str = "",
) -> str:
    preferences = preferences or UserPreferences()
    # An explicit choice first, then whatever OpenWebUI reports, then English. The
    # explicit path carries the weight: OpenWebUI keeps the interface locale client-side
    # like the theme, so the server-side lookup usually returns nothing. The model, on
    # the other hand, is in the conversation and can see which language it is in.
    language = language if language != "auto" else preferences.language
    strings = STRINGS.get(language, STRINGS["en"])
    kind_label = strings[kind]

    return _env.from_string(CONFIG_HTML).render(
        kind=kind,
        lang=language,
        t={key: value.format(kind=kind_label) for key, value in strings.items()},
        unit=strings.get(f"unit_{kind}", ""),
        templates=[item for item in templates if item.get("kind") == kind],
        fonts=font_choices(language),
        default_size=18 if kind == "pptx" else 11,
        default_font="Microsoft YaHei" if language.startswith("zh") else "Calibri",
        prefill_topic=prefill_topic,
        prefill_audience=prefill_audience,
        dark_css=dark_css(),
        theme_sync=PARENT_THEME_SYNC,
        verb_json=js_literal(strings[f"verb_{kind}"]),
        about_json=js_literal(strings["about"]),
        use_options_json=js_literal(strings["use_options"]),
    )


def font_choices(language: str) -> list[str]:
    """Which fonts to offer for a document in this language.

    A Latin-only font on Chinese text renders through glyph substitution, so the document
    silently comes out in a typeface nobody picked. Offering fonts that cover the script
    is cheaper than explaining the result afterwards.
    """
    if language.startswith("zh"):
        return sorted(CJK_FONTS) + sorted(SAFE_FONTS)
    return sorted(SAFE_FONTS)


def js_literal(value: object) -> Markup:
    """Embed a Python value as a JavaScript literal inside a ``<script>`` block.

    Two separate hazards, and the usual reflex handles neither:

    * HTML autoescaping is *wrong* here. The contents of a ``<script>`` element are raw
      text, so an escaped quote arrives as the literal ``&#34;`` and breaks the script
      rather than protecting it. Hence ``Markup``.
    * ``</script>`` inside a string ends the element early no matter where it appears,
      which is the classic way a quoted string turns into executable markup. So the
      characters that could start a tag are escaped as JavaScript unicode sequences,
      which JSON parsers and JS engines both read back as the original characters.
    """
    encoded = json.dumps(value)
    for char, escape in (("<", "\\u003c"), (">", "\\u003e"), ("&", "\\u0026")):
        encoded = encoded.replace(char, escape)
    return Markup(encoded)
