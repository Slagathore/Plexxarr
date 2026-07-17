# =============================================================================
# app_icon.py
# =============================================================================
# The one Sensarr mark, drawn in code so the tray icon, the window/taskbar
# icon, and the generated .ico can never drift apart: deep indigo rounded
# square with a white double chevron ">>" — the "arr" motion. Nothing in it
# borrows from anyone's brand; the pre-rename mark used Plex's amber and
# play triangle, and both went out with the name.
# Run this file directly to regenerate assets/sensarr.ico for the EXE build.
# =============================================================================

from PIL import Image, ImageDraw

_BG = (91, 79, 207, 255)       # deep indigo
_FG = (245, 246, 250, 255)     # near-white marks


def icon_image(size: int = 64) -> Image.Image:
    """Draw the mark at any square size (crisp at 16px and 256px)."""
    s = size / 64.0  # design on a 64-grid, scale everything
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((2 * s, 2 * s, 62 * s, 62 * s),
                        radius=14 * s, fill=_BG)
    # the ">>": two chevrons, centered, carrying the motion
    for x0 in (15, 33):
        d.polygon([
            (x0 * s, 16 * s), (x0 * s + 9 * s, 32 * s), (x0 * s, 48 * s),
            (x0 * s + 5 * s, 48 * s), (x0 * s + 14 * s, 32 * s),
            (x0 * s + 5 * s, 16 * s),
        ], fill=_FG)
    return img


def write_ico(dest: str = "assets/sensarr.ico") -> str:
    from pathlib import Path
    path = Path(__file__).parent / dest
    path.parent.mkdir(parents=True, exist_ok=True)
    base = icon_image(256)
    base.save(path, format="ICO",
              sizes=[(16, 16), (24, 24), (32, 32), (48, 48),
                     (64, 64), (128, 128), (256, 256)])
    return str(path)


if __name__ == "__main__":
    print("wrote", write_ico())
