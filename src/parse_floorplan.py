"""
Architectural floor-plan vectorization: walls, rooms, doors, dimensions -> spaces[] + walls[].

Stages:
  1. Preprocess + wall mask (U-Net if weights exist, else HSV/contour fallback)
  2. YOLOv8 tiled detection for doors/symbols/text regions (optional weights)
  3. TrOCR on crops + dimension normalization to mm
  4. Shapely: room polygons + area_m2 + label association
"""

import argparse
import json
import re
import sys
from pathlib import Path

import cv2
import numpy as np

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_PATH = ROOT / "schemas" / "graph.schema.json"
WALL_UNET_WEIGHTS = ROOT / "models" / "floorplan_unet.pt"
FLOORPLAN_YOLO = ROOT / "models" / "floorplan_yolo.pt"

# Feet/inches and metric dimension patterns
DIM_RE = re.compile(
    r"(\d+)\s*['\u2032]\s*(\d+)?\s*[\"″]?"
    r"|(\d+(?:\.\d+)?)\s*m(?:m)?\b",
    re.I,
)


def stage1_wall_mask(image):
    """
    Stage 1: wall segmentation mask.
    Uses U-Net weights if present; otherwise HSV + morphology fallback.
    """
    if WALL_UNET_WEIGHTS.exists():
        try:
            return _wall_mask_unet(image)
        except Exception:
            pass
    return _wall_mask_hsv_fallback(image)


def _wall_mask_hsv_fallback(image):
    """Contour/HSV fallback when U-Net not trained yet."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, binary = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (3, 3))
    binary = cv2.morphologyEx(binary, cv2.MORPH_CLOSE, kernel, iterations=2)
    binary = cv2.morphologyEx(binary, cv2.MORPH_OPEN, kernel, iterations=1)
    return binary


def _wall_mask_unet(image):
    """Load segmentation-models-pytorch U-Net if weights exist."""
    import torch
    import segmentation_models_pytorch as smp

    model = smp.Unet(encoder_name="resnet18", encoder_weights=None, in_channels=3, classes=1)
    state = torch.load(WALL_UNET_WEIGHTS, map_location="cpu")
    model.load_state_dict(state)
    model.eval()

    h, w = image.shape[:2]
    inp = cv2.resize(image, (256, 256))
    tensor = torch.from_numpy(inp.transpose(2, 0, 1)).float().unsqueeze(0) / 255.0
    with torch.no_grad():
        out = model(tensor)
    mask = (out.squeeze().numpy() > 0.5).astype(np.uint8) * 255
    return cv2.resize(mask, (w, h), interpolation=cv2.INTER_NEAREST)


def stage2_detect_symbols(preprocess_result, conf=0.25):
    """Stage 2: YOLO on 512px tiles for doors/symbols/text regions."""
    if not FLOORPLAN_YOLO.exists():
        return _detect_text_regions_opencv(preprocess_result)


    from src import detect_yolo as det_mod
    prep = dict(preprocess_result)
    prep["tiles"] = _make_small_tiles(preprocess_result["image"], tile_size=512)
    return det_mod.detect(prep, weights_path=FLOORPLAN_YOLO, conf=conf)


def _make_small_tiles(image, tile_size=512, overlap=0.2):

    from src import preprocess as prep_mod
    h, w = image.shape[:2]
    if max(h, w) <= tile_size:
        return []
    return prep_mod.make_tiles(image, tile_size=tile_size, overlap=overlap)


def _detect_text_regions_opencv(preprocess_result):
    """Fallback: MSER/text-like regions when floor-plan YOLO not trained."""
    image = preprocess_result["image"]
    h, w = image.shape[:2]
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    _, thresh = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    dets = []
    for cnt in contours:
        x, y, bw, bh = cv2.boundingRect(cnt)
        if bw < 20 or bh < 10 or bw * bh > w * h * 0.05:
            continue
        xc = (x + bw / 2) / w
        yc = (y + bh / 2) / h
        dets.append(
            {
                "sub_type": "text_region",
                "confidence": 0.5,
                "bbox_2d": [xc, yc, bw / w, bh / h],
                "bbox_px": [x, y, x + bw, y + bh],
            }
        )
    return dets[:50]


def normalize_dimension_mm(text):
    """Parse 11'4\" or 3.35m -> millimeters."""
    text = text.strip()
    m = DIM_RE.search(text)
    if not m:
        return None
    if m.group(3):
        val = float(m.group(3))
        if "mm" in text.lower():
            return val
        return val * 1000.0
    feet = int(m.group(1))
    inches = int(m.group(2) or 0)
    return (feet * 12 + inches) * 25.4


