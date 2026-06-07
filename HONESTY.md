# HONESTY.md

> Mandatory hackathon disclosure. Lives at the repo root; judges cross-check it against the code and the technical video.
>
> **The deal:** disclosed shortcuts are *not* penalized. Hidden ones are. Telling the truth here costs nothing.
>
> ⚠ **Note for the team:** this was assembled from the README, `REPOSITORY_GUIDE.md`, the build plan, git history, and working sessions — not a line-by-line audit of every module. Confirm teammate rows and any `[verify]` items before final submission.

---

## 1. Team — who did what

Judges compare this against `git shortlog -sn`, so keep it honest.

| Member | GitHub handle | Main contributions |
|---|---|---|
| Arsenii Harbar | `z1nare` | Graph schema (`schemas/graph.schema.json`), fusion router (`src/fusion.py`), floor-plan + P&ID hybrid parsers, FastAPI app (`src/api.py`), overlay pipeline, finance module, Docker packaging, golden-path UI (`static/`), `HONESTY.md`, final repo integration |
| Yehor-X | `Yehor-X` | Early BlueprintStudio work (preserved in git history): compliance checker scaffold (`dc_compliance_checker` → merged into `src/compliance_checker/`), Claude wiring in compliance parsers, frontend iterations |
| | `CrashPine` | Initial BlueprintStudio repository commit; org/repo owner |
| | | |

**AI tooling (disclosed):** `cursoragent` appears in some commit history (Cursor background agent). We also used Claude (Anthropic) for vision parsing in the pipeline and to help build `static/twin.html` and `static/roadmap.html`. All AI-assisted implementation was done during the hackathon window.

**Git history note:** `git shortlog -sn` on `main` may show mixed author strings (`Arsenii Harbar` vs `z1nare`) for the same developer — compare against commit messages, not display names alone.

---

## 2. What is fully working

End-to-end on the live app, real data, real logic.

- **Docker one-command start** — `docker compose up --build` → http://localhost:8000 (`/health` returns `{"ok":true}`).
- **Stage-safe demo (no API keys)** — **Load demo** serves frozen F2 floor plan (`data/demo_floorplan.json`) + pre-baked overlay (`static/demo/f2_overlay.png`); overlay, rooms table, valuation, and compliance tabs populate without network.
- **Live floor-plan parsing** — upload image → `POST /parse` (`engine=cv-hybrid`) → Roboflow room boxes + Claude set-of-marks labeling (`src/floorplan_hybrid.py`); falls back to Claude coordinate parser or OpenCV when room detection is weak. **Requires** `ANTHROPIC_API_KEY` + `ROBOFLOW_API_KEY`.
- **Labeled 2D overlay** — parsed graph + source image → `POST /overlay` (`scripts/visualize.py`) → PNG with room polygons, category colors, areas, fixture boxes, legend, optional scale bar.
- **HK property valuation** — parsed graph area → `POST /finance/property` (`src/finance.py` + static tables in `config/property_prices/`) → estimated HKD value + annual rental ROI % by district/estate.
- **Compliance validation (API-level)** — `POST /compliance/init-demo` seeds rules extracted from `src/compliance_checker/data/sample_standard.txt` (spaCy regex path) → `POST /compliance/validate` checks geometry/topology on a graph JSON → violations with actual vs limit (`src/compliance_checker/engine/validator.py`).
- **Compliance on the loaded graph in the UI** — after parsing or **Load demo**, **Run compliance check** validates `currentGraph` via `/compliance/validate` (`static/app.js`); does not silently swap to frozen report unless validation fails.
- **Graph schema contract** — `schemas/graph.schema.json` validates parser output across diagram types.
- **3D building twin** — `static/twin.html` extrudes `spaces[]`, MEP `nodes[]`/`edges[]`, and `fixtures[]` (toilets, doors, windows, etc.); loads the latest graph from the main app via `localStorage` (`flowdraft:graph` set in `static/app.js`); falls back to embedded demo graph if none loaded.
- **Capability roadmap page** — `static/roadmap.html` documents the pipeline and links to the twin.

**Partially working / CLI-first (not the main demo path):**

- **P&ID parsing** — `POST /parse` with `diagram_type=PID` + UI dropdown option; `src/pid_hybrid.py` (local YOLO `models/yolov8n_pid.pt` + Claude). Works via API/CLI on bundled samples (`data/raw_diagrams/images__train__113.jpg`); less polished than the floor-plan path; not wired into the guided 3-minute UI flow.
- **Datacenter layout parsing** — `src/datacenter.py` (OpenCV regions + Claude); reachable via fusion router; demo assets in `data/demo_datacentre.json` and overlay PNGs; not the primary upload journey in the UI.
- **Electrical load / EMSD benchmark** — `POST /loads` (`src/loads.py`) computes downstream kW sum and compares to a **hardcoded** EMSD EUI constant; API works but is not exposed in the main guided UI tabs.

