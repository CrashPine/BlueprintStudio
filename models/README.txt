Place trained P&ID weights here (~6–7 MB). Either filename works:

  models/yolov8n.pt       (preferred)
  models/yolov8n_pid.pt   (as downloaded from Kaggle — no rename needed)

1. Kaggle notebook Output → download yolov8n_pid.pt (NOT yolov8n.pt from Output — that is COCO).
2. Drop the file in this folder.
3. Run: python scripts/infer.py data/raw_diagrams/images__train__113.jpg --type PID --out data/parsed.json

Do NOT use COCO pretrained weights — the pipeline will reject them.
