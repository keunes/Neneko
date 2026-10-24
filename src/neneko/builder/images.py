"""Image processing for e-ink: greyscale, downscaling and ordered dithering.

The dither is a port of Calibre's ``eink_dither_image`` (src/calibre/utils/img.py),
which calls ``ordered_dither`` in src/calibre/utils/imageops/ordered_dither.cpp
(calibre master, October 2026). Calibre's routine is in turn ImageMagick's
``-ordered-dither o8x8,16``:

    Copyright 1999-2019 ImageMagick Studio LLC, licensed under the ImageMagick
    License (https://imagemagick.org/script/license.php).

It reduces an image to the 16 grey levels of an e-ink screen using a fixed 8x8
threshold map. The output is pixel-identical to Calibre's (see tests/test_images.py).
"""

import io
import logging

import numpy as np
from PIL import Image, ImageOps

log = logging.getLogger(__name__)

# ImageMagick config/thresholds.xml, map "o8x8"
_THRESHOLD_O8X8 = np.array(
    [
        1, 49, 13, 61, 4, 52, 16, 64,
        33, 17, 45, 29, 36, 20, 48, 32,
        9, 57, 5, 53, 12, 60, 8, 56,
        41, 25, 37, 21, 44, 28, 40, 24,
        3, 51, 15, 63, 2, 50, 14, 62,
        35, 19, 47, 31, 34, 18, 46, 30,
        11, 59, 7, 55, 10, 58, 6, 54,
        43, 27, 39, 23, 42, 26, 38, 22,
    ],
    dtype=np.uint32,
).reshape(8, 8)

EINK_PALETTE = [level * 17 for level in range(16)]


def _gray_array(image: Image.Image) -> np.ndarray:
    """Qt's qGray, as Calibre uses it: (11 R + 16 G + 5 B) / 32, integer maths."""
    rgb = np.asarray(image.convert("RGB"), dtype=np.uint32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    if np.array_equal(r, g) and np.array_equal(g, b):
        return r
    return (r * 11 + g * 16 + b * 5) >> 5


def eink_dither(image: Image.Image) -> Image.Image:
    """Ordered-dither an image to the 16-level e-ink palette. Returns mode 'L'."""
    image = flatten_alpha(image)
    gray = _gray_array(image)
    # threshold = v * ((levels - 1) * (map divisor - 1) + 1) / 255, with Calibre's DIV255
    scaled = gray * ((15 << 6) + 1) + 128
    t = ((scaled >> 8) + scaled) >> 8
    level = t >> 6
    t = t - (level << 6)
    height, width = gray.shape
    thresholds = np.tile(_THRESHOLD_O8X8, (height // 8 + 1, width // 8 + 1))[:height, :width]
    out = np.minimum((level + (t >= thresholds)) * 17, 255).astype(np.uint8)
    return Image.fromarray(out, mode="L")


def flatten_alpha(image: Image.Image, background: str = "white") -> Image.Image:
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        canvas = Image.new("RGBA", rgba.size, background)
        canvas.alpha_composite(rgba)
        return canvas.convert("RGB")
    return image


def encode_eink_png(image: Image.Image) -> bytes:
    """Save a dithered 'L' image as a 4-bit palette PNG; JPEG would undo the dither."""
    levels = (np.asarray(image, dtype=np.uint8) // 17).astype(np.uint8)
    paletted = Image.frombytes("P", image.size, levels.tobytes())
    paletted.putpalette([value for level in EINK_PALETTE for value in (level, level, level)])
    buffer = io.BytesIO()
    paletted.save(buffer, "PNG", optimize=True, bits=4)
    return buffer.getvalue()


def process_image(data: bytes, *, max_width: int, grayscale: bool, dither: str) -> tuple[bytes, str] | None:
    """Prepare a downloaded image for the e-reader. Returns (bytes, media type), or
    None if the data isn't an image Pillow can read."""
    try:
        image = Image.open(io.BytesIO(data))
        image.seek(0)  # first frame of animations
        image = ImageOps.exif_transpose(image)
        image.load()
    except Exception as exc:  # noqa: BLE001 - any decoding failure means "skip this image"
        log.debug("unreadable image: %r", exc)
        return None
    if image.width < 2 or image.height < 2:
        return None  # tracking pixels
    image = flatten_alpha(image)
    if image.width > max_width:
        height = round(image.height * max_width / image.width)
        image = image.resize((max_width, max(height, 1)), Image.Resampling.LANCZOS)
    if dither == "ordered":
        return encode_eink_png(eink_dither(image)), "image/png"
    image = image.convert("L" if grayscale else "RGB")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=82, optimize=True, progressive=False)
    return buffer.getvalue(), "image/jpeg"
