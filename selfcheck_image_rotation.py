"""
SELF-CHECK for image auto-rotation (§5d). Verifies _rotate_image_bytes actually
turns pixels the right way and normalizes EXIF — no DB, no API, no network.

    python selfcheck_image_rotation.py

Builds tiny test images in memory, runs them through the SAME function the pipeline
uses on saved image source docs, and asserts the result. Prints PASS/FAIL per case.
Needs Pillow installed (python -m pip install -r requirements.txt).
"""

import io
import sys

try:
    from PIL import Image
except ImportError:
    sys.exit("Pillow is not installed. Run:  python -m pip install -r requirements.txt")

from classify_emails import _rotate_image_bytes

W, H = 48, 24          # landscape test image
RED, WHITE = (255, 0, 0), (255, 255, 255)


def _make_png(exif_orientation=None):
    """A white landscape image with a RED block in the TOP-LEFT corner."""
    im = Image.new("RGB", (W, H), WHITE)
    for x in range(12):
        for y in range(12):
            im.putpixel((x, y), RED)
    buf = io.BytesIO()
    if exif_orientation is not None:
        exif = im.getexif()
        exif[0x0112] = exif_orientation
        im.save(buf, "JPEG", exif=exif)
    else:
        im.save(buf, "PNG")
    return buf.getvalue()


def _open(data):
    return Image.open(io.BytesIO(data)).convert("RGB")


def _check(name, ok):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    return ok


def main():
    results = []

    # --- direction: red top-left block ends up in the right corner per rotation ---
    # 90 clockwise -> portrait 24x48, red now TOP-RIGHT
    out = _rotate_image_bytes(_make_png(), "image/png", {"1": 90})
    im = _open(out)
    results.append(_check("90 CW: size swaps to 24x48", im.size == (24, 48)))
    results.append(_check("90 CW: red moved to top-right",
                          im.getpixel((18, 6)) == RED and im.getpixel((6, 6)) == WHITE))

    # 180 -> same size 48x24, red now BOTTOM-RIGHT
    out = _rotate_image_bytes(_make_png(), "image/png", {"1": 180})
    im = _open(out)
    results.append(_check("180: size stays 48x24", im.size == (48, 24)))
    results.append(_check("180: red moved to bottom-right",
                          im.getpixel((42, 18)) == RED and im.getpixel((6, 6)) == WHITE))

    # 270 clockwise -> portrait 24x48, red now BOTTOM-LEFT
    out = _rotate_image_bytes(_make_png(), "image/png", {"1": 270})
    im = _open(out)
    results.append(_check("270 CW: size swaps to 24x48", im.size == (24, 48)))
    results.append(_check("270 CW: red moved to bottom-left",
                          im.getpixel((6, 42)) == RED and im.getpixel((6, 6)) == WHITE))

    # --- EXIF normalization: orientation=6 (needs 90 CW) applied even with deg 0 ---
    out = _rotate_image_bytes(_make_png(exif_orientation=6), "image/jpeg", None)
    ok = out is not None
    if ok:
        im = _open(out)
        cleared = im.getexif().get(0x0112) in (None, 1)
        ok = im.size == (24, 48) and cleared
    results.append(_check("EXIF orientation=6 normalized (24x48, tag cleared)", ok))

    # --- JPEG stays JPEG + rotates ---
    jpg = io.BytesIO(); Image.new("RGB", (W, H), WHITE).save(jpg, "JPEG")
    out = _rotate_image_bytes(jpg.getvalue(), "image/jpeg", {"1": 90})
    ok = out is not None and Image.open(io.BytesIO(out)).format == "JPEG" and _open(out).size == (24, 48)
    results.append(_check("JPEG input -> JPEG output, rotated", ok))

    # --- safe fallbacks: nothing to do -> None (keep original bytes) ---
    results.append(_check("deg 0, no EXIF -> None (save as-is)",
                          _rotate_image_bytes(_make_png(), "image/png", {"1": 0}) is None))
    results.append(_check("invalid deg (45) -> None (save as-is)",
                          _rotate_image_bytes(_make_png(), "image/png", {"1": 45}) is None))
    results.append(_check("no page_rotations -> None (save as-is)",
                          _rotate_image_bytes(_make_png(), "image/png", None) is None))

    passed = sum(1 for r in results if r)
    print(f"\n{passed}/{len(results)} checks passed.")
    sys.exit(0 if passed == len(results) else 1)


if __name__ == "__main__":
    main()
