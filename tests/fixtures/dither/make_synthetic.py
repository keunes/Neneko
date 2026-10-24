"""Generate synthetic.png, the input for the Calibre dither reference.

The reference synthetic.eink.png is made with desktop Calibre:

    calibre-debug -c "from calibre.utils.img import save_cover_data_to as s; \
s(open('synthetic.png','rb').read(), 'synthetic.eink.png', grayscale=True, eink=True, minify_to=(100000,100000))"
"""

from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

WIDTH, HEIGHT = 512, 384


def make() -> Image.Image:
    rgb = np.zeros((HEIGHT, WIDTH, 3), dtype=np.uint8)
    x = np.linspace(0, 255, WIDTH).astype(np.uint8)
    rgb[0:64] = x[None, :, None]                                  # grey ramp
    rgb[64:96, :, 0] = x                                          # red, green and blue ramps
    rgb[96:128, :, 1] = x
    rgb[128:160, :, 2] = x
    hue = np.linspace(0, 1, WIDTH)                                # a rainbow, for colour-to-grey weights
    rgb[160:192] = (np.stack([np.abs(hue * 6 - 3) - 1, 2 - np.abs(hue * 6 - 2), 2 - np.abs(hue * 6 - 4)],
                             axis=1).clip(0, 1) * 255).astype(np.uint8)[None]
    yy, xx = np.mgrid[192:HEIGHT, 0:256]
    radius = np.hypot(xx - 128, yy - 288) / 140                   # radial gradient
    rgb[192:, :256] = (255 * (1 - radius.clip(0, 1)))[..., None].astype(np.uint8)
    rgb[192:, 256:] = 255
    image = Image.fromarray(rgb, "RGB")
    draw = ImageDraw.Draw(image)
    for offset in range(0, 120, 4):                               # fine lines and a checkerboard
        draw.line((272 + offset, 200, 272 + offset, 300), fill=(0, 0, 0), width=1)
    for row in range(8):
        for col in range(8):
            if (row + col) % 2:
                draw.rectangle((400 + col * 12, 200 + row * 12, 411 + col * 12, 211 + row * 12), fill=(90, 90, 90))
    draw.ellipse((280, 310, 360, 380), fill=(200, 60, 40), outline=(20, 20, 120), width=3)
    draw.ellipse((380, 310, 500, 380), fill=(140, 140, 140))
    return image


if __name__ == "__main__":
    make().save(Path(__file__).with_name("synthetic.png"), optimize=True)
