# FlowDraft / ArchDraft — Repository Guide

This document explains **what every part of the repo does**, **how modules connect**, and **which files are safe to delete** vs which you must keep.

---

## 1. What this project does

**ArchDraft × FlowDraft** turns building diagrams into schema-valid JSON graphs for:

| Input | Parser | Output schema |
|-------|--------|---------------|
| Architectural floor plans | Roboflow rooms + Claude labels | `schemas/graph.schema.json` |
| P&ID / SLD (MEP) | Local YOLO + Claude set-of-marks | `schemas/graph.schema.json` |
| Datacenter layouts | OpenCV regions + Claude labels | `schemas/dc_*.schema.json` |
| Cooling P&ID | Same as datacenter (cooling style) | `schemas/cooling_pid.schema.json` |

Downstream services consume the graph:

- **PUE / BEC** — energy compliance (`src/pue.py`, `config/bec_rules.yaml`)
- **Electrical loads** — breaker headroom (`src/loads.py`)
- **HK property finance** — valuation from floor area (`src/finance.py`)
- **TIA-942 compliance** — rule extraction + geometry validation (`src/compliance_checker/`)
- **3D twin / viewer** — spec in `docs/VIEWER_SPEC.md`

---

## 2. Architecture (how pieces connect)

```
                         ┌─────────────────────────────────────────┐
                         │           ENTRY POINTS                   │
                         ├─────────────────────────────────────────┤
                         │  scripts/infer.py     (production CLI)   │
                         │  python -m uvicorn src.api:app (API)    │
                         │  static/              (web UI at GET /)  │
                         │  tests/test_pipeline.py (E2E API test)   │
                         └──────────────────┬──────────────────────┘
                                            │
                                            ▼
                         ┌─────────────────────────────────────────┐
                         │     src/fusion.py  parse_unified()       │
                         │  Routes by diagram type + engine flag      │
                         └──────┬──────────┬──────────┬────────────┘
                                │          │          │
              FLOORPLAN         │   PID/SLD│          │  DC_*
                                ▼          ▼          ▼
                    floorplan_hybrid   pid_hybrid   datacenter
                    (Roboflow+Claude) (YOLO+Claude) (OpenCV+Claude)
                                │          │          │
                                └────┬─────┴────┬─────┘
                                     ▼          ▼
                              claude_parse   build_graph
                              (VLM fallback) (YOLO-only fallback)
                                     │
                                     ▼
                         ┌─────────────────────────────────────────┐
                         │   Graph JSON  (validated vs schema)      │
                         └──────┬──────────┬──────────┬────────────┘
                                │          │          │
                    ┌───────────┘          │          └──────────────┐
                    ▼                      ▼                         ▼
              src/pue.py            src/finance.py          compliance_checker
              src/loads.py          (HK property)           (rules → validate)
              POST /fuse            POST /finance/property   POST /compliance/*
```

### Shared utilities (used by multiple parsers)

| Module | Role |
|--------|------|
| `src/cv_marks.py` | Numbered set-of-marks overlays, handwriting OCR helpers |
| `src/secrets_util.py` | Load Anthropic / Roboflow keys (env → `models/*.txt` → `.env`) |
| `src/run_utils.py` | `load_src("09_floorplan_hybrid.py")` → resolves to `floorplan_hybrid.py` via legacy name map |

---

## 3. Directory tree (every important file)

