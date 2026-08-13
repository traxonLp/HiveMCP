"""Fonts, colours and other cross-format rendering helpers."""

from __future__ import annotations

import base64
import binascii
import re
from collections.abc import Callable
from io import BytesIO
from typing import Protocol

from pptx.util import Cm

# Fonts that ship with both Microsoft Office and LibreOffice on every mainstream
# platform. Anything outside this set gets a warning attached to the render result,
# because OOXML stores only the font *name*: if the reader's machine lacks it, the
# viewer silently substitutes and the document looks nothing like the preview.
SAFE_FONTS = frozenset(
    {
        "Arial",
        "Calibri",
        "Cambria",
        "Candara",
        "Consolas",
        "Constantia",
        "Corbel",
        "Courier New",
        "Georgia",
        "Segoe UI",
        "Tahoma",
        "Times New Roman",
        "Trebuchet MS",
        "Verdana",
    }
)

# Fonts with CJK coverage that ship with Office or the operating system. Smaller than the
# Latin set because cross-platform CJK coverage genuinely is: Windows and macOS overlap
# barely at all here.
CJK_FONTS = frozenset(
    {
        "Microsoft YaHei",
        "Microsoft JhengHei",
        "SimSun",
        "SimHei",
        "PMingLiU",
        "PingFang SC",
        "PingFang TC",
        "Songti SC",
        "Noto Sans CJK SC",
        "Noto Sans CJK TC",
    }
)

# Languages written in scripts that a Latin-only font cannot cover.
CJK_LANGUAGES = ("zh", "ja", "ko")

DEFAULT_MONO_FONT = "Consolas"

_HEX_COLOR = re.compile(r"^[0-9A-Fa-f]{6}$")


class SupportsImage(Protocol):
    """Structural type for models.ImageRef, kept local so render/ stays import-light."""

    file_id: str | None
    data_base64: str | None
    width_cm: float | None
    height_cm: float | None


ImageResolver = Callable[[str], bytes]
"""Resolves an OpenWebUI file id to raw bytes.

Injected by the caller so the render layer never performs I/O of its own. That keeps
renderers synchronous, unit-testable without a network, and reusable from the CLI.
"""


class RenderWarnings(list):
    """A list of warnings that de-duplicates while preserving order."""

    def add(self, message: str) -> None:
        if message not in self:
            self.append(message)


def check_font(
    font_family: str | None, warnings: RenderWarnings, language: str = "en"
) -> str | None:
    if not font_family:
        return None

    if font_family not in SAFE_FONTS and font_family not in CJK_FONTS:
        warnings.add(
            f"Font {font_family!r} is not in the set of fonts that ship with Office by "
            "default. If it is not installed on the reader's machine, their viewer will "
            "substitute a different one."
        )

    # A Latin-only font on Chinese, Japanese or Korean text still renders, because the
    # viewer substitutes glyph by glyph. It just renders in a second, unchosen typeface,
    # which is the kind of result that looks like a bug in the document rather than in
    # the font choice.
    if language.split("-")[0].lower() in CJK_LANGUAGES and font_family not in CJK_FONTS:
        warnings.add(
            f"Font {font_family!r} has no {language} glyphs, so that text will be shown "
            "in whatever font the reader's viewer substitutes. Consider a font with CJK "
            f"coverage instead: {', '.join(sorted(CJK_FONTS)[:4])}."
        )
    return font_family


def normalize_hex(color: str | None) -> str | None:
    """Accept '#RRGGBB' or 'RRGGBB'; return 'RRGGBB' uppercase, or None if invalid."""
    if not color:
        return None
    candidate = color.lstrip("#").strip()
    if not _HEX_COLOR.match(candidate):
        return None
    return candidate.upper()


def decode_image(image: SupportsImage, resolver: ImageResolver | None) -> BytesIO:
    """Turn an ImageRef into a seekable stream."""
    if image.data_base64:
        try:
            raw = base64.b64decode(image.data_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("data_base64 is not valid base64") from exc
    elif image.file_id:
        if resolver is None:
            raise ValueError(
                "image.file_id was given but this renderer has no file resolver configured"
            )
        raw = resolver(image.file_id)
    else:
        raise ValueError("image has neither file_id nor data_base64")

    if not raw:
        raise ValueError("image resolved to zero bytes")
    return BytesIO(raw)


def image_size(image: SupportsImage) -> dict[str, object]:
    """Build the width/height kwargs for python-pptx / python-docx.

    Passing only one dimension makes both libraries preserve the aspect ratio, which
    is nearly always what the caller wants.
    """
    kwargs: dict[str, object] = {}
    if image.width_cm:
        kwargs["width"] = Cm(image.width_cm)
    if image.height_cm:
        kwargs["height"] = Cm(image.height_cm)
    return kwargs


def safe_filename(name: str | None, fallback: str, extension: str) -> str:
    """Build a filesystem- and Content-Disposition-safe filename.

    Strips path separators and control characters rather than escaping them: a model
    that emits ``../../etc/passwd`` as a title should produce a boring filename, not
    a traversal.
    """
    base = (name or fallback or "document").strip()
    base = re.sub(r"[\x00-\x1f\x7f]", "", base)
    base = re.sub(r"[<>:\"/\\|?*]", "-", base)
    base = base.replace("..", "-").strip(". ")
    base = re.sub(r"\s+", " ", base)[:80].strip()
    if not base:
        base = fallback
    extension = extension.lstrip(".")
    return f"{base}.{extension}"


def estimate_pages(character_count: int, density: str = "normal") -> int:
    """Rough page estimate for a Word document.

    Word decides real pagination at open time based on the installed fonts and the
    printer driver, so no library can know the true count. These constants come from
    A4 with 2.5 cm margins at 11 pt.
    """
    per_page = {"sparse": 1800, "normal": 2800, "dense": 3800}.get(density, 2800)
    return max(1, -(-character_count // per_page))
