# HONESTY.md

> Mandatory disclosure for the hackathon. Judges cross-check this against the code and the technical video.
>
> **The deal:** disclosed shortcuts are **not** penalized. Hidden ones are.

---

## 1. Team — who did what

| Member | GitHub handle | Main contributions |
|---|---|---|
| Arsenii Harbar | @z1nare (primary), commits also under name | Architecture, graph schema, fusion router, floor-plan + P&ID hybrid parsers, compliance engine, API, Docker, golden-path UI |
| Collaborator | @CrashPine | Early schema / repo contributions (1 commit) |
| AI pair-programming | @cursoragent | Assisted implementation sessions (UI polish, bug fixes, Docker, twin viewer) |

*Note: `git shortlog -sn` shows 15 commits under “Arsenii Harbar”, 10 under “z1nare”, 1 under “CrashPine” — same team, mixed author strings on local git config.*

---

## 2. What is fully working

Features that run end-to-end on the live app:

- **Docker one-command start** — `docker compose up --build` → http://localhost:8000 (healthcheck on `/health`).
- **Stage-safe demo (no API keys)** — **Load demo** loads frozen F2 floor plan (`data/demo_floorplan.json`) + pre-baked overlay (`static/demo/f2_overlay.png`); rooms table, summary chips, and tabs all populate.
- **Live floor-plan parse** (with `ANTHROPIC_API_KEY` + `ROBOFLOW_API_KEY`) — `POST /parse` routes to Roboflow room boxes + Claude set-of-marks labeling (`src/floorplan_hybrid.py`); falls back to Claude coordinate parser or OpenCV when detection is weak.
- **Overlay rendering** — `POST /overlay` runs `scripts/visualize.py` on the uploaded image + parsed graph (room polygons, fixture boxes, legend).
- **HK property valuation** — `POST /finance/property` reads static district price/rent tables in `config/property_prices/`, multiplies parsed total area → HKD value + annual ROI %.
- **Compliance on real geometry** — `POST /compliance/validate` runs TIA-942-style rules from `src/compliance_checker/` against the graph currently loaded in the UI (after `/compliance/init-demo` seeds rules from `sample_standard.txt`).
- **P&ID / datacenter parse paths** — YOLO weights (`models/yolov8n_pid.pt`) + Claude hybrid for MEP diagrams; OpenCV + Claude datacenter parser for layout images (tested on bundled samples).
- **3D building twin** — `static/twin.html` extrudes `spaces[]`, MEP `nodes[]`, and `fixtures[]` (toilets, doors, windows) from the latest parsed graph (via `localStorage` handoff from main UI).
- **Capability roadmap page** — `static/roadmap.html` (links to twin, ROI strip, pipeline overview).

---

## 3. What is mocked, stubbed, or hardcoded

| What is faked | Where | Why we mocked it | What the real version would do |
|---|---|---|---|
| **Load demo floor plan** | `GET /demo/floorplan`, `data/demo_floorplan.json`, `static/demo/f2_overlay.png` | Reliable jury demo without network or API spend | Always live-parse the uploaded image |
| **Pre-baked compliance report fallback** | `GET /demo/compliance-report`, `data/demo_compliance_report.json`; `static/app.js` last-resort path | Offline / failed validate still shows something on stage | Only show live validation results |
| **Datacenter sample graph** | `data/demo_datacentre.json`, `GET /demo-datacentre` | Compliance tab demo when user has no DC graph loaded | User’s own parsed datacenter layout only |
| **HK market prices** | `config/property_prices/*.txt` | No live MLS / government API in hackathon window | Pull current transacted prices from Centaline / gov open data |
| **Rule de-duplication embeddings** | `src/compliance_checker/main.py` → Ollama `localhost:11434` | Optional; logs warning and continues without embeddings if Ollama is down | Managed embedding service always available |
| **Compliance DB** | `src/compliance_checker/database/setup.py` | Postgres optional; defaults to file SQLite `dc_compliance.db` | Production Postgres with migrations |
| **Conversational RAG chat** | *Not implemented* — mentioned in pitch deck only | Out of scope for this submission | Chat over blueprints + standards with retrieval |
| **“BlueprintStudio” product shell** | Marketing name in video script | Branding for presentation | Same engine, fuller IDE UX |

