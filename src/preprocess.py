"""
Load a diagram image or PDF, resize if huge, boost contrast, optional tiles.
Returns a plain dict you pass to the next step.
"""

import argparse
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
RAW_DIR = ROOT / "data" / "raw_diagrams"


def load_from_pdf(path):
    import fitz

    doc = fitz.open(path)
    page = doc[0]
    pix = page.get_pixmap(dpi=200)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:
        img = cv2.cvtColor(img, cv2.COLOR_RGBA2BGR)
    else:
        img = cv2.cvtColor(img, cv2.COLOR_RGB2BGR)
    return img


def load_image(path, max_edge=2048):
    path = Path(path)
    suffix = path.suffix.lower()

    if suffix == ".pdf":
        img = load_from_pdf(path)
    else:
        img = cv2.imread(str(path))
        if img is None:
            raise FileNotFoundError("Could not read: " + str(path))

    h, w = img.shape[:2]
    long_side = max(h, w)
    if long_side > max_edge:
        scale = max_edge / long_side
        new_w = int(w * scale)
        new_h = int(h * scale)
        img = cv2.resize(img, (new_w, new_h), interpolation=cv2.INTER_AREA)

    return img, path.name


def enhance_contrast(bgr):
    lab = cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)
    l, a, b = cv2.split(lab)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    l = clahe.apply(l)
    merged = cv2.merge([l, a, b])
    return cv2.cvtColor(merged, cv2.COLOR_LAB2BGR)


def make_tiles(image, tile_size=1280, overlap=0.2):
    h, w = image.shape[:2]
    if max(h, w) <= tile_size:
        return []

    step = int(tile_size * (1 - overlap))
    tiles = []
    for y0 in range(0, h, step):
        for x0 in range(0, w, step):
            y1 = min(y0 + tile_size, h)
            x1 = min(x0 + tile_size, w)
            if (y1 - y0) < tile_size // 4 or (x1 - x0) < tile_size // 4:
                continue
            crop = image[y0:y1, x0:x1].copy()
            tiles.append(
                {
                    "image": crop,
                    "x0": x0,
                    "y0": y0,
                    "width": x1 - x0,
                    "height": y1 - y0,
                }
            )
    return tiles


def preprocess(path, max_edge=2048, tile_size=1280, use_tiles=None):
    image, name = load_image(path, max_edge=max_edge)
    image = enhance_contrast(image)
    h, w = image.shape[:2]

    if use_tiles is None:
        use_tiles = max(h, w) > tile_size

    tiles = []
    if use_tiles:
        tiles = make_tiles(image, tile_size=tile_size)

    return {
        "image": image,
        "width": w,
        "height": h,
        "source_file": name,
        "tiles": tiles,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("input", nargs="?", default=str(RAW_DIR))
    parser.add_argument("--out", default=None, help="Save enhanced PNG")
    args = parser.parse_args()

    inp = Path(args.input)
    if inp.is_dir():
        files = []
        for ext in (".png", ".jpg", ".jpeg", ".pdf", ".tif", ".tiff"):
            files.extend(inp.glob("*" + ext))
        files = sorted(files)
        if not files:
            print("No images in", inp)
            return
        inp = files[0]

    result = preprocess(inp)
    print("OK:", result["source_file"], result["width"], "x", result["height"], "tiles:", len(result["tiles"]))

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        cv2.imwrite(str(out), result["image"])
        print("Wrote", out)


if __name__ == "__main__":
    main()