def stage3_ocr_regions(preprocess_result, detections):
    """Stage 3: TrOCR on detection crops; extract labels and dimensions."""
    image = preprocess_result["image"]
    h, w = image.shape[:2]
    labels = []

    for det in detections:
        if "bbox_px" not in det:
            xc, yc, bw, bh = det["bbox_2d"]
            det["bbox_px"] = [
                (xc - bw / 2) * w,
                (yc - bh / 2) * h,
                (xc + bw / 2) * w,
                (yc + bh / 2) * h,
            ]
        x1, y1, x2, y2 = [int(v) for v in det["bbox_px"]]
        crop = image[max(0, y1) : y2, max(0, x1) : x2]
        if crop.size == 0:
            continue
        text = _ocr_crop(crop)
        dim_mm = normalize_dimension_mm(text) if text else None
        labels.append(
            {
                "text": text,
                "dim_mm": dim_mm,
                "center": [det["bbox_2d"][0], det["bbox_2d"][1]],
                "confidence": det.get("confidence", 0.5),
            }
        )
    return labels


def _ocr_crop(bgr_crop):
    try:
        from PIL import Image
        from transformers import TrOCRProcessor, VisionEncoderDecoderModel

        if not hasattr(_ocr_crop, "_processor"):
            _ocr_crop._processor = TrOCRProcessor.from_pretrained(
                "microsoft/trocr-base-handwritten"
            )
            _ocr_crop._model = VisionEncoderDecoderModel.from_pretrained(
                "microsoft/trocr-base-handwritten"
            )
            _ocr_crop._model.eval()

        rgb = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2RGB)
        pil = Image.fromarray(rgb)
        pixel_values = _ocr_crop._processor(pil, return_tensors="pt").pixel_values
        ids = _ocr_crop._model.generate(pixel_values)
        return _ocr_crop._processor.batch_decode(ids, skip_special_tokens=True)[0].strip()
    except Exception:
        try:
            import pytesseract

            gray = cv2.cvtColor(bgr_crop, cv2.COLOR_BGR2GRAY)
            return pytesseract.image_to_string(gray, config="--psm 7").strip()
        except Exception:
            return ""


