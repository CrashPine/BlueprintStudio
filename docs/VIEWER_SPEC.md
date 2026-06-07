# Person B — Unified 3D Digital Twin Viewer Spec

**Owner:** 3D / Frontend Engineer  
**Consumes:** Graph JSON v2 from `GET /demo-datacentre` or live `POST /parse` + `POST /fuse`  
**Stack:** React + Vite, `react-three-fiber`, `@react-three/drei`

---

## 1. Scene structure

```
Scene
├── BuildingGroup (local origin)
│   ├── FloorSlabs[]          ← from spaces[].polygon_2d extruded
│   ├── WallMeshes[]          ← from walls[].polyline_2d (optional)
│   ├── SpaceLabels[]         ← spaces[].name at polygon centroid
│   ├── MepNodes[]            ← nodes[] positioned inside space_id room
│   ├── MepEdges[]            ← TubeGeometry along edge polylines
│   └── GhostNodes[]          ← semi-transparent what-if duplicates
├── OrbitControls
└── UI overlays (side panel, legend, upload)
```

---

## 2. Loading the graph

```typescript
// Primary demo path
const res = await fetch('/demo-datacentre');
const graph: UnifiedGraph = await res.json();

// Live path
const form = new FormData();
form.append('file', floorPlanFile);
const arch = await fetch('/parse?diagram_type=FLOORPLAN', { method: 'POST', body: form });
// ... then MEP parse, then POST /fuse
```

Required fields: `meta`, `spaces[]`, `nodes[]`, `edges[]`.

---

## 3. Room rendering (`spaces[]`)

Each space:

| Field | 3D use |
|-------|--------|
| `polygon_2d` | `[[x,y],...]` normalized 0–1 → multiply by `meta.image_width/height` for local X/Y |
| `floor` | Z offset: `G=0`, `L1=3`, `B1=-3` (meters, configurable) |
| `category` | Color: `data_hall`=#e8f4fc, `electrical_room`=#fff3cd, `plant_room`=#d4edda |
| `area_m2` | Show in side panel on click |
| `name` | `Html` label at polygon centroid |

**Extrusion algorithm:**

1. Convert `polygon_2d` to local vertices `(x * W, y * H, z_floor)`.
2. `THREE.Shape` from X/Y points → `ExtrudeGeometry` height `0.15` m (slab).
3. Optional: vertical quads along wall polylines from `walls[]`.

**Click handler:** raycast slab → set `selectedSpaceId` → panel shows `name`, `area_m2`, `category`, summed IT load from nodes with matching `space_id`.

---

## 4. MEP rendering (`nodes[]`, `edges[]`)

### Positioning

If `node.space_id` is set:
- Place glyph at **room centroid** + small offset grid (avoid overlap).
- Use `attributes.floor` for Z if `space_id` missing.

If no `space_id`:
- Use `bbox_2d` center × image dimensions (legacy P&ID layout mode).

### Glyphs by `node.type`

| type | Geometry | Color |
|------|----------|-------|
| `crac`, `crah` | Box 1×0.6×2 m | cyan |
| `chiller` | Box 2×1×1.5 m | blue |
| `pump` | Cylinder r=0.3 h=0.5 | green |
| `ups`, `pdu` | Box 0.8×0.6×1.2 | orange |
| `busway` | Flat box 2×0.2×0.4 | yellow |
| `rack` | Box 0.6×1×2 | gray |
| `transformer`, `breaker` | Box variants | red/purple |

### Edges

- `CatmullRomCurve3` through `polyline_2d` points (or straight line center-to-center).
- `TubeGeometry`: radius ∝ `attributes.diameter_mm` or fixed 0.05 m.
- Color by `edge.type`: `chw_supply`=#00bcd4, `electrical_cable`=#ff9800.

---

## 5. System toggles

Partition edges by type; toggle visibility:

- Cooling (`chw_*`, `condenser_water`)
- Air (`air_duct`)
- Electrical (`electrical_cable`)
- Control (`control_signal`)

---

## 6. Ghost what-if mode

1. User selects node (e.g. chiller `N-CH-1`).
2. Clone mesh at 50% opacity; side panel edits `rated_power_kW`, `efficiency.COP`.
3. `POST /whatif` with `{ node_id, new_attributes }`.
4. Recolor downstream edges: green=OK, red=overload (from response `limiting_node`).
5. `POST /finance/roi` → show payback in panel.

---

## 7. PUE / compliance panel

`GET /compliance/pue?diagram_id=demo-dc-01` returns:

```json
{
  "pue": 1.42,
  "it_power_kW": 2000,
  "facility_power_kW": 2840,
  "target_pue": 1.2,
  "status": "FAIL",
  "clauses": [{ "clause": "PUE-DC-01", "status": "FAIL", "actual": 1.42, "limit": 1.2 }]
}
```

Display: large PUE gauge; pass/fail badge; clause table on equipment click.

---

## 8. Upload flow

1. **Step 1:** Upload floor plan → `POST /parse?diagram_type=FLOORPLAN` → preview rooms overlay on 2D thumbnail.
2. **Step 2:** Upload P&ID/SLD → `POST /parse?diagram_type=PID` → preview symbols.
3. **Step 3:** `POST /fuse` with both JSON bodies → refresh 3D scene.

**Fallback toggle:** checkbox “Use demo datacentre” → `GET /demo-datacentre` (no live parse).

---

## 9. Camera & demo polish

- Default camera: isometric, frames full building bounding box.
- “Reset camera” button.
- Legend: room colors + system pipe colors.
- Pre-record backup screen capture of happy path (H30).

---

## 10. Acceptance criteria (done when)

- [ ] `demo_datacentre.json` loads without errors
- [ ] ≥3 room slabs visible with labels
- [ ] ≥5 MEP nodes inside correct rooms (`space_id`)
- [ ] Click room → area; click equipment → attributes
- [ ] Ghost chiller swap triggers edge recolor + ROI panel update
- [ ] Upload → parse → 3D refresh (or fallback toggle works)
