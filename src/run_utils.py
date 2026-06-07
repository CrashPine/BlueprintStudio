"""Load src modules by filename (supports legacy numbered names)."""

import importlib.util
from pathlib import Path

SRC = Path(__file__).parent

# Legacy numbered names → current filenames (see refactor.py)
_LEGACY_NAMES = {
    "01_preprocess.py": "preprocess.py",
    "02_detect_yolo.py": "detect_yolo.py",
    "03_extract_text.py": "extract_text.py",
    "04_build_graph.py": "build_graph.py",
    "05_parse_floorplan.py": "parse_floorplan.py",
    "06_fusion.py": "fusion.py",
    "07_claude_parse.py": "claude_parse.py",
    "08_datacenter.py": "datacenter.py",
    "09_floorplan_hybrid.py": "floorplan_hybrid.py",
    "10_pid_hybrid.py": "pid_hybrid.py",
}


def load_src(filename: str):
    resolved = _LEGACY_NAMES.get(filename, filename)
    path = SRC / resolved
    if not path.is_file():
        raise FileNotFoundError(
            f"Module not found: {path} (requested as {filename!r})"
        )
    mod_name = "flowdraft_" + resolved.replace(".py", "")
    spec = importlib.util.spec_from_file_location(mod_name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