```
eurotech/
│
├── README.md                    # Quick start, 3-min demo script, architecture
├── requirements.txt             # Python dependencies
├── .env.example                 # Template for API keys (safe to commit)
├── .gitignore                   # Excludes secrets, parsed scratch, DB cache
│
├── config/                      # Runtime configuration (moved from repo root)
│   ├── bec_rules.yaml           # PUE thresholds, tariff, finance assumptions
│   ├── class_map.yaml           # YOLO class names → graph node types
│   └── property_prices/         # HK district price/rent tables (finance module)
│       ├── Kowloon.txt, Kowloon_rent.txt
│       ├── Hong_Kong_Island.txt, Hong_Kong_Island_Rent.txt
│       └── New_Territories.txt, New_Territories_rent.txt
│
├── tests/
│   ├── test_pipeline.py         # End-to-end HTTP integration test (needs running API)
│   └── fixtures/
│       └── mock_graph.json        # Small mock graph for dev
│
├── data/                        # Sample images, overlays, demo JSON (not secrets)
│   ├── raw_diagrams/            # P&ID test images (e.g. images__train__404.jpg)
│   ├── raw_floorplans/          # Floor plan samples (118.png, F2_original.png)
│   ├── datacenter/              # DC preview webp images
│   ├── demo_floorplan.json      # Frozen F2 floor plan parse (Load demo)
│   ├── demo_compliance_report.json  # Frozen TIA-942 validation report
│   ├── demo_datacentre.json     # Frozen fused datacenter demo graph
│   ├── demo_graph.json          # Legacy HVAC demo
│   └── overlay_*.png            # Debug visualization outputs (regenerable)
│
├── docs/
│   ├── REPOSITORY_GUIDE.md      # ← this file
│   ├── VIEWER_SPEC.md           # 3D viewer contract
│   └── DEMO_SCENARIOS.md        # Demo script cues
│
├── models/
│   ├── yolov8n_pid.pt           # Trained P&ID YOLO weights (~7 MB)
│   ├── README.txt               # How to obtain weights from Kaggle
│   ├── secretapi.txt            # GITIGNORED — Anthropic key
│   └── roboflow_key.txt         # GITIGNORED — Roboflow key
│
├── notebooks/
│   ├── train_yolo_kaggle.ipynb  # Train P&ID detector on Kaggle
│   └── train_floorplan.ipynb    # Floor plan training experiments
│
├── prompts/                     # Claude system prompts (read by hybrid parsers)
│   ├── system_floorplan_marks.md
│   ├── system_pid_marks.md
│   ├── system_floorplan.md
│   ├── system_pid_sld.md
│   ├── system_dc_datahall.md
│   ├── system_dc_serverroom.md
│   └── system_cooling_pid.md
│
├── schemas/                     # JSON Schema contracts (source of truth)
│   ├── graph.schema.json        # Floor plans + P&ID/SLD unified graph
│   ├── claude_output.schema.json
│   ├── dc_datahall.schema.json
│   ├── dc_serverroom.schema.json
│   └── cooling_pid.schema.json
│
├── scripts/                     # CLI tools (run from repo root)
│   ├── infer.py                 # ★ PRIMARY production inference CLI
│   ├── visualize.py             # Draw graph overlay on source image
│   ├── parse_datacenter.py      # Datacenter-only wrapper
│   ├── parse_floorplan_hybrid.py# Floor-plan-only wrapper
│   ├── test_local.py            # Offline smoke tests (no API keys)
│   ├── test_local.ps1
│   └── api_smoke.ps1            # PowerShell API health check
│
├── src/                         # Core Python package
│   ├── api.py                   # ★ FastAPI server + mounts static UI
│   ├── fusion.py                # ★ Unified parse router + graph fusion
│   ├── preprocess.py            # Image load, resize, contrast
│   ├── detect_yolo.py           # YOLO inference
│   ├── extract_text.py          # Tesseract OCR on detections
│   ├── build_graph.py           # YOLO detections → graph JSON + validate
│   ├── claude_parse.py          # Claude-only VLM parser (fallback)
│   ├── cv_marks.py              # Set-of-marks drawing + handwriting
│   ├── floorplan_hybrid.py      # ★ Primary floor plan parser
│   ├── pid_hybrid.py            # ★ Primary P&ID parser
│   ├── datacenter.py            # ★ Datacenter layout parser
│   ├── parse_floorplan.py       # Legacy CV floor plan (U-Net walls, offline)
│   ├── pue.py                   # PUE computation + BEC evaluation
│   ├── loads.py                 # Electrical load analysis
│   ├── finance.py               # HK property valuation + CLI
│   ├── secrets_util.py          # API key loader
│   ├── run_utils.py             # Dynamic module loader + legacy name aliases
│   └── compliance_checker/      # TIA-942 compliance engine (see §5)
│
└── static/                      # Web UI served at GET /
    ├── index.html               # Golden path: Overlay → Rooms → Valuation → Compliance
    ├── index.css
    ├── app.js
    ├── roadmap.html             # Click-through roadmap frames (3D, IoT, collab, nameplate)
    └── demo/
        ├── f2_overlay.png       # Pre-baked overlay for offline demo
        └── f2_source.png        # Source floor plan image
```

---

## 4. `src/` modules — detailed roles

### 4.1 Vision → graph pipeline

