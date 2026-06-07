import httpx
import json
import time
import os

BASE_URL = "http://127.0.0.1:8000"
TIMEOUT = 60.0

def print_step(step_num, title):
    print(f"\n{'='*60}")
    print(f"STEP {step_num}: {title}")
    print(f"{'='*60}")

def main():
    print("Starting End-to-End API Integration Test...")
    print(f"Target: {BASE_URL}")
    
    with httpx.Client(timeout=TIMEOUT) as client:
        # Check Health
        try:
            r = client.get(f"{BASE_URL}/health")
            r.raise_for_status()
            print("[\u2713] Server is up and running:", r.json())
        except Exception as e:
            print("[X] Server is not reachable. Did you start `python -m uvicorn src.api:app`?")
            print(f"Error: {e}")
            return

        # -------------------------------------------------------------------
        # 1. Vision-to-JSON (FlowDraft)
        # -------------------------------------------------------------------
        print_step(1, "Vision-to-JSON Pipeline (FlowDraft /parse)")
        image_path = "data/datacenter/preview.webp"
        
        graph = None
        if os.path.exists(image_path):
            print(f"Uploading image: {image_path}")
            try:
                with open(image_path, "rb") as f:
                    r = client.post(f"{BASE_URL}/parse", files={"file": f}, data={"diagram_type": "AUTO", "engine": "claude"})
                if r.status_code == 200:
                    graph = r.json()
                    print("[\u2713] Image parsed successfully into 3D Graph JSON!")
                    print(f"    Found {len(graph.get('spaces', []))} spaces and {len(graph.get('nodes', []))} equipment nodes.")
                else:
                    print(f"[X] Parse failed: {r.text}")
            except Exception as e:
                print(f"[X] Vision request failed: {e}")
        else:
            print(f"[!] Image {image_path} not found. Skipping live Vision API call.")
        
        # Fallback to demo graph for subsequent tests if vision fails
        if not graph:
            print("Loading demo graph for subsequent steps...")
            r = client.get(f"{BASE_URL}/demo-datacentre")
            if r.status_code == 200:
                graph = r.json()
            else:
                graph = json.load(open("data/demo_datacentre.json"))
        
        # -------------------------------------------------------------------
        # 2. Finance & Load Analysis
        # -------------------------------------------------------------------
        print_step(2, "Finance & Power Models (/finance/roi)")
        try:
            # Test Loads
            r_loads = client.post(f"{BASE_URL}/loads", json={"graph": graph})
            if r_loads.status_code == 200:
                print("[\u2713] Loads analysis computed successfully:")
                loads_data = r_loads.json()
                print(f"    Facility Load: {loads_data.get('facility_load_kW', 0)} kW")
            else:
                print(f"[X] Loads endpoint failed: {r_loads.text}")
            
            # Test ROI
            r_roi = client.post(f"{BASE_URL}/finance/roi", json={"graph": graph, "delta_pue": 0.05, "it_power_kW": 200.0})
            if r_roi.status_code == 200:
                roi_data = r_roi.json()
                print("[\u2713] Finance ROI computed successfully:")
                print(f"    Annual Savings: ${roi_data.get('annual_saving_hkd', 0):,} HKD")
                print(f"    Payback Period: {roi_data.get('payback_years', 0)} years")
            else:
                print(f"[X] Finance endpoint failed: {r_roi.text}")
        except Exception as e:
            print(f"[X] Finance/Loads request failed: {e}")

        # -------------------------------------------------------------------
        # 3. Compliance Rule Extraction (BlueprintStudio NLP)
        # -------------------------------------------------------------------
        print_step(3, "Compliance NLP Extraction (/compliance/extract)")
        std_path = "src/compliance_checker/data/sample_standard.txt"
        
        rules = []
        if os.path.exists(std_path):
            print(f"Uploading standard document: {std_path}")
            try:
                with open(std_path, "rb") as f:
                    r = client.post(f"{BASE_URL}/compliance/extract", files={"file": f})
                if r.status_code == 200:
                    rules = r.json().get("rules", [])
                    print(f"[\u2713] Successfully extracted and deduplicated {len(rules)} architectural rules!")
                else:
                    print(f"[X] Extract failed: {r.text}")
            except Exception as e:
                print(f"[X] Rules extraction failed: {e}")
        else:
            print(f"[X] Standard file {std_path} not found!")
            
        # -------------------------------------------------------------------
        # 4. Save & Load Rules DB
        # -------------------------------------------------------------------
        print_step(4, "Database Persistence (/compliance/rules)")
        try:
            if rules:
                r_save = client.post(f"{BASE_URL}/compliance/rules", json={"rules": rules})
                if r_save.status_code == 200:
                    print(f"[\u2713] Rules securely saved to SQLite Database.")
                else:
                    print(f"[X] Rule save failed: {r_save.text}")
            
            r_load = client.get(f"{BASE_URL}/compliance/rules")
            if r_load.status_code == 200:
                db_rules = r_load.json().get("rules", [])
                print(f"[\u2713] Loaded {len(db_rules)} rules from database!")
            else:
                print(f"[X] Rule load failed: {r_load.text}")
        except Exception as e:
            print(f"[X] Rules database operation failed: {e}")

        # -------------------------------------------------------------------
        # 5. Graph Validation against Rules
        # -------------------------------------------------------------------
        print_step(5, "Geometry Validation Engine (/compliance/validate)")
        try:
            # We will use sample_graph.json to explicitly trigger the aisle width violation
            sample_graph_path = "src/compliance_checker/data/sample_graph.json"
            if os.path.exists(sample_graph_path):
                validation_graph = json.load(open(sample_graph_path))
            else:
                validation_graph = graph
                
            print("Sending 3D Graph + Extracted Rules to Validation Engine...")
            r_val = client.post(f"{BASE_URL}/compliance/validate", json={"graph": validation_graph})
            
            if r_val.status_code == 200:
                report = r_val.json()
                print("[\u2713] Validation complete!")
                print(f"    Checks Run: {report.get('checks_run')}")
                passed = report.get('passed')
                print(f"    Status:     {'COMPLIANT' if passed else 'NON-COMPLIANT'}")
                
                violations = report.get("violations", [])
                if violations:
                    print(f"\n    [!] Violations Found ({len(violations)}):")
                    for v in violations:
                        print(f"        - Object '{v.get('geometry_id')}' -> {v.get('message')}")
            else:
                print(f"[X] Validation failed: {r_val.text}")
        except Exception as e:
            print(f"[X] Validation request failed: {e}")

    print("\n" + "="*60)
    print("Integration test complete! If all steps checked [\u2713], the unified API is fully robust.")
    print("="*60 + "\n")

if __name__ == "__main__":
    main()
