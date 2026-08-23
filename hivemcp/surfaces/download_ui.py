"""The download card, rendered inline in the chat.

A tool result is shown as JSON, so a URL in it is text: not clickable, and the warnings
beside it are easy to miss. This turns the result into a card with a real button, and puts
the warnings where the person who asked for the document will actually read them.

The link uses ``target="_blank"`` rather than the ``download`` attribute. Rich UI iframes
carry ``allow-downloads``, but triggering a download from inside a sandboxed frame is
unreliable and on iOS impossible; opening the signed URL in a tab lets the server's
``Content-Disposition: attachment`` do the work instead.

Theme and escaping follow the same rules as the settings card — see ``config_ui``.
"""

from __future__ import annotations

from typing import Any, Literal

from jinja2 import Environment, select_autoescape
from markupsafe import Markup

from ..core.preferences import UserPreferences
from .config_ui import DARK_VARIABLES, dark_css

DownloadKind = Literal["pptx", "docx", "xlsx"]

# Office's own colours, so the card reads at a glance without needing the extension.
KIND_STYLE: dict[str, dict[str, str]] = {
    "pptx": {"colour": "#c43e1c", "label": "PowerPoint", "glyph": "P"},
    "docx": {"colour": "#2b579a", "label": "Word", "glyph": "W"},
    "xlsx": {"colour": "#217346", "label": "Excel", "glyph": "X"},
}

STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "download": "Download",
        "ready": "Ready to download",
        "warnings": "Worth knowing",
        "expires": "The link expires after a while. Ask again to get a fresh one.",
    },
    "de": {
        "download": "Herunterladen",
        "ready": "Bereit zum Download",
        "warnings": "Hinweise",
        "expires": "Der Link läuft nach einer Weile ab. Frag erneut, um einen neuen zu bekommen.",
    },
    "zh-CN": {
        "download": "下载",
        "ready": "已准备好下载",
        "warnings": "注意事项",
        "expires": "链接会在一段时间后失效。再次询问即可获取新链接。",
    },
    "zh-TW": {
        "download": "下載",
        "ready": "已準備好下載",
        "warnings": "注意事項",
        "expires": "連結會在一段時間後失效。再次詢問即可取得新連結。",
    },
}

_env = Environment(autoescape=select_autoescape(default_for_string=True, default=True))

CARD_HTML = """<!DOCTYPE html>
<html lang="{{ lang }}"><head><meta charset="utf-8">
<style>
  :root {
    /* Both schemes, so prefers-color-scheme below reports the embedder's rather than the
       operating system's, and the canvas stays transparent. See config_ui. */
    color-scheme: light dark;
    --fg: #1a1a1a; --muted: #6b7280; --border: #d1d5db;
    --surface: rgba(0,0,0,.03); --warn-bg: rgba(180,120,0,.10); --warn-fg: #8a5a00;
  }
  {{ dark_css }}
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 16px; background: transparent; color: var(--fg);
    font: 14px/1.45 system-ui, -apple-system, "Segoe UI", "PingFang SC",
          "Microsoft YaHei", sans-serif;
  }
  .card {
    display: flex; align-items: center; gap: 14px;
    border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 16px; background: var(--surface);
  }
  .badge {
    flex: none; width: 44px; height: 44px; border-radius: 9px;
    background: {{ colour }}; color: #fff;
    display: flex; align-items: center; justify-content: center;
    font-size: 1.15rem; font-weight: 700; letter-spacing: -.02em;
  }
  .meta { flex: 1; min-width: 0; }
  .name {
    font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  }
  .sub { font-size: .8rem; color: var(--muted); margin-top: 2px; }
  a.button {
    flex: none; display: inline-block; text-decoration: none;
    padding: 9px 18px; border-radius: 8px;
    background: {{ colour }}; color: #fff; font-weight: 600; font-size: .87rem;
  }
  a.button:hover { filter: brightness(1.08); }
  .warnings {
    margin-top: 12px; padding: 10px 12px; border-radius: 9px;
    background: var(--warn-bg); color: var(--warn-fg); font-size: .82rem;
  }
  .warnings strong { display: block; margin-bottom: 4px; }
  .warnings ul { margin: 0; padding-left: 18px; }
  .expiry { margin-top: 10px; font-size: .74rem; color: var(--muted); }
</style></head>
<body>
  <div class="card">
    <div class="badge">{{ glyph }}</div>
    <div class="meta">
      <div class="name" title="{{ filename }}">{{ filename }}</div>
      <div class="sub">{{ label }}{% if size %} · {{ size }}{% endif %}{% if detail %} · {{ detail }}{% endif %}</div>
    </div>
    <a class="button" href="{{ url }}" target="_blank" rel="noopener noreferrer"
       >{{ t.download }}</a>
  </div>

  {% if warnings %}
  <div class="warnings">
    <strong>{{ t.warnings }}</strong>
    <ul>{% for warning in warnings %}<li>{{ warning }}</li>{% endfor %}</ul>
  </div>
  {% endif %}

  <p class="expiry">{{ t.expires }}</p>

<script>
  function reportHeight() {
    parent.postMessage(
      { type: 'iframe:height', height: document.documentElement.scrollHeight }, '*');
  }
  window.addEventListener('load', reportHeight);
  new ResizeObserver(reportHeight).observe(document.body);
</script>
</body></html>"""


def human_size(size_bytes: int | None) -> str:
    if not size_bytes:
        return ""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.0f} KB"
    return f"{size_bytes / 1024 / 1024:.1f} MB"


def render_download_card(
    *,
    url: str,
    filename: str,
    kind: DownloadKind,
    size_bytes: int | None = None,
    detail: str | None = None,
    warnings: list[str] | None = None,
    preferences: UserPreferences | None = None,
    language: str = "auto",
) -> str:
    preferences = preferences or UserPreferences()
    resolved_language = language if language != "auto" else preferences.language
    strings = STRINGS.get(resolved_language, STRINGS["en"])
    style = KIND_STYLE.get(kind, KIND_STYLE["pptx"])

    return _env.from_string(CARD_HTML).render(
        lang=resolved_language,
        t=strings,
        url=url,
        filename=filename,
        label=style["label"],
        colour=Markup(style["colour"]),
        glyph=style["glyph"],
        size=human_size(size_bytes),
        detail=detail,
        warnings=warnings or [],
        dark_css=dark_css(preferences.theme or "auto"),
    )


__all__ = ["DownloadKind", "render_download_card", "human_size", "DARK_VARIABLES"]
