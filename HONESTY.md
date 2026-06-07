# HONESTY.md

> Mandatory disclosure for the hackathon. Judges cross-check this against the code and the technical video.
>
> **The deal:** disclosed shortcuts are **not** penalized. Hidden ones are.

---

## 1. Team — who did what

| Member | GitHub handle | Main contributions |
|---|---|---|
| Arsenii Harbar | @z1nare | Architecture, graph schema, fusion router, floor-plan + P&ID hybrid parsers, compliance engine, API, Docker, golden-path UI |
| Yehor | @Yehor-X | Financial analysis engine (`src/finance.py`), HK property datasets, electrical load math (`src/loads.py`), PUE computation, scripts & automation |
| CrashPine | @CrashPine | Early schema definitions, repository structure, environment configuration, and initial project scaffolding |
| AI pair-programming | @cursoragent | Assisted implementation sessions (UI polish, bug fixes, Docker, twin viewer) |

*Note: `git shortlog -sn` shows commits under “Arsenii Harbar”, “z1nare”, “Yehor-X”, and “CrashPine”. Yehor-X and z1nare are primary active contributors.*

---

## 2. What is fully working

Features that run end-to-end on the live app, with real data and real logic:

- **Dockerized Environment** — `docker compose up --build` launches the full FastAPI + Static UI stack with all dependencies (OpenCV, Tesseract, YOLO).
- **Hybrid Blueprint Parsing** — `POST /parse` uses a multi-stage pipeline: Roboflow for geometry, Claude 3.5 Sonnet for semantic labeling, and YOLOv8 for P&ID symbols.
- **Graph Fusion Engine** — Merges architectural floor plans with MEP (Mechanical, Electrical, Plumbing) diagrams into a unified `FUSED` graph with PUE metrics.
- **3D Digital Twin Viewer** — Real-time extrusion of the parsed graph into a 3D Three.js environment (rooms, fixtures, equipment nodes).
- **Compliance Validation** — Engine in `src/compliance_checker/` runs real geometric and connectivity checks against TIA-942 standards.
- **HK Property Valuation** — `src/finance.py` computes property value and ROI based on district-level transacted price/rent data.
- **Electrical Load Summation** — `src/loads.py` performs deterministic DFS summation of downstream power loads and breaker headroom checks.

---

## 3. What is mocked, stubbed, or hardcoded

| What is faked | Where | Why we mocked it | What the real version would do |
|---|---|---|---|
| **Demo Floor Plan** | `data/demo_floorplan.json` | High-speed jury demo stability | Live-parse every upload from scratch |
| **EMSD EUI Benchmark** | `src/loads.py:14` | Lack of live EMSD API access | Fetch real-time annual EUI from government open data |
| **Market Price Tables** | `config/property_prices/*.txt` | No live API for transacted prices | Call Centaline or HK Gov APIs for daily updates |
| **Offline Rule Parser** | `src/compliance_checker/parsers/text_parser.py:267` | Fallback for when Gemini API is unavailable | Use LLM for all rule extractions from text |
| **Compliance DB** | `src/compliance_checker/database/` | Postgres is optional; defaults to SQLite | Production-grade Postgres cluster |
| **Rule Embeddings** | `src/compliance_checker/main.py:75` | Ollama local dependency | Use managed OpenAI or Anthropic embedding service |
| **Sanitary Fixture Models** | `scripts/infer.py` | Roboflow model specializes in doors/windows | Custom-trained model for all 50+ plumbing fixture types |

---

## 4. External APIs, services & data sources

| Service / API / dataset | Used for | Real call or mocked? | Auth |
|---|---|---|---|
| **Anthropic Claude 3.5** | Room labeling, coordinate extraction, OCR cleanup | **Real** | `ANTHROPIC_API_KEY` |
| **Roboflow** | Object detection (rooms, doors, windows) | **Real** | `ROBOFLOW_API_KEY` |
| **Google Gemini** | Compliance rule extraction from PDFs | **Real** (Optional) | `GEMINI_API_KEY` |
| **Ultralytics YOLOv8** | MEP / P&ID symbol detection | **Real** (Local) | None |
| **Tesseract OCR** | Text extraction from blueprints | **Real** (Local) | None |
| **HK Property Data** | Valuation and ROI calculations | **Real Data** (Static) | None |
| **Ollama** | Local semantic deduplication of rules | **Real** (Optional) | None |

---

## 5. Pre-existing code

| Item | Source | Roughly how much | License |
|---|---|---|---|
| **YOLO P&ID Model** | Trained on Kaggle pre-hackathon | ~7MB weights | Ultralytics AGPL |
| **F2 Floor Plan Asset** | Public domain architectural sample | 1 Image | Sample Asset |
| **Graph Schema** | Drafted at kickoff (`schemas/`) | ~300 lines | Project |
| **Twin Viewer Boilerplate** | Three.js orbit/scene setup | ~150 lines | MIT |

---

## 6. Known limitations & next steps

- **Accuracy of Coordinate Fallback:** When Roboflow fails, Claude's coordinate guessing can be imprecise; needs a more robust OpenCV-based geometric verification.
- **Sanitary Fixture Detection:** Default Roboflow model detects doors/windows, but not toilets/sinks; twin renders fixtures when present but detection is sparse on residential plans.
- **Handwritten / Photo Blueprints:** Red-ink annotations or low-quality photos can confuse the room detector; needs a specialized handwriting/denoising model.
- **RAG for Blueprints:** Future versions will include a conversational chat interface to "talk" to the blueprints and compliance reports.
- **Live Financials:** Integration with live real estate APIs (e.g., Centaline) to provide real-time ROI updates instead of static averages.
- **MEP Detail:** Currently focusing on major cooling/electrical nodes; next step is full wire/pipe routing pathfinding.

*Most application logic (`src/`, compliance checker, finance, Docker, static UI) was written during the hackathon window (June 6–7, 2026).*

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