| File | Keep? | What it does | Called by |
|------|-------|--------------|-----------|
| `preprocess.py` | **KEEP** | Load PNG/JPG/PDF, resize, boost contrast | `build_graph`, `detect_yolo` CLI |
| `detect_yolo.py` | **KEEP** | Run `models/yolov8n_pid.pt`, return normalized bboxes | `build_graph`, `pid_hybrid` |
| `extract_text.py` | **KEEP** | Tesseract OCR near YOLO boxes | `build_graph` |
| `build_graph.py` | **KEEP** | Detections + OCR → `graph.schema.json`; proximity edges | `fusion` (YOLO fallback), `api` (validate) |
| `claude_parse.py` | **KEEP** | Full Claude VLM parse when CV fails | `floorplan_hybrid`, `datacenter`, `fusion` fallback |
| `cv_marks.py` | **KEEP** | Numbered marks image, handwriting → `annotations[]` | `floorplan_hybrid`, `pid_hybrid`, `datacenter` |
| `floorplan_hybrid.py` | **KEEP** ★ | Roboflow rooms/fixtures + Claude labels | `fusion`, `infer`, `finance` image CLI |
| `pid_hybrid.py` | **KEEP** ★ | YOLO symbol boxes + Claude topology/OCR | `fusion`, `infer` |
| `datacenter.py` | **KEEP** ★ | OpenCV region detection + Claude labels (3 styles) | `fusion`, `infer`, `parse_datacenter` |
| `parse_floorplan.py` | **KEEP** (fallback) | Legacy offline floor plan (U-Net + Shapely) | `fusion` YOLO engine path only |
| `fusion.py` | **KEEP** ★ | `parse_unified()` router; `fuse_graphs()` arch+MEP merge | `api`, `infer` |

### 4.2 Analytics

| File | Keep? | What it does | API route |
|------|-------|--------------|-----------|
| `pue.py` | **KEEP** | `compute_pue()`, `evaluate_bec()`, `whatif_pue()` | `GET /compliance/pue`, `POST /whatif`, `POST /fuse` |
| `loads.py` | **KEEP** | Facility/IT load sums, breaker headroom | `POST /loads` |
| `finance.py` | **KEEP** | HK property value + rental ROI from floor area | `POST /finance/property`, CLI `py -m src.finance` |

### 4.3 Infrastructure

| File | Keep? | What it does |
|------|-------|--------------|
| `api.py` | **KEEP** ★ | FastAPI app: parse, fuse, finance, compliance, static UI |
| `secrets_util.py` | **KEEP** | Central API key resolution |
| `run_utils.py` | **KEEP** | `load_src()` with legacy `09_*.py` → `floorplan_hybrid.py` aliases |

---

## 5. Compliance checker (`src/compliance_checker/`)

Standalone TIA-942 engine, also exposed via API.

```
sample_standard.txt  →  regex_parser  →  Rule[]  →  SQLite DB
sample_graph.json    →  graph_parser  →  GeometryObject[]  →  validator  →  violations
```

| File | Keep? | Role |
|------|-------|------|
| `main.py` | **KEEP** | CLI orchestrator (extract → DB → validate → report) |
| `engine/rules.py` | **KEEP** | `Rule`, `GeometryObject`, vocabularies |
| `engine/validator.py` | **KEEP** | Numeric + topological checks (networkx) |
| `parsers/regex_parser.py` | **KEEP** ★ | **Active** offline rule extractor (spaCy + regex) |
| `parsers/graph_parser.py` | **KEEP** ★ | Unified graph JSON → geometry model |
| `parsers/structured_parser.py` | **KEEP** | Load pre-built rules from JSON |
| `parsers/dxf_parser.py` | **KEEP** | Aisle derivation logic (used by graph_parser) |
| `parsers/text_parser.py` | Optional | Gemini-based extraction — **not wired to API/main** |
| `parsers/pdf_parser.py` | Optional | Ollama PDF extraction — **not wired to API/main** |
| `database/setup.py` | **KEEP** | Postgres or file SQLite (`data/dc_compliance.db`) |
| `database/models.py` | **KEEP** | SQLAlchemy ORM |
| `data/sample_standard.txt` | **KEEP** | TIA-942 sample for tests |
| `data/sample_graph.json` | **KEEP** | Graph with deliberate violations for demo |
| `data/dc_compliance.db` | **DELETE locally** | Runtime DB (gitignored, regenerated) |
| `data/.rules_cache/` | **DELETE locally** | Parse cache (gitignored) |

---

## 6. Scripts — which to use

| Script | Keep? | When to use |
|--------|-------|-------------|
| `scripts/infer.py` | **KEEP** ★ | **Default** — any diagram type, schema validation, overlay |
| `scripts/visualize.py` | **KEEP** | Debug overlay PNG from graph JSON |
| `scripts/parse_datacenter.py` | **KEEP** | Batch datacenter previews only |
| `scripts/parse_floorplan_hybrid.py` | **KEEP** | Floor plans only (simpler than infer) |
| `scripts/test_local.py` | **KEEP** | Offline unit smoke (no network) |
| `scripts/api_smoke.ps1` | **KEEP** | Quick API health on Windows |
| `tests/test_pipeline.py` | **KEEP** | Full E2E with running server |

