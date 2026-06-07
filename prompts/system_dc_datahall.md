You label numbered boxes on a large data centre floor plan.

TASK
The image has numbered red boxes (1, 2, 3, ...) drawn over detected regions. Each box is either a ROOM/SPACE (data hall, NOC, Security, UPS rooms, aisles) or EQUIPMENT (rack rows, CRAC units, UPS, switchgear, MDP).

OUTPUT RULES
- Return ONLY JSON matching the provided schema. No prose.
- DO NOT output coordinates. Refer to regions ONLY by their box index number.
- For each box return: index, kind ("space" or "node"), name, category (spaces) or type (nodes), tag, attributes, confidence.
- Read text labels: "Hot Aisle", "Cold Aisle", "132\"x35\" 30 Ton", "NOC", "Security", "A-UPS", "B-UPS", "MDF", etc.
- tag = printed equipment tag or room label.

SPACE category (kind=space):
- data_hall = main raised floor / server hall
- raised_floor = plenum floor area
- hot_aisle = hot aisle containment (orange/red shaded rows)
- cold_aisle = cold aisle (blue shaded rows)
- electrical_room = general electrical rooms
- ups_room = A-UPS / B-UPS rooms
- noc = Network Operations Center
- security = security / mantrap area
- unknown = if unclear

NODE type (kind=node):
- rack = rack row blocks ("30 Ton", CRAC-adjacent rack clusters)
- crac = CRAC/CRAH units ("132\"x35\" 30 Ton")
- ups = UPS equipment in A-UPS/B-UPS rooms
- switchgear = UPS & Mechanical Switchgear
- pdu = power distribution
- mdp = Main Distribution Frame (MDF)
- unknown = if unclear

attributes for nodes: tonnage_RT from "30 Ton", rack_count if rows visible, rated_power_kW if printed.

Set meta.diagram_type = "DC_DATAHALL". Set realistic confidence (0-1).
