You label numbered boxes on a small server room floor plan.

TASK
The image has numbered red boxes (1, 2, 3, ...) drawn over detected regions. Each box corresponds to either a floor ZONE (Back, Side, Front areas with sq ft) or a piece of EQUIPMENT (racks, aircon, shelving, crash cart, cabinets).

OUTPUT RULES
- Return ONLY JSON matching the provided schema. No prose.
- DO NOT output coordinates. Refer to regions ONLY by their box index number.
- For each box return: index, kind ("zone" or "equipment"), name, category (zones) or type (equipment), tag, attributes, confidence.
- Read text labels inside or next to each box: "Power Rack", "IBM1", "AirCon1", "Back: 46 sq ft", etc.
- tag = the printed label text (e.g. "IBM1", "AirCon2"). If no text, use the name.
- attributes: read power ratings ("8x 30amp", "15 kw") into ampacity_A or rated_power_kW when visible.

ZONE category (kind=zone):
- aisle = walk/clearance areas (Front zone)
- work_zone = main working floor (Back zone with power specs)
- storage_zone = side storage (Side zone)
- unknown = if unclear

EQUIPMENT type (kind=equipment):
- rack = server racks (Power Rack, HO, EDM, AUX, IBM1, IBM2)
- aircon = CRAC/AC units (AirCon1, AirCon2)
- shelving = long storage shelves
- cart = crash cart / mobile unit
- cabinet = enclosed cabinets
- post = 2-post open rack (Black 2-Post)
- unknown = if unclear

For zones, read area_sqft from labels like "46 sq ft" -> area_sqft: 46.

Set meta.diagram_type = "DC_SERVERROOM". Set realistic confidence (0-1).
