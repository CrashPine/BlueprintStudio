# Demo Scenarios & Datasets (Claude VLM)

With Claude API credits, the parser is a **Vision Language Model** — no model training needed. Claude does perception (symbols, dimensions, handwriting, nameplate ratings → schema v2 JSON); deterministic code does all math (PUE, BEC, electrical loads).

YOLO+OCR remains as an **offline fallback** (`engine=yolo`, or automatic when the API key/credits are unavailable).

---

## Datasets (demo images + few-shot examples only)

### Architectural / datacentre floor plans

| Dataset | Access | Use |
|---------|--------|-----|
| **FloorPlanCAD** (ICCV 2021, ~15k plans, walls/doors/windows) | HF `Voxel51/FloorPlanCAD` (site shut 2022, mirror live) | Demo floor plans + few-shot example |
| **ArchCAD-400K** (NeurIPS 2025, 27 categories, raster+SVG+JSON+QA) | HF `jackluoluo/ArchCAD` | Diverse commercial/datacentre plans |

### P&ID / HVAC / electrical

| Dataset | Access | Use |
|---------|--------|-----|
| **PIDQA** (500 sheets, 64k QA + Cypher) | `github.com/mgupta70/PIDQA` (CC0) | P&ID demo + graph-reasoning story |
| **Dataset-P&ID** (source of PIDQA) | Zenodo `8028570` (6.7 GB) | Raw P&ID sheets |
| **Azure P&ID Synthetic** (500 annotated, 32 symbols) | Microsoft | Few-shot symbol legend |
| **Deep-Learning SLD** (6.7k scanned SLDs) | dataset repo | Electrical SLD demo |

Place your downloaded test images here:

```
data/demo_pack/floorplans/   <- 3 floor plans
data/demo_pack/pid_sld/      <- 3 P&ID / SLD diagrams
```

---

## Scenario 1 — ArchDraft (architectural + datacentre)

**Story:** A developer converts an aging commercial floor into a high-density data centre. The AI parses the 2D plan to find physical boundaries, server-room dimensions, and structural constraints.

**Flow:**
1. `POST /parse?diagram_type=FLOORPLAN&engine=claude` on a floor plan.
2. Claude returns `spaces[]` (data_hall, electrical_room, plant_room) with `area_m2`.
3. Pitch: parsed room areas dictate how many server racks physically fit **before** energy optimization begins.

**CLI:**
```powershell
python src/07_claude_parse.py data/demo_pack/floorplans/<img> --type FLOORPLAN
```

---

## Scenario 2 — FlowDraft (P&ID / HVAC + electrical SLD)

**Story:** Engineers integrate cooling (HVAC) and power (electrical) into the new data centre. The AI checks that new server load won't trip the main breakers or violate BEC.

**Flow:**
1. `POST /parse?diagram_type=PID&engine=claude` (cooling P&ID) and `diagram_type=SLD` (power).
2. Claude maps **Grid → Transformer → Breaker → Rack** as nodes/edges with nameplate ratings.
3. `POST /loads` → deterministic downstream load sum, breaker headroom, EMSD benchmark.
4. `POST /fuse` + `GET /compliance/pue` → fused PUE + BEC verdict.

**CLI:**
```powershell
python src/07_claude_parse.py data/demo_pack/pid_sld/<img> --type SLD
```

---

## How the hard parts are handled

### 1. Sizes & dimensions
Claude reads explicit dimension annotations (e.g. "15m", "3000mm") into `attributes` / `area_m2`. One known dimension calibrates a pixel-to-meter scale; the 3D engine extrapolates the rest of the shell. Unlabelled rooms get an estimated area with lower `confidence`.

### 2. Handwriting & red-line markups
Claude's VLM reads handwritten engineer notes, red-line revisions, and scribbled tags natively — **no separate OCR/HTR pipeline**. Transcribed into `tag` / `attributes`.

### 3. Electrical consumption (never ask the LLM to compute)
- **Extraction (Claude):** raw nameplate ratings only — `rated_power_kW`, `voltage_V`, `ampacity_A`. Unreadable → `null`, never guessed.
- **Math (code):** [`src/loads.py`](../src/loads.py) sums downstream `rated_power_kW` via graph traversal, computes 3-phase breaker current `I = P / (√3 · V · pf)`, flags overload at 80% ampacity.
- **Benchmark:** compares computed annual energy vs EMSD HK datacentre EUI for the business-value slide.

---

## Engine selection

| Request | Behavior |
|---------|----------|
| `engine=claude` (default) | Claude VLM; auto-fallback to YOLO/OpenCV on any failure |
| `engine=yolo` | Legacy YOLO+OCR / OpenCV only (offline) |
| `engine=auto` | Alias of claude |

`meta.parser` in the response shows which path produced the graph (`claude` / `yolo` / `floorplan-cv`), and a `claude_fallback:` warning is appended if it fell back.
