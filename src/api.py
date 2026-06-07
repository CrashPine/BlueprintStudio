"""
ArchDraft x FlowDraft API — unified parse, fusion, PUE, compliance.
Run: python -m uvicorn src.api:app --reload
"""

import json
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

app = FastAPI(title="ArchDraft x FlowDraft API")

# Mount static files for frontend UI
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

@app.get("/", response_class=HTMLResponse)
def index():
    path = ROOT / "static" / "index.html"
    if path.exists():
        return path.read_text(encoding="utf-8")
    return "<h1>Frontend missing</h1><p>Please build the static frontend.</p>"


class FuseRequest(BaseModel):
    arch_graph: Dict[str, Any]
    mep_graph: Dict[str, Any]
    diagram_id: str = "fused-01"


class WhatIfRequest(BaseModel):
    graph: Dict[str, Any]
    node_id: str
    new_attributes: Dict[str, Any]


class RoiRequest(BaseModel):
    graph: Dict[str, Any]
    delta_pue: Optional[float] = None
    it_power_kW: Optional[float] = None


class PropertyEstimateRequest(BaseModel):
    graph: Dict[str, Any]
    district: str
    estate: Optional[str] = None


@app.get("/health")
def health():
    return {"ok": True, "product": "ArchDraft x FlowDraft"}


@app.post("/parse")
async def parse(
    file: UploadFile = File(...),
    diagram_type: str = "AUTO",
    conf: float = 0.15,
    mep_type: str = "PID",
    engine: str = "claude",
    handwriting: bool = False,
):
    """
    Unified parse router.
    diagram_type: AUTO | FLOORPLAN | PID | SLD
    engine: claude (primary, YOLO fallback) | cv-hybrid (Roboflow rooms + Claude
            for floor plans) | yolo (legacy) | auto
    """
    if not 0.0 < conf <= 1.0:
        raise HTTPException(400, "conf must be between 0 and 1")
    if engine not in ("claude", "yolo", "auto", "cv-hybrid"):
        raise HTTPException(400, "engine must be claude, cv-hybrid, yolo, or auto")

    suffix = Path(file.filename or "upload.png").suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".pdf", ".tif", ".tiff"):
        raise HTTPException(400, "Need png, jpg, webp, or pdf")

    data = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    try:

        from src import fusion as fusion
        graph = fusion.parse_unified(
            tmp_path, diagram_type=diagram_type, conf=conf, mep_type=mep_type,
            engine=engine, handwriting=handwriting,
        )
        # Datacenter graphs are validated against their own per-style schema
        # inside the parser; only re-validate FLOORPLAN/PID/SLD against graph.schema.
        dtype = str(graph.get("meta", {}).get("diagram_type", "")).upper()
        if not dtype.startswith("DC_") and dtype not in ("COOLING_PID",):
            from src import build_graph as build
            build.validate_graph(graph)
        return graph
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


@app.post("/fuse")
def fuse_graphs(req: FuseRequest):
    """Merge floor-plan graph + MEP graph into FUSED."""

    from src import fusion as fusion
    from src import build_graph as build
    from src import pue as pue_mod

    fused = fusion.fuse_graphs(req.arch_graph, req.mep_graph, diagram_id=req.diagram_id)
    pue_result = pue_mod.compute_pue(fused)
    fused["meta"]["pue"] = pue_result["pue"]
    fused["meta"]["it_power_kW"] = pue_result["it_power_kW"]
    fused["meta"]["facility_power_kW"] = pue_result["facility_power_kW"]
    fused["meta"]["target_pue"] = pue_result["target_pue"]
    build.validate_graph(fused)
    return fused


@app.get("/schema")
def get_schema():
    path = ROOT / "schemas" / "graph.schema.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/demo-graph")
def get_demo_graph():
    path = ROOT / "data" / "demo_graph.json"
    if not path.exists():
        raise HTTPException(404, "demo_graph.json not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/demo-datacentre")
def get_demo_datacentre():
    path = ROOT / "data" / "demo_datacentre.json"
    if not path.exists():
        raise HTTPException(404, "demo_datacentre.json not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/demo/floorplan")