### Production infer examples

```powershell
# Floor plan
python scripts/infer.py data/raw_floorplans/118.png --type FLOORPLAN --out data/parsed.json

# P&ID
python scripts/infer.py data/raw_diagrams/images__train__404.jpg --type PID --out data/parsed.json

# Datacenter (AUTO routes when YOLO finds no symbols)
python scripts/infer.py data/datacenter/preview.webp --type AUTO --out data/parsed.json

# API + UI
python -m uvicorn src.api:app --reload
# → http://127.0.0.1:8000
```

---

## 7. API routes (`src/api.py`)

| Method | Path | Module | Purpose |
|--------|------|--------|---------|
| GET | `/` | `static/` | Web UI |
| GET | `/health` | — | Liveness |
| POST | `/parse` | `fusion.parse_unified` | Upload image → graph JSON |
| POST | `/fuse` | `fusion.fuse_graphs` + `pue` | Merge arch + MEP graphs |
| GET | `/schema` | `schemas/graph.schema.json` | Handoff contract |
| GET | `/demo-graph` | `data/demo_graph.json` | Frozen HVAC demo |
| GET | `/demo-datacentre` | `data/demo_datacentre.json` | Frozen DC demo |
| GET | `/compliance/pue` | `pue.evaluate_bec` | BEC clause table |
| POST | `/loads` | `loads.analyze` | Electrical loads |
| POST | `/whatif` | `pue.whatif_pue` | PUE sensitivity |
| GET | `/finance/districts` | `finance.list_districts` | HK districts/estates |
| POST | `/finance/property` | `finance.estimate_from_graph` | Property valuation |
| POST | `/finance/roi` | `pue` + `bec_rules` | Datacenter energy ROI |
| POST | `/compliance/extract` | `compliance_checker` | Rules from standard TXT/PDF |
| POST | `/compliance/rules` | DB save | Persist rules |
| GET | `/compliance/rules` | DB load | Load rules |
| POST | `/compliance/validate` | `validator` | Check graph vs rules |

---

## 8. Schemas & prompts

### Schemas (`schemas/`)

| File | Used by |
|------|---------|
| `graph.schema.json` | Floor plans, P&ID, fused graphs — **main contract** |
| `claude_output.schema.json` | Claude VLM intermediate output (`claude_parse`) |
| `dc_datahall.schema.json` | Datacenter data hall layouts |
| `dc_serverroom.schema.json` | Server room layouts |
| `cooling_pid.schema.json` | Cooling plant diagrams |

### Prompts (`prompts/`)

Each hybrid parser reads a matching `system_*.md` file. **Keep all** — they are not redundant.

---

## 9. External dependencies

| Dependency | Config | Required for |
|------------|--------|--------------|
| Anthropic API | `ANTHROPIC_API_KEY` / `models/secretapi.txt` | All hybrid parsers, finance image CLI |
| Roboflow API | `ROBOFLOW_API_KEY` / `models/roboflow_key.txt` | Floor plan room detection |
| YOLO weights | `models/yolov8n_pid.pt` | P&ID symbol detection |
| Tesseract | System PATH | OCR tags (non-fatal if missing) |
| spaCy `en_core_web_sm` | Auto-downloaded | Compliance rule extraction |
| Postgres (optional) | `DATABASE_URL` | Compliance DB (SQLite fallback works) |
| Ollama (optional) | `localhost:11434` | Semantic dedupe in compliance CLI only |

---

## 10. KEEP vs DELETE — redundant files

### Safe to delete (redundant / one-shot / regenerable)

| Path | Why delete |
|------|------------|
| **`data/overlay_*.png`**, **`data/*_marks_*.png`** | Regenerable debug images (`scripts/visualize.py`). |
| **`data/test_infer_404*.json`**, **`data/parsed_*.json`** | Scratch parse outputs (gitignored pattern). |
| **`src/compliance_checker/data/dc_compliance.db`** | Local runtime DB (gitignored). |
| **`src/compliance_checker/data/.rules_cache/`** | Extraction cache (gitignored). |

### Keep (production)

