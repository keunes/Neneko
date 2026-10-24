"""Generated covers: feed name, title and date, with the lead image when there is one."""

import io
import logging
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageOps

from .images import eink_dither, encode_eink_png, flatten_alpha

log = logging.getLogger(__name__)

FONT_DIRS = [Path("/usr/share/fonts/truetype/dejavu"), Path("/usr/share/fonts/dejavu")]
TITLE_FONT = "DejaVuSerif-Bold.ttf"
LABEL_FONT = "DejaVuSans.ttf"


@lru_cache(maxsize=64)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for directory in FONT_DIRS:
        path = directory / name
        if path.exists():
            return ImageFont.truetype(str(path), size)
    return ImageFont.load_default(size)


def _wrap(draw: ImageDraw.ImageDraw, text: str, font, width: int, max_lines: int) -> list[str]:
    words, lines, current = text.split(), [], ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        while lines[-1] and draw.textlength(lines[-1] + "…", font=font) > width:
            lines[-1] = lines[-1].rsplit(" ", 1)[0] if " " in lines[-1] else lines[-1][:-1]
        lines[-1] += "…"
    return lines


def render_cover(width: int, height: int, *, title: str, feed: str, published: int | None,
                 lead_image: bytes | None, dither: str) -> tuple[bytes, str]:
    """Return (image bytes, media type) for a width x height cover."""
    width, height = max(60, min(width, 2000)), max(80, min(height, 2700))
    canvas = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(canvas)
    margin = round(width * 0.07)
    inner = width - 2 * margin

    label_font = _font(LABEL_FONT, max(10, round(width * 0.045)))
    y = margin
    for line in _wrap(draw, feed.upper(), label_font, inner, 2):
        draw.text((margin, y), line, font=label_font, fill=60)
        y += round(label_font.size * 1.25)
    y += round(width * 0.02)
    draw.line((margin, y, width - margin, y), fill=0, width=max(1, round(width * 0.006)))
    y += round(width * 0.05)

    title_size = round(width * (0.085 if len(title) < 60 else 0.07))
    title_font = _font(TITLE_FONT, max(12, title_size))
    title_lines = _wrap(draw, title, title_font, inner, 6)
    for line in title_lines:
        draw.text((margin, y), line, font=title_font, fill=0)
        y += round(title_font.size * 1.2)

    footer_font = _font(LABEL_FONT, max(9, round(width * 0.038)))
    footer_y = height - margin - footer_font.size
    if published:
        stamp = datetime.fromtimestamp(published, tz=timezone.utc).strftime("%d %b %Y")
        draw.text((margin, footer_y), stamp, font=footer_font, fill=60)

    image_top, image_bottom = y + round(width * 0.04), footer_y - round(width * 0.05)
    if lead_image and image_bottom - image_top > height * 0.15:
        try:
            picture = flatten_alpha(Image.open(io.BytesIO(lead_image)))
            picture = ImageOps.exif_transpose(picture).convert("L")
            fitted = ImageOps.fit(picture, (inner, image_bottom - image_top), Image.Resampling.LANCZOS)
            canvas.paste(fitted, (margin, image_top))
        except Exception as exc:  # noqa: BLE001 - a broken image just means no picture
            log.debug("cover image unusable: %r", exc)

    if dither == "ordered":
        return encode_eink_png(eink_dither(canvas)), "image/png"
    buffer = io.BytesIO()
    canvas.save(buffer, "JPEG", quality=88)
    return buffer.getvalue(), "image/jpeg"
