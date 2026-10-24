import io
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from neneko.builder.cover import render_cover
from neneko.builder.images import EINK_PALETTE, eink_dither, encode_eink_png, process_image

DITHER = Path(__file__).parent / "fixtures" / "dither"


@pytest.mark.skipif(not (DITHER / "synthetic.eink.png").exists(), reason="Calibre reference not added yet")
def test_dither_matches_calibre_pixel_for_pixel():
    """synthetic.eink.png is Calibre's save_cover_data_to(..., grayscale=True, eink=True) output for
    synthetic.png (see make_synthetic.py)."""
    expected = np.asarray(Image.open(DITHER / "synthetic.eink.png").convert("L"))
    actual = np.asarray(eink_dither(Image.open(DITHER / "synthetic.png")))
    assert actual.shape == expected.shape
    assert np.array_equal(actual, expected)


def test_eink_png_keeps_the_16_levels():
    gradient = Image.fromarray(np.tile(np.arange(256, dtype=np.uint8), (64, 1)), mode="L")
    dithered = eink_dither(gradient)
    assert set(np.unique(np.asarray(dithered))) <= set(EINK_PALETTE)
    decoded = Image.open(io.BytesIO(encode_eink_png(dithered))).convert("L")
    assert np.array_equal(np.asarray(decoded), np.asarray(dithered))


def _jpeg(width: int, height: int, mode: str = "RGB") -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, (width, height), (200, 30, 30) if mode == "RGB" else 128).save(buffer, "JPEG")
    return buffer.getvalue()


def test_process_image_downscales_and_greys():
    data, media_type = process_image(_jpeg(3000, 1500), max_width=1264, grayscale=True, dither="off")
    image = Image.open(io.BytesIO(data))
    assert media_type == "image/jpeg"
    assert image.size == (1264, 632)
    assert image.mode == "L"


def test_process_image_dithered_png_and_junk():
    data, media_type = process_image(_jpeg(100, 50), max_width=1264, grayscale=True, dither="ordered")
    assert media_type == "image/png"
    assert process_image(b"not an image", max_width=1264, grayscale=True, dither="off") is None
    assert process_image(_jpeg(1, 1), max_width=1264, grayscale=True, dither="off") is None  # tracking pixel


@pytest.mark.parametrize("dither", ["ordered", "off"])
def test_render_cover(dither):
    data, media_type = render_cover(355, 530, title="A rather long title " * 6, feed="Example Tech Daily",
                                    published=1792794971, lead_image=_jpeg(800, 400), dither=dither)
    image = Image.open(io.BytesIO(data))
    assert image.size == (355, 530)
    assert media_type == ("image/png" if dither == "ordered" else "image/jpeg")
