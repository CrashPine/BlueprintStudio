"""
Run YOLO on preprocessed image. Output list of detections (normalized bbox + class).
"""

import argparse
import sys
from pathlib import Path

import cv2

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
MODELS_DIR = ROOT / "models"
DEFAULT_WEIGHTS = MODELS_DIR / "yolov8n.pt"
WEIGHT_CANDIDATES = ("yolov8n.pt", "yolov8n_pid.pt", "best.pt")


def resolve_weights(weights_path=None):
    """Find trained P&ID weights in models/ (yolov8n.pt or yolov8n_pid.pt from Kaggle)."""
    if weights_path is not None:
        p = Path(weights_path)
        if not p.exists():
            raise FileNotFoundError(f"Weights not found: {p}")
        return p
    for name in WEIGHT_CANDIDATES:
        p = MODELS_DIR / name
        if p.exists():
            return p
    raise FileNotFoundError(
        f"No trained weights in {MODELS_DIR}. "
        "Download yolov8n_pid.pt from Kaggle Output and place it there "
        f"(as yolov8n.pt or yolov8n_pid.pt)."
    )

_COCO_MARKERS = frozenset({"person", "bicycle", "car", "boat", "dog"})


def assert_pid_weights(weights_path):
    """Reject COCO pretrained weights (80 classes) — they produce boat/person detections."""
    from ultralytics import YOLO

    model = YOLO(str(weights_path))
    names = set(model.names.values()) if hasattr(model, "names") else set()
    if names & _COCO_MARKERS:
        raise ValueError(
            f"Weights at {weights_path} look like COCO pretrained (classes: "
            f"{sorted(names & _COCO_MARKERS)}). "
            "Train on Kaggle (notebooks/train_yolo_kaggle.ipynb), download "
            "yolov8n_pid.pt, and copy to models/yolov8n.pt."
        )
    if len(names) < 100:
        raise ValueError(
            f"Weights at {weights_path} have only {len(names)} classes; "
            "expected ~203 P&ID symbol classes from Kaggle training."
        )


def run_yolo(image, weights_path, conf=0.15):
    from ultralytics import YOLO

    model = YOLO(str(weights_path))
    # ultralytics wants RGB or path; we pass BGR numpy — it still works
    results = model.predict(image, conf=conf, verbose=False)
    if not results:
        return []

    r = results[0]
    names = r.names
    h, w = image.shape[:2]
    detections = []

    if r.boxes is None or len(r.boxes) == 0:
        return detections

    for box in r.boxes:
        cls_id = int(box.cls[0])
        score = float(box.conf[0])
        xyxy = box.xyxy[0].tolist()
        x1, y1, x2, y2 = xyxy

        xc = ((x1 + x2) / 2) / w
        yc = ((y1 + y2) / 2) / h
        bw = (x2 - x1) / w
        bh = (y2 - y1) / h

        sub_type = names.get(cls_id, "unknown")
        if isinstance(sub_type, int):
            sub_type = str(sub_type)

        detections.append(
            {
                "sub_type": sub_type,
                "confidence": score,
                "bbox_2d": [xc, yc, bw, bh],
                "bbox_px": [x1, y1, x2, y2],
            }
        )

    return detections


def detect_on_tiles(preprocess_result, weights_path, conf=0.25):
    """Run on full image + tiles, merge by simple NMS on pixel boxes."""
    all_dets = []

    full = run_yolo(preprocess_result["image"], weights_path, conf=conf)
    for d in full:
        d["tile"] = None
        all_dets.append(d)

    for tile in preprocess_result.get("tiles", []):
        tile_dets = run_yolo(tile["image"], weights_path, conf=conf)
        tw = tile["width"]
        th = tile["height"]
        ox = tile["x0"]
        oy = tile["y0"]
        img_w = preprocess_result["width"]
        img_h = preprocess_result["height"]

        for d in tile_dets:
            x1, y1, x2, y2 = d["bbox_px"]
            x1 = x1 + ox
            x2 = x2 + ox
            y1 = y1 + oy
            y2 = y2 + oy
            xc = ((x1 + x2) / 2) / img_w
            yc = ((y1 + y2) / 2) / img_h
            bw = (x2 - x1) / img_w
            bh = (y2 - y1) / img_h
            all_dets.append(
                {
                    "sub_type": d["sub_type"],
                    "confidence": d["confidence"],
                    "bbox_2d": [xc, yc, bw, bh],
                    "bbox_px": [x1, y1, x2, y2],
                    "tile": True,
                }
            )

    return merge_overlapping(all_dets)


def iou(box_a, box_b):
    ax1, ay1, ax2, ay2 = box_a
    bx1, by1, bx2, by2 = box_b
    ix1 = max(ax1, bx1)
    iy1 = max(ay1, by1)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)
    iw = max(0, ix2 - ix1)
    ih = max(0, iy2 - iy1)
    inter = iw * ih
    area_a = (ax2 - ax1) * (ay2 - ay1)
    area_b = (bx2 - bx1) * (by2 - by1)
    union = area_a + area_b - inter
    if union <= 0:
        return 0
    return inter / union


def merge_overlapping(detections, iou_thresh=0.5):
    if not detections:
        return []

    detections = sorted(detections, key=lambda d: d["confidence"], reverse=True)
    kept = []

    for det in detections:
        skip = False
        for k in kept:
            if det["sub_type"] == k["sub_type"] and iou(det["bbox_px"], k["bbox_px"]) > iou_thresh:
                skip = True
                break
        if not skip:
            kept.append(det)

    return kept


def detect(preprocess_result, weights_path=None, conf=0.15):
    weights_path = resolve_weights(weights_path)
    assert_pid_weights(weights_path)

    if preprocess_result.get("tiles"):
        return detect_on_tiles(preprocess_result, weights_path, conf=conf)
    return run_yolo(preprocess_result["image"], weights_path, conf=conf)


def draw_debug(image, detections, out_path):
    img = image.copy()
    h, w = img.shape[:2]
    for d in detections:
        x1, y1, x2, y2 = d["bbox_px"]
        x1, y1, x2, y2 = int(x1), int(y1), int(x2), int(y2)
        cv2.rectangle(img, (x1, y1), (x2, y2), (0, 255, 0), 2)
        label = d["sub_type"][:30]
        cv2.putText(img, label, (x1, max(0, y1 - 5)), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 255, 0), 1)
    cv2.imwrite(str(out_path), img)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("image", nargs="?", default=None)
    parser.add_argument("--weights", default=str(DEFAULT_WEIGHTS))
    parser.add_argument("--out", default=str(ROOT / "data" / "debug_detect.png"))
    args = parser.parse_args()


    from src import preprocess as prep_mod
    if args.image:
        path = args.image
    else:
        files = list(RAW_DIR.glob("*.*"))
        if not files:
            print("Put a diagram in data/raw_diagrams/")
            return
        path = str(files[0])

    prep = prep_mod.preprocess(path)
    dets = detect(prep, weights_path=args.weights)
    print("Detections:", len(dets))
    for d in dets[:10]:
        print(" ", d["sub_type"], round(d["confidence"], 2), d["bbox_2d"])
    draw_debug(prep["image"], dets, args.out)
    print("Debug image:", args.out)


if __name__ == "__main__":
    main()
