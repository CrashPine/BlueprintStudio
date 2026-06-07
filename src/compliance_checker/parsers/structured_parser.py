import json
from src.compliance_checker.engine.rules import Rule

def parse_structured(path: str) -> tuple[list[Rule], dict]:
    """Parse rules from a structured JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    
    items = data.get("rules", []) if isinstance(data, dict) else data if isinstance(data, list) else []
    rules = []
    for item in items:
        try:
            rules.append(Rule.model_validate(item))
        except Exception as e:
            print(f"[structured_parser] Skipping invalid rule: {e}")
    
    print(f"[structured_parser] Extracted {len(rules)} rules from {path}")
    return rules, {"engine": "structured", "complete": True}