---

## 3. What is mocked, stubbed, or hardcoded

Every shortcut. **Disclosed = free. Undisclosed = small penalty each.**

| What is faked | Where | Why we did it | What the real version does |
|---|---|---|---|
| **"Load demo" serves pre-parsed data** | `GET /demo/floorplan`, `data/demo_floorplan.json`, `static/demo/f2_overlay.png` | Stage-safe, no keys/network | Always live `POST /parse` on the uploaded image |
| **Frozen compliance report fallback** | `GET /demo/compliance-report`, `data/demo_compliance_report.json`; `static/app.js` last-resort catch | Offline / failed validate still shows something | Only show live `/compliance/validate` results |
| **Datacenter sample graph fallback** | `data/demo_datacentre.json`, `GET /demo-datacentre`, `GET /demo/compliance-graph` | Compliance tab works before any user parse | Only validate user-parsed graphs |
| **HK market prices are static files** | `config/property_prices/*.txt` | No live MLS/gov API in hackathon window | Live transaction feed (Centaline / RVD / gov open data) |
| **Roadmap “What-if Retrofit ROI” calculator** | `static/roadmap.html` (`TARIFF=1.35`, `CO2=0.39`, slider inputs) | Concept demo; not tied to a parsed plant graph | Derive plant load/efficiency from parsed P&ID + live tariff tables |
| **Roadmap SVG capability frames** | `static/roadmap.html` `.mock` SVG blocks | Illustrative UI mockups | Live screenshots from the running app |
| **CSDI building context panel** | `static/roadmap.html` — marked “Roadmap” | Planned M6 feature | Query CSDI Building API + glTF shell anchoring |
| **EMSD EUI benchmark constant** | `src/loads.py` — `EMSD_DATACENTRE_EUI_KWH_M2_YR = 1800.0` | Stub until EMSD CSV is cached | Load real EMSD Energy End-use table |
| **Twin demo fallback** | `static/twin.html` — `DEMO_GRAPH` embedded French plan | Works when opened cold without parsing first | Always open from main app after parse (localStorage path already wired) |
| **No editing-studio canvas / live on-canvas compliance flagging** | Not present | Out of scope | Drag/drop canvas that flags geometric violations visually in real time. Today compliance returns a violation list via API. **Do not demo this as working.** |
| **No conversational RAG / “chat with blueprints & contracts”** | Not in current tree (prior BlueprintStudio chat backend was replaced) | Out of scope for this submission | Vector store + chat over drawings/contracts. **Do not demo this as working.** |
| **Unwired optional compliance parsers** | `src/compliance_checker/parsers/text_parser.py` (Gemini), `pdf_parser.py` (Ollama) | Experimental CLI paths | Wire to `/compliance/extract` for PDF standards at scale |
| **Ollama embeddings for rule dedup** | `src/compliance_checker/main.py` → `localhost:11434` | Optional; skipped with warning if down | Always-on embedding service |
| **Postgres compliance DB** | `src/compliance_checker/database/setup.py` | Optional; SQLite file fallback is the default in Docker | Managed Postgres |

---

## 4. External APIs, services & data sources

| Service / API / dataset | Used for | Real or mocked? | Auth |
|---|---|---|---|
| **Anthropic Claude API** (`claude-sonnet-4-6`) | Vision parsing (room labels, topology, attributes, datacenter/P&ID OCR) | **Real** when key present; demo button bypasses via frozen JSON | `ANTHROPIC_API_KEY` / `models/secretapi.txt` |
| **Roboflow hosted** (`architectural-blueprint/2`, `floor-plans-500-r7jy4/1`) | Floor-plan room + door/window detection | **Real** when key present | `ROBOFLOW_API_KEY` / `models/roboflow_key.txt` |
| **YOLOv8 / Ultralytics** (local) | P&ID symbol detection | **Real** — local inference on `models/yolov8n_pid.pt` | None |
| **Tesseract OCR** | Text near detections / dimensions | **Real** (installed in Docker image; optional locally) | None |
| **spaCy `en_core_web_sm`** | Compliance rule extraction (`regex_parser.py`) | **Real** (baked into Docker image) | None |
| **HK property price/rent tables** | Valuation (`config/property_prices/`) | **Real reference-style data, static markdown tables** — estate names and HK$/ft² values; not a live API; original scrape/source not documented in repo | None |
| **TIA-942 sample rules text** | Compliance demo | **Real extractor** on `sample_standard.txt` — subset only, not full standard PDF | None |
| **Ollama** | Rule deduplication embeddings; optional PDF parser | **Optional real**; gracefully skipped if host unreachable | Local `:11434` |
| **PostgreSQL** | Compliance rule storage | **Optional**; SQLite fallback is real | `DATABASE_URL` |
| **Gemini** | Experimental text/PDF parser paths | **Optional**; not used in main UI demo | `GEMINI_API_KEY` |
| **CSDI / EMSD / RVD live APIs** | Building shell, EUI benchmarks, audit-due (M6 in build plan) | **NOT WIRED** — roadmap/mock only | — |
| **Three.js** (cdnjs) | 3D twin rendering | **Real** CDN dependency | None |