---

## 4. External APIs, services & data sources

| Service / API / dataset | Used for | Real call or mocked? | Auth |
|---|---|---|---|
| **Anthropic Claude** (`claude-sonnet-4-6`) | Room labeling, coordinate parse fallback, P&ID/datacenter OCR | **Real** when key present | `ANTHROPIC_API_KEY` or `models/secretapi.txt` |
| **Roboflow hosted** (`architectural-blueprint/2`, `floor-plans-500-r7jy4/1`) | Room + door/window detection on floor plans | **Real** when key present | `ROBOFLOW_API_KEY` or `models/roboflow_key.txt` |
| **Ultralytics YOLO** (`models/yolov8n_pid.pt`) | P&ID symbol boxes (local inference) | **Real** (local weights, trained pre-hackathon on Kaggle) | None |
| **Tesseract OCR** | Dimension / label text on floor plans & diagrams | **Real** (bundled in Docker image) | None |
| **HK property tables** | Valuation & ROI | **Static files** (not a live API) | None |
| **TIA-942 sample rules** | Compliance demo | **Real extractor** on `sample_standard.txt`; not full standard PDF corpus | None |
| **Ollama embeddings** | Rule deduplication | **Optional real**; skipped if host unreachable | Local `11434` |
| **PostgreSQL** | Compliance rule storage | **Optional**; SQLite fallback | `DATABASE_URL` |
| **Gemini** | PDF rule extraction path in compliance checker | **Optional**; not required for main UI demo | `GEMINI_API_KEY` |

---

## 5. Pre-existing code

Anything brought in **before** or **outside** the main hackathon build window:

| Item | Source | Roughly how much | License |
|---|---|---|---|
| **Graph contract schemas** | Written June 5 kickoff (`schemas/graph.schema.json`, `claude_output.schema.json`) | ~2 days before bulk of app | Project |
| **YOLO P&ID training notebook** | `notebooks/train_yolo_kaggle.ipynb` + `models/yolov8n_pid.pt` from Kaggle training | Weights ~6–7 MB; notebook pre-dates full UI | Ultralytics AGPL (weights ours) |
| **Build plans** | `FlowDraft_Build_Plan.md`, `ArchDraft_Build_Plan.md` | Planning docs from kickoff | Project |
| **Open-source libraries** | FastAPI, OpenCV, Ultralytics, Anthropic SDK, Three.js (CDN), Shapely, spaCy, etc. | Standard dependencies | Per `requirements.txt` / CDN |
| **F2 demo floor plan image** | `data/raw_floorplans/F2_original.png` | Public sample plan used across CV tests | Sample asset |
| **Three.js twin viewer template** | Evolved during hackathon; uses Three.js r128 from cdnjs | Frontend only | MIT (Three.js) |

*Most application logic (`src/`, compliance checker, finance, Docker, static UI) was written during the hackathon window (June 6–7, 2026).*

---

## 6. Known limitations & next steps

- **Handwritten / photo floor plans** (e.g. red-ink annotations): Roboflow room detector often finds too few boxes → Claude-only fallback can collapse to one big room; quality gate needs tuning.
- **Sanitary fixture detection**: default Roboflow fixture model detects doors/windows, not toilets/sinks; twin renders fixtures when present but detection is sparse on residential plans.
- **No conversational RAG** over uploaded drawings yet — compliance is rule geometry, not document Q&A.
- **Finance** uses static district averages, not estate-level live transactions.
- **YOLO floor-plan weights** not in repo — residential rooms use Roboflow hosted model, not our own YOLO checkpoint.
- **Next:** improve weak-plan fallback (keep partial Roboflow boxes + Claude gap-fill), sanitary detector model, live HK price API, RAG chat, PDF standards ingestion at scale.

---

## 7. How to verify our claims

```bash
git clone https://github.com/CrashPine/BlueprintStudio.git
cd BlueprintStudio
docker compose up --build
# → http://localhost:8000 → Load demo (no keys)
# → Upload data/raw_floorplans/F2_original.png with keys for live parse
curl http://localhost:8000/health
python tests/test_pipeline.py   # with local venv, server running
```
