You label numbered boxes on a cooling and power schematic diagram.

TASK
The image has numbered red boxes (1, 2, 3, ...) drawn over detected equipment symbols and room boundaries. This is a topological cooling/power diagram (PAC, cooling tower, CRAH, PDU, UPS, server room).

OUTPUT RULES
- Return ONLY JSON matching the provided schema. No prose.
- DO NOT output coordinates. Refer to regions ONLY by their box index number.
- For each box return: index, kind ("node" or "space"), name, type (nodes) or category (spaces), tag, attributes, confidence.
- Read French/English labels: PAC, TOUR, CRAH, PDU, UPS, AC/DC, DC/AC, "Salle informatique", "Réseau électrique".

NODE type (kind=node):
- chiller = PAC / heat pump (Pompe à Chaleur)
- cooling_tower = TOUR / tour de refroidissement
- pump = circulation pumps on pipe lines
- pdu = Power Distribution Unit
- ups = UPS backup
- transformer = grid transformer
- ac_dc = AC/DC converter blocks
- dc_dc = DC/DC converter blocks
- server_room = "Salle informatique" enclosure (if boxed as a region)
- unknown = if unclear

SPACE category (kind=space):
- data_hall = IT room / salle informatique
- electrical_room = electrical room enclosure
- unknown = if unclear

EDGES (also return edges[] array):
- Identify pipe/cable connections between labeled nodes.
- edge.type: chw_supply, chw_return, condenser_water, air_duct, electrical_cable, control_signal, unknown.
- edge.from / edge.to: refer to node tags or ids you assigned.
- Return at least the main cooling loop (PAC -> TOUR -> CRAH -> racks) and electrical path (grid -> UPS -> PDU -> racks).

Set meta.diagram_type = "COOLING_PID". Set realistic confidence (0-1).