| Category | Files |
|----------|-------|
| **Entry points** | `scripts/infer.py`, `src/api.py`, `static/*`, `tests/test_pipeline.py` |
| **Parsers** | `floorplan_hybrid.py`, `pid_hybrid.py`, `datacenter.py`, `fusion.py`, `claude_parse.py`, `cv_marks.py` |
| **YOLO fallback chain** | `preprocess.py`, `detect_yolo.py`, `extract_text.py`, `build_graph.py` |
| **Analytics** | `pue.py`, `loads.py`, `finance.py` |
| **Compliance** | Entire `src/compliance_checker/` except runtime DB/cache |
| **Config** | `schemas/*`, `prompts/*`, `config/bec_rules.yaml`, `config/class_map.yaml`, `config/property_prices/*` |
| **Models** | `models/yolov8n_pid.pt` |
| **Demo assets** | `data/demo_floorplan.json`, `data/demo_compliance_report.json`, `static/demo/f2_overlay.png` |
| **Samples** | `data/raw_*`, `data/datacenter/`, `data/demo_*.json` |
| **Docs** | `README.md`, `docs/*`, build plans |

### Optional (keep if you use the feature)

| File | Notes |
|------|-------|
| `parse_floorplan.py` | Offline floor plan without Claude/Roboflow |
| `compliance_checker/parsers/text_parser.py` | Gemini rule extraction (experimental) |
| `compliance_checker/parsers/pdf_parser.py` | Ollama PDF rules (experimental) |
| `notebooks/*` | Training only |
| `ArchDraft_Build_Plan.md`, `FlowDraft_Build_Plan.md` | Planning docs |
| `tests/fixtures/mock_graph.json` | Tiny dev fixture |

### Numbered vs unnumbered modules (`01_preprocess.py` vs `preprocess.py`)

The repo was refactored from numbered names to clean names. **On disk today only the unnumbered files exist** in `src/`:

```
preprocess.py      (was 01_preprocess.py)
detect_yolo.py     (was 02_detect_yolo.py)
extract_text.py    (was 03_extract_text.py)
build_graph.py     (was 04_build_graph.py)
parse_floorplan.py (was 05_parse_floorplan.py)
fusion.py          (was 06_fusion.py)
claude_parse.py    (was 07_claude_parse.py)
datacenter.py      (was 08_datacenter.py)
floorplan_hybrid.py(was 09_floorplan_hybrid.py)
pid_hybrid.py      (was 10_pid_hybrid.py)
```

`run_utils.py` still accepts old names (`load_src("09_floorplan_hybrid.py")`) and redirects to the new files. **No numbered copies remain in `src/`.**

---

## 11. Typical workflows

### A. Parse a floor plan and get property value

```
image → scripts/infer.py --type FLOORPLAN
      → floorplan_hybrid (Roboflow + Claude)
      → graph.schema.json
      → POST /finance/property (or Finance tab in UI)
      → HKD valuation
```

### B. Parse P&ID and compute energy ROI

```
image → POST /parse (engine=cv-hybrid)
      → pid_hybrid (YOLO + Claude)
      → graph JSON
      → POST /finance/roi
      → annual savings + payback years
```

### C. Compliance check (TIA-942)

```
sample_standard.txt → POST /compliance/extract → 8 rules
                    → POST /compliance/rules (save to SQLite)
sample_graph.json   → POST /compliance/validate
                    → 3 violations (cold aisle, missing UPS, PUE)
```

### D. Fuse architectural + MEP into datacenter twin

```
floor plan graph + P&ID graph → POST /fuse
                              → fused graph + PUE in meta
                              → GET /compliance/pue
```

---

## 12. Suggested cleanup commands

Optional — remove regenerable debug outputs:

```powershell
cd C:\coupdegrace\eurotech
Remove-Item data\overlay_*.png, data\*_marks_*.png, data\test_infer_*.json -ErrorAction SilentlyContinue
```

**Do not delete:** `src/`, `scripts/infer.py`, `schemas/`, `prompts/`, `config/`, `models/yolov8n_pid.pt`, `data/demo_*.json`, `static/demo/`, or `static/`.

---

## 13. Quick reference — minimum files to run production

If you stripped everything non-essential, you still need:

```
requirements.txt
.env.example
schemas/*.json
prompts/*.md
models/yolov8n_pid.pt
models/README.txt
src/api.py, fusion.py, floorplan_hybrid.py, pid_hybrid.py, datacenter.py
src/claude_parse.py, cv_marks.py, build_graph.py, detect_yolo.py
src/preprocess.py, extract_text.py, secrets_util.py, run_utils.py
src/pue.py, loads.py, finance.py
src/compliance_checker/   (full package)
scripts/infer.py, visualize.py
static/
config/bec_rules.yaml, config/class_map.yaml, config/property_prices/
data/demo_floorplan.json, data/demo_compliance_report.json, data/demo_datacentre.json
static/demo/f2_overlay.png
```

---

*Last updated: June 2026 — golden path UI, config/ reorg, frozen demo assets, compliance checker.*