---

## 5. Pre-existing code

Brought in from before or outside the main hackathon build window. **Disclosed = free. Undisclosed pre-built code = heavily penalized.**

| Item | Source | Roughly how much | License |
|---|---|---|---|
| **YOLOv8 / Ultralytics** | github.com/ultralytics/ultralytics | Detection framework | **AGPL-3.0** |
| **`yolov8n_pid.pt` weights** | Trained via `notebooks/train_yolo_kaggle.ipynb` on a Kaggle P&ID dataset | ~6–7 MB weights file | Derived from YOLOv8 (AGPL) + dataset terms — trained **before/during early hackathon** (kickoff week), not during the final UI sprint |
| **Roboflow hosted models** | roboflow.com (`architectural-blueprint/2`, fixture model) | External inference | Roboflow terms |
| **Early graph schemas + build plans** | `FlowDraft_Build_Plan.md`, `ArchDraft_Build_Plan.md`, initial `schema.json` commits (June 5) | Planning + contract docs | Project |
| **Prior BlueprintStudio scaffold** | Commits by `CrashPine`, `Yehor-X`, `Arsenii Harbar` on this repo before the final tree replacement — included early compliance checker + frontend experiments | ~6 commits preserved in history; **replaced** by current `src/` layout in commit `b550f8f` | Project |
| **`src/compliance_checker/`** | Evolved from Yehor-X’s `dc_compliance_checker/` + hackathon integration (regex parser, graph validator, API routes) | Core module; not a blind fork of an external repo | Project |
| **Three.js** | cdnjs in `static/twin.html` | 3D rendering | MIT |
| **FastAPI, OpenCV, Shapely, Pillow, NetworkX, jsonschema, anthropic SDK, …** | PyPI (`requirements.txt`) | Standard dependencies | various OSS |

*Standard pip packages are listed in `requirements.txt`; the rows above are the ones needing explicit disclosure.*

---

## 6. Known limitations & next steps

- **Demo path uses frozen assets** for stage safety; live parse needs API keys + network, takes several seconds per drawing, and is non-deterministic.
- **Handwritten / photo floor plans** (red ink, perspective): Roboflow often detects too few rooms → Claude-only fallback can collapse to one large room; quality gate in `floorplan_hybrid.py` needs tuning.
- **Sanitary fixtures** (toilet/sink): default Roboflow fixture model emits doors/windows; twin renders fixtures when present but detection is sparse on residential plans.
- **P&ID / datacenter** parsing is less polished than the floor-plan path; accuracy depends on drawing quality and is CLI/API-first.
- **Compliance** covers a **small extracted subset** of TIA-942-style rules from `sample_standard.txt`, not the full standard; HK **BEC/EAC** energy-code coverage is planned, not built.
- **Running TIA-942 / datacenter compliance on a residential floor-plan demo** is conceptually mismatched — for judges, prefer `data/demo_datacentre.json` / a parsed DC graph for the compliance tab, or explain that rules are geometry-generic.
- **No editing-studio / live on-canvas compliance flagging** — validation is API-level only.
- **No conversational document RAG** in the current submission (prior chat UI was superseded).
- **CSDI building context** (glTF shell, EUI benchmarks, audit-due) is on the roadmap page only, not wired in `src/`.
- **Valuation** uses static district/estate tables, not a live market feed.
- **Roadmap ROI calculator** uses fixed tariff/carbon constants, not parsed MEP data.
- **Next:** improve weak-plan fallback; sanitary detector model; integrate roadmap ROI with parsed graphs; expand BEC/EAC rules; add document chat; wire CSDI anchoring; on-canvas compliance overlays.

---

## 7. How to verify our claims

```bash
git clone https://github.com/CrashPine/BlueprintStudio.git
cd BlueprintStudio
docker compose up --build
# → http://localhost:8000 → Load demo (no keys)
# → Upload data/raw_floorplans/F2_original.png with keys for live parse
curl http://localhost:8000/health
python tests/test_pipeline.py   # local venv, server running
.\scripts\demo_jury.ps1         # Windows: automated jury talk-track
```