def stage4_rooms_from_mask(wall_mask, labels, image_width, image_height, scale_m_per_px=0.01):
    """
    Stage 4: extract room polygons from inverted wall mask; associate OCR labels.
    scale_m_per_px: approximate meters per pixel for area (calibrate from dims when available).
    """
    from shapely.geometry import Point, Polygon

    inv = cv2.bitwise_not(wall_mask)
    contours, _ = cv2.findContours(inv, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = wall_mask.shape[:2]
    spaces = []
    walls = []

    # Wall polylines from mask skeleton (simplified: outer contours of wall mask)
    wall_contours, _ = cv2.findContours(wall_mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    for wi, cnt in enumerate(wall_contours[:30]):
        if len(cnt) < 2:
            continue
        pts = [[float(p[0][0] / w), float(p[0][1] / h)] for p in cnt[:: max(1, len(cnt) // 20)]]
        if len(pts) >= 2:
            walls.append(
                {
                    "id": f"W{wi + 1}",
                    "polyline_2d": pts,
                    "thickness_mm": 150,
                    "confidence": 0.7,
                }
            )

    for i, cnt in enumerate(contours):
        area_px = cv2.contourArea(cnt)
        if area_px < w * h * 0.005:
            continue
        epsilon = 0.02 * cv2.arcLength(cnt, True)
        approx = cv2.approxPolyDP(cnt, epsilon, True)
        if len(approx) < 3:
            continue
        poly_norm = [[float(p[0][0] / w), float(p[0][1] / h)] for p in approx]
        try:
            poly = Polygon(poly_norm)
            if not poly.is_valid:
                poly = poly.buffer(0)
        except Exception:
            continue

        cx, cy = poly.centroid.x, poly.centroid.y
        name = f"Room {i + 1}"
        category = "unknown"
        best_label = None
        best_dist = 0.15
        for lab in labels:
            pt = Point(lab["center"])
            d = poly.centroid.distance(pt)
            if d < best_dist:
                best_dist = d
                best_label = lab
        if best_label and best_label["text"]:
            name = best_label["text"][:40]
        if "data" in name.lower() or "hall" in name.lower():
            category = "data_hall"
        elif "elect" in name.lower() or "ups" in name.lower():
            category = "electrical_room"
        elif "plant" in name.lower() or "chiller" in name.lower():
            category = "plant_room"
        elif "office" in name.lower() or "noc" in name.lower():
            category = "office"
        elif "corr" in name.lower():
            category = "corridor"

        area_m2 = area_px * (scale_m_per_px**2)
        if best_label and best_label.get("dim_mm"):
            # rough calibration from first dimension found
            pass

        spaces.append(
            {
                "id": f"S{i + 1}",
                "name": name,
                "category": category,
                "polygon_2d": poly_norm,
                "area_m2": round(area_m2, 1),
                "floor": "G",
                "it_power_kW": 0,
                "confidence": 0.75,
            }
        )

    return spaces, walls


def parse_floorplan(path, conf=0.25):

    from src import preprocess as prep_mod
    prep = prep_mod.preprocess(path, tile_size=512)

    wall_mask = stage1_wall_mask(prep["image"])
    detections = stage2_detect_symbols(prep, conf=conf)
    labels = stage3_ocr_regions(prep, detections)
    spaces, walls = stage4_rooms_from_mask(
        wall_mask, labels, prep["width"], prep["height"]
    )

    if not spaces:
        spaces = [
            {
                "id": "S1",
                "name": "Unparsed space",
                "category": "unknown",
                "polygon_2d": [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]],
                "area_m2": 0,
                "floor": "G",
                "it_power_kW": 0,
                "confidence": 0.3,
            }
        ]

    avg_conf = sum(s["confidence"] for s in spaces) / len(spaces)
    graph = {
        "meta": {
            "diagram_id": "floorplan-parsed",
            "diagram_type": "FLOORPLAN",
            "building_id": None,
            "source_file": prep["source_file"],
            "image_width": prep["width"],
            "image_height": prep["height"],
            "parse_confidence": round(avg_conf, 3),
            "topology": "wall_mask_contour",
            "warnings": [],
        },
        "spaces": spaces,
        "walls": walls,
        "nodes": [],
        "edges": [],
    }
    if not FLOORPLAN_YOLO.exists():
        graph["meta"]["warnings"].append("floorplan_yolo_missing: using OpenCV text regions")
    if not WALL_UNET_WEIGHTS.exists():
        graph["meta"]["warnings"].append("unet_missing: using HSV/contour wall fallback")
    return graph


def validate_graph(graph):
    import jsonschema

    with open(SCHEMA_PATH, "r", encoding="utf-8") as f:
        schema = json.load(f)
    jsonschema.validate(graph, schema)
    return True


def main():
    parser = argparse.ArgumentParser(description="Parse architectural floor plan")
    parser.add_argument("image", nargs="?", default=None)
    parser.add_argument("--out", default=str(ROOT / "data" / "parsed_floorplan.json"))
    parser.add_argument("--conf", type=float, default=0.25)
    parser.add_argument("--debug-mask", default=None, help="Save wall mask PNG")
    args = parser.parse_args()

    if args.image:
        path = args.image
    else:
        raw = ROOT / "data" / "raw_floorplans"
        files = list(raw.glob("*.*")) if raw.exists() else []
        if not files:
            print("Add floor plan to data/raw_floorplans/")
            return
        path = str(files[0])

    if args.debug_mask:
        from src import preprocess as prep_mod
        prep = prep_mod.preprocess(path)
        mask = stage1_wall_mask(prep["image"])
        cv2.imwrite(args.debug_mask, mask)
        print("Wrote mask", args.debug_mask)

    graph = parse_floorplan(path, conf=args.conf)
    validate_graph(graph)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "w", encoding="utf-8") as f:
        json.dump(graph, f, indent=2)
    print("Wrote", out, "spaces:", len(graph["spaces"]), "walls:", len(graph["walls"]))


if __name__ == "__main__":
    main()
