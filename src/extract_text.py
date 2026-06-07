"""
OCR text near each detection box. Fills tag + simple specs on each detection.
Needs Tesseract installed: https://github.com/tesseract-ocr/tesseract
"""

import argparse
import re
import shutil
import sys
from pathlib import Path

import cv2
import pytesseract

ROOT = Path(__file__).resolve().parent.parent


def _configure_tesseract():
    if shutil.which("tesseract"):
        return
    for candidate in (
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ):
        if candidate.is_file():
            pytesseract.pytesseract.tesseract_cmd = str(candidate)
            return


_configure_tesseract()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Common tag patterns on P&IDs
TAG_RE = re.compile(r"\b([A-Z]{1,4}[-/]?\d{1,4}[A-Z]?)\b")
KW_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kW", re.I)
COP_RE = re.compile(r"COP\s*[:=]?\s*(\d+(?:\.\d+)?)", re.I)


def crop_box(image, bbox_px, pad=0.15):
    h, w = image.shape[:2]
    x1, y1, x2, y2 = bbox_px
    bw = x2 - x1
    bh = y2 - y1
    x1 = max(0, x1 - pad * bw)
    y1 = max(0, y1 - pad * bh)
    x2 = min(w, x2 + pad * bw)
    y2 = min(h, y2 + pad * bh)
    x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
    if x2 <= x1 or y2 <= y1:
        return None
    return image[y1:y2, x1:x2]


def ocr_region(bgr_crop):
    if bgr_crop is None or bgr_crop.size == 0:
        return ""
    gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY)
    gray = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]
    text = pytesseract.image_to_string(gray, config="--psm 6")
    return text.strip()


def parse_specs(text):
    attrs = {}
    m = KW_RE.search(text)
    if m:
        attrs["rated_power_kW"] = float(m.group(1))
    m = COP_RE.search(text)
    if m:
        attrs["efficiency"] = {"metric": "COP", "value": float(m.group(1))}
    return attrs


def find_tag(text):
    tags = TAG_RE.findall(text)
    if tags:
        return tags[0]
    return ""


def extract_for_detections(preprocess_result, detections):
    image = preprocess_result["image"]
    h, w = image.shape[:2]

    for det in detections:
        if "bbox_px" not in det:
            # rebuild pixel box from normalized
            xc, yc, bw, bh = det["bbox_2d"]
            x1 = (xc - bw / 2) * w
            y1 = (yc - bh / 2) * h
            x2 = (xc + bw / 2) * w
            y2 = (yc + bh / 2) * h
            det["bbox_px"] = [x1, y1, x2, y2]

        crop = crop_box(image, det["bbox_px"], pad=0.4)
        try:
            text = ocr_region(crop)
        except Exception as exc:
            text = ""
            det["ocr_error"] = str(exc)
        det["ocr_text"] = text
        det["tag"] = find_tag(text)
        det["parsed_attrs"] = parse_specs(text)

    return detections


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?", default=None)
    args = parser.parse_args()


    from src import preprocess as prep_mod
    from src import detect_yolo as det_mod

    if args.image:
        path = args.image
    else:
        files = list((ROOT / "data" / "raw_diagrams").glob("*.*"))
        path = str(files[0]) if files else None
        if not path:
            print("No input image")
            return

    prep = prep_mod.preprocess(path)
    dets = det_mod.detect(prep)
    dets = extract_for_detections(prep, dets)

    for d in dets[:15]:
        print(d.get("sub_type"), "tag=", d.get("tag"), "text=", (d.get("ocr_text") or "")[:60])


if __name__ == "__main__":
    main()