def get_demo_floorplan():
    """Frozen F2 floor-plan graph + pre-baked overlay for stage-safe demos."""
    graph_path = ROOT / "data" / "demo_floorplan.json"
    if not graph_path.exists():
        raise HTTPException(404, "demo_floorplan.json not found — run infer on F2_original.png")
    with open(graph_path, "r", encoding="utf-8") as f:
        graph = json.load(f)
    return {
        "graph": graph,
        "overlay_url": "/static/demo/f2_overlay.png",
        "source_image": "/static/demo/f2_source.png",
        "demo": True,
    }


@app.get("/demo/compliance-graph")
def get_demo_compliance_graph():
    """Sample datacenter graph with deliberate TIA-942 violations."""
    path = ROOT / "src" / "compliance_checker" / "data" / "sample_graph.json"
    if not path.exists():
        raise HTTPException(404, "sample_graph.json not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.get("/demo/compliance-report")
def get_demo_compliance_report():
    """Precomputed TIA-942 validation report (sample_graph.json)."""
    path = ROOT / "data" / "demo_compliance_report.json"
    if not path.exists():
        raise HTTPException(404, "demo_compliance_report.json not found")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.post("/overlay")
async def render_overlay(
    file: UploadFile = File(...),
    graph_json: str = Form(...),
    max_labels: int = 80,
):
    """Render graph overlay on uploaded source image; returns PNG bytes."""
    import importlib.util

    try:
        graph = json.loads(graph_json)
    except json.JSONDecodeError as e:
        raise HTTPException(400, f"Invalid graph_json: {e}")

    suffix = Path(file.filename or "upload.png").suffix.lower()
    if suffix not in (".png", ".jpg", ".jpeg", ".webp", ".tif", ".tiff"):
        raise HTTPException(400, "Need png, jpg, or webp")

    data = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as img_tmp:
        img_tmp.write(data)
        img_path = img_tmp.name
    out_path = img_path + ".overlay.png"

    try:
        spec = importlib.util.spec_from_file_location(
            "viz", ROOT / "scripts" / "visualize.py"
        )
        viz = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(viz)
        viz.overlay_graph(img_path, graph, out_path=out_path, max_labels=max_labels)
        png = Path(out_path).read_bytes()
        return Response(content=png, media_type="image/png")
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        Path(img_path).unlink(missing_ok=True)
        Path(out_path).unlink(missing_ok=True)


@app.post("/compliance/init-demo")
def compliance_init_demo():
    """Extract TIA-942 rules from bundled sample_standard.txt and persist to DB."""
    from src.compliance_checker.main import get_rules_for_document, dedupe_rules
    from src.compliance_checker.database.setup import init_db, save_rules

    std_path = ROOT / "src" / "compliance_checker" / "data" / "sample_standard.txt"
    if not std_path.exists():
        raise HTTPException(404, "sample_standard.txt not found")
    rules = get_rules_for_document(str(std_path), reparse=True)
    rules = dedupe_rules(rules)
    session_factory = init_db()
    save_rules(session_factory, rules)
    return {"ok": True, "rules_loaded": len(rules)}


@app.get("/compliance/pue")
def compliance_pue(diagram_id: str = "demo-dc-01"):
    path = ROOT / "data" / "demo_datacentre.json"
    if not path.exists():
        raise HTTPException(404, "demo_datacentre.json not found")
    with open(path, "r", encoding="utf-8") as f:
        graph = json.load(f)
    if graph.get("meta", {}).get("diagram_id") != diagram_id:
        pass  # still evaluate provided file for hackathon

    from src import pue as pue_mod
    return pue_mod.evaluate_bec(graph)


@app.post("/loads")
def loads(req: RoiRequest):
    """Deterministic electrical loads + breaker headroom + EMSD benchmark."""

    from src import loads as loads_mod
    return loads_mod.analyze(req.graph)


@app.post("/whatif")
def whatif(req: WhatIfRequest):

    from src import pue as pue_mod
    return pue_mod.whatif_pue(req.graph, req.node_id, req.new_attributes)


@app.get("/finance/districts")
def finance_districts(district: Optional[str] = None):
    """Hong Kong districts (and optional estate list) for property valuation."""
    from src import finance as fin_mod

    districts = fin_mod.list_districts()
    if district:
        if district not in districts:
            raise HTTPException(400, f"Unknown district. Choose from: {districts}")
        return {"district": district, "estates": fin_mod.list_estates(district)}
    return {"districts": districts}


@app.post("/finance/property")
def finance_property(req: PropertyEstimateRequest):
    """Estimate HK property value and rental ROI from a parsed floor-plan graph."""
    from src import finance as fin_mod

    try:
        return fin_mod.estimate_from_graph(req.graph, req.district, req.estate)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError as e:
        raise HTTPException(503, str(e))


@app.post("/finance/roi")
def finance_roi(req: RoiRequest):
    """ROI from PUE improvement: delta_PUE x IT x hours x tariff."""

    from src import pue as pue_mod
    rules = pue_mod.load_rules()
    fin = rules.get("finance") or {}
    tariff = float(fin.get("tariff_hkd_per_kwh", 1.2))
    hours = float(fin.get("hours_per_year", 8760))

    pue_before = pue_mod.compute_pue(req.graph)
    it = req.it_power_kW or pue_before["it_power_kW"]
    delta = req.delta_pue
    if delta is None:
        delta = float(fin.get("chw_temp_raise_pue_delta", 0.03))

    # facility saving ≈ delta_pue * IT * hours (kWh) * tariff
    annual_kwh_saved = delta * it * hours
    annual_saving_hkd = annual_kwh_saved * tariff
    capex_hkd = 250000  # illustrative containment / controls upgrade
    payback = capex_hkd / annual_saving_hkd if annual_saving_hkd > 0 else 99

    return {
        "capex_hkd": capex_hkd,
        "annual_saving_hkd": round(annual_saving_hkd, 0),
        "payback_years": round(payback, 1),
        "delta_pue": delta,
        "it_power_kW": it,
        "assumptions": {"tariff_hkd_per_kwh": tariff, "hours_per_year": hours},
    }

# ---------------------------------------------------------------------------
# Compliance Checker (Unified from BlueprintStudio)
# ---------------------------------------------------------------------------

@app.post("/compliance/extract")
async def extract_rules(file: UploadFile = File(...)):
    """Extract rules from a standard PDF/TXT document."""
    from src.compliance_checker.main import get_rules_for_document, dedupe_rules
    
    suffix = Path(file.filename or "upload.txt").suffix.lower()
    data = await file.read()
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
        
    try:
        rules = get_rules_for_document(tmp_path, reparse=True)
        rules = dedupe_rules(rules)
        return {"rules": [r.model_dump() for r in rules]}
    except Exception as e:
        raise HTTPException(500, str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)


class RulesRequest(BaseModel):
    rules: list[Dict[str, Any]]


@app.post("/compliance/rules")
def save_rules_db(req: RulesRequest):
    """Save extracted rules to the local SQLite database."""
    from src.compliance_checker.database.setup import init_db, save_rules
    from src.compliance_checker.engine.rules import Rule
    try:
        rules = [Rule.model_validate(r) for r in req.rules]
        session_factory = init_db()
        save_rules(session_factory, rules)
        return {"ok": True, "saved": len(rules)}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/compliance/rules")
def load_rules_db():
    """Load active rules from the local SQLite database."""
    from src.compliance_checker.database.setup import init_db, load_rules
    try:
        session_factory = init_db()
        rules = load_rules(session_factory)
        return {"rules": [r.model_dump() for r in rules]}
    except Exception as e:
        raise HTTPException(500, str(e))


class ValidateRequest(BaseModel):
    graph: Dict[str, Any]


@app.post("/compliance/validate")
def validate_graph(req: ValidateRequest):
    """Validate a 3D graph against stored compliance rules."""
    from src.compliance_checker.database.setup import init_db, load_rules
    from src.compliance_checker.parsers.graph_parser import parse_graph_dict
    from src.compliance_checker.engine.validator import validate as engine_validate
    
    try:
        session_factory = init_db()
        db_rules = load_rules(session_factory)
        
        objects = parse_graph_dict(req.graph)
        report = engine_validate(db_rules, objects)
        
        violations = []
        for v in report.violations:
            violations.append({
                "rule": v.rule.model_dump(),
                "geometry_id": v.geometry_id,
                "message": v.message
            })
            
        return {
            "checks_run": report.checks_run,
            "passed": report.passed,
            "violations": violations
        }
    except Exception as e:
        raise HTTPException(500, str(e))
