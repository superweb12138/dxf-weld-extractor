"""
European-standard weld extraction entry points and BOM/catalog helpers.

GB-specific logic (COMP_CONFIG, Unknown BOM, etc.) must NOT live here.
Shared geometry still runs via weld_extractor.extract_welds with standard='eu'.
"""
from __future__ import annotations

import json
import os
import re

FOLDER = os.path.dirname(os.path.abspath(__file__))
EU_SECTIONS_DEFAULT = os.path.join(FOLDER, "eu_sections.json")

_EU_CATALOG_CACHE = {}


def discover_eu_view_roles(doc):
    """
    Map Tekla view_id → section letter / main elev using drawing labels.

    - Unknown-*… - {vid} containing a single letter A–H → that view is A-A / B-B / …
    - SectionMark-*… - {vid} → that view is the main elevation (cut markers)

    Returns
    -------
    {
      'letter_by_view': {view_id: 'A'|'B'|…},
      'view_by_letter': {'A': view_id, …},
      'main_views': set(view_id),
    }
    """
    letter_by_view = {}
    main_views = set()

    for blk in doc.blocks:
        name = blk.name or ''
        m = re.search(r' - (\d+)$', name)
        if not m:
            continue
        vid = m.group(1)
        letters = []
        for e in blk:
            t = ''
            if e.dxftype() == 'TEXT':
                t = (e.dxf.text or '').strip()
            elif e.dxftype() == 'MTEXT':
                t = (e.plain_text() if hasattr(e, 'plain_text') else e.text or '')
                t = t.strip()
            else:
                continue
            if re.match(r'^[A-Ha-h]$', t):
                letters.append(t.upper())
        if not letters:
            continue
        # Prefer first letter occurrence
        letter = letters[0]
        if name.startswith('SectionMark'):
            main_views.add(vid)
        elif name.startswith('Unknown-'):
            # Section-view title block
            if vid not in letter_by_view:
                letter_by_view[vid] = letter

    view_by_letter = {L: v for v, L in letter_by_view.items()}
    return {
        'letter_by_view': letter_by_view,
        'view_by_letter': view_by_letter,
        'main_views': main_views,
    }


def load_eu_catalog(path=None):
    path = path or EU_SECTIONS_DEFAULT
    if path in _EU_CATALOG_CACHE:
        return _EU_CATALOG_CACHE[path]
    data = {"by_catalog": {}, "aliases": {}}
    if path and os.path.isfile(path):
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
    _EU_CATALOG_CACHE[path] = data
    return data


def normalize_eu_profile(profile):
    """HEA300 → HE300A; HEB300 → HE300B; pass through UPN/IPE."""
    if not profile:
        return None
    p = profile.strip().upper().replace(' ', '')
    m = re.match(r'^HE([ABM])(\d+)$', p)
    if m:
        series, num = m.group(1), m.group(2)
        return f'HE{num}{series}'
    m = re.match(r'^HE(\d+)([ABM])$', p)
    if m:
        return f'HE{m.group(1)}{m.group(2)}'
    return p


def lookup_eu_section(profile, catalog_path=None):
    """Return {depth, flange_w, web_t, flange_t} for a BOM/EU profile name."""
    if not profile:
        return {}
    cat = load_eu_catalog(catalog_path)
    key = profile.strip().upper().replace(' ', '')
    catalog_name = cat.get('aliases', {}).get(key) or normalize_eu_profile(key)
    dims = cat.get('by_catalog', {}).get(catalog_name)
    if not dims:
        return {}
    return {
        'depth': float(dims['depth']),
        'flange_w': float(dims['flange_w']),
        'web_t': float(dims['web_t']),
        'flange_t': float(dims['flange_t']),
        'catalog_name': catalog_name,
        'bom_profile': key,
    }


PART_RE_EU = re.compile(r'^[sS]?[pP]\d+$|^A\d{4}$|^[A-Z]{2,3}\d+$|^\d{3,}$')


def _part_bbox_mm(lines, scale=10.0):
    if not lines:
        return 0.0, 0.0
    xs = [p[0] for ln in lines for p in (ln['start'], ln['end'])]
    ys = [p[1] for ln in lines for p in (ln['start'], ln['end'])]
    return (max(xs) - min(xs)) * scale, (max(ys) - min(ys)) * scale


def _bbox_match_score(bw, bh, plate_w, plate_l, tol=0.20):
    """
    Score how well a Part bbox (bw x bh mm) matches a BOM plate (w x L).
    Considers edge-on views where one dim ≈ thickness (~plate thick side)
    and the long dim ≈ width or length.
    Returns ratio error (lower better), or None if no match.
    """
    if not plate_w or plate_w <= 0:
        return None
    dims = sorted([bw, bh])
    long_d, short_d = max(bw, bh), min(bw, bh)
    candidates = []
    # Face view: bbox ≈ width x length
    if plate_l and plate_l > 0:
        candidates.append(max(abs(long_d - max(plate_w, plate_l)) / max(plate_w, plate_l, 1),
                              abs(short_d - min(plate_w, plate_l)) / max(min(plate_w, plate_l), 1)))
        # Edge view: long ≈ length or width, short ≈ thickness (ignore short strictly)
        candidates.append(abs(long_d - plate_l) / max(plate_l, 1))
        candidates.append(abs(long_d - plate_w) / max(plate_w, 1))
    else:
        candidates.append(abs(long_d - plate_w) / max(plate_w, 1))
        candidates.append(abs(short_d - plate_w) / max(plate_w, 1))
    best = min(candidates)
    return best if best <= tol else None


def assign_eu_unlabeled_from_bom(part_number_map, part_lines_map, part_dims, comp, scale=10.0):
    """
    EU-only: map unlabeled Part blocks to BOM plates not yet shown in that view.

    Typical AT assemblies label the plate in a detail view while the weld view
    only labels the main UPN — the plate Part stays unlabeled and becomes a
    false self-weld (AT/AT).  Match by bbox vs BOM width/length.
    """
    if not part_dims:
        return part_number_map, []

    bom_plates = [k for k in part_dims.keys() if k != comp]
    if not bom_plates:
        return part_number_map, []

    assigned = []
    # Cross-view: dims already known for labeled plates
    for view_id, view_parts in part_lines_map.items():
        unlabeled = [pn for pn in view_parts if pn not in part_number_map]
        if not unlabeled:
            continue
        used_here = {part_number_map[pn] for pn in view_parts if pn in part_number_map}
        available = [b for b in bom_plates if b not in used_here]
        if not available:
            continue

        scored = []
        for up in unlabeled:
            bw, bh = _part_bbox_mm(view_parts[up], scale)
            for plbl in available:
                pd = part_dims[plbl]
                sc = _bbox_match_score(bw, bh, pd.get('width'), pd.get('bom_len'))
                if sc is not None:
                    scored.append((sc, up, plbl))
        scored.sort()
        used_up, used_pl = set(), set()
        for sc, up, plbl in scored:
            if up in used_up or plbl in used_pl:
                continue
            part_number_map[up] = plbl
            used_up.add(up)
            used_pl.add(plbl)
            assigned.append((view_id, up, plbl, round(sc, 3)))

    return part_number_map, assigned


def parse_bom_eu(doc, comp, catalog_path=None):
    """Parse Tekla PART_LIST BOM for EU drawings (PL* / HEA / UPN)."""
    from weld_extractor import _collect_bom_rows_from_block, _bom_row_qty_len

    part_dims = {}
    comp_dims = {}
    eu_prof_re = re.compile(
        r'^(?:PL\d|HE[ABM]\d|HE\d+[ABM]|IPE\d|IPN\d|UPN\d|UPE\d|UB\d|UC\d)',
        re.I)

    blocks = [b for b in doc.blocks if 'PART_LIST' in b.name]
    if not blocks:
        blocks = [b for b in doc.blocks
                  if b.name.startswith('Unknown') and ' - ' not in b.name]

    for blk in blocks:
        found_any = False
        for vals in _collect_bom_rows_from_block(blk):
            mark = next(
                (v for v in vals
                 if re.match(r'^(?:sp|p)\d+$', v, re.I)
                 or re.match(r'^A\d{4}$', v, re.I)
                 or v == comp),
                None)
            spec = next((v for v in vals if eu_prof_re.match(v)), None)
            if not (mark and spec):
                continue
            found_any = True
            qty, bom_len = _bom_row_qty_len(vals, spec)

            pm = re.match(r'PL(\d+(?:\.\d+)?)[*xX×](\d+(?:\.\d+)?)', spec, re.I)
            if pm:
                t, w = float(pm.group(1)), float(pm.group(2))
                if bom_len and w > 0 and bom_len > w * 4:
                    bom_len = None
                part_dims[mark] = {'thick': t, 'width': w, 'bom_len': bom_len, 'qty': qty}
                continue

            dims = lookup_eu_section(spec, catalog_path)
            if dims and mark == comp:
                comp_dims = {
                    'depth': dims['depth'],
                    'flange_w': dims['flange_w'],
                    'web_t': dims['web_t'],
                    'flange_t': dims['flange_t'],
                }
                part_dims[mark] = {
                    'thick': dims['flange_t'],
                    'width': dims['flange_w'],
                    'bom_len': bom_len,
                    'qty': qty,
                    'profile': spec,
                }
            elif dims:
                part_dims[mark] = {
                    'thick': dims['flange_t'],
                    'width': dims['flange_w'],
                    'bom_len': bom_len,
                    'qty': qty,
                    'profile': spec,
                    'depth': dims['depth'],
                    'web_t': dims['web_t'],
                }
            else:
                part_dims[mark] = {
                    'thick': None, 'width': None, 'bom_len': bom_len,
                    'qty': qty, 'profile': spec,
                }

        if found_any:
            break

    return part_dims, comp_dims


def extract_eu(dxf_path, config=None):
    """
    EU-only extraction entry. Rejects GB drawings.
    Reuses shared weld geometry via extract_welds(standard='eu').
    """
    from weld_extractor import (
        JobConfig, extract_welds, extract_comp_id, is_eu_comp,
    )

    comp = extract_comp_id(dxf_path)
    if not is_eu_comp(comp):
        raise ValueError(f"extract_eu refused non-EU component {comp!r} from {dxf_path}")

    if config is None:
        config = JobConfig(standard='eu', dxf_paths=[dxf_path],
                           section_catalog_path=EU_SECTIONS_DEFAULT)
    else:
        # force EU path; do not mutate caller's other fields unexpectedly
        config = JobConfig(
            standard='eu',
            dxf_paths=list(config.dxf_paths or [dxf_path]),
            output_dir=config.output_dir,
            section_catalog_path=config.section_catalog_path or EU_SECTIONS_DEFAULT,
            ifc_dir=config.ifc_dir,
            skip_names=list(config.skip_names or []),
            run_annotate=config.run_annotate,
            progress=config.progress,
        )
    return extract_welds(dxf_path, config)


# ============================================================
# EU 2S / 3S edge enum — per-edge real adjacency (main↔plate and/or plate↔plate)
# Main member in section = 工字(I) or H outline; labeled A#### ≈ plates.
# ============================================================

def _eu_edge_mid(ln):
    return ((ln['start'][0] + ln['end'][0]) * 0.5,
            (ln['start'][1] + ln['end'][1]) * 0.5)


def _eu_dist_pt_to_seg(pt, s, e):
    import math
    dx, dy = e[0] - s[0], e[1] - s[1]
    len_sq = dx * dx + dy * dy
    if len_sq < 1e-12:
        return math.hypot(pt[0] - s[0], pt[1] - s[1])
    t = max(0.0, min(1.0, ((pt[0] - s[0]) * dx + (pt[1] - s[1]) * dy) / len_sq))
    return math.hypot(pt[0] - (s[0] + t * dx), pt[1] - (s[1] + t * dy))


def _eu_line_angle(ln):
    import math
    return math.atan2(ln['end'][1] - ln['start'][1], ln['end'][0] - ln['start'][0])


def _eu_part_bbox(lines):
    if not lines:
        return None
    xs = [p for ln in lines for p in (ln['start'][0], ln['end'][0])]
    ys = [p for ln in lines for p in (ln['start'][1], ln['end'][1])]
    return (min(xs), min(ys), max(xs), max(ys))


def _eu_bbox_area(bb):
    if not bb:
        return 0.0
    return max(0.0, bb[2] - bb[0]) * max(0.0, bb[3] - bb[1])


def _eu_line_near_part(g_ln, other_lns, adj_tol):
    """Endpoint / tee proximity (weaker than face adjacency)."""
    for ln in other_lns:
        if (_eu_dist_pt_to_seg(g_ln['start'], ln['start'], ln['end']) <= adj_tol
                or _eu_dist_pt_to_seg(g_ln['end'], ln['start'], ln['end']) <= adj_tol):
            return True
        if (_eu_dist_pt_to_seg(ln['start'], g_ln['start'], g_ln['end']) <= adj_tol
                or _eu_dist_pt_to_seg(ln['end'], g_ln['start'], g_ln['end']) <= adj_tol):
            return True
    return False


def _eu_face_adj_to_lines(g_ln, other_lns, adj_tol=4.5, ang_tol_deg=20.0):
    """
    True if g_ln has parallel face contact with some line in other_lns
    (overlap along length + small perpendicular gap). This is the real weld edge.
    """
    import math
    if not other_lns or g_ln.get('length', 0) < 1e-6:
        return False
    ga = _eu_line_angle(g_ln)
    ang_tol = math.radians(ang_tol_deg)
    for p in other_lns:
        pa = _eu_line_angle(p)
        da = abs((ga - pa + math.pi) % (2 * math.pi) - math.pi)
        da = min(da, math.pi - da)
        if da > ang_tol:
            continue
        d1 = _eu_dist_pt_to_seg(g_ln['start'], p['start'], p['end'])
        d2 = _eu_dist_pt_to_seg(g_ln['end'], p['start'], p['end'])
        if max(d1, d2) > adj_tol * 1.5:
            continue
        dx, dy = math.cos(pa), math.sin(pa)

        def _proj(pt):
            return pt[0] * dx + pt[1] * dy

        gs, ge = sorted([_proj(g_ln['start']), _proj(g_ln['end'])])
        ps, pe = sorted([_proj(p['start']), _proj(p['end'])])
        ov = min(ge, pe) - max(gs, ps)
        if ov >= 0.30 * g_ln['length']:
            return True
    return False


def _eu_classify_section_kind(comp_dims):
    """
    'I' = 工字型 (flange narrower → prefer left/right TYP)
    'H' = H型 (flange ≈ depth → prefer top/bottom TYP, also allow LR)
    """
    if not comp_dims:
        return 'H'  # EU HE sections default to H-like
    d = float(comp_dims.get('depth') or 0)
    fw = float(comp_dims.get('flange_w') or 0)
    if d <= 0 or fw <= 0:
        return 'H'
    ratio = fw / d
    # Classic 工: flange clearly narrower than depth (e.g. IPE)
    if ratio < 0.72:
        return 'I'
    return 'H'


def _eu_dims_match_section(bw_mm, bh_mm, depth, flange_w, tol=0.18):
    """BBox (mm) matches section depth × flange (either orientation)."""
    if depth <= 0 or flange_w <= 0:
        return False
    for a, b in ((bw_mm, bh_mm), (bh_mm, bw_mm)):
        if (abs(a - flange_w) / max(flange_w, 1) <= tol
                and abs(b - depth) / max(depth, 1) <= tol):
            return True
    return False


def _eu_find_span_spine_blocks(view_parts, part_number_map, comp, comp_dims=None,
                               scale=10.0):
    """
    Elevation / long views: a part spanning most of the view width with height
    near section depth is the structural spine (web drawing), even if labeled
    A####. Treat as main-role for weld adjacency.
    """
    depth = float((comp_dims or {}).get('depth') or 0)
    flange_w = float((comp_dims or {}).get('flange_w') or 0)
    vbb = _eu_view_bbox(view_parts)
    if not vbb:
        return []
    view_w = (vbb[2] - vbb[0]) * scale
    view_h = max((vbb[3] - vbb[1]) * scale, 1.0)
    # Need a clearly elongated view
    if view_w < max(depth, flange_w, 200) * 2.2 or view_w / view_h < 1.8:
        return []
    spines = []
    for pn, lns in view_parts.items():
        bb = _eu_part_bbox(lns)
        if not bb:
            continue
        bw = (bb[2] - bb[0]) * scale
        bh = (bb[3] - bb[1]) * scale
        if bw < view_w * 0.55:
            continue
        # Height near section depth (web elevation) or flange
        h_ok = False
        if depth > 0 and abs(bh - depth) <= max(45.0, 0.28 * depth):
            h_ok = True
        if flange_w > 0 and abs(bh - flange_w) <= max(45.0, 0.28 * flange_w):
            h_ok = True
        if not h_ok:
            continue
        lbl = part_number_map.get(pn)
        if lbl == comp:
            continue  # already explicit main
        spines.append((pn, bw * bh))
    if not spines:
        return []
    spines.sort(key=lambda t: -t[1])
    return [spines[0][0]]


def _eu_find_main_body_blocks(view_parts, part_number_map, comp, comp_dims=None,
                              scale=10.0):
    """
    Main member blocks in this view: labeled as `comp`, or unlabeled/mis-drawn
    geometry whose bbox matches section depth×flange (工/H outline).
    Labeled A#### plates are NOT section bodies (reject elongated plate marks).
    Also returns elevation span-spine as extra main-role blocks.
    """
    depth = float((comp_dims or {}).get('depth') or 0)
    flange_w = float((comp_dims or {}).get('flange_w') or 0)

    explicit = [pn for pn, lbl in part_number_map.items()
                if lbl == comp and pn in view_parts]
    main = list(explicit)

    matched = []
    for pn, lns in view_parts.items():
        if pn in main:
            continue
        bb = _eu_part_bbox(lns)
        if not bb:
            continue
        bw = (bb[2] - bb[0]) * scale
        bh = (bb[3] - bb[1]) * scale
        lbl = part_number_map.get(pn)
        if min(bw, bh) < 80:
            continue
        if not _eu_dims_match_section(bw, bh, depth, flange_w):
            continue
        # Plate marks: only accept compact section-cut aspect (not tall gussets)
        if lbl and lbl != comp:
            aspect = max(bw, bh) / max(min(bw, bh), 1.0)
            if aspect > 1.85:
                continue
        matched.append((pn, _eu_bbox_area(bb), lbl))

    if not main and matched:
        matched.sort(key=lambda t: (
            0 if t[2] in (None, comp) else 1,  # prefer unlabeled
            -t[1],
        ))
        main = [matched[0][0]]
    elif not main:
        unlabeled = []
        for pn, lns in view_parts.items():
            if pn in part_number_map:
                continue
            bb = _eu_part_bbox(lns)
            if bb:
                unlabeled.append((pn, _eu_bbox_area(bb)))
        if unlabeled:
            unlabeled.sort(key=lambda t: -t[1])
            main = [unlabeled[0][0]]

    # Elevation spine (long web drawing) — merge for weld partner role
    for sp in _eu_find_span_spine_blocks(
            view_parts, part_number_map, comp, comp_dims=comp_dims, scale=scale):
        if sp not in main:
            main.append(sp)

    return main, _eu_classify_section_kind(comp_dims)


def _eu_bom_lengths_mm(lbl, part_dims):
    pd = part_dims.get(lbl) or {}
    cands = []
    bw = pd.get('width') or 0
    bl = pd.get('bom_len') or 0
    if bw:
        cands.append(float(bw))
    if bl and bl != bw:
        cands.append(float(bl))
    if bw and bw > 40:
        for cope in (25.0, 28.0):
            v = bw - cope
            if v > 10:
                cands.append(v)
    return cands


def _eu_len_matches(geo_mm, targets, rel=0.22, abs_tol=8.0):
    if not targets:
        return True
    for t in targets:
        if t <= 0:
            continue
        if abs(geo_mm - t) <= max(abs_tol, rel * t):
            return True
    return False


def _eu_best_neighbor_for_edge(g_ln, view_parts, gusset_blk_set, part_number_map,
                               main_body_set, comp, adj_tol):
    """
    Pick the best neighbour for one gusset edge.
    Prefer face-adjacency; partner may be main body OR another plate.
    Returns (other_block, other_lbl, face_score) or None.
      face_score: 0=endpoint only, 1=face
    """
    best = None  # (rank_tuple, face_i, pname, olbl)
    for pname, plines in view_parts.items():
        if pname in gusset_blk_set:
            continue
        face = _eu_face_adj_to_lines(g_ln, plines, adj_tol)
        near = face or _eu_line_near_part(g_ln, plines, adj_tol)
        if not near:
            continue
        olbl = part_number_map.get(pname)
        is_main = (pname in main_body_set) or (olbl == comp)
        if olbl is None:
            olbl = comp if is_main else None
        if olbl is None:
            continue
        face_i = 1 if face else 0
        tq = 1e9
        for ln in plines:
            tq = min(
                tq,
                _eu_dist_pt_to_seg(g_ln['start'], ln['start'], ln['end']),
                _eu_dist_pt_to_seg(g_ln['end'], ln['start'], ln['end']),
            )
        rank = (0 if face_i else 1, 0 if is_main else 1, tq)
        if best is None or rank < best[0]:
            best = (rank, face_i, pname, olbl)
    if best is None:
        return None
    _, face_i, pname, olbl = best
    return (pname, olbl, face_i)



def enumerate_eu_multiside_edges(
        arrow, matches, view_parts, part_number_map, part_dims, comp,
        expected_edges, adj_tol=4.5, scale=10.0, min_edge_cad=1.5,
        comp_dims=None):
    """
    EU 2S/3S without IFC:
      - Arrow → plate (gusset); labeled A#### = plate
      - Main body = 工/H outline (comp block or section-dim match)
      - Each weld edge keeps ITS real neighbour (main OR another plate)
      - Prefer face-adjacent edges; fill to expected_edges
    """
    import math

    if not matches or not view_parts:
        return None

    main_blocks, section_kind = _eu_find_main_body_blocks(
        view_parts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
    main_body_set = set(main_blocks)

    # Gusset = single plate at arrow (labeled A####), not the main 工/H body.
    # Important: do NOT pull both TYP-symmetric plates into gusset_names here —
    # TYP expansion handles the sibling.
    plate_matches = [m for m in matches
                     if m['part'] not in main_body_set
                     and part_number_map.get(m['part']) not in (None, comp)]
    if not plate_matches:
        plate_matches = [m for m in matches if m['part'] not in main_body_set]
    if not plate_matches:
        return None

    # Prefer nearest plate line to arrow among labeled plates
    def _m_dist(m):
        mid = _eu_edge_mid(m['line'])
        return math.hypot(mid[0] - arrow[0], mid[1] - arrow[1])

    plate_matches.sort(key=_m_dist)
    gusset_name = plate_matches[0]['part']
    gusset_names = [gusset_name]
    gusset_blk_set = {gusset_name}
    lbl_g = part_number_map.get(gusset_name, '?')
    if lbl_g == comp:
        return None

    # Score every gusset edge with its best neighbour
    # (dist_arrow, len, oblock, olbl, g_ln, gn, face)
    candidates = []
    for gn in gusset_names:
        for g_ln in view_parts.get(gn, []):
            if g_ln.get('length', 0) < min_edge_cad:
                continue
            mid = _eu_edge_mid(g_ln)
            d_arrow = math.hypot(mid[0] - arrow[0], mid[1] - arrow[1])
            nb = _eu_best_neighbor_for_edge(
                g_ln, view_parts, gusset_blk_set, part_number_map,
                main_body_set, comp, adj_tol)
            if nb is None:
                continue
            oblock, olbl, face_i = nb
            if olbl == lbl_g:
                continue
            candidates.append((d_arrow, g_ln['length'], oblock, olbl, g_ln, gn, face_i))

    if not candidates:
        return None

    # Seed: nearest arrow, prefer face contact
    near = sorted(candidates, key=lambda c: (0 if c[6] else 1, c[0]))
    seed = near[0]
    seed_mm = seed[1] * scale
    bom_targets = _eu_bom_lengths_mm(lbl_g, part_dims)
    len_targets = list(bom_targets) + [seed_mm]

    # Select edges: face-adjacency only (never pad with endpoint-only junk).
    face_cands = [c for c in candidates if c[6]]
    pool_c = face_cands if face_cands else [near[0]]

    def _dedup(cands):
        out, seen = [], set()
        for c in cands:
            mid = _eu_edge_mid(c[4])
            key = (round(mid[0], 1), round(mid[1], 1), round(c[1], 2), c[3])
            if key in seen:
                continue
            seen.add(key)
            out.append(c)
        return out

    pool_c = _dedup(pool_c)

    # Reseed onto nearest face when available
    if face_cands:
        seed = sorted(face_cands, key=lambda c: c[0])[0]
        seed_mm = seed[1] * scale
        len_targets = list(bom_targets) + [seed_mm]

    # Prefer selecting a face edge against the main body when filling 3S/2S
    selected = [seed]
    selected_ids = {id(seed[4])}
    rest = [c for c in pool_c if id(c[4]) not in selected_ids]

    def _len_ok(c):
        return _eu_len_matches(c[1] * scale, len_targets)

    def _is_main_partner(c):
        return (c[2] in main_body_set) or (c[3] == comp)

    rest_pref = sorted(rest, key=lambda c: (
        0 if _is_main_partner(c) else 1,
        0 if _len_ok(c) else 1,
        c[0],
    ))

    while len(selected) < expected_edges and rest_pref:
        cms = [_eu_edge_mid(c[4]) for c in selected]
        used_partners = {c[3] for c in selected}
        has_main = any(_is_main_partner(c) for c in selected)
        best_i, best_score = -1, None
        for i, c in enumerate(rest_pref):
            m = _eu_edge_mid(c[4])
            spr = min(math.hypot(m[0] - cm[0], m[1] - cm[1]) for cm in cms)
            sc = spr
            if _len_ok(c):
                sc *= 1.15
            if c[3] not in used_partners:
                sc *= 1.25
            # Prefer including at least one main-body weld for 3S
            if expected_edges >= 3 and not has_main and _is_main_partner(c):
                sc *= 2.0
            if best_score is None or sc > best_score:
                best_score, best_i = sc, i
        chosen = rest_pref.pop(best_i)
        selected.append(chosen)
        selected_ids.add(id(chosen[4]))

    # If still short of expected and missing main contact, try add face→main
    if (len(selected) < expected_edges
            and not any(_is_main_partner(c) for c in selected)):
        mains = [c for c in face_cands
                 if id(c[4]) not in selected_ids and _is_main_partner(c)]
        if mains:
            mains.sort(key=lambda c: -c[1])
            selected.append(mains[0])
            selected_ids.add(id(mains[0][4]))

    # Still short: carefully admit near (non-parallel) contacts.
    # Prefer short/plate-width edges near the seed mid — never long random
    # flange edges (AB0002 A-A previously took L=261 @ y=203 instead of L=146 @ y=174).
    if len(selected) < expected_edges:
        seed_mid = _eu_edge_mid(seed[4])
        near_pool = [c for c in candidates
                     if id(c[4]) not in selected_ids and not c[6]]
        near_pool = _dedup(near_pool)

        def _near_score(c):
            m = _eu_edge_mid(c[4])
            dy = abs(m[1] - seed_mid[1])
            dx = abs(m[0] - seed_mid[0])
            Lmm = c[1] * scale
            # Penalize far from seed; reward BOM/seed-length match and new partners
            sc = dy * 3.0 + dx * 0.35
            if _len_ok(c):
                sc -= 40.0
            # Prefer lengths near other selected face edges (plate depth/height)
            for s in selected:
                sc -= max(0.0, 25.0 - abs(Lmm - s[1] * scale) * 0.5)
            used_partners = {x[3] for x in selected}
            if c[3] not in used_partners:
                sc -= 30.0
            if _is_main_partner(c):
                sc -= 15.0
            # Heavy penalty for very long non-face edges (ghost outline)
            if Lmm > max(seed_mm * 0.85, 200.0):
                sc += 80.0
            return sc

        near_pool.sort(key=_near_score)
        while len(selected) < expected_edges and near_pool:
            chosen = near_pool.pop(0)
            m = _eu_edge_mid(chosen[4])
            # Must stay near seed band (H cut): reject if |dy| huge
            if abs(m[1] - seed_mid[1]) > max(25.0, seed[1] * 0.15):
                continue
            selected.append(chosen)
            selected_ids.add(id(chosen[4]))

    if len(selected) > expected_edges:
        # Keep seed + prefer main + spread
        keep = [seed]
        rest3 = [c for c in selected if id(c[4]) != id(seed[4])]
        # Prefer to retain a main-partner edge
        main_rest = [c for c in rest3 if _is_main_partner(c)]
        if main_rest and not _is_main_partner(seed):
            pick = max(main_rest, key=lambda c: c[1])
            keep.append(pick)
            rest3 = [c for c in rest3 if id(c[4]) != id(pick[4])]
        while len(keep) < expected_edges and rest3:
            cms = [_eu_edge_mid(c[4]) for c in keep]
            used_partners = {c[3] for c in keep}
            best_i, best_spr = -1, -1
            for i, c in enumerate(rest3):
                m = _eu_edge_mid(c[4])
                spr = min(math.hypot(m[0] - cm[0], m[1] - cm[1]) for cm in cms)
                if c[3] not in used_partners:
                    spr *= 1.3
                if spr > best_spr:
                    best_spr, best_i = spr, i
            keep.append(rest3.pop(best_i))
        selected = keep

    by_g = {}
    partners = set()
    for c in selected:
        _d, leng, oblock, olbl, g_ln, gn, _f = c
        by_g.setdefault(gn, []).append((leng, oblock, [g_ln]))
        partners.add(olbl)

    if not by_g:
        return None

    return {
        'gusset_name': gusset_name,
        'gusset_names': gusset_names,
        'partner_lbl': ','.join(sorted(partners)),  # may be mixed
        'partners': sorted(partners),
        'section_kind': section_kind,
        'main_blocks': list(main_blocks),
        'weld_edges_by_gusset': by_g,
        'seed_mm': round(seed_mm, 1),
        'edge_partners': [
            (part_number_map.get(c[5], '?'), c[3], round(c[1] * scale, 1), bool(c[6]))
            for c in selected
        ],
    }


def find_eu_typ_sibling_edges(
        weld_edges_by_gusset, gusset_names, partner_lbl, view_parts,
        part_number_map, expected_edges, adj_tol=4.5, scale=10.0,
        len_rel=0.22, section_kind='H', main_blocks=None, comp='?',
        part_dims=None, arrow=None):
    """
    TYP (same view): find plates with a *similar face-adj weld structure*
    (length multiset + partner roles). Prefer same-label LR/TB mirrors; also
    accept other labels whose edge fingerprint matches.
    """
    import math

    if not weld_edges_by_gusset or not gusset_names:
        return 0

    primary = gusset_names[0]
    lbl_g = part_number_map.get(primary)
    if not lbl_g:
        return 0

    ref_lens = []
    ref_partners = set()
    for edges in weld_edges_by_gusset.values():
        for leng, ob, _fr in edges:
            ref_lens.append(leng * scale)
            olbl = part_number_map.get(ob, '?')
            ref_partners.add(olbl)
    if not ref_lens:
        return 0

    view_id = primary.split(' - ')[-1]
    main_body_set = set(main_blocks or [])
    pbb = _eu_part_bbox(view_parts.get(primary, []))
    if not pbb:
        return 0
    pcx = 0.5 * (pbb[0] + pbb[2])
    pcy = 0.5 * (pbb[1] + pbb[3])

    mxs, mys = [], []
    for mb in main_body_set:
        bb = _eu_part_bbox(view_parts.get(mb, []))
        if bb:
            mxs.extend([bb[0], bb[2]])
            mys.extend([bb[1], bb[3]])
    mcx = 0.5 * (min(mxs) + max(mxs)) if mxs else pcx
    mcy = 0.5 * (min(mys) + max(mys)) if mys else pcy

    # Same-label first; other-label only when mirror-like and similar bbox.
    same_lbl, other_lbl = [], []
    for pn, lbl in part_number_map.items():
        if not lbl or pn == primary or pn.split(' - ')[-1] != view_id:
            continue
        if pn in main_body_set or lbl == comp:
            continue
        if lbl == lbl_g:
            same_lbl.append(pn)
        else:
            other_lbl.append(pn)

    def _mirror_score(sib):
        bb = _eu_part_bbox(view_parts.get(sib, []))
        if not bb:
            return -1e9
        scx = 0.5 * (bb[0] + bb[2])
        scy = 0.5 * (bb[1] + bb[3])
        same = 50.0 if part_number_map.get(sib) == lbl_g else 0.0
        if section_kind == 'I':
            return same - abs((2 * mcx - pcx) - scx) - 0.25 * abs(scy - pcy)
        tb = -abs((2 * mcy - pcy) - scy) - 0.25 * abs(scx - pcx)
        lr = -abs((2 * mcx - pcx) - scx) - 0.25 * abs(scy - pcy)
        return same + max(tb, lr)

    def _bbox_similar(sib):
        bb = _eu_part_bbox(view_parts.get(sib, []))
        if not bb or not pbb:
            return False
        pw = max(pbb[2] - pbb[0], 1e-6)
        ph = max(pbb[3] - pbb[1], 1e-6)
        sw = max(bb[2] - bb[0], 1e-6)
        sh = max(bb[3] - bb[1], 1e-6)
        tol = 0.32
        return (abs(sw - pw) / pw <= tol and abs(sh - ph) / ph <= tol) or (
            abs(sw - ph) / ph <= tol and abs(sh - pw) / pw <= tol)


    def _near_primary(sib, fac=3.5):
        """Keep siblings in the same structural cluster (avoid far-end plates)."""
        bb = _eu_part_bbox(view_parts.get(sib, []))
        if not bb or not pbb:
            return False
        scx = 0.5 * (bb[0] + bb[2])
        scy = 0.5 * (bb[1] + bb[3])
        span = max(pbb[2] - pbb[0], pbb[3] - pbb[1], 5.0)
        # Allow LR/TB mirror across main center even if far
        mir_x = abs((2 * mcx - pcx) - scx) <= span * 1.2
        mir_y = abs((2 * mcy - pcy) - scy) <= span * 1.2
        if mir_x or mir_y:
            return True
        return math.hypot(scx - pcx, scy - pcy) <= span * fac

    same_lbl = [p for p in same_lbl if _bbox_similar(p) and _near_primary(p)]
    other_lbl = [p for p in other_lbl if _bbox_similar(p) and _near_primary(p)]
    other_lbl.sort(key=_mirror_score, reverse=True)
    # Compact section: at most one mirror sibling (3S left+right → 6 edges).
    # Elevation: allow a few other-label stiffener repeats.
    vbb = _eu_view_bbox(view_parts)
    is_sec = bool(vbb) and not _eu_is_assembly_view(view_parts, vbb) and (
        (vbb[2] - vbb[0]) / max(vbb[3] - vbb[1], 1e-6) < 2.2)
    if is_sec:
        # Exactly one sibling plate (left↔right) for 3S → 6 edges total
        pick = same_lbl[:1] or other_lbl[:1]
        candidates = list(dict.fromkeys(pick))
    else:
        # Elevation: same-mark repeats only (other marks via expand fingerprints)
        candidates = list(dict.fromkeys(same_lbl[:4]))
    if not candidates:
        return 0

    candidates.sort(key=_mirror_score, reverse=True)

    def _collect_face_hits(sib):
        hits = []
        for g_ln in view_parts.get(sib, []):
            if g_ln.get('length', 0) < 1.5:
                continue
            geo_mm = g_ln['length'] * scale
            nb = _eu_best_neighbor_for_edge(
                g_ln, view_parts, {sib}, part_number_map,
                main_body_set, comp, adj_tol)
            if nb is None:
                continue
            oblock, olbl, face_i = nb
            if olbl == part_number_map.get(sib):
                continue
            if not face_i:
                continue
            if not any(abs(geo_mm - r) <= max(8.0, len_rel * r) for r in ref_lens):
                continue
            hits.append((g_ln['length'], oblock, [g_ln], face_i, geo_mm, olbl))
        hits.sort(key=lambda h: -h[0])
        dedup, seen = [], set()
        for h in hits:
            mid = _eu_edge_mid(h[2][0])
            key = (round(mid[0], 1), round(mid[1], 1), round(h[0], 2))
            if key in seen:
                continue
            seen.add(key)
            dedup.append(h)
        return dedup

    def _lens_match(hits):
        used = [False] * len(ref_lens)
        matched = 0
        for h in hits:
            for i, r in enumerate(ref_lens):
                if used[i]:
                    continue
                if abs(h[4] - r) <= max(8.0, len_rel * r):
                    used[i] = True
                    matched += 1
                    break
        need = max(1, min(expected_edges, len(set(round(x, 0) for x in ref_lens))))
        return matched >= max(1, need - (0 if expected_edges <= 2 else 1))

    added = 0
    for sib in candidates:
        if sib in weld_edges_by_gusset:
            continue
        dedup = _collect_face_hits(sib)
        if not dedup or not _lens_match(dedup):
            continue
        # Other-label stiffener / gusset: must share partner roles (esp. main)
        if part_number_map.get(sib) != lbl_g:
            hit_partners = {h[5] for h in dedup}
            hit_main = any((h[1] in main_body_set) or (h[5] == comp) for h in dedup)
            if not hit_main and not (hit_partners & ref_partners):
                continue
        if len(dedup) > expected_edges:
            # keep spread + partner diversity
            keep = []
            rest = list(dedup)
            while len(keep) < expected_edges and rest:
                if not keep:
                    keep.append(rest.pop(0))
                    continue
                cms = [_eu_edge_mid(h[2][0]) for h in keep]
                used_p = {h[5] for h in keep}
                best_i, best_sc = -1, -1
                for i, h in enumerate(rest):
                    m = _eu_edge_mid(h[2][0])
                    spr = min(math.hypot(m[0] - c[0], m[1] - c[1]) for c in cms)
                    sc = spr * (1.3 if h[5] not in used_p else 1.0)
                    if sc > best_sc:
                        best_sc, best_i = sc, i
                keep.append(rest.pop(best_i))
            dedup = keep
        weld_edges_by_gusset[sib] = [(h[0], h[1], h[2]) for h in dedup[:expected_edges]]
        added += 1
    return added


def _eu_geo_fingerprint(rows, scale_tol=1.0):
    """Unique geometric edges from Above/Below rows → sorted length list."""
    seen = set()
    lens = []
    for r in rows:
        pos = r.get('dxf_pos') or (0, 0)
        key = (round(r.get('length_mm', 0), 0),
               tuple(sorted((r.get('part1'), r.get('part2')))),
               round(pos[0], 0), round(pos[1], 0))
        if key in seen:
            continue
        seen.add(key)
        lens.append(float(r.get('length_mm') or 0))
    return sorted(lens), seen


def _eu_result_covers(results, view_id, length_mm, parts, mid, tol=3.5,
                      lane=0, station=None):
    pair = frozenset(parts)
    for r in results:
        if r.get('view_id') != view_id:
            continue
        if r.get('_eu_u_lane', 0) != lane:
            continue
        if station is not None and r.get('_eu_u_station') != station:
            continue
        if frozenset((r.get('part1'), r.get('part2'))) != pair:
            continue
        if abs((r.get('length_mm') or 0) - length_mm) > max(6.0, 0.12 * length_mm):
            continue
        pos = r.get('dxf_pos')
        if not pos:
            continue
        if abs(pos[0] - mid[0]) <= tol and abs(pos[1] - mid[1]) <= tol:
            return True
    return False


def _eu_group_u_plate_bands(small_pns, y_tol=3.5):
    """Cluster small U plates into Y bands; each band may contain 1–2 biting Us."""
    items = sorted(
        small_pns,
        key=lambda x: 0.5 * (x[1][1] + x[1][3]))
    bands = []
    for pn, bb in items:
        cy = 0.5 * (bb[1] + bb[3])
        placed = False
        for band in bands:
            rep_cy = 0.5 * (band[0][1][1] + band[0][1][3])
            if abs(cy - rep_cy) <= y_tol:
                band.append((pn, bb))
                placed = True
                break
        if not placed:
            bands.append([(pn, bb)])
    return bands


def _eu_count_wrap_stations(wrap_rows, y_tol=4.0):
    """
    CIRCLE/hf5 station count on main elev.
    Prefer Above-tip count / 3 (TYP multiplies whole 3-edge packs that may
    still share one seed Y). Fall back to unique Y clusters.
    """
    above = [
        r for r in wrap_rows
        if r.get('position') == 'Above' and r.get('dxf_pos')
    ]
    if not above:
        above = [r for r in wrap_rows if r.get('dxf_pos')]
    if not above:
        return 0
    # TYP xN duplicates the CIRCLE edge pack (~3 edges) at the seed before
    # geometric relocate — Y clustering alone under-counts (e.g. 2 WMs → 2).
    n_edge = int(round(len(above) / 3.0))
    ys = sorted(r['dxf_pos'][1] for r in above)
    clusters = [ys[0]]
    for y in ys[1:]:
        if abs(y - clusters[-1]) > y_tol:
            clusters.append(y)
        else:
            clusters[-1] = 0.5 * (clusters[-1] + y)
    n_y = len(clusters)
    return max(n_edge, n_y)


def _eu_perimeter_length_by_gusset(wrap_rows, comp):
    """
    Perimeter length per CIRCLE gusset label = sum of one native 3-edge pack.

    TYP mirrors often pile at the same Y on the opposite flange; take the
    leftmost pack (seed WM side) so L is 300+100+300 / 300+120+300, not a
    collapsed duplicate sum.
    """
    from collections import defaultdict

    by_g = defaultdict(list)
    for r in wrap_rows:
        if r.get('position') != 'Above' or not r.get('dxf_pos'):
            continue
        g = None
        for p in (r.get('part1'), r.get('part2')):
            if p and p != comp:
                g = p
                break
        if not g:
            continue
        by_g[g].append(r)
    out = {}
    for g, rows in by_g.items():
        rows = sorted(rows, key=lambda r: (r['dxf_pos'][0], r['dxf_pos'][1]))
        x0 = rows[0]['dxf_pos'][0]
        pack = [r for r in rows if abs(r['dxf_pos'][0] - x0) < 50.0][:3]
        if len(pack) < 3:
            pack = rows[:3]
        L = round(sum(float(r.get('length_mm') or 0) for r in pack), 1)
        if L > 0:
            out[g] = L
    return out


def _eu_list_main_wrap_plates(
        vparts, part_number_map, wrap_labels, main_body_set, scale=10.0,
        min_size_mm=40.0):
    """Main-elev plate instances whose BOM label is a CIRCLE wrap gusset."""
    wrap_labels = set(wrap_labels or [])
    if not wrap_labels:
        return []
    items = []
    for pn, lns in vparts.items():
        if pn in main_body_set:
            continue
        bb = _eu_part_bbox(lns)
        if not bb:
            continue
        lbl = part_number_map.get(pn)
        if lbl not in wrap_labels:
            continue
        pw = (bb[2] - bb[0]) * scale
        ph = (bb[3] - bb[1]) * scale
        if max(pw, ph) < min_size_mm:
            continue
        items.append({
            'pn': pn,
            'bb': bb,
            'lbl': lbl,
            'cy': 0.5 * (bb[1] + bb[3]),
        })
    items.sort(key=lambda x: x['cy'])
    return items


def _eu_wrap_stations_from_seeds(wrap_rows, n_target=0, y_tol=4.0):
    """
    Build one perimeter-wrap station per CIRCLE/hf5 pack on main elev.

    Each station: mid = mean of pack tip positions, L = sum of the ~3 edge
    lengths (full U perimeter, no scallop holes on EU AC plates).

    Note: TYP×N mirrors often share one seed Y, so this under-counts the
    physical station stack — prefer `_eu_list_main_wrap_plates` for emit.
    """
    above = [
        r for r in wrap_rows
        if r.get('position') == 'Above' and r.get('dxf_pos')
    ]
    if not above:
        above = [r for r in wrap_rows if r.get('dxf_pos')]
    if not above:
        return []
    above = sorted(above, key=lambda r: (r['dxf_pos'][1], r['dxf_pos'][0]))
    # Cluster by Y, then chunk each cluster into packs of ~3 edges
    y_groups = []
    for r in above:
        y = r['dxf_pos'][1]
        if not y_groups or abs(y - y_groups[-1][0]['dxf_pos'][1]) > y_tol:
            y_groups.append([r])
        else:
            y_groups[-1].append(r)
    stations = []
    for grp in y_groups:
        grp = sorted(grp, key=lambda r: (r['dxf_pos'][0], float(r.get('length_mm') or 0)))
        # One CIRCLE pack ≈ 3 Above edges
        i = 0
        while i < len(grp):
            pack = grp[i:i + 3]
            i += 3
            if not pack:
                break
            L = round(sum(float(r.get('length_mm') or 0) for r in pack), 1)
            if L <= 0:
                continue
            mx = sum(r['dxf_pos'][0] for r in pack) / len(pack)
            my = sum(r['dxf_pos'][1] for r in pack) / len(pack)
            p1 = pack[0].get('part1')
            p2 = pack[0].get('part2')
            stations.append({
                'mid': (mx, my),
                'L': L,
                'p1': p1,
                'p2': p2,
                'hf': pack[0].get('hf'),
                'annotation': pack[0].get('annotation') or '',
            })
    stations.sort(key=lambda s: s['mid'][1])
    if n_target and len(stations) > n_target:
        # Keep evenly spaced by Y (prefer covering the full stack)
        if n_target == 1:
            stations = [stations[len(stations) // 2]]
        else:
            idxs = [
                int(round(i * (len(stations) - 1) / (n_target - 1)))
                for i in range(n_target)
            ]
            seen = set()
            picked = []
            for j in idxs:
                if j not in seen:
                    seen.add(j)
                    picked.append(stations[j])
            stations = picked
    return stations


def _eu_trim_u_bands_to_target(bands, n_target, prefer_labels=None):
    """
    Keep at most n_target U-station bands on the cut view.

    When prefer_labels (CIRCLE wrap plates) is set, bands that carry other BOM
    labels (e.g. F/H stiffeners A0191/A0368) are ranked last so they cannot
    displace the real single-U stack — including low-Y bottom stations.
    """
    if not bands or n_target <= 0 or len(bands) <= n_target:
        return bands
    prefer_labels = set(prefer_labels or [])

    def _band_cy(band):
        return 0.5 * (band[0][1][1] + band[0][1][3])

    def _band_score(band):
        area = 0.0
        hit = 0
        foreign = 0
        for item in band:
            bb = item[1]
            area += max(bb[2] - bb[0], 0.0) * max(bb[3] - bb[1], 0.0)
            lbl = item[2] if len(item) >= 3 else None
            if not lbl:
                continue
            if lbl in prefer_labels:
                hit += 1
            elif prefer_labels:
                foreign += 1
        # clean=1 for unlabeled / wrap-label U stack; foreign stiffener bands last
        clean = 0 if foreign else 1
        return (clean, hit, area)

    ranked = sorted(bands, key=_band_score, reverse=True)
    keep = ranked[:n_target]
    return sorted(keep, key=_band_cy)


def _eu_collect_plate_face_edges(pn, view_parts, part_number_map, main_body_set,
                                 comp, adj_tol, scale, min_len_cad=1.5,
                                 face_only=True):
    """Weld-adjacent edges for one plate block. face_only=False also keeps near."""
    out = []
    lbl = part_number_map.get(pn)
    for g_ln in view_parts.get(pn, []):
        if g_ln.get('length', 0) < min_len_cad:
            continue
        nb = _eu_best_neighbor_for_edge(
            g_ln, view_parts, {pn}, part_number_map, main_body_set, comp, adj_tol)
        if nb is None:
            continue
        oblock, olbl, face_i = nb
        if olbl == lbl:
            continue
        if face_only and not face_i:
            continue
        mid = _eu_edge_mid(g_ln)
        out.append({
            'length_mm': g_ln['length'] * scale,
            'leng_cad': g_ln['length'],
            'partner': olbl,
            'oblock': oblock,
            'mid': mid,
            'gusset': lbl,
            'gusset_pn': pn,
            'face': bool(face_i),
            'line': g_ln,
        })
    # Dedup
    dedup, seen = [], set()
    for e in out:
        key = (round(e['mid'][0], 1), round(e['mid'][1], 1), round(e['leng_cad'], 2))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(e)
    return dedup


def _eu_match_fingerprint(edges, ref_lens, len_rel=0.22):
    """Match each seed length to the closest unused edge (prefer face)."""
    if not ref_lens or not edges:
        return None
    used_e = [False] * len(edges)
    matched = []
    # Shortest refs first so 71 is not stolen by a near-miss 90 edge.
    for r in sorted(ref_lens):
        tol = max(8.0, len_rel * r)
        best_i, best_sc = None, 1e18
        for i, e in enumerate(edges):
            if used_e[i]:
                continue
            d = abs(e['length_mm'] - r)
            if d > tol:
                continue
            sc = d + (0.0 if e.get('face') else 6.0)
            if sc < best_sc:
                best_sc, best_i = sc, i
        if best_i is None:
            continue
        used_e[best_i] = True
        matched.append(edges[best_i])
    uniq_ref = len(set(round(x, 0) for x in ref_lens))
    # Require covering all unique fingerprint lengths
    uniq_matched = len(set(round(e['length_mm'], 0) for e in matched))
    if uniq_matched < uniq_ref:
        return None
    if len(matched) < max(1, len(ref_lens) - (1 if len(ref_lens) >= 3 else 0)):
        return None
    return matched


def _eu_refine_match_cluster_y(matched, pool, len_rel=0.22):
    """
    Prefer face edges whose mid.y sits with the cluster (section cuts).
    Fixes fingerprints that land on a vertical outline mid far below the
    contact band used by sibling welds in the same group.

    Length must stay close to the fingerprint edge — do not swap 71↔90
    just because both fall inside a loose relative tol.
    """
    if not matched or len(matched) < 2 or not pool:
        return matched
    out = list(matched)
    for _ in range(2):
        ys = sorted(e['mid'][1] for e in out)
        med = ys[len(ys) // 2]
        new_out = []
        used_mids = set()
        for e in out:
            # Tight length band for replacements; cluster-y is about y, not L.
            tol = min(max(5.0, 0.10 * e['length_mm']),
                      max(8.0, len_rel * e['length_mm']))
            alts = [
                a for a in pool
                if abs(a['length_mm'] - e['length_mm']) <= tol
                and (a.get('partner') == e.get('partner')
                     or a.get('gusset') == e.get('gusset'))
            ]
            if e not in alts:
                alts.append(e)
            def _sc(a, _e=e):
                mid_key = (round(a['mid'][0], 1), round(a['mid'][1], 1),
                           round(a['length_mm'], 0))
                pen = 40.0 if mid_key in used_mids else 0.0
                face_bonus = 0.0 if a.get('face') else 8.0
                len_pen = abs(a['length_mm'] - _e['length_mm']) * 1.5
                return abs(a['mid'][1] - med) + face_bonus + pen + len_pen
            best = min(alts, key=_sc)
            used_mids.add((round(best['mid'][0], 1), round(best['mid'][1], 1),
                           round(best['length_mm'], 0)))
            new_out.append(best)
        out = new_out
    return out


def _eu_native_elev_plates(results, vid, comp, main_lbls):
    """Non-main plate labels that already have a native WM on this elev view."""
    plates = set()
    for r in results:
        if r.get('view_id') != vid:
            continue
        if (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')
                or r.get('_eu_typ_rematch') or r.get('_eu_assy_end')
                or r.get('_eu_typ_mirror') or r.get('_eu_plate_sides')
                or r.get('_eu_fillet_sib')):
            continue
        for p in (r.get('part1'), r.get('part2')):
            if p and p != comp and p not in main_lbls:
                plates.add(p)
    return plates


def cleanup_eu_elev_to_native_plates(results, part_lines_map, part_number_map,
                                     comp, comp_dims=None, scale=10.0,
                                     main_view_ids=None):
    """
    On SectionMark elev: keep native WM rows + mirrors of native-plate welds.
    Drop fingerprint/`_eu_assy_end` inventions (mid stiffener, brace extras).
    """
    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    drop = set()
    by_vid = {}
    for i, r in enumerate(results):
        by_vid.setdefault(r.get('view_id'), []).append((i, r))

    for vid, items in by_vid.items():
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue
        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}
        native_plates = _eu_native_elev_plates(
            [r for _, r in items], vid, comp, main_lbls)
        # Also accept plates from truly native rows on this view (recompute from full results)
        native_plates |= _eu_native_elev_plates(results, vid, comp, main_lbls)
        if not native_plates:
            # elev with only expands → wipe expands (empty-WM elev handled elsewhere)
            for i, r in items:
                if r.get('_eu_typ_expand') or r.get('_eu_assy_end'):
                    drop.add(i)
            continue
        for i, r in items:
            if not (r.get('_eu_typ_expand') or r.get('_eu_assy_end')
                    or r.get('_eu_typ_soft') or r.get('_eu_typ_rematch')):
                continue
            # Keep L/R mirrors that still involve a native elev plate
            pair_plates = {
                p for p in (r.get('part1'), r.get('part2'))
                if p and p != comp and p not in main_lbls}
            if pair_plates & native_plates:
                continue
            drop.add(i)

    if not drop:
        return 0
    kept = [r for i, r in enumerate(results) if i not in drop]
    n = len(results) - len(kept)
    results[:] = kept
    return n


def cleanup_eu_plate_face_expands(results, part_lines_map, comp_dims=None,
                                 scale=10.0, part_number_map=None, comp=None,
                                 main_view_ids=None):
    """Drop TYP expands that landed on plate-face / bolt-layout views."""
    if not results or not part_lines_map:
        return 0
    drop = set()
    for i, r in enumerate(results):
        if not (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')
                or r.get('_eu_typ_rematch') or r.get('_eu_assy_end')):
            continue
        vid = r.get('view_id')
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if main_view_ids and vid in main_view_ids:
            continue
        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map or {}, comp or '',
            comp_dims=comp_dims, scale=scale)
        if _eu_is_plan_or_bolt_view(
                vparts, main_body_set, bbox=tbb, comp_dims=comp_dims, scale=scale):
            drop.add(i)
    if not drop:
        return 0
    kept = [r for i, r in enumerate(results) if i not in drop]
    n = len(results) - len(kept)
    results[:] = kept
    return n


def fill_eu_sparse_fillet_plates(
        results, part_lines_map, part_number_map, comp, part_dims=None,
        comp_dims=None, scale=10.0, adj_tol=4.5, wm_views=None,
        main_view_ids=None):
    """
    Same-view per-plate fill: single fillet WM on a compact plate with ≥3
    face-adj edges → emit the remaining sides (e.g. seat plate A0218 on D-D).
    Does not rematch whole WM views.
    """
    import math
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    added = 0
    depth = float((comp_dims or {}).get('depth') or 290)
    flange = float((comp_dims or {}).get('flange_w') or 300)
    compact_cap = max(depth, flange) * 1.15

    by_view = defaultdict(list)
    for r in results:
        by_view[r.get('view_id')].append(r)

    for vid, rows in by_view.items():
        if vid not in wm_views:
            continue
        if main_view_ids and vid in main_view_ids:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue
        if _eu_is_plan_or_bolt_view(
                vparts, set(), bbox=tbb, comp_dims=comp_dims, scale=scale):
            continue
        if not _eu_is_section_cut_view(vparts, tbb, comp_dims=comp_dims, scale=scale):
            # Still allow rematch-like compact cuts that miss dims slightly
            w = (tbb[2] - tbb[0]) * scale
            h = (tbb[3] - tbb[1]) * scale
            if max(w, h) / max(min(w, h), 1) > 2.8:
                continue

        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}

        # Unique native geos per plate label
        plate_geos = defaultdict(list)
        for r in rows:
            if (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')
                    or r.get('_eu_typ_rematch') or r.get('_eu_assy_end')
                    or r.get('_eu_typ_mirror') or r.get('_eu_plate_sides')):
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            for p in (r.get('part1'), r.get('part2')):
                if p and p not in main_lbls and p != comp:
                    key = (round(pos[0], 1), round(pos[1], 1),
                           round(float(r.get('length_mm') or 0), 1))
                    plate_geos[p].append((r, key))

        for lbl, entries in plate_geos.items():
            uniq = {}
            native_only = {}
            multi_only = {}
            for r, key in entries:
                uniq.setdefault(key, r)
                if r.get('_eu_multi_arrow'):
                    multi_only.setdefault(key, r)
                else:
                    native_only.setdefault(key, r)
            # Exactly one true WM seed (ignore multi-arrow extras that land
            # on the same plate — those must not block 3-side fill).
            # Do not seed from a lone multi-arrow tip: that tip is already
            # the weld; using it as sparse-fill seed invents extra sides.
            multi_seed = False
            if len(native_only) == 1:
                seed_r = next(iter(native_only.values()))
            else:
                continue
            seed_L = float(seed_r.get('length_mm') or 0)
            # Compact plate from BOM
            dims = (part_dims or {}).get(lbl) or {}
            bw = float(dims.get('width') or dims.get('w') or 0)
            bl = float(dims.get('bom_len') or dims.get('L') or dims.get('length') or 0)
            if bw and bl and max(bw, bl) > compact_cap:
                continue
            if not bw and not bl and seed_L > compact_cap:
                continue

            # Plate part blocks in this view
            pns = [pn for pn, pl in part_number_map.items()
                   if pl == lbl and pn in vparts and pn not in main_body_set]
            if not pns:
                continue

            # Prefer reuse of EU multi-side enum (same as 3S WM path)
            arrow = seed_r.get('dxf_pos')
            selected = []
            if arrow:
                matches = []
                for pn in pns:
                    for g_ln in vparts.get(pn, []):
                        if g_ln.get('length', 0) < 1.5:
                            continue
                        matches.append({'part': pn, 'line': g_ln})
                if matches:
                    ms = enumerate_eu_multiside_edges(
                        arrow, matches, vparts, part_number_map, part_dims,
                        comp, 3, adj_tol=adj_tol, scale=scale,
                        min_edge_cad=1.5, comp_dims=comp_dims)
                    if ms and ms.get('weld_edges_by_gusset'):
                        for _gn, edges in ms['weld_edges_by_gusset'].items():
                            for leng, ob, frags in edges:
                                if not frags:
                                    continue
                                g_ln = frags[0]
                                Lmm = g_ln['length'] * scale
                                if Lmm < max(50.0, seed_L * 0.45):
                                    continue
                                selected.append({
                                    'length_mm': Lmm,
                                    'partner': part_number_map.get(ob, comp),
                                    'gusset': part_number_map.get(_gn, lbl),
                                    'mid': _eu_edge_mid(g_ln),
                                    'face': True,
                                })
                        # Dedup enum hits
                        dedup, seen = [], set()
                        for e in selected:
                            key = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                                   round(e['length_mm'], 1))
                            if key in seen:
                                continue
                            seen.add(key)
                            dedup.append(e)
                        selected = dedup

            if len(selected) < 3:
                all_edges = []
                for pn in pns:
                    all_edges.extend(_eu_collect_plate_face_edges(
                        pn, vparts, part_number_map, main_body_set,
                        comp, adj_tol, scale, face_only=False))
                # Also edges ON other parts that face this plate (weld line
                # often lives on the partner when the plate outline is short).
                for opn, olbl in list(part_number_map.items()):
                    if opn not in vparts or olbl == lbl:
                        continue
                    for e in _eu_collect_plate_face_edges(
                            opn, vparts, part_number_map, main_body_set,
                            comp, adj_tol, scale, face_only=False):
                        if e.get('partner') != lbl:
                            continue
                        all_edges.append({
                            'length_mm': e['length_mm'],
                            'partner': e['gusset'],
                            'gusset': lbl,
                            'mid': e['mid'],
                            'face': e.get('face'),
                        })
                # Perimeter BOM/seed lengths on the plate itself
                for pn in pns:
                    for g_ln in vparts.get(pn, []):
                        Lmm = g_ln.get('length', 0) * scale
                        if Lmm < 40:
                            continue
                        if not (
                            abs(Lmm - seed_L) <= max(12.0, 0.22 * seed_L)
                            or (bw and abs(Lmm - bw) <= max(12.0, 0.22 * bw))
                            or (bl and abs(Lmm - bl) <= max(12.0, 0.22 * bl))
                        ):
                            continue
                        mid = _eu_edge_mid(g_ln)
                        nb = _eu_best_neighbor_for_edge(
                            g_ln, vparts, {pn}, part_number_map,
                            main_body_set, comp, adj_tol + 2.0)
                        olbl = part_number_map.get(nb[0], comp) if nb else comp
                        all_edges.append({
                            'length_mm': Lmm, 'partner': olbl if nb else comp,
                            'gusset': lbl, 'mid': mid, 'face': bool(nb and nb[2]),
                        })
                # Drop stub thickness edges
                all_edges = [e for e in all_edges
                             if e['length_mm'] >= max(50.0, seed_L * 0.45)]
                face_e = [e for e in all_edges if e.get('face')]
                pool = face_e if len(face_e) >= 3 else all_edges
                dedup, seen = [], set()
                for e in pool:
                    key = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                           round(e['length_mm'], 1), e.get('partner'))
                    if key in seen:
                        continue
                    seen.add(key)
                    dedup.append(e)
                pool = dedup
                if len(pool) >= 3 and (
                        any(abs(e['length_mm'] - seed_L) <= max(12.0, 0.22 * seed_L)
                            for e in pool)
                        or any(
                            (bw and abs(e['length_mm'] - bw) <= max(12.0, 0.22 * bw))
                            or (bl and abs(e['length_mm'] - bl) <= max(12.0, 0.22 * bl))
                            for e in pool)):
                    pool_sorted = sorted(
                        pool,
                        key=lambda e: (
                            0 if abs(e['length_mm'] - seed_L) <= max(12.0, 0.22 * seed_L) else 1,
                            0 if e.get('face') else 1,
                            -e['length_mm'],
                        ))
                    selected = [pool_sorted[0]]
                    rest = pool_sorted[1:]
                    while len(selected) < 3 and rest:
                        cms = [e['mid'] for e in selected]
                        used_p = {e['partner'] for e in selected}
                        best_i, best_sc = -1, -1
                        for i, e in enumerate(rest):
                            spr = min(math.hypot(e['mid'][0] - c[0], e['mid'][1] - c[1])
                                      for c in cms)
                            sc = spr * (1.35 if e['partner'] not in used_p else 1.0)
                            if e.get('face'):
                                sc *= 1.1
                            if sc > best_sc:
                                best_sc, best_i = sc, i
                        selected.append(rest.pop(best_i))

            if len(selected) < 3 and arrow:
                # Edge-on seat plates often lack long outline edges — gather
                # long nearby partner edges around the seed WM tip.
                near_pool = []
                for opn, olns in vparts.items():
                    olbl = part_number_map.get(opn) or '?'
                    for g_ln in olns:
                        Lmm = g_ln.get('length', 0) * scale
                        if Lmm < max(50.0, seed_L * 0.45):
                            continue
                        mid = _eu_edge_mid(g_ln)
                        d = math.hypot(mid[0] - arrow[0], mid[1] - arrow[1])
                        if d > max(22.0, seed_L * 0.08):
                            continue
                        # Attribute to plate: partner is the edge owner if owner
                        # is not the plate; else find neighbor.
                        if olbl == lbl:
                            nb = _eu_best_neighbor_for_edge(
                                g_ln, vparts, {opn}, part_number_map,
                                main_body_set, comp, adj_tol + 2.0)
                            partner = part_number_map.get(nb[0], comp) if nb else comp
                        else:
                            partner = olbl
                        near_pool.append({
                            'length_mm': Lmm, 'partner': partner, 'gusset': lbl,
                            'mid': mid, 'face': True, '_d': d,
                        })
                dedup, seen = [], set()
                for e in near_pool:
                    key = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                           round(e['length_mm'], 1))
                    if key in seen:
                        continue
                    # Avoid stealing existing multi-side stiffener mids (2S/3S PP)
                    if any(
                        r2.get('dxf_pos')
                        and r2.get('weld_type') == 'PP'
                        and abs(r2['dxf_pos'][0] - e['mid'][0]) < 2.5
                        and abs(r2['dxf_pos'][1] - e['mid'][1]) < 2.5
                        and lbl not in (r2.get('part1'), r2.get('part2'))
                        for r2 in rows
                        if not (r2.get('_eu_typ_expand') or r2.get('_eu_typ_rematch')
                                or r2.get('_eu_plate_sides'))
                    ):
                        continue
                    seen.add(key)
                    dedup.append(e)
                # Prefer seed/BOM lengths first
                dedup.sort(key=lambda e: (
                    0 if abs(e['length_mm'] - seed_L) <= max(12.0, 0.22 * seed_L) else 1,
                    0 if (bw and abs(e['length_mm'] - bw) <= max(12.0, 0.22 * bw)) else 1,
                    0 if (bl and abs(e['length_mm'] - bl) <= max(12.0, 0.22 * bl)) else 1,
                    e.get('_d', 0), -e['length_mm'],
                ))
                if len(dedup) >= 3:
                    # Prefer distinct fingerprint lengths (seed + BOM sides)
                    targets = [seed_L]
                    if bw:
                        targets.append(bw)
                    if bl and bl not in targets:
                        targets.append(bl)
                    selected = []
                    used_keys = set()
                    for tL in targets:
                        best = None
                        for e in dedup:
                            key = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                                   round(e['length_mm'], 1))
                            if key in used_keys:
                                continue
                            if abs(e['length_mm'] - tL) > max(12.0, 0.22 * tL):
                                continue
                            if best is None or e.get('_d', 99) < best.get('_d', 99):
                                best = e
                        if best:
                            selected.append(best)
                            used_keys.add((round(best['mid'][0], 1),
                                           round(best['mid'][1], 1),
                                           round(best['length_mm'], 1)))
                    # Fill to 3 with spread among remaining
                    rest = [e for e in dedup
                            if (round(e['mid'][0], 1), round(e['mid'][1], 1),
                                round(e['length_mm'], 1)) not in used_keys]
                    while len(selected) < 3 and rest:
                        cms = [e['mid'] for e in selected] if selected else [(0, 0)]
                        used_p = {e['partner'] for e in selected}
                        used_L = {round(e['length_mm'], 0) for e in selected}
                        best_i, best_sc = -1, -1
                        for i, e in enumerate(rest):
                            spr = min(math.hypot(e['mid'][0] - c[0], e['mid'][1] - c[1])
                                      for c in cms)
                            sc = spr
                            if e['partner'] not in used_p:
                                sc *= 1.4
                            if round(e['length_mm'], 0) not in used_L:
                                sc *= 1.25
                            if sc > best_sc:
                                best_sc, best_i = sc, i
                        chosen = rest.pop(best_i)
                        selected.append(chosen)
                        used_keys.add((round(chosen['mid'][0], 1),
                                       round(chosen['mid'][1], 1),
                                       round(chosen['length_mm'], 1)))

            if len(selected) < 3:
                continue
            # Final stub filter
            selected = [e for e in selected
                        if e['length_mm'] >= max(50.0, seed_L * 0.45)]
            # Sparse fill is for the seed plate against its known partners /
            # main body — do not invent contacts to unrelated nearby plates
            # (e.g. seat WM picking up an adjacent stiffener thickness edge).
            _allowed = {comp} | set(main_lbls)
            for _p in (seed_r.get('part1'), seed_r.get('part2')):
                if _p:
                    _allowed.add(_p)
            selected = [
                e for e in selected
                if lbl in (e.get('gusset'), e.get('partner'))
                and (
                    (e.get('gusset') == lbl and e.get('partner') in _allowed)
                    or (e.get('partner') == lbl and e.get('gusset') in _allowed)
                )
            ]
            if len(selected) < 3:
                continue

            seed_side = seed_r.get('position') or 'Above'
            hf = seed_r.get('hf') if seed_r.get('hf') is not None else 6.0
            seed_ann = seed_r.get('annotation') or ''
            seed_wt = seed_r.get('weld_type') or 'PP'
            # Never invent CJP / PL* sides from a fillet seed on H cuts.
            seed_is_cjp = (
                seed_wt == 'CJP'
                or (isinstance(seed_ann, str)
                    and (seed_ann.upper().startswith('CJP')
                         or seed_ann.upper().startswith('PL')))
                or hf is None
            )
            if seed_is_cjp:
                continue
            # Multi-arrow-only seed: emit at most one complementary new mid
            # (the dual tip already counts as one side).
            n_added_here = 0
            for e in selected:
                p1, p2 = sorted((e['gusset'], e['partner']))
                mid = e['mid']
                L = round(e['length_mm'], 1)
                # Skip only if same mid+length already present (don't block
                # distinct seat sides that share a nearby tip).
                if any(
                    r2.get('view_id') == vid
                    and r2.get('dxf_pos')
                    and abs(r2['dxf_pos'][0] - mid[0]) < 2.0
                    and abs(r2['dxf_pos'][1] - mid[1]) < 2.0
                    and abs(float(r2.get('length_mm') or 0) - L) < 1.0
                    for r2 in results
                ):
                    continue
                if multi_seed and n_added_here >= 1:
                    break
                for side in ('Above', 'Below'):
                    results.append({
                        'component': comp,
                        'position': side,
                        'hf': hf,
                        'length_mm': L,
                        'annotation': seed_ann,
                        'part1': p1,
                        'part2': p2,
                        'dxf_pos': mid,
                        'view_id': vid,
                        '_eu_plate_sides': True,
                        'weld_type': seed_wt if seed_wt != 'CJP' else 'PP',
                    })
                    added += 1
                n_added_here += 1
    return added


def expand_eu_fillet_typ_siblings(
        results, part_lines_map, part_number_map, comp, part_dims=None,
        comp_dims=None, scale=10.0, adj_tol=4.5, wm_views=None,
        main_view_ids=None, len_rel=0.22):
    """
    Same-view same-label sibling for TYP fillet clusters (not only 2S/3S).
    Copies the full native half-cluster so a left plate+TYP group can fill
    the opposite same-label plate.
    """
    import math
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    added = 0

    by_view = defaultdict(list)
    for r in results:
        by_view[r.get('view_id')].append(r)

    for vid, rows in by_view.items():
        if vid not in wm_views:
            continue
        if main_view_ids and vid in main_view_ids:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_plan_or_bolt_view(
                vparts, set(), bbox=tbb, comp_dims=comp_dims, scale=scale):
            continue
        if _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue

        main_body_set, sk = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}
        vcx = 0.5 * (tbb[0] + tbb[2])

        lbl_blocks = defaultdict(list)
        for pn in vparts:
            lbl = part_number_map.get(pn)
            if not lbl or lbl in main_lbls or pn in main_body_set:
                continue
            lbl_blocks[lbl].append(pn)
        multi = {lbl: pns for lbl, pns in lbl_blocks.items() if len(pns) >= 2}
        if not multi:
            continue

        native = []
        for r in rows:
            if (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')
                    or r.get('_eu_typ_rematch') or r.get('_eu_assy_end')
                    or r.get('_eu_typ_mirror') or r.get('_eu_plate_sides')
                    or r.get('_eu_fillet_sib')):
                continue
            if not r.get('dxf_pos'):
                continue
            native.append(r)
        if len(native) < 2:
            continue

        def _nearest_pn(lbl, mid, pns):
            best, best_d = None, 1e18
            for pn in pns:
                bb = _eu_part_bbox(vparts.get(pn, []))
                if not bb:
                    continue
                cx = 0.5 * (bb[0] + bb[2])
                cy = 0.5 * (bb[1] + bb[3])
                d = math.hypot(mid[0] - cx, mid[1] - cy)
                if d < best_d:
                    best_d, best = d, pn
            return best

        def _match_edges(edges, ref_lens):
            matched = _eu_match_fingerprint(edges, ref_lens, len_rel=len_rel)
            if matched:
                return matched
            matched, used = [], [False] * len(ref_lens)
            for e in sorted(edges, key=lambda x: (0 if x.get('face') else 1, -x['length_mm'])):
                for i, rl in enumerate(ref_lens):
                    if used[i]:
                        continue
                    if abs(e['length_mm'] - rl) <= max(10.0, len_rel * rl):
                        used[i] = True
                        matched.append(e)
                        break
            return matched

        for lbl, pns in multi.items():
            geos, seen = [], set()
            for r in native:
                if lbl not in (r.get('part1'), r.get('part2')):
                    continue
                pos = r['dxf_pos']
                key = (round(pos[0], 1), round(pos[1], 1),
                       round(float(r.get('length_mm') or 0), 1))
                if key in seen:
                    continue
                seen.add(key)
                geos.append((r, _nearest_pn(lbl, pos, pns), key))
            if len(geos) < 2:
                continue
            per_pn = defaultdict(list)
            for r, pn, key in geos:
                if pn:
                    per_pn[pn].append(r)
            if not per_pn:
                continue
            src_pn = max(per_pn.keys(), key=lambda p: len(per_pn[p]))
            if len(per_pn[src_pn]) < 2:
                continue
            src_bb = _eu_part_bbox(vparts.get(src_pn, []))
            if not src_bb:
                continue
            src_cx = 0.5 * (src_bb[0] + src_bb[2])
            src_half = 'L' if src_cx <= vcx else 'R'

            half_rows, half_seen = [], set()
            for r in native:
                pos = r['dxf_pos']
                if ('L' if pos[0] <= vcx else 'R') != src_half:
                    continue
                key = (round(pos[0], 1), round(pos[1], 1),
                       round(float(r.get('length_mm') or 0), 1))
                if key in half_seen:
                    continue
                half_seen.add(key)
                half_rows.append(r)
            src_geos = half_rows or per_pn[src_pn]
            ref_lens = [float(r.get('length_mm') or 0) for r in src_geos]
            if len(ref_lens) < 2:
                ref_lens = [float(r.get('length_mm') or 0) for r in per_pn[src_pn]]
                src_geos = per_pn[src_pn]

            # Relative role of each seed mid inside the source plate bbox
            # (keeps "top L=107" from matching "bottom L=107" on the sibling).
            def _rel(mid, bb):
                return (
                    (mid[0] - bb[0]) / max(bb[2] - bb[0], 1e-6),
                    (mid[1] - bb[1]) / max(bb[3] - bb[1], 1e-6),
                )

            seed_roles = []
            for r in src_geos:
                pos = r['dxf_pos']
                seed_roles.append((
                    float(r.get('length_mm') or 0),
                    _rel(pos, src_bb),
                    r,
                ))

            # Tip snapped onto a longer flange while sitting on a same-label
            # plate top/bottom face: rewrite length/role to that plate edge so
            # the sibling gets the plate bottom/top mid (not flange outer tip).
            src_face = _eu_collect_plate_face_edges(
                src_pn, vparts, part_number_map, main_body_set,
                comp, adj_tol, scale, face_only=True)
            rewritten = []
            used_face = set()
            for Lref, (rx, ry), sr in seed_roles:
                tip = sr['dxf_pos']
                best_e, best_d = None, 3.0
                for e in src_face:
                    if e.get('gusset') != lbl and e.get('gusset_pn') != src_pn:
                        if part_number_map.get(e.get('gusset_pn')) != lbl:
                            continue
                    mid = e['mid']
                    d = math.hypot(mid[0] - tip[0], mid[1] - tip[1])
                    if d >= best_d:
                        continue
                    # Prefer horizontal face edges when tip is near top/bottom
                    ln = e.get('line') or {}
                    s0, s1 = ln.get('start'), ln.get('end')
                    horiz = bool(
                        s0 and s1
                        and abs(s1[0] - s0[0]) >= abs(s1[1] - s0[1]) * 1.2)
                    if not horiz and abs(ry - 0.5) < 0.25:
                        continue
                    best_d, best_e = d, e
                if best_e is not None:
                    mid = best_e['mid']
                    key = (round(mid[0], 1), round(mid[1], 1),
                           round(best_e['length_mm'], 1))
                    used_face.add(key)
                    nr = dict(sr)
                    nr['length_mm'] = round(best_e['length_mm'], 1)
                    nr['dxf_pos'] = mid
                    if best_e.get('partner'):
                        p1, p2 = sorted((lbl, best_e['partner']))
                        nr['part1'], nr['part2'] = p1, p2
                    rewritten.append((
                        float(best_e['length_mm']),
                        _rel(mid, src_bb),
                        nr,
                    ))
                else:
                    rewritten.append((Lref, (rx, ry), sr))
            seed_roles = rewritten

            # Also include opposite-role same-length face edges on the source
            # plate (top L=107 seed → also bottom L=107) so TYP siblings get
            # the full 3-side stiffener set.
            for e in src_face:
                mid = e['mid']
                key = (round(mid[0], 1), round(mid[1], 1),
                       round(e['length_mm'], 1))
                if key in used_face:
                    continue
                if e.get('gusset') != lbl and part_number_map.get(
                        e.get('gusset_pn')) != lbl:
                    if e.get('gusset_pn') != src_pn:
                        continue
                Lmm = float(e['length_mm'])
                erx, ery = _rel(mid, src_bb)
                # Need a same-length seed already on the opposite y role
                mate = False
                for Lref, (rx, ry), _sr in seed_roles:
                    if abs(Lref - Lmm) > max(10.0, len_rel * max(Lref, Lmm)):
                        continue
                    if (ry > 0.55 and ery < 0.45) or (ry < 0.45 and ery > 0.55):
                        mate = True
                        break
                if not mate:
                    continue
                if any(
                    abs(mid[0] - sr['dxf_pos'][0]) < 2.5
                    and abs(mid[1] - sr['dxf_pos'][1]) < 2.5
                    for _L, _r, sr in seed_roles
                ):
                    continue
                syn = {
                    'dxf_pos': mid,
                    'length_mm': round(Lmm, 1),
                    'position': 'Above',
                    'hf': (src_geos[0].get('hf') if src_geos else 6),
                    'annotation': '',
                    'part1': lbl,
                    'part2': e.get('partner') or comp,
                }
                seed_roles.append((Lmm, (erx, ery), syn))
                used_face.add(key)

            template = {}
            for r in src_geos:
                template[r.get('position')] = {
                    'hf': r.get('hf'), 'annotation': r.get('annotation') or ''}
            # Always emit Above+Below so annotator can pair (half_rows dedupes
            # by tip and may only keep one side in template).
            sides = ['Above', 'Below']
            if not template:
                template = {
                    'Above': {'hf': (src_geos[0].get('hf') if src_geos else 6),
                              'annotation': ''},
                    'Below': {'hf': (src_geos[0].get('hf') if src_geos else 6),
                              'annotation': ''},
                }

            for sib in pns:
                if sib == src_pn or per_pn.get(sib):
                    continue
                sib_bb = _eu_part_bbox(vparts.get(sib, []))
                if not sib_bb:
                    continue
                sib_cx = 0.5 * (sib_bb[0] + sib_bb[2])
                if (sib_cx <= vcx) == (src_cx <= vcx):
                    if abs((2 * vcx - src_cx) - sib_cx) > max(
                            abs(sib_bb[2] - sib_bb[0]), 8.0) * 1.5:
                        continue
                sib_half = 'L' if sib_cx <= vcx else 'R'
                edge_pool = _eu_collect_plate_face_edges(
                    sib, vparts, part_number_map, main_body_set,
                    comp, adj_tol, scale, face_only=False)
                for pn2, lbl2 in list(part_number_map.items()):
                    if pn2 not in vparts or pn2 in main_body_set or pn2 == sib:
                        continue
                    if not lbl2 or lbl2 in main_lbls:
                        continue
                    bb2 = _eu_part_bbox(vparts.get(pn2, []))
                    if not bb2:
                        continue
                    if ('L' if 0.5 * (bb2[0] + bb2[2]) <= vcx else 'R') != sib_half:
                        continue
                    edge_pool.extend(_eu_collect_plate_face_edges(
                        pn2, vparts, part_number_map, main_body_set,
                        comp, adj_tol, scale, face_only=False))
                dedup, seen_e = [], set()
                for e in edge_pool:
                    k = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                         round(e['length_mm'], 1), e.get('gusset'), e.get('partner'))
                    if k in seen_e:
                        continue
                    seen_e.add(k)
                    dedup.append(e)

                # Pair each seed to best same-length + same relative role edge
                matched, used = [], set()
                for Lref, (rx, ry), _sr in seed_roles:
                    seed_mid = _sr['dxf_pos']
                    flip_x = 2 * vcx - seed_mid[0]
                    best, best_sc = None, 1e18
                    for i, e in enumerate(dedup):
                        if i in used:
                            continue
                        if abs(e['length_mm'] - Lref) > max(10.0, len_rel * Lref):
                            continue
                        # Prefer edges belonging to the sibling plate itself
                        pen = 0.0 if (
                            e.get('gusset_pn') == sib
                            or e.get('gusset') == part_number_map.get(sib)
                        ) else 10.0
                        erx, ery = _rel(e['mid'], sib_bb)
                        # y role must match; x uses flipped seed + relative role
                        sc = abs(ery - ry) * 55.0
                        sc += abs(erx - (1.0 - rx)) * 18.0
                        sc += abs(e['mid'][0] - flip_x) * 0.45
                        sc += abs(e['length_mm'] - Lref) / max(Lref, 1.0) * 5.0
                        sc += pen
                        if not e.get('face'):
                            sc += 4.0
                        if sc < best_sc:
                            best_sc, best = sc, (i, e)
                    if best is not None:
                        used.add(best[0])
                        matched.append(best[1])

                if len(matched) < max(2, len(seed_roles) - 1):
                    # Fallback to pure length fingerprint
                    matched = _match_edges(dedup, ref_lens)
                need = len(seed_roles) if seed_roles else len(
                    set(round(x, 0) for x in ref_lens))
                if len(matched) < max(2, need - 1):
                    continue
                if len(matched) > need:
                    matched = matched[:need]
                # Ensure sibling plate's own unmatched face edges of the same
                # lengths as seeds are kept (inner vertical often loses the
                # role-score race to a near-duplicate web-gap tip).
                _matched_mids = {
                    (round(e['mid'][0], 1), round(e['mid'][1], 1),
                     round(e['length_mm'], 1))
                    for e in matched
                }
                _seed_lens = [float(Lref) for Lref, _r, _sr in seed_roles]
                for e in dedup:
                    if e.get('gusset_pn') != sib and e.get('gusset') != part_number_map.get(sib):
                        continue
                    if not e.get('face'):
                        continue
                    Lmm = float(e['length_mm'])
                    if not any(
                        abs(Lmm - sl) <= max(10.0, len_rel * max(Lmm, sl))
                        for sl in _seed_lens
                    ):
                        continue
                    mk = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                          round(Lmm, 1))
                    if mk in _matched_mids:
                        continue
                    matched.append(e)
                    _matched_mids.add(mk)
                for e in matched:
                    p1, p2 = sorted((e['gusset'], e['partner']))
                    mid, L = e['mid'], round(e['length_mm'], 1)
                    # Long horizontal fillet on a non-sibling plate (e.g. flange
                    # strip): put tip on the outer end. Same-label stiffener
                    # edges keep their edge mid (plate top/bottom center).
                    ln = e.get('line') or {}
                    s0, s1 = ln.get('start'), ln.get('end')
                    sib_lbl = part_number_map.get(sib)
                    if (s0 and s1 and L >= 180
                            and e.get('gusset') != sib_lbl
                            and abs(s1[0] - s0[0]) >= abs(s1[1] - s0[1]) * 2.5):
                        if sib_half == 'R':
                            mid = s0 if s0[0] >= s1[0] else s1
                        else:
                            mid = s0 if s0[0] <= s1[0] else s1
                    if _eu_result_covers(results, vid, L, (p1, p2), mid, tol=5.0):
                        continue
                    # Near-dup only when conflict tip is inside THIS sibling
                    # plate bbox. Web-gap native mid (~0.8mm from sib face)
                    # must not suppress the sib's own inner vertical.
                    _near_dup = False
                    _sbb = sib_bb
                    for _er in results:
                        if _er.get('view_id') != vid or not _er.get('dxf_pos'):
                            continue
                        if abs((_er.get('length_mm') or 0) - L) > max(
                                8.0, 0.15 * max(L, 1.0)):
                            continue
                        _ep = _er['dxf_pos']
                        if (abs(_ep[0] - mid[0]) > 6.0
                                or abs(_ep[1] - mid[1]) > 6.0):
                            continue
                        # Tight pad: web-gap tips sit ~0.8mm outside the
                        # plate face and must remain non-blocking.
                        if (_sbb[0] - 0.25 <= _ep[0] <= _sbb[2] + 0.25
                                and _sbb[1] - 0.25 <= _ep[1] <= _sbb[3] + 0.25):
                            _near_dup = True
                            break
                    if _near_dup:
                        continue
                    for side in sides:
                        meta = template.get(side) or next(
                            iter(template.values()), {'hf': 6, 'annotation': ''})
                        # Skip only if this side already exists on THIS sibling
                        # plate (web-gap native tip is ~5mm away but off-plate).
                        _side_dup = False
                        for _er in results:
                            if (_er.get('view_id') != vid
                                    or _er.get('position') != side
                                    or not _er.get('dxf_pos')):
                                continue
                            if abs((_er.get('length_mm') or 0) - L) > max(
                                    8.0, 0.15 * max(L, 1.0)):
                                continue
                            _ep = _er['dxf_pos']
                            if (abs(_ep[0] - mid[0]) > 3.0
                                    or abs(_ep[1] - mid[1]) > 3.0):
                                continue
                            if (_sbb[0] - 0.25 <= _ep[0] <= _sbb[2] + 0.25
                                    and _sbb[1] - 0.25 <= _ep[1] <= _sbb[3] + 0.25):
                                _side_dup = True
                                break
                        if _side_dup:
                            continue
                        results.append({
                            'component': comp, 'position': side,
                            'hf': meta.get('hf'), 'length_mm': L,
                            'annotation': meta.get('annotation') or '',
                            'part1': p1, 'part2': p2, 'dxf_pos': mid,
                            'view_id': vid,
                            '_eu_typ_expand': True, '_eu_fillet_sib': True,
                        })
                        added += 1
    return added


def refine_eu_section_expand_mids(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, adj_tol=4.5, main_view_ids=None, len_rel=0.22):
    """
    Pull fingerprint mids into the weld cluster band on compact sections.
    Handles single-length TYP seeds that skip multi-edge cluster refine
    (e.g. L=300 landing at y=-189 instead of ~-173).
    """
    import math
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    main_view_ids = set(main_view_ids or [])
    by_view = defaultdict(list)
    for i, r in enumerate(results):
        by_view[r.get('view_id')].append((i, r))

    n_fix = 0
    for vid, items in by_view.items():
        if main_view_ids and vid in main_view_ids:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb or not _eu_is_section_cut_view(
                vparts, tbb, comp_dims=comp_dims, scale=scale):
            continue
        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        core_ys = [
            r['dxf_pos'][1] for _, r in items
            if r.get('dxf_pos') and (
                r.get('_eu_typ_rematch')
                or not (r.get('_eu_typ_expand') or r.get('_eu_typ_soft'))
            )
        ]
        ys = core_ys if len(core_ys) >= 2 else [
            r['dxf_pos'][1] for _, r in items if r.get('dxf_pos')]
        if len(ys) < 2:
            continue
        med = sorted(ys)[len(ys) // 2]
        # Collect alternate face edges in the view
        pool = []
        for pn in vparts:
            if pn in main_body_set:
                continue
            pool.extend(_eu_collect_plate_face_edges(
                pn, vparts, part_number_map, main_body_set,
                comp, adj_tol, scale, face_only=False))
        for i, r in items:
            if not (r.get('_eu_typ_expand') or r.get('_eu_typ_rematch')
                    or r.get('_eu_typ_soft')):
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            if abs(pos[1] - med) <= 12.0:
                continue
            # Skip if this mid already sits in a local band of same-length welds
            # (ignore Above/Below duplicates at the exact same midpoint)
            my_key = (round(pos[0], 1), round(pos[1], 1),
                      round(float(r.get('length_mm') or 0), 1))
            near = sum(
                1 for _, r2 in items
                if r2.get('dxf_pos')
                and r2 is not r
                and abs(float(r2.get('length_mm') or 0) - float(r.get('length_mm') or 0)) < 15.0
                and abs(r2['dxf_pos'][1] - pos[1]) < 8.0
                and abs(r2['dxf_pos'][0] - pos[0]) < 25.0
                and (round(r2['dxf_pos'][0], 1), round(r2['dxf_pos'][1], 1),
                     round(float(r2.get('length_mm') or 0), 1)) != my_key)
            if near >= 1:
                continue
            L = float(r.get('length_mm') or 0)
            pair = frozenset((r.get('part1'), r.get('part2')))
            alts = [
                e for e in pool
                if abs(e['length_mm'] - L) <= max(10.0, len_rel * L)
                and frozenset((e.get('gusset'), e.get('partner'))) == pair
            ]
            if not alts:
                alts = [
                    e for e in pool
                    if abs(e['length_mm'] - L) <= max(10.0, len_rel * L)
                    and (e.get('gusset') in pair or e.get('partner') in pair)
                ]
            if alts:
                # Prefer face mid near cluster median; slight preference for
                # staying close in x so left-plate right edges keep identity.
                best = min(alts, key=lambda e: (
                    abs(e['mid'][1] - med)
                    + 0.35 * abs(e['mid'][0] - pos[0])
                    + (0 if e.get('face') else 6)))
                if abs(best['mid'][1] - med) <= 14.0 and (
                        abs(best['mid'][1] - med) + 0.5 < abs(pos[1] - med)
                        or abs(best['mid'][0] - pos[0]) + abs(best['mid'][1] - pos[1])
                        < abs(pos[1] - med)):
                    if (abs(best['mid'][0] - pos[0]) > 0.3
                            or abs(best['mid'][1] - pos[1]) > 0.3):
                        r['dxf_pos'] = best['mid']
                        r['length_mm'] = round(best['length_mm'], 1)
                        n_fix += 1
                    continue
            # Last resort for true loners only — keep x, snap y to cluster
            if abs(pos[1] - med) > 8.0:
                r['dxf_pos'] = (pos[0], med)
                n_fix += 1
    return n_fix


def _eu_view_bbox(vparts):
    xs, ys = [], []
    for lns in vparts.values():
        for ln in lns:
            xs.extend([ln['start'][0], ln['end'][0]])
            ys.extend([ln['start'][1], ln['end'][1]])
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _eu_map_label(src_lbl, target_lbls, part_dims, main_lbls, comp):
    """Map a seed part label onto a label present in the target view."""
    if src_lbl in target_lbls:
        return src_lbl
    if src_lbl == comp or src_lbl in main_lbls:
        for t in target_lbls:
            if t == comp or t in main_lbls:
                return t
        return comp
    src_pd = (part_dims or {}).get(src_lbl) or {}
    sw = float(src_pd.get('width') or 0)
    sl = float(src_pd.get('bom_len') or 0)
    best, best_sc = None, 1e18
    plates = [t for t in target_lbls if t != comp and t not in main_lbls]
    for t in plates:
        td = (part_dims or {}).get(t) or {}
        tw = float(td.get('width') or 0)
        tl = float(td.get('bom_len') or 0)
        if sw <= 0 and sl <= 0:
            continue
        sc = 0.0
        n = 0
        if sw > 0 and tw > 0:
            sc += abs(sw - tw) / sw; n += 1
        if sl > 0 and tl > 0:
            sc += abs(sl - tl) / sl; n += 1
        if n:
            sc /= n
        if sc < best_sc:
            best_sc, best = sc, t
    if best is not None and best_sc < 0.55:
        return best
    # Fallback: sole non-main plate, else first plate
    if len(plates) == 1:
        return plates[0]
    return plates[0] if plates else None


def _eu_is_assembly_view(vparts, bbox):
    """Long elevation / main view: wide aspect, many parts."""
    if not bbox or not vparts:
        return False
    w = bbox[2] - bbox[0]
    h = max(bbox[3] - bbox[1], 1e-6)
    n = len(vparts)
    return (w / h >= 2.2 and n >= 4) or n >= 10


def _eu_is_long_elevation(vparts, bbox, part_number_map=None, comp=None,
                          view_id=None, main_view_ids=None):
    """
    True main-side elev only (1-1).

    When Tekla SectionMark mapping is available, ONLY those views count —
    never mistake a wide A-A sheet for the assembly elev.
    """
    if main_view_ids is not None and view_id is not None:
        return view_id in main_view_ids

    if not bbox or not vparts:
        return False
    w = bbox[2] - bbox[0]
    h = max(bbox[3] - bbox[1], 1e-6)
    n = len(vparts)
    aspect = w / h
    if aspect < 2.5 or n < 4:
        return False

    # Prefer: elongated main-member mark spans most of the view width
    if part_number_map and comp:
        best = 0.0
        for pn, lns in vparts.items():
            if part_number_map.get(pn) != comp:
                continue
            pbb = _eu_part_bbox(lns)
            if not pbb:
                continue
            pw = pbb[2] - pbb[0]
            ph = max(pbb[3] - pbb[1], 1e-6)
            if pw / ph >= 2.5:
                best = max(best, pw / max(w, 1e-6))
        if best >= 0.45:
            return True
        return False

    return aspect >= 4.5 and n >= 10


def _eu_is_section_cut_view(vparts, bbox, comp_dims=None, scale=10.0):
    """Compact H/工 section cut (not long elevation)."""
    if not bbox or not vparts:
        return False
    if _eu_is_assembly_view(vparts, bbox):
        return False
    w = (bbox[2] - bbox[0]) * scale
    h = (bbox[3] - bbox[1]) * scale
    aspect = max(w, h) / max(min(w, h), 1.0)
    if aspect > 2.6:
        return False
    depth = float((comp_dims or {}).get('depth') or 0)
    flange = float((comp_dims or {}).get('flange_w') or 0)
    if depth and flange and _eu_dims_match_section(w, h, depth, flange, tol=0.30):
        return True
    # Stiffener cuts with a protruding plate (e.g. AB0003 C-C/D-D): view AABB
    # is elongated, but one part still matches the HE/I outline.
    if depth and flange and 2 <= len(vparts) <= 10:
        for _lns in vparts.values():
            _bb = _eu_part_bbox(_lns)
            if not _bb:
                continue
            _pw = (_bb[2] - _bb[0]) * scale
            _ph = (_bb[3] - _bb[1]) * scale
            if min(_pw, _ph) < min(depth, flange) * 0.55:
                continue
            if _eu_dims_match_section(_pw, _ph, depth, flange, tol=0.28):
                return True
    # Stiffener sections often extend slightly beyond flange×depth
    return aspect <= 1.6 and 2 <= len(vparts) <= 10


def _eu_is_bolt_or_plate_face_view(vparts, bbox, comp_dims=None, scale=10.0):
    """
    End-plate / flange-face / bolt-layout views: not a true H section cut and
    not a long elevation. Geometric only (no view-letter hardcode).
    """
    if not bbox or not vparts:
        return False
    if _eu_is_assembly_view(vparts, bbox):
        return False
    # True H/工 cuts must never be classified as plate-face
    if _eu_is_section_cut_view(vparts, bbox, comp_dims=comp_dims, scale=scale):
        return False
    w = (bbox[2] - bbox[0]) * scale
    h = (bbox[3] - bbox[1]) * scale
    aspect = max(w, h) / max(min(w, h), 1.0)
    depth = float((comp_dims or {}).get('depth') or 0)
    flange = float((comp_dims or {}).get('flange_w') or 0)
    if depth and flange and _eu_dims_match_section(w, h, depth, flange, tol=0.25):
        return False
    if len(vparts) > 6:
        return False
    if depth <= 0 or flange <= 0:
        return False
    short_d, long_d = min(w, h), max(w, h)
    # Compact flange-face: short ≈ depth/flange, long clearly larger
    if aspect < 2.2:
        if abs(short_d - depth) <= max(35.0, 0.2 * depth) and long_d >= flange * 1.22:
            return True
        if abs(short_d - flange) <= max(35.0, 0.2 * flange) and long_d >= depth * 1.22:
            return True
    # Tall/narrow end-plate face (e.g. short≈flange, long>>section depth):
    # common bolt/end views that exceed the old aspect=2.2 cutoff.
    if len(vparts) <= 4 and aspect < 4.0:
        sec_other = max(depth, flange)
        if abs(short_d - flange) <= max(40.0, 0.22 * flange) and long_d >= max(
                depth * 1.45, flange * 1.45, sec_other * 1.45):
            return True
        if abs(short_d - depth) <= max(40.0, 0.22 * depth) and long_d >= max(
                flange * 1.45, depth * 1.45, sec_other * 1.45):
            return True
    return False


def _eu_is_plan_or_bolt_view(vparts, main_body_set, bbox=None, comp_dims=None,
                            scale=10.0):
    """
    Plan / bolt-layout / plate-face views: skip TYP fingerprint and rematch.
    Plate-face geometry wins even when a main-body mark is present.
    True H/工 section cuts are never treated as plan/bolt.
    """
    if bbox is not None and _eu_is_section_cut_view(
            vparts, bbox, comp_dims=comp_dims, scale=scale):
        return False
    if bbox is not None and _eu_is_bolt_or_plate_face_view(
            vparts, bbox, comp_dims=comp_dims, scale=scale):
        return True
    # Fallback only when no main body AND few parts (generic plan-ish clutter)
    if main_body_set:
        return False
    n = len(vparts)
    if n < 3:
        return False
    return n >= 3 and n <= 8


def expand_eu_typ_from_seeds(
        results, seeds, part_lines_map, part_number_map, comp,
        comp_dims=None, scale=10.0, adj_tol=4.5, len_rel=0.18,
        part_dims=None, wm_views=None, main_view_ids=None):
    """
    TYP essence: similar weld structures in this or other views.
    1) Face-adj length fingerprint match (same marks or remapped geometry)
    2) Same-view assembly L/R mirror of seed mids onto face edges
    3) Empty-WM section: rematch face-adj edges (prefer over soft bbox)
    """
    import math

    if not seeds or not part_lines_map:
        return 0

    added = 0
    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    soft_done = set()
    rematch_done = set()
    mirror_done = set()
    main_cache = {}
    bbox_cache = {}
    for vid, vparts in part_lines_map.items():
        mbs, sk = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        main_cache[vid] = (set(mbs), sk)
        bbox_cache[vid] = _eu_view_bbox(vparts)

    def _norm_partner(lbl, main_set_lbls):
        if lbl == comp or lbl in main_set_lbls:
            return '$MAIN'
        return lbl

    def _emit(vid, p1, p2, mid, L, template_sides, flags=None):
        nonlocal added
        flags = flags or {}
        if _eu_result_covers(results, vid, L, (p1, p2), mid):
            return 0
        n = 0
        sides = list(template_sides.keys()) or ['Above', 'Below']
        force_cjp = bool(flags.get('_eu_cjp_mirror'))
        if force_cjp:
            sides = ['Above']
        for side in sides:
            meta = template_sides.get(side) or next(
                iter(template_sides.values()), {'hf': 6, 'annotation': ''})
            if any(r for r in results
                   if r.get('view_id') == vid
                   and r.get('position') == side
                   and frozenset((r.get('part1'), r.get('part2'))) == frozenset((p1, p2))
                   and abs((r.get('length_mm') or 0) - L) < 0.6
                   and r.get('dxf_pos')
                   and abs(r['dxf_pos'][0] - mid[0]) < 3.5
                   and abs(r['dxf_pos'][1] - mid[1]) < 3.5):
                continue
            ann = meta.get('annotation') or ''
            hf = meta.get('hf')
            if force_cjp or (isinstance(ann, str) and ann.upper().startswith('CJP')):
                ann = 'CJP'
                hf = None
            row = {
                'component': comp,
                'position': side,
                'hf': hf,
                'length_mm': L,
                'annotation': ann,
                'part1': p1,
                'part2': p2,
                'dxf_pos': mid,
                'view_id': vid,
                '_eu_typ_expand': True,
            }
            row.update(flags)
            results.append(row)
            added += 1
            n += 1
        return n

    def _pick_spread(edges, n):
        if len(edges) <= n:
            return list(edges)
        keep = [edges[0]]
        rest = list(edges[1:])
        while len(keep) < n and rest:
            cms = [e['mid'] for e in keep]
            best_i, best_sc = -1, -1
            for i, e in enumerate(rest):
                spr = min(math.hypot(e['mid'][0] - c[0], e['mid'][1] - c[1])
                          for c in cms)
                if spr > best_sc:
                    best_sc, best_i = spr, i
            keep.append(rest.pop(best_i))
        return keep

    def _snap_to_face(vparts, mid, main_body_set, tol=12.0):
        """Snap a mirrored point onto nearest face-adj plate edge midpoint."""
        best = None
        for pn, lns in vparts.items():
            if pn in main_body_set:
                continue
            lbl = part_number_map.get(pn)
            if not lbl or lbl == comp:
                continue
            for e in _eu_collect_plate_face_edges(
                    pn, vparts, part_number_map, main_body_set,
                    comp, adj_tol, scale, face_only=False):
                d = math.hypot(e['mid'][0] - mid[0], e['mid'][1] - mid[1])
                if d > tol:
                    continue
                if best is None or d < best[0]:
                    best = (d, e)
        return best[1] if best else None

    for seed in seeds:
        seed_rows = seed.get('rows') or []
        if not seed_rows:
            continue
        ref_lens, _ = _eu_geo_fingerprint(seed_rows)
        if not ref_lens:
            continue
        uniq_ref = sorted(set(round(x, 0) for x in ref_lens))
        expected_n = int(seed.get('n_sides') or max(1, len(uniq_ref)))

        by_geo = {}
        seed_partners = set()
        for r in seed_rows:
            pos = r.get('dxf_pos') or (0, 0)
            gk = (round(r.get('length_mm', 0), 0),
                  tuple(sorted((r.get('part1'), r.get('part2')))))
            by_geo.setdefault(gk, []).append(r)
            seed_partners.add(r.get('part1'))
            seed_partners.add(r.get('part2'))
        seed_partners.discard(None)

        template_sides = {}
        for _gk, rs in by_geo.items():
            for r in rs:
                template_sides[r.get('position')] = {
                    'hf': r.get('hf'),
                    'annotation': r.get('annotation', ''),
                }

        seed_view = seed.get('view_id')
        seed_main_lbls = {comp}
        smb, _ = main_cache.get(seed_view, (set(), 'H'))
        for mb in smb:
            seed_main_lbls.add(part_number_map.get(mb, comp))
        seed_roles = {_norm_partner(p, seed_main_lbls) for p in seed_partners}
        seed_plate_lbls = {p for p in seed_partners if p not in seed_main_lbls}

        # Unique geo mids from seed for assembly mirror
        geo_seeds = []
        seen_g = set()
        for r in seed_rows:
            pos = r.get('dxf_pos')
            if not pos:
                continue
            gk = (round(r.get('length_mm', 0), 0),
                  tuple(sorted((r.get('part1'), r.get('part2')))),
                  round(pos[0], 0), round(pos[1], 0))
            if gk in seen_g:
                continue
            seen_g.add(gk)
            geo_seeds.append(r)

        view_rank = []
        for vid, vparts in part_lines_map.items():
            vl = {part_number_map.get(pn) for pn in vparts}
            vl.discard(None)
            shared = len(seed_plate_lbls & vl) if seed_plate_lbls else len(seed_partners & vl)
            if vid == seed_view:
                view_rank.append((0, 0, vid))
            elif len(vparts) >= 2:
                view_rank.append((1, -shared, vid))
        view_rank.sort()

        for _prio, _sh, vid in view_rank:
            vparts = part_lines_map.get(vid) or {}
            main_body_set, _sk = main_cache.get(vid, (set(), 'H'))
            main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}
            tbb = bbox_cache.get(vid)

            # Skip bolt/plan / plate-face views (B-B)
            if vid != seed_view and _eu_is_plan_or_bolt_view(
                    vparts, main_body_set, bbox=tbb, comp_dims=comp_dims, scale=scale):
                continue
            # U-cut (A-A) gets u_wrap later; never fingerprint TYP onto it
            if vid != seed_view and _eu_is_u_channel_cut_view(
                    vparts, tbb, part_number_map=part_number_map, comp=comp,
                    scale=scale):
                continue

            plates = [pn for pn in vparts
                      if part_number_map.get(pn) not in (None, comp)
                      and pn not in main_body_set]

            same_wm_view = (vid == seed_view and vid in wm_views)
            is_assy = _eu_is_assembly_view(vparts, tbb)
            is_long_elev = _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None)
            is_sec = _eu_is_section_cut_view(
                vparts, tbb, comp_dims=comp_dims, scale=scale)

            # Fingerprint into empty-WM views only — never invent onto long elev
            # (elev L/R is native WM mirror only; mid stiffener/brace extras banned).
            all_edges = []
            allow_fp = (vid not in wm_views) and (not is_long_elev)
            if allow_fp:
                for pn in plates:
                    edges = _eu_collect_plate_face_edges(
                        pn, vparts, part_number_map, main_body_set,
                        comp, adj_tol, scale, face_only=False)
                    for e in edges:
                        if e['length_mm'] < 40 and min(ref_lens) >= 40:
                            continue
                        role_p = _norm_partner(e['partner'], main_lbls)
                        if vid == seed_view:
                            if not ({_norm_partner(e['gusset'], main_lbls), role_p} <= seed_roles
                                    or e['partner'] in seed_partners
                                    or e['gusset'] in seed_partners
                                    or role_p == '$MAIN'):
                                continue
                        all_edges.append(e)

            matched = (_eu_match_fingerprint(all_edges, ref_lens,
                                             len_rel=max(len_rel, 0.28 if is_long_elev else len_rel))
                       if all_edges else None)
            if matched:
                # On long elev, emit matched edges on BOTH ends when possible
                if is_long_elev and tbb and expected_n >= 2:
                    tw = max(tbb[2] - tbb[0], 1e-6)
                    left_e = [e for e in all_edges
                              if (e['mid'][0] - tbb[0]) / tw <= 0.35]
                    right_e = [e for e in all_edges
                               if (e['mid'][0] - tbb[0]) / tw >= 0.65]
                    for half in (left_e, right_e):
                        hm = _eu_match_fingerprint(
                            half, ref_lens, len_rel=max(len_rel, 0.32))
                        if not hm:
                            hm = _pick_spread(
                                sorted(half, key=lambda e: (
                                    0 if e.get('face') else 1, -e['length_mm'])),
                                min(expected_n, max(1, len(half)))) if half else None
                        if not hm:
                            continue
                        for e in _pick_spread(hm, expected_n):
                            p1, p2 = sorted((e['gusset'], e['partner']))
                            _emit(vid, p1, p2, e['mid'], round(e['length_mm'], 1),
                                  template_sides, {'_eu_typ_expand': True,
                                                   '_eu_assy_end': True})
                    if not (vid == seed_view and is_long_elev):
                        continue
                else:
                    matched = _pick_spread(matched, expected_n)
                    if is_sec and len(matched) >= 2:
                        matched = _eu_refine_match_cluster_y(
                            matched, all_edges,
                            len_rel=max(len_rel, 0.28))
                    for e in matched:
                        p1, p2 = sorted((e['gusset'], e['partner']))
                        _emit(vid, p1, p2, e['mid'], round(e['length_mm'], 1),
                              template_sides)
                    if not (vid == seed_view and is_assy):
                        continue

            # --- Assembly L/R mirror: snap-only, avoid seed-side dups ---
            if (vid == seed_view
                    and is_long_elev
                    and (seed_view, id(seed)) not in mirror_done
                    and geo_seeds):
                sx0, sy0, sx1, sy1 = tbb
                cx = 0.5 * (sx0 + sx1)
                seed_xs = [r['dxf_pos'][0] for r in geo_seeds if r.get('dxf_pos')]
                if seed_xs:
                    mean_x = sum(seed_xs) / len(seed_xs)
                    side_bias = abs(mean_x - cx) / max((sx1 - sx0) * 0.5, 1e-6)
                    if side_bias >= 0.12:
                        n_m = 0
                        for r in geo_seeds:
                            ox, oy = r['dxf_pos']
                            if abs(ox - cx) < (sx1 - sx0) * 0.08:
                                continue
                            mx, my = (2 * cx - ox), oy
                            Lseed = float(r.get('length_mm') or 0)
                            is_cjp = ((r.get('annotation') or '').upper().startswith('CJP')
                                      or (r.get('weld_type') == 'CJP')
                                      or (r.get('hf') is None
                                          and (r.get('annotation') or '').startswith('PL')))
                            if _eu_result_covers(
                                    results, vid, Lseed,
                                    (r.get('part1'), r.get('part2')), (mx, my),
                                    tol=18.0 if is_cjp else 12.0):
                                continue
                            snapped = _snap_to_face(
                                vparts, (mx, my), main_body_set,
                                tol=36.0 if is_cjp else 28.0)
                            if snapped and Lseed > 0 and abs(
                                    snapped['length_mm'] - Lseed) > max(
                                    80.0 if is_cjp else 40.0, 0.55 * Lseed):
                                snapped = None
                            if not snapped:
                                # Opposite-half: same non-main plate mark, nearby y
                                seed_pair = frozenset(
                                    (r.get('part1'), r.get('part2')))
                                seed_plates = {
                                    p for p in seed_pair
                                    if p and p != comp
                                    and _norm_partner(p, main_lbls) != '$MAIN'}
                                best_e, best_d = None, 1e18
                                for pn in plates:
                                    for e in _eu_collect_plate_face_edges(
                                            pn, vparts, part_number_map,
                                            main_body_set, comp, adj_tol, scale,
                                            face_only=False):
                                        if (mean_x > cx and e['mid'][0] > cx) or (
                                                mean_x < cx and e['mid'][0] < cx):
                                            continue
                                        if abs(e['mid'][1] - my) > (55.0 if is_cjp else 35.0):
                                            continue
                                        if not (e['gusset'] in seed_plates
                                                or e['partner'] in seed_plates):
                                            continue
                                        d = math.hypot(e['mid'][0] - mx,
                                                       e['mid'][1] - my)
                                        len_pen = 0.0
                                        if Lseed > 0:
                                            len_pen = abs(e['length_mm'] - Lseed) / max(Lseed, 1.0)
                                        score = d + 40.0 * len_pen
                                        if score < best_d:
                                            best_d, best_e = score, e
                                snapped = best_e
                            if not snapped:
                                # Last resort: keep mirrored point with seed parts/length
                                if is_cjp or Lseed >= 200:
                                    mid = (mx, my)
                                    p1, p2 = sorted((r.get('part1'), r.get('part2')))
                                    flags = {'_eu_typ_mirror': True}
                                    if is_cjp:
                                        flags['_eu_cjp_mirror'] = True
                                    n_m += _emit(vid, p1, p2, mid, round(Lseed, 1),
                                                 template_sides, flags)
                                continue
                            mid = snapped['mid']
                            if (mean_x > cx and mid[0] > cx) or (
                                    mean_x < cx and mid[0] < cx):
                                continue
                            p1, p2 = sorted((snapped['gusset'], snapped['partner']))
                            flags = {'_eu_typ_mirror': True}
                            if is_cjp:
                                # Keep CJP length/annotation from seed template
                                flags['_eu_cjp_mirror'] = True
                                p1, p2 = sorted((r.get('part1'), r.get('part2')))
                                L_out = Lseed if Lseed > 0 else snapped['length_mm']
                            else:
                                L_out = snapped['length_mm']
                            n_m += _emit(vid, p1, p2, mid, round(L_out, 1),
                                         template_sides, flags)
                        if n_m:
                            mirror_done.add((seed_view, id(seed)))
                if matched or same_wm_view:
                    continue

            if matched:
                continue

            # --- Empty-WM section: rematch (+ LR → 6 for 3S) ---
            # Section cuts with sparse WM: still fill to 6 pairs by structure.
            if vid == seed_view or expected_n < 1:
                continue
            if vid in rematch_done or vid in soft_done:
                continue
            # Never invent TYP rematch onto SectionMark main elev
            if main_view_ids and vid in main_view_ids:
                continue
            if is_long_elev:
                continue
            if len(vparts) > 12 or len(vparts) < 2:
                continue
            seed_n = len(part_lines_map.get(seed_view) or {})
            if abs(seed_n - len(vparts)) > 8 and seed_n > 8 and len(vparts) > 8:
                continue
            if _eu_is_plan_or_bolt_view(
                    vparts, main_body_set, bbox=tbb, comp_dims=comp_dims, scale=scale):
                continue
            # Compact section without a recognized main still OK if 2S/3S-like
            if (not main_body_set and len(vparts) <= 8
                    and not is_sec and expected_n < 2):
                continue

            target_n = 6 if (expected_n >= 2 and (is_sec or len(vparts) <= 8)) else expected_n
            if expected_n >= 3 and is_sec:
                target_n = max(target_n, 6)
            # D-D style: seed 3S + extra plate groups → allow up to 9
            if expected_n >= 3 and is_sec and len(plates) >= 4:
                target_n = max(target_n, 9)
            # Compact empty-WM sections (C-C): pad to 6 even from weak seeds
            if (vid not in wm_views and len(vparts) <= 6 and expected_n >= 2):
                target_n = max(target_n, 6)

            if wm_views and vid in wm_views:
                # Never rematch-inflate a cut that already has its own WeldMarks
                # (keeps A-A=5, D-D=2, etc. from native 2S/3S only).
                continue

            all_rm = []
            for pn in plates:
                edges = _eu_collect_plate_face_edges(
                    pn, vparts, part_number_map, main_body_set,
                    comp, adj_tol, scale, face_only=False)
                for e in edges:
                    if e['length_mm'] < 35 and min(ref_lens) >= 40:
                        continue
                    all_rm.append(e)

            best_group = None
            if all_rm:
                ranked = sorted(all_rm, key=lambda e: (
                    0 if e.get('face') else 1,
                    0 if (e['partner'] == comp or e['partner'] in main_lbls
                          or e['oblock'] in main_body_set) else 1,
                    -e['length_mm'],
                ))
                m = _eu_match_fingerprint(ranked, ref_lens, len_rel=max(len_rel, 0.32))
                if m:
                    m = _pick_spread(m, max(expected_n, min(target_n, len(m))))
                    best_group = (sum(e['length_mm'] for e in m), m)
                elif expected_n >= 2:
                    to_main = [e for e in ranked
                               if e['partner'] == comp or e['partner'] in main_lbls
                               or e['oblock'] in main_body_set]
                    pool = to_main if len(to_main) >= max(1, expected_n - 1) else ranked
                    need = max(expected_n, min(target_n, len(pool)))
                    if len(pool) >= max(2, expected_n - 1):
                        top = _pick_spread(pool, need)
                        ok_each = sum(
                            1 for e in top
                            if any(abs(e['length_mm'] - r) <= max(14.0, 0.50 * r)
                                   for r in ref_lens))
                        sum_ok = abs(sum(e['length_mm'] for e in top) - sum(ref_lens)) <= max(
                            40.0, 0.50 * sum(ref_lens))
                        if ok_each >= max(1, expected_n - 1) or sum_ok or (
                                len(to_main) >= expected_n and expected_n <= 3) or (
                                len(top) >= target_n - 1 and is_sec):
                            best_group = (sum(e['length_mm'] for e in top) * 0.5, top)

            emit_edges = list(best_group[1]) if best_group else []
            if emit_edges and target_n > len(emit_edges) and tbb:
                tcx = 0.5 * (tbb[0] + tbb[2])
                used_pn = {e.get('gusset_pn') for e in emit_edges}
                seed_cx = sum(e['mid'][0] for e in emit_edges) / len(emit_edges)
                opposite = []
                for e in all_rm:
                    if e.get('gusset_pn') in used_pn:
                        continue
                    if (seed_cx >= tcx and e['mid'][0] >= tcx) or (
                            seed_cx < tcx and e['mid'][0] < tcx):
                        continue
                    opposite.append(e)
                if opposite:
                    need = target_n - len(emit_edges)
                    add = _pick_spread(
                        sorted(opposite, key=lambda e: (
                            0 if e.get('face') else 1, -e['length_mm'])),
                        need)
                    emit_edges.extend(add)
            # Still short: pad with remaining face edges (distinct mid)
            if emit_edges and len(emit_edges) < target_n:
                used_mids = {(round(e['mid'][0], 1), round(e['mid'][1], 1))
                             for e in emit_edges}
                pad = []
                for e in sorted(all_rm, key=lambda x: (
                        0 if x.get('face') else 1, -x['length_mm'])):
                    mk = (round(e['mid'][0], 1), round(e['mid'][1], 1))
                    if mk in used_mids:
                        continue
                    pad.append(e)
                    used_mids.add(mk)
                    if len(emit_edges) + len(pad) >= target_n:
                        break
                emit_edges.extend(pad)

            if emit_edges:
                emit_edges = emit_edges[:target_n]
                if is_sec and len(emit_edges) >= 2:
                    emit_edges = _eu_refine_match_cluster_y(
                        emit_edges, all_rm, len_rel=max(len_rel, 0.28))
                for e in emit_edges:
                    p1, p2 = sorted((e['gusset'], e['partner']))
                    _emit(vid, p1, p2, e['mid'], round(e['length_mm'], 1),
                          template_sides, {'_eu_typ_rematch': True})
                exist_now = sum(
                    1 for r in results
                    if r.get('view_id') == vid and r.get('position') == 'Above')
                if exist_now >= target_n or len(emit_edges) >= target_n:
                    rematch_done.add(vid)
                continue


            # Soft bbox mirror — last resort for empty section only
            if expected_n < 2:
                continue
            sbb = bbox_cache.get(seed_view)
            if not sbb or not tbb:
                continue
            # Don't soft-copy elevation seeds into compact section cuts (or vice versa)
            sw0 = max(sbb[2] - sbb[0], 1e-6)
            sh0 = max(sbb[3] - sbb[1], 1e-6)
            tw0 = max(tbb[2] - tbb[0], 1e-6)
            th0 = max(tbb[3] - tbb[1], 1e-6)
            s_aspect = sw0 / sh0
            t_aspect = tw0 / th0
            if (s_aspect > 2.0 and t_aspect < 1.4) or (t_aspect > 2.0 and s_aspect < 1.4):
                continue
            sx0, sy0, sx1, sy1 = sbb
            tx0, ty0, tx1, ty1 = tbb
            sw, sh = sw0, sh0
            tw, th = tw0, th0
            tgt_lbls = {part_number_map.get(pn) for pn in vparts}
            tgt_lbls.discard(None)
            n_before = added
            for r in geo_seeds[:expected_n]:
                sp1, sp2 = r.get('part1'), r.get('part2')
                tp1 = _eu_map_label(sp1, tgt_lbls, part_dims, main_lbls, comp)
                tp2 = _eu_map_label(sp2, tgt_lbls, part_dims, main_lbls, comp)
                if tp1 and tp2 and tp1 == tp2:
                    alts = [t for t in tgt_lbls if t != tp1]
                    mains = [t for t in alts if t == comp or t in main_lbls]
                    plates_left = [t for t in alts if t not in mains and t != comp]
                    if sp1 in seed_main_lbls or sp1 == comp:
                        tp1 = mains[0] if mains else tp1
                        tp2 = plates_left[0] if plates_left else (alts[0] if alts else tp2)
                    else:
                        tp2 = mains[0] if mains else (
                            plates_left[0] if plates_left else (alts[0] if alts else tp2))
                if not tp1 or not tp2 or tp1 == tp2:
                    continue
                ox, oy = r['dxf_pos']
                nx_flip = tx0 + (sx1 - ox) / sw * tw
                ny = ty0 + (oy - sy0) / sh * th
                mid = (nx_flip, ny)
                # Prefer snap onto local face edge — skip unsapped soft junk
                snapped = _snap_to_face(vparts, mid, main_body_set, tol=16.0)
                if not snapped:
                    continue
                p1, p2 = sorted((snapped['gusset'], snapped['partner']))
                L = round(snapped['length_mm'], 1)
                mid = snapped['mid']
                if L < 40 and min(ref_lens) >= 40:
                    continue
                _emit(vid, p1, p2, mid, L, template_sides,
                      {'_eu_typ_soft': True})
            if added > n_before:
                soft_done.add(vid)
    return added


def dedupe_eu_cross_view_welds(results, wm_views, part_lines_map,
                               typ_wm_views=None, comp_dims=None, scale=10.0,
                               part_number_map=None, comp=None,
                               main_view_ids=None):
    """
    Soft cross-view cleanup only.

    Never drop native WeldMark rows (needed for elev L/R end plates and
    intact 2S/3S groups). Only drop *expanded* soft/fingerprint copies on an
    assembly elev when a section WM view already has the same part-pair as a
    native (non-expand) weld.
    """
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    bbox_cache = {vid: _eu_view_bbox(vp) for vid, vp in part_lines_map.items()}

    def _is_expand(r):
        return bool(r.get('_eu_typ_expand') or r.get('_eu_typ_soft')
                    or r.get('_eu_typ_rematch'))

    def _is_native(r):
        return not _is_expand(r) and not r.get('_eu_typ_mirror')

    def _is_elev(vid):
        vparts = part_lines_map.get(vid) or {}
        vbb = bbox_cache.get(vid)
        return bool(vbb) and _eu_is_long_elevation(
            vparts, vbb, part_number_map=part_number_map, comp=comp,
            view_id=vid, main_view_ids=main_view_ids if main_view_ids else None)

    # Native pairs present on each WM section (non-elevation)
    native_sec_pairs = set()
    for r in results:
        if not _is_native(r):
            continue
        vid = r.get('view_id')
        if vid not in wm_views:
            continue
        if _is_elev(vid):
            continue
        p1, p2 = r.get('part1'), r.get('part2')
        if p1 and p2:
            native_sec_pairs.add(frozenset((p1, p2)))

    if not native_sec_pairs:
        return 0

    drop = set()
    for i, r in enumerate(results):
        # Only soft/fingerprint expands on long elev — never native / L-R mirror
        if not (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')
                or r.get('_eu_typ_rematch')):
            continue
        if r.get('_eu_typ_mirror') or r.get('_eu_assy_end'):
            continue
        vid = r.get('view_id')
        if not _is_elev(vid):
            continue
        pair = frozenset((r.get('part1'), r.get('part2')))
        if pair in native_sec_pairs:
            drop.add(i)

    if not drop:
        return 0
    kept = [r for i, r in enumerate(results) if i not in drop]
    n = len(results) - len(kept)
    results[:] = kept
    return n


def mirror_eu_long_elev_native(results, part_lines_map, part_number_map, comp,
                               comp_dims=None, scale=10.0, adj_tol=4.5,
                               main_view_ids=None):
    """
    Post-pass: every native/mirrored weld on one end of a long elev must have
    an opposite-end counterpart (fillet pairs + CJP). Uses view center x-flip.
    Only runs on Tekla SectionMark main views (when mapping is provided).
    """
    import math
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    main_view_ids = set(main_view_ids or [])
    added = 0
    by_view = defaultdict(list)
    for r in results:
        by_view[r.get('view_id')].append(r)

    for vid, rows in by_view.items():
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue
        sx0, sy0, sx1, sy1 = tbb
        tw = max(sx1 - sx0, 1e-6)
        th = max(sy1 - sy0, 1e-6)

        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}
        # Tall column elev: flip across main-body centre (flange↔flange),
        # not the elongated view AABB (which includes protruding plates).
        cx = 0.5 * (sx0 + sx1)
        if th / tw >= 2.0 and main_body_set:
            mxs, mys = [], []
            for mb in main_body_set:
                for ln in vparts.get(mb, []):
                    mxs.extend([ln['start'][0], ln['end'][0]])
                    mys.extend([ln['start'][1], ln['end'][1]])
            if mxs:
                cx = 0.5 * (min(mxs) + max(mxs))
                tw = max(max(mxs) - min(mxs), tw * 0.15)

        # Unique geos (ignore Above/Below duplication): prefer native first
        geos = {}
        for r in rows:
            pos = r.get('dxf_pos')
            if not pos:
                continue
            key = (round(pos[0], 0), round(pos[1], 0),
                   round(float(r.get('length_mm') or 0), 0),
                   tuple(sorted((r.get('part1'), r.get('part2')))))
            prev = geos.get(key)
            if prev is None or (not r.get('_eu_typ_expand') and prev.get('_eu_typ_expand')):
                geos[key] = r

        items = list(geos.values())
        # Ignore tips far outside the view AABB (CIRCLE TYP distribute junk)
        pad = max(tw, th) * 0.05
        in_view = [
            r for r in items
            if (sx0 - pad <= r['dxf_pos'][0] <= sx1 + pad
                and sy0 - pad <= r['dxf_pos'][1] <= sy1 + pad)
        ]
        if not in_view:
            in_view = items
        right = [r for r in in_view if r['dxf_pos'][0] >= cx + 0.08 * tw]
        left = [r for r in in_view if r['dxf_pos'][0] <= cx - 0.08 * tw]
        # Prefer mirroring the richer side onto the poorer side
        if len(right) >= len(left):
            src, dst_sign = right, -1
        else:
            src, dst_sign = left, 1

        plates = [pn for pn in vparts
                  if part_number_map.get(pn) not in (None, comp)
                  and pn not in main_body_set]

        tall_col = th / max(tw, 1e-6) >= 2.0
        cover_tol = 8.0 if tall_col else 16.0

        for r in src:
            # Perimeter wraps already cover TYP L/R via plate stack — do not
            # invent a second column of wrap tips on the opposite flange.
            if r.get('_eu_u_wrap_main') or r.get('_eu_u_perimeter'):
                continue
            ox, oy = r['dxf_pos']
            mx, my = (2 * cx - ox), oy
            L = float(r.get('length_mm') or 0)
            pair = (r.get('part1'), r.get('part2'))
            if _eu_result_covers(results, vid, L, pair, (mx, my), tol=cover_tol):
                continue
            is_cjp = (r.get('weld_type') == 'CJP'
                      or (r.get('annotation') or '').upper().startswith('CJP')
                      or (r.get('hf') is None
                          and (r.get('annotation') or '').startswith('PL')))
            # Snap to opposite-end plate of same marks
            seed_plates = {
                p for p in pair
                if p and p != comp and p not in main_lbls}
            best, best_sc = None, 1e18
            for pn in plates:
                for e in _eu_collect_plate_face_edges(
                        pn, vparts, part_number_map, main_body_set,
                        comp, adj_tol, scale, face_only=False):
                    # Must be on destination half
                    if dst_sign < 0 and e['mid'][0] > cx:
                        continue
                    if dst_sign > 0 and e['mid'][0] < cx:
                        continue
                    if not (e['gusset'] in seed_plates or e['partner'] in seed_plates
                            or e['partner'] == comp or e['gusset'] == comp):
                        continue
                    if abs(e['mid'][1] - my) > (60.0 if is_cjp else 40.0):
                        continue
                    d = math.hypot(e['mid'][0] - mx, e['mid'][1] - my)
                    sc = d
                    if L > 0:
                        sc += 25.0 * abs(e['length_mm'] - L) / max(L, 1.0)
                    if sc < best_sc:
                        best_sc, best = sc, e
            mid = best['mid'] if best else (mx, my)
            # Keep seed y if snap jumped to a different flange level
            if best and abs(best['mid'][1] - my) > 10.0:
                mid = (best['mid'][0], my)
            # Tall column: keep pure x-flip if snap drifted too far in x
            if tall_col and abs(mid[0] - mx) > max(6.0, 0.35 * tw):
                mid = (mx, my)
            # Never emit mirrors outside the elev AABB
            if not (sx0 - pad <= mid[0] <= sx1 + pad
                    and sy0 - pad <= mid[1] <= sy1 + pad):
                continue
            # Tall column: also keep within main-body x band when available
            if tall_col and main_body_set:
                mxs = []
                for mb in main_body_set:
                    for ln in vparts.get(mb, []):
                        mxs.extend([ln['start'][0], ln['end'][0]])
                if mxs:
                    mx0, mx1 = min(mxs), max(mxs)
                    mpad = max(mx1 - mx0, 1.0) * 0.35
                    if not (mx0 - mpad <= mid[0] <= mx1 + mpad):
                        continue
            L_out = L if (is_cjp or not best) else best['length_mm']
            if is_cjp or tall_col:
                L_out = L
            p1, p2 = sorted(pair)

            sides = ['Above'] if is_cjp else ['Above', 'Below']
            for side in sides:
                x_tol = 5.0 if tall_col else 8.0
                if any(x for x in results
                       if x.get('view_id') == vid
                       and x.get('position') == side
                       and frozenset((x.get('part1'), x.get('part2'))) == frozenset((p1, p2))
                       and x.get('dxf_pos')
                       and abs(x['dxf_pos'][0] - mid[0]) < x_tol
                       and abs(x['dxf_pos'][1] - mid[1]) < 8):
                    continue
                # Also skip if same pair+length already at mirrored y±3 (any x nearby)
                x_near = 5.0 if tall_col else 12.0
                if any(x for x in results
                       if x.get('view_id') == vid
                       and x.get('position') == side
                       and frozenset((x.get('part1'), x.get('part2'))) == frozenset((p1, p2))
                       and abs((x.get('length_mm') or 0) - L_out) < 1.0
                       and x.get('dxf_pos')
                       and abs(x['dxf_pos'][0] - mid[0]) < x_near
                       and abs(x['dxf_pos'][1] - mid[1]) < 3):
                    continue
                row = {
                    'component': comp,
                    'position': side,
                    'hf': None if is_cjp else r.get('hf'),
                    'length_mm': round(L_out, 1),
                    'annotation': 'CJP' if is_cjp else (r.get('annotation') or ''),
                    'part1': p1,
                    'part2': p2,
                    'dxf_pos': mid,
                    'view_id': vid,
                    '_eu_typ_expand': True,
                    '_eu_typ_mirror': True,
                }
                if is_cjp:
                    row['_eu_cjp_mirror'] = True
                results.append(row)
                added += 1
    return added


def cleanup_eu_tall_elev_outliers(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None):
    """Drop expand tips that landed far outside the main-body x band on tall elevs."""
    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    drop = set()
    by_view = {}
    for i, r in enumerate(results):
        by_view.setdefault(r.get('view_id'), []).append((i, r))
    for vid, items in by_view.items():
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        tw = max(tbb[2] - tbb[0], 1e-6)
        th = max(tbb[3] - tbb[1], 1e-6)
        if th / tw < 2.0:
            continue
        if not _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue
        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        if not main_body_set:
            continue
        mxs = []
        for mb in main_body_set:
            for ln in vparts.get(mb, []):
                mxs.extend([ln['start'][0], ln['end'][0]])
        if not mxs:
            continue
        mx0, mx1 = min(mxs), max(mxs)
        pad = max(mx1 - mx0, 1.0) * 0.5
        for i, r in items:
            if not (r.get('_eu_typ_expand') or r.get('_eu_typ_mirror')):
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            if pos[0] < mx0 - pad or pos[0] > mx1 + pad:
                drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def drop_eu_section_cjp_near_multiside(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None, cluster_tol=18.0):
    """
    Drop native CJP rows that sit inside a 2S/3S fillet tip cluster on the
    same non-elev view (e.g. A-A extra PL10 between plates already in the 3S set).
    """
    import math
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    main_view_ids = set(main_view_ids or [])
    by_view = defaultdict(list)
    for i, r in enumerate(results):
        by_view[r.get('view_id')].append((i, r))

    drop = set()
    for vid, items in by_view.items():
        if main_view_ids and vid in main_view_ids:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue
        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}

        fillet_tips = []
        fillet_parts = set()
        for _i, r in items:
            if r.get('_eu_typ_expand') or r.get('_eu_plate_sides'):
                continue
            ann = (r.get('annotation') or '')
            wt = r.get('weld_type') or ''
            is_cjp = (wt == 'CJP' or ann.upper().startswith('CJP')
                      or ann.upper().startswith('PL') or r.get('hf') is None)
            if is_cjp:
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            fillet_tips.append(pos)
            for p in (r.get('part1'), r.get('part2')):
                if p and p not in main_lbls:
                    fillet_parts.add(p)
        if len(fillet_tips) < 2:
            continue

        for i, r in items:
            ann = (r.get('annotation') or '')
            wt = r.get('weld_type') or ''
            is_cjp = (wt == 'CJP' or ann.upper().startswith('CJP')
                      or ann.upper().startswith('PL') or (
                          r.get('hf') is None and ann))
            if not is_cjp:
                continue
            if r.get('_eu_typ_expand') or r.get('_eu_typ_mirror'):
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            near = any(
                math.hypot(pos[0] - t[0], pos[1] - t[1]) <= cluster_tol
                for t in fillet_tips)
            if not near:
                continue
            pair = {r.get('part1'), r.get('part2')} - {None}
            # Plate–plate CJP inside a multi-side fillet tip cluster
            if (len(pair) == 2 and not (pair & main_lbls)
                    and (pair & fillet_parts or len(fillet_tips) >= 3)):
                drop.add(i)

    if not drop:
        return 0
    kept = [r for i, r in enumerate(results) if i not in drop]
    n = len(results) - len(kept)
    results[:] = kept
    return n


def drop_eu_cjp_plate_sides(results):
    """Remove `_eu_plate_sides` rows that carried CJP/PL annotation."""
    if not results:
        return 0
    kept = []
    n = 0
    for r in results:
        if r.get('_eu_plate_sides'):
            ann = (r.get('annotation') or '')
            wt = r.get('weld_type') or ''
            if (wt == 'CJP' or ann.upper().startswith('CJP')
                    or ann.upper().startswith('PL') or r.get('hf') is None):
                n += 1
                continue
        kept.append(r)
    if n:
        results[:] = kept
    return n


def _eu_classify_u_plate_side(bb, dual_pair):
    """Match a section U-plate to main-elev L or R by bottom Y."""
    if not bb or not dual_pair:
        return None
    y1 = bb[1]
    l_y1 = dual_pair['L'][1][1]
    r_y1 = dual_pair['R'][1][1]
    if abs(y1 - l_y1) <= abs(y1 - r_y1) + 0.25:
        return 'L'
    return 'R'


def _eu_u_plate_lane(side):
    """A-A dual-U: L-profile → lane0, R-profile → lane1."""
    return 0 if side == 'L' else 1


def _eu_build_section_u_x_template(bands, dual_pairs, dual_idx_by_st):
    """Section bbox template per L/R from the first complete A-A dual band."""
    tpl = {}
    for st_i, band in enumerate(bands):
        if len(band) < 2:
            continue
        dual_idx = dual_idx_by_st.get(st_i)
        if dual_idx is None or dual_idx >= len(dual_pairs):
            continue
        pair = dual_pairs[dual_idx]
        for _pn, bb in band:
            side = _eu_classify_u_plate_side(bb, pair)
            if side:
                tpl[side] = bb
        if 'L' in tpl and 'R' in tpl:
            return tpl
    return tpl


def _eu_short_tip_y_offsets(bb, tips):
    """Top/bot Y offset from plate bbox extremes."""
    top_y = max(tips[0][1], tips[1][1])
    bot_y = min(tips[0][1], tips[1][1])
    return {'top': top_y - bb[3], 'bot': bot_y - bb[1]}


def _eu_infer_section_short_tips(bb_main, side, x_template, y_offsets=None):
    """
    Build A-A short-edge tips for a missing L/R plate using main-elev bbox Y
    and a two-plate section X template (L opens right, R opens left).
    """
    tpl_bb = (x_template or {}).get(side)
    if not tpl_bb or not bb_main:
        return None
    defaults = {'top': -0.65, 'bot': 0.65}
    yo = y_offsets or defaults
    top_off = yo.get('top', defaults['top'])
    bot_off = yo.get('bot', defaults['bot'])
    open_x = tpl_bb[2] if side == 'L' else tpl_bb[0]
    return (
        (open_x, bb_main[3] + top_off),
        (open_x, bb_main[1] + bot_off),
    )


def _eu_u_edge_open_end(edge, spine_x=None):
    """Open-side tip of a U flange short edge (farther from web / larger |Δx|)."""
    s = edge.get('start')
    e = edge.get('end')
    if not s or not e:
        return edge.get('mid')
    if spine_x is None:
        # Prefer the rightmost end for typical U opening to +X; else farthest mid-x.
        return s if s[0] >= e[0] else e
    ds = abs(s[0] - spine_x)
    de = abs(e[0] - spine_x)
    if abs(ds - de) < 1e-6:
        return s if s[0] >= e[0] else e
    return s if ds >= de else e


def _pick_u_station_four_pairs(pn, bb, vparts, local_map, main_body_set, comp,
                               adj_tol, scale, lane=0, peer_spine_x=None):
    """U-station welds per Figure-2 style:
    3 singles = web back + top flange tip + bottom flange tip;
    1 branched short_pair = two short-edge open ends (inner flange corners).
    lane>0 selects the mirrored U when two profiles bite at the same Y band.
    """
    import math

    cy = 0.5 * (bb[1] + bb[3])
    y_lo, y_hi = bb[1] - 0.6, bb[3] + 0.6
    spine_lbl = comp
    for mbp in main_body_set:
        spine_lbl = local_map.get(mbp) or spine_lbl
        break
    lbl = local_map.get(pn) or comp
    edges = []

    def _is_horiz(ln):
        return abs(ln['start'][1] - ln['end'][1]) < 0.4

    def _append_edge(g_ln, partner, face):
        Lmm = g_ln.get('length', 0) * scale
        if Lmm < 22:
            return
        mid = _eu_edge_mid(g_ln)
        if mid[1] < y_lo or mid[1] > y_hi:
            return
        edges.append({
            'length_mm': Lmm, 'partner': partner, 'gusset': lbl,
            'mid': mid, 'face': bool(face),
            'horiz': _is_horiz(g_ln),
            'start': tuple(g_ln['start'][:2]),
            'end': tuple(g_ln['end'][:2]),
        })

    for g_ln in vparts.get(pn, []):
        nb = _eu_best_neighbor_for_edge(
            g_ln, vparts, {pn}, local_map, main_body_set, comp, adj_tol + 3.0)
        partner = (local_map.get(nb[0]) if nb else None) or spine_lbl
        _append_edge(g_ln, partner, bool(nb and nb[2]))
    for e in _eu_collect_plate_face_edges(
            pn, vparts, local_map, main_body_set, comp, adj_tol, scale,
            face_only=False):
        g_ln = e.get('line')
        if not g_ln:
            continue
        _append_edge(g_ln, e.get('partner') or spine_lbl, e.get('face'))

    seen, dedup = set(), []
    for e in edges:
        key = (round(e['mid'][0], 1), round(e['mid'][1], 1),
               round(e['length_mm'], 0))
        if key in seen:
            continue
        seen.add(key)
        dedup.append(e)
    if not dedup:
        return None

    # Web back (vertical spine) — one single on left of U
    longs = sorted(
        [e for e in dedup if e['length_mm'] >= 68 and not e.get('horiz')],
        key=lambda e: (
            0 if e.get('face') else 1,
            0 if e.get('partner') in (comp, spine_lbl) else 1,
            e['mid'][0],
            -e['length_mm'],
        ))
    if not longs:
        longs = sorted(
            [e for e in dedup if e['length_mm'] >= 68],
            key=lambda e: (e['mid'][0], -e['length_mm']))
    if lane > 0 and peer_spine_x is not None and longs:
        spine = max(longs, key=lambda e: abs(e['mid'][0] - peer_spine_x))
    else:
        spine = longs[0] if longs else None
    spine_x = spine['mid'][0] if spine else 0.5 * (bb[0] + bb[2])

    # Horizontal shorts: extreme-Y = flange tips (singles);
    # remaining pair = short-edge branch (open ends form the fork tips).
    horiz = [e for e in dedup if e.get('horiz') and e['length_mm'] < 75]
    if len(horiz) < 2:
        horiz = [e for e in dedup if e['length_mm'] < 75]

    top_tip = max(horiz, key=lambda e: e['mid'][1]) if horiz else None
    bot_tip = min(horiz, key=lambda e: e['mid'][1]) if horiz else None
    if top_tip is bot_tip:
        bot_tip = None

    remain_h = [e for e in horiz if e is not top_tip and e is not bot_tip]
    if len(remain_h) >= 2:
        # Shorter inset horizontals = short-edge branch pair
        remain_h = sorted(remain_h, key=lambda e: (e['length_mm'], -e['mid'][0]))
        top_half = [e for e in remain_h if e['mid'][1] >= cy]
        bot_half = [e for e in remain_h if e['mid'][1] < cy]
        if top_half and bot_half:
            inner_top = min(top_half, key=lambda e: e['length_mm'])
            inner_bot = min(bot_half, key=lambda e: e['length_mm'])
        else:
            inner_top = max(remain_h, key=lambda e: e['mid'][1])
            inner_bot = min(remain_h, key=lambda e: e['mid'][1])
            if inner_top is inner_bot:
                inner_bot = None
    else:
        inner_top = inner_bot = None

    # 3 singles: web back + top/bot flange tip edges (mid)
    singles = []
    for e in (spine, top_tip, bot_tip):
        if e is not None and e not in singles:
            singles.append(e)

    # Short-edge bifurcation tips = open ends of the inset short edges
    # (endpoint farther from web → natural open-flange x, ~227).
    short_pair = None
    if (inner_top and inner_bot and inner_top is not inner_bot
            and abs(inner_top['mid'][1] - inner_bot['mid'][1]) >= 3.0):
        tip_top = dict(inner_top)
        tip_bot = dict(inner_bot)
        tip_top['mid'] = _eu_u_edge_open_end(inner_top, spine_x)
        tip_bot['mid'] = _eu_u_edge_open_end(inner_bot, spine_x)
        tip_top['_open_tip'] = tip_top['mid']
        tip_bot['_open_tip'] = tip_bot['mid']
        short_pair = (tip_top, tip_bot)

    # Fallback: keep 3 singles if inner pair missing
    if len(singles) < 3:
        used = {id(e) for e in singles}
        if short_pair:
            used |= {id(short_pair[0]), id(short_pair[1])}
        remain = [e for e in dedup if id(e) not in used]
        while len(singles) < 3 and remain:
            cms = [e['mid'] for e in singles] or [(0.5 * (bb[0] + bb[2]), cy)]
            best, best_sc = None, -1.0
            for e in remain:
                spr = min(
                    math.hypot(e['mid'][0] - c[0], e['mid'][1] - c[1])
                    for c in cms)
                sc = spr + (3.0 if e.get('face') else 0.0)
                if sc > best_sc:
                    best_sc, best = sc, e
            if best is None:
                break
            singles.append(best)
            remain = [e for e in remain if e is not best]

    return {
        'singles': singles[:3],
        'short_pair': short_pair,
    }


def _pick_u_station_four_edges(pn, bb, vparts, local_map, main_body_set, comp,
                               adj_tol, scale):
    """Backward-compatible wrapper → flat edge list for probes."""
    packs = _pick_u_station_four_pairs(
        pn, bb, vparts, local_map, main_body_set, comp, adj_tol, scale)
    if not packs:
        return []
    out = list(packs.get('singles') or [])
    sp = packs.get('short_pair')
    if sp:
        out.extend(sp)
    return out[:4]


def _eu_stiffener_group_bbox(vparts, main_body_set, part_number_map, comp,
                             comp_dims=None, scale=10.0):
    """BBox of compact stiffener plates in an H-section cut (excl. main body)."""
    main_lbls = {comp} | {
        part_number_map.get(mb, comp) for mb in (main_body_set or [])}
    boxes = []
    for pn, lns in (vparts or {}).items():
        if pn in (main_body_set or set()):
            continue
        lbl = part_number_map.get(pn)
        if lbl in main_lbls:
            continue
        bb = _eu_part_bbox(lns)
        if not bb:
            continue
        pw = (bb[2] - bb[0]) * scale
        ph = (bb[3] - bb[1]) * scale
        if max(pw, ph) > max(
                float((comp_dims or {}).get('depth') or 400),
                float((comp_dims or {}).get('flange_w') or 400)) * 0.85:
            continue
        boxes.append(bb)
    if not boxes:
        return None
    return (
        min(b[0] for b in boxes), min(b[1] for b in boxes),
        max(b[2] for b in boxes), max(b[3] for b in boxes),
    )


def _eu_is_u_channel_cut_view(vparts, bbox, part_number_map=None, comp=None,
                              scale=10.0, min_stations=4):
    """
    Compact cut with many similar small plate stations (U / channel stack)
    plus a long spine — typical B-B wrap detail, not an H stiffener cut.
    """
    if not vparts or not bbox:
        return False
    w = (bbox[2] - bbox[0]) * scale
    h = (bbox[3] - bbox[1]) * scale
    if max(w, h) < 80:
        return False
    # Prefer tall stacks of small plates
    aspect = max(w, h) / max(min(w, h), 1.0)
    if aspect < 1.8 and len(vparts) < min_stations + 2:
        return False

    small = []
    for pn, lns in vparts.items():
        bb = _eu_part_bbox(lns)
        if not bb:
            continue
        pw = (bb[2] - bb[0]) * scale
        ph = (bb[3] - bb[1]) * scale
        if max(pw, ph) <= 0:
            continue
        # Small station: compact footprint, not the long spine
        if max(pw, ph) < 180 and min(pw, ph) < 80:
            small.append(bb)
    if len(small) < min_stations:
        return False
    # Cluster by centre-x / centre-y so slanted extras don't inflate the span
    from collections import defaultdict
    by_cx = defaultdict(list)
    by_cy = defaultdict(list)
    for b in small:
        by_cx[round(0.5 * (b[0] + b[2]), 0)].append(b)
        by_cy[round(0.5 * (b[1] + b[3]), 0)].append(b)
    best_x = max(by_cx.values(), key=len)
    best_y = max(by_cy.values(), key=len)
    if len(best_x) >= min_stations:
        cys = [0.5 * (b[1] + b[3]) for b in best_x]
        if (max(cys) - min(cys)) * scale >= 120:
            return True
    if len(best_y) >= min_stations:
        cxs = [0.5 * (b[0] + b[2]) for b in best_y]
        if (max(cxs) - min(cxs)) * scale >= 120:
            return True
    return False


def _eu_main_column_x_span(vparts, part_number_map, comp, comp_dims=None,
                           scale=10.0):
    """Return (cx, x_lo, x_hi) of the main-body column on an elev view."""
    main_body_set, _ = _eu_find_main_body_blocks(
        vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
    mxs = []
    for mb in main_body_set:
        for ln in vparts.get(mb, []):
            mxs.extend([ln['start'][0], ln['end'][0]])
    if not mxs:
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            return 0.0, 0.0, 0.0
        cx = 0.5 * (tbb[0] + tbb[2])
        return cx, tbb[0], tbb[2]
    x_lo, x_hi = min(mxs), max(mxs)
    return 0.5 * (x_lo + x_hi), x_lo, x_hi


def _eu_list_main_dual_plate_pairs(
        vparts, part_number_map, comp, comp_dims=None, scale=10.0, y_tol=3.5):
    """
    Main-elev plate pairs at the same Y (left + right of column) — one pair
    per dual-U station. Returns list of
    {cy, left:(pn,bb,lbl), right:(pn,bb,lbl)}.
    """
    cx, _, _ = _eu_main_column_x_span(
        vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
    main_body_set, _ = _eu_find_main_body_blocks(
        vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
    items = []
    for pn, lns in vparts.items():
        if pn in main_body_set:
            continue
        bb = _eu_part_bbox(lns)
        if not bb:
            continue
        pw = (bb[2] - bb[0]) * scale
        ph = (bb[3] - bb[1]) * scale
        # Skip base plate / tiny junk; keep side stiffener plates
        if ph < 40 or pw < 80:
            continue
        if ph > 250:
            continue
        cy = 0.5 * (bb[1] + bb[3])
        cpx = 0.5 * (bb[0] + bb[2])
        lbl = part_number_map.get(pn) or comp
        side = 'L' if cpx < cx else 'R'
        items.append((cy, side, pn, bb, lbl))
    # Cluster by Y
    items.sort(key=lambda x: x[0])
    bands = []
    for cy, side, pn, bb, lbl in items:
        placed = False
        for band in bands:
            if abs(cy - band['cy']) <= y_tol:
                band[side] = (pn, bb, lbl)
                band['cy'] = 0.5 * (band['cy'] + cy)
                placed = True
                break
        if not placed:
            bands.append({'cy': cy, side: (pn, bb, lbl)})
    out = []
    for band in bands:
        if 'L' in band and 'R' in band:
            out.append(band)
    return out


def _eu_tips_on_plate_column_contact(
        pn, bb, vparts, part_number_map, main_body_set, comp,
        col_x_lo, col_x_hi, scale, n_tips=3, lengths=None):
    """
    n_tips weld mids along the plate's vertical edge that touches the column.
    lengths (optional) assigned bottom→top.
    """
    contact_x = col_x_lo if 0.5 * (bb[0] + bb[2]) < 0.5 * (col_x_lo + col_x_hi) else col_x_hi
    # Prefer actual vertical edge nearest the column flange
    best_ln, best_d = None, 1e18
    for g_ln in vparts.get(pn, []):
        if abs(g_ln['start'][0] - g_ln['end'][0]) > 0.5:
            continue  # need vertical
        Lmm = g_ln.get('length', 0) * scale
        if Lmm < 30:
            continue
        mid = _eu_edge_mid(g_ln)
        d = abs(mid[0] - contact_x)
        if d < best_d:
            best_d, best_ln = d, g_ln
    if best_ln is not None:
        x = 0.5 * (best_ln['start'][0] + best_ln['end'][0])
        y0 = min(best_ln['start'][1], best_ln['end'][1])
        y1 = max(best_ln['start'][1], best_ln['end'][1])
    else:
        x = contact_x
        y0, y1 = bb[1], bb[3]
    if abs(y1 - y0) < 1e-6:
        ys = [0.5 * (y0 + y1)] * n_tips
    elif n_tips <= 1:
        ys = [0.5 * (y0 + y1)]
    else:
        # Outer flange edges at ends (e.g. y=344 & 355), mid tip(s) in between.
        # Prefer plate bbox extremes so leaders hit the visible outer lines.
        y_lo = min(bb[1], y0)
        y_hi = max(bb[3], y1)
        ys = [y_lo + (y_hi - y_lo) * i / (n_tips - 1) for i in range(n_tips)]
    lens = list(lengths) if lengths else [round((y1 - y0) * scale, 1)] * n_tips
    while len(lens) < n_tips:
        lens.append(lens[-1] if lens else 100.0)
    tips = []
    for i, y in enumerate(ys):
        tips.append({
            'mid': (x, y),
            'length_mm': round(float(lens[i]), 1),
        })
    return tips


def relocate_eu_circle_wraps_to_u_cut(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, adj_tol=4.5, main_view_ids=None):
    """
    Continuous CIRCLE/hf5 wraps (no wrap-scallop ARCs on the contact face):
    one perimeter weld per station on the main elev.  Do NOT explode onto a
    U-channel cut.  Scalloped wraps (`_wrap_has_scallops`) keep per-edge
    segments and are left untouched.

    Geometry gate (GB + EU, not component family): see
    `gusset_has_wrap_scallops` → `_wrap_has_scallops` on seed rows.
    """
    import math
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    main_view_ids = set(main_view_ids or [])
    if not main_view_ids:
        # Fallback: any view that hosts continuous CIRCLE wrap seeds
        main_view_ids = {
            r.get('view_id') for r in results
            if r.get('_eu_circle_wrap') and not r.get('_wrap_has_scallops')
            and r.get('view_id')
        }
    if not main_view_ids:
        return 0

    # U-cut optional: used only to clean cut-view junk after perimeter emit
    u_vid = None
    for vid, vparts in part_lines_map.items():
        if vid in main_view_ids:
            continue
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids):
            continue
        if _eu_is_u_channel_cut_view(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                scale=scale):
            u_vid = vid
            break

    # Collect elev wrap seeds (continuous CIRCLE only — skip scalloped)
    wrap_rows = []
    for r in results:
        if r.get('view_id') not in main_view_ids:
            continue
        if r.get('_wrap_has_scallops'):
            continue
        if r.get('_eu_circle_wrap'):
            wrap_rows.append(r)
            continue
        if (r.get('hf') == 5 and r.get('dxf_pos')
                and not (r.get('annotation') or '').upper().startswith('CJP')
                and not r.get('_wrap_has_scallops')):
            wrap_rows.append(r)
    if not wrap_rows:
        return 0

    # Template hf / annotation from seeds
    hf = 5.0
    for r in wrap_rows:
        if r.get('hf') is not None:
            hf = r['hf']
            break
    ann = ''
    for r in wrap_rows:
        if r.get('annotation'):
            ann = r['annotation']
            break

    vparts = part_lines_map[u_vid]
    main_body_set, _ = _eu_find_main_body_blocks(
        vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
    main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}
    spine_lbl = comp
    for mbp in main_body_set:
        spine_lbl = part_number_map.get(mbp, comp)
        break

    wrap_plates = set()
    for r in wrap_rows:
        for p in (r.get('part1'), r.get('part2')):
            if p and p != comp and p not in main_lbls:
                wrap_plates.add(p)
    plate_lbl = sorted(wrap_plates)[0] if wrap_plates else None
    main_vid = sorted(main_view_ids)[0] if main_view_ids else None

    # Main elev geometry for dual-U L/R placement
    main_vparts = part_lines_map.get(main_vid) or {}
    main_aabb = _eu_view_bbox(main_vparts)
    main_body_main, _ = _eu_find_main_body_blocks(
        main_vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
    col_cx, col_lo, col_hi = _eu_main_column_x_span(
        main_vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
    dual_pairs = _eu_list_main_dual_plate_pairs(
        main_vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)

    L_by_gusset = _eu_perimeter_length_by_gusset(wrap_rows, comp)
    wrap_plate_list = _eu_list_main_wrap_plates(
        main_vparts, part_number_map, wrap_plates, main_body_main, scale=scale)
    n_wrap = _eu_count_wrap_stations(wrap_rows)
    n_dual = len(dual_pairs) if dual_pairs else 0
    n_plates = len(wrap_plate_list)
    # Physical main-elev plates (TYP×2 + TYP×7 → 9) beat Y-collapsed seed packs
    n_target = max(n_plates, n_wrap, n_dual) if (n_plates or n_wrap or n_dual) else 0
    stations = _eu_wrap_stations_from_seeds(wrap_rows, n_target=n_wrap or n_target)

    # Lowest U-stack seed Y on main (for keeping bottom-flange natives)
    seed_ys = [r['dxf_pos'][1] for r in wrap_rows if r.get('dxf_pos')]
    if wrap_plate_list:
        lowest_station_y = min(p['cy'] for p in wrap_plate_list)
    elif dual_pairs:
        lowest_station_y = min(p['cy'] for p in dual_pairs)
    elif seed_ys:
        lowest_station_y = min(seed_ys)
    else:
        lowest_station_y = 1e9
    bottom_keep_y = min(40.0, lowest_station_y - 35.0)

    # Strip continuous elev CIRCLE/hf5 wraps only; keep scalloped per-edge
    # segments and bottom-flange seeds below the U stack.
    n_drop = 0
    kept = []
    for r in results:
        if r.get('view_id') in main_view_ids and (
                r.get('_eu_circle_wrap')
                or (r.get('hf') == 5
                    and not (r.get('annotation') or '').upper().startswith('CJP'))):
            if r.get('_wrap_has_scallops'):
                kept.append(r)
                continue
            pos = r.get('dxf_pos')
            if bottom_keep_y is not None and pos and pos[1] <= bottom_keep_y:
                kept.append(r)
                continue
            n_drop += 1
            continue
        if u_vid and r.get('view_id') == u_vid and (
                r.get('_eu_u_wrap')
                or r.get('_eu_u_short_pair')
                or (r.get('hf') == 5 and (
                    r.get('_eu_typ_expand') or r.get('_eu_typ_soft')))):
            n_drop += 1
            continue
        if r.get('_eu_u_wrap_main'):
            n_drop += 1
            continue
        kept.append(r)
    results[:] = kept

    def _in_main_aabb(mid, pad=8.0):
        if not main_aabb or not mid:
            return False
        return (main_aabb[0] - pad <= mid[0] <= main_aabb[2] + pad
                and main_aabb[1] - pad <= mid[1] <= main_aabb[3] + pad)

    added = 0

    def _emit_perimeter(mid, L, p1, p2, st_i, lane=0):
        nonlocal added
        if not main_vid or not mid or L <= 0:
            return
        if p1 == p2:
            return
        if not _in_main_aabb(mid):
            return
        if _eu_result_covers(
                results, main_vid, L, (p1, p2), mid, tol=1.5,
                lane=lane, station=st_i):
            return
        if any(
                x.get('view_id') == main_vid
                and x.get('_eu_u_wrap_main')
                and x.get('dxf_pos')
                and abs(x['dxf_pos'][0] - mid[0]) < 1.2
                and abs(x['dxf_pos'][1] - mid[1]) < 1.2
                for x in results):
            return
        # Perimeter wrap = one weld (all-around) → single F label, not Above/Below pair
        results.append({
            'component': comp,
            'position': 'Above',
            'hf': hf,
            'length_mm': L,
            'annotation': ann,
            'part1': p1,
            'part2': p2,
            'dxf_pos': mid,
            'view_id': main_vid,
            '_eu_typ_expand': True,
            '_eu_u_station': st_i,
            '_eu_u_lane': lane,
            '_eu_u_wrap_main': True,
            '_eu_u_perimeter': True,
        })
        added += 1

    def _parts_for(st):
        p1, p2 = st.get('p1'), st.get('p2')
        if p1 and p2 and p1 != p2:
            return sorted((p1, p2))
        if plate_lbl and comp:
            return sorted((plate_lbl, comp))
        return sorted((spine_lbl or comp, comp))

    def _lane_for_bb(bb):
        cpx = 0.5 * (bb[0] + bb[2])
        return 1 if cpx < col_cx else 0

    # Prefer physical wrap plates on main elev: covers dual L/R stacks and
    # single-side TYP stacks (e.g. AC0002 A0100×3 left-only).
    if wrap_plate_list:
        for st_i, pl in enumerate(wrap_plate_list):
            L = L_by_gusset.get(pl['lbl'])
            if not L:
                continue
            tips = _eu_tips_on_plate_column_contact(
                pl['pn'], pl['bb'], main_vparts, part_number_map,
                main_body_main, comp, col_lo, col_hi, scale,
                n_tips=1, lengths=[L])
            if tips:
                mid = tips[0]['mid']
            else:
                contact_x = (
                    col_lo if 0.5 * (pl['bb'][0] + pl['bb'][2]) < col_cx
                    else col_hi)
                mid = (contact_x, pl['cy'])
            p1, p2 = sorted((pl['lbl'], comp))
            _emit_perimeter(mid, L, p1, p2, st_i, lane=_lane_for_bb(pl['bb']))
        return added if added else n_drop

    # Dual-U fallback when wrap plates were not listed
    if dual_pairs and (stations or L_by_gusset):
        dual_sorted = list(enumerate(dual_pairs))
        dual_sorted.sort(key=lambda x: x[1]['cy'])
        st_sorted = sorted(stations, key=lambda s: s['mid'][1]) if stations else []
        for k, (_di, pair) in enumerate(dual_sorted):
            sy = pair['cy']
            st = st_sorted[k] if k < len(st_sorted) else None
            for lane, side_key in ((1, 'L'), (0, 'R')):
                if side_key not in pair:
                    continue
                pn, bb, lbl = pair[side_key]
                L = (L_by_gusset.get(lbl)
                     or (st['L'] if st else 0)
                     or next(iter(L_by_gusset.values()), 0))
                if L <= 0:
                    continue
                tips = _eu_tips_on_plate_column_contact(
                    pn, bb, main_vparts, part_number_map, main_body_main, comp,
                    col_lo, col_hi, scale, n_tips=1, lengths=[L])
                if tips:
                    mid = tips[0]['mid']
                    mid = (mid[0], 0.35 * mid[1] + 0.65 * sy)
                else:
                    mx = bb[0] if side_key == 'L' else bb[2]
                    mid = (mx, sy)
                p1, p2 = sorted((lbl, comp))
                _emit_perimeter(mid, L, p1, p2, k, lane=lane)
        return added if added else n_drop

    for st_i, st in enumerate(stations):
        p1, p2 = _parts_for(st)
        _emit_perimeter(st['mid'], st['L'], p1, p2, st_i, lane=0)
    return added if added else n_drop


def ensure_eu_bottom_flange_lr(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None, y_max=30.0):
    """
    Bottom flange on main elev: keep centre native; mirror the outer TYP weld
    across the column centre so left/centre/right = 3 pairs (L↔R TYP).
    """
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    added = 0
    by_view = defaultdict(list)
    for r in results:
        by_view[r.get('view_id')].append(r)

    for vid, rows in by_view.items():
        if main_view_ids and vid not in main_view_ids:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue
        cx, _, _ = _eu_main_column_x_span(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)

        natives = []
        for r in rows:
            if r.get('_eu_u_wrap_main') or r.get('_eu_typ_expand'):
                continue
            pos = r.get('dxf_pos')
            if not pos or pos[1] > y_max:
                continue
            natives.append(r)
        if not natives:
            continue

        # Drop previous bad mirrors at bottom (from x=0 flip)
        drop = set()
        for i, r in enumerate(results):
            if r.get('view_id') != vid or not r.get('_eu_typ_mirror'):
                continue
            pos = r.get('dxf_pos')
            if pos and pos[1] <= y_max:
                drop.add(i)
        if drop:
            results[:] = [r for i, r in enumerate(results) if i not in drop]
            # rebuild natives list indices not needed

        for r in natives:
            ox, oy = r['dxf_pos']
            # Near column centre → centre pair, do not mirror
            if abs(ox - cx) < 4.0:
                continue
            mx, my = (2 * cx - ox), oy
            if (ox < cx and mx < cx) or (ox > cx and mx > cx):
                continue
            # Keep mirror on the base-plate / column band
            if tbb and not (tbb[0] - 5 <= mx <= tbb[2] + 5):
                continue
            L = float(r.get('length_mm') or 0)
            pair = (r.get('part1'), r.get('part2'))
            if _eu_result_covers(results, vid, L, pair, (mx, my), tol=8.0):
                continue
            p1, p2 = sorted(pair)
            for side in ('Above', 'Below'):
                if any(x for x in results
                       if x.get('view_id') == vid
                       and x.get('position') == side
                       and frozenset((x.get('part1'), x.get('part2'))) == frozenset((p1, p2))
                       and x.get('dxf_pos')
                       and abs(x['dxf_pos'][0] - mx) < 8.0
                       and abs(x['dxf_pos'][1] - my) < 6.0):
                    continue
                row = {
                    'component': comp,
                    'position': side,
                    'hf': r.get('hf'),
                    'length_mm': round(L, 1),
                    'annotation': r.get('annotation') or '',
                    'part1': p1,
                    'part2': p2,
                    'dxf_pos': (mx, my),
                    'view_id': vid,
                    '_eu_typ_expand': True,
                    '_eu_typ_mirror': True,
                }
                results.append(row)
                added += 1
    return added


def complete_eu_h_section_typ_pairs(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, adj_tol=4.5, wm_views=None, main_view_ids=None,
        target_n=6, len_rel=0.22):
    """
    H-section cuts with a partial native fillet set (e.g. 3 tips) → pad to
    target_n by mirroring face-adj edges across the section centre (AB0002 C-C).
    """
    import math
    from collections import defaultdict

    if not results or not part_lines_map:
        return 0

    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    added = 0
    by_view = defaultdict(list)
    for r in results:
        by_view[r.get('view_id')].append(r)

    for vid, rows in by_view.items():
        if vid not in wm_views:
            continue
        if main_view_ids and vid in main_view_ids:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids if main_view_ids else None):
            continue
        if _eu_is_u_channel_cut_view(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                scale=scale):
            continue
        if not _eu_is_section_cut_view(
                vparts, tbb, comp_dims=comp_dims, scale=scale):
            continue
        if _eu_is_plan_or_bolt_view(
                vparts, set(), bbox=tbb, comp_dims=comp_dims, scale=scale):
            continue

        main_body_set, sk = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        if sk != 'H' and not main_body_set:
            continue
        main_lbls = {comp} | {part_number_map.get(mb, comp) for mb in main_body_set}
        vcx = 0.5 * (tbb[0] + tbb[2])

        # Unique native fillet Above tips
        geos = {}
        for r in rows:
            if r.get('position') != 'Above':
                continue
            if (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')
                    or r.get('_eu_typ_rematch') or r.get('_eu_plate_sides')
                    or r.get('_eu_fillet_sib') or r.get('_eu_u_wrap')):
                continue
            ann = (r.get('annotation') or '')
            wt = r.get('weld_type') or ''
            if (wt == 'CJP' or ann.upper().startswith('CJP')
                    or ann.upper().startswith('PL') or r.get('hf') is None):
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            key = (round(pos[0], 1), round(pos[1], 1),
                   round(float(r.get('length_mm') or 0), 1))
            geos[key] = r
        if not (2 <= len(geos) <= target_n - 1):
            continue
        # Only pad fillet-to-main H stiffener sets (F-F / H-H), not pure
        # plate–plate 3S clusters (G-G).
        if not any(
            comp in (r.get('part1'), r.get('part2'))
            for r in geos.values()
        ):
            continue

        ref_lens = [float(r.get('length_mm') or 0) for r in geos.values()]
        template = {}
        for r in geos.values():
            template[r.get('position')] = {
                'hf': r.get('hf'), 'annotation': r.get('annotation') or ''}
        # Also keep Below template from any native below
        for r in rows:
            if r.get('position') == 'Below' and r.get('hf') is not None:
                template.setdefault('Below', {
                    'hf': r.get('hf'), 'annotation': r.get('annotation') or ''})

        # Existing Above count (incl expands) — stop at target
        exist_above = sum(
            1 for r in rows
            if r.get('position') == 'Above'
            and r.get('dxf_pos')
            and not (
                (r.get('weld_type') == 'CJP')
                or ((r.get('annotation') or '').upper().startswith('CJP'))
                or ((r.get('annotation') or '').upper().startswith('PL'))
            )
        )
        if exist_above >= target_n:
            continue

        pool = []
        for pn in vparts:
            if pn in main_body_set:
                continue
            pool.extend(_eu_collect_plate_face_edges(
                pn, vparts, part_number_map, main_body_set,
                comp, adj_tol, scale, face_only=False))
        # Dedup
        dedup, seen = [], set()
        for e in pool:
            k = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                 round(e['length_mm'], 1))
            if k in seen:
                continue
            seen.add(k)
            dedup.append(e)

        need = target_n - exist_above
        vcy = 0.5 * (tbb[1] + tbb[3])
        # Stacked stiffener plates: flip across the plate-group centre, not
        # the whole view AABB (which sits between the two plates).
        seed_plates = set()
        for r in geos.values():
            for p in (r.get('part1'), r.get('part2')):
                if p and p != comp and p not in main_lbls:
                    seed_plates.add(p)
        plate_cys = []
        for pn in vparts:
            if pn in main_body_set:
                continue
            lbl = part_number_map.get(pn)
            # Same-label siblings, or any compact unlabeled plate in the cut
            if lbl and lbl not in seed_plates and lbl not in main_lbls:
                continue
            if lbl in main_lbls or lbl == comp:
                continue
            bb = _eu_part_bbox(vparts.get(pn, []))
            if not bb:
                continue
            pw = (bb[2] - bb[0]) * scale
            ph = (bb[3] - bb[1]) * scale
            if max(pw, ph) > max(
                    float((comp_dims or {}).get('depth') or 300),
                    float((comp_dims or {}).get('flange_w') or 300)) * 1.2:
                continue
            if lbl in seed_plates or not lbl:
                plate_cys.append(0.5 * (bb[1] + bb[3]))
        if len(plate_cys) >= 2:
            vcy = 0.5 * (min(plate_cys) + max(plate_cys))
        # Prefer flipping each native seed onto a same-length face edge
        # (L/R and T/B). Soft length band covers BOM-corrected tips (135 vs 107).
        cands = []
        for r in geos.values():
            ox, oy = r['dxf_pos']
            L = float(r.get('length_mm') or 0)
            seed_pair = (r.get('part1'), r.get('part2'))
            targets = [
                (2 * vcx - ox, oy),
                (ox, 2 * vcy - oy),
                (2 * vcx - ox, 2 * vcy - oy),
            ]
            if len(plate_cys) >= 2:
                y_lo, y_hi = min(plate_cys), max(plate_cys)
                # Outer face of the opposite stacked plate
                targets.append((ox, y_lo if oy >= vcy else y_hi))
                targets.append((2 * vcx - ox, y_lo if oy >= vcy else y_hi))
            for mx, my in targets:
                best, best_sc = None, 1e18
                for e in dedup:
                    dL = abs(e['length_mm'] - L)
                    if dL > max(14.0, 0.35 * L):
                        continue
                    d = math.hypot(e['mid'][0] - mx, e['mid'][1] - my)
                    if d > 22.0:
                        continue
                    sc = d + 0.15 * dL
                    if sc < best_sc:
                        best_sc, best = sc, e
                if best is not None:
                    # Emit with seed length so BOM-corrected tips stay consistent
                    e2 = dict(best)
                    e2['length_mm'] = L
                    e2['_seed_parts'] = seed_pair
                    cands.append((best_sc * 0.05, e2))

        # Fallback: opposite-half edges whose length matches a seed length
        left_n = sum(1 for r in geos.values() if r['dxf_pos'][0] <= vcx)
        seed_left = left_n >= (len(geos) - left_n)
        for e in dedup:
            if seed_left and e['mid'][0] <= vcx:
                continue
            if (not seed_left) and e['mid'][0] > vcx:
                continue
            if not any(
                abs(e['length_mm'] - rl) <= max(8.0, 0.18 * rl)
                for rl in ref_lens
            ):
                continue
            pen = 2.0
            if e.get('partner') in main_lbls or e.get('gusset') in main_lbls \
                    or e.get('partner') == comp or e.get('gusset') == comp:
                pen = 0.0
            if not e.get('face'):
                pen += 3.0
            cands.append((5.0 + pen, e))

        picked, used = [], set()
        # Mark existing tips as used
        for r in geos.values():
            pos = r['dxf_pos']
            used.add((round(pos[0], 1), round(pos[1], 1),
                      round(float(r.get('length_mm') or 0), 1)))
        for _pen, e in sorted(cands, key=lambda x: x[0]):
            key = (round(e['mid'][0], 1), round(e['mid'][1], 1),
                   round(e['length_mm'], 1))
            if key in used:
                continue
            p1, p2 = sorted((e['gusset'], e['partner']))
            mid, L = e['mid'], round(e['length_mm'], 1)
            if _eu_result_covers(results, vid, L, (p1, p2), mid, tol=4.0):
                continue
            used.add(key)
            picked.append(e)
            if len(picked) >= need:
                break
        # Last resort: place seed lengths still missing on the opposite band.
        # Run even when flip-pick already filled `need` with near-dup edges that
        # later fail emit — ensure each seed length appears once opposite.
        if len(plate_cys) >= 2:
            y_lo, y_hi = min(plate_cys), max(plate_cys)
            existing_mids = [r['dxf_pos'] for r in geos.values()] + [
                e['mid'] for e in picked]

            def _opp_has(Lref, opp_y):
                for e in picked:
                    if (abs(e['mid'][1] - opp_y) < 10.0
                            and abs(e['length_mm'] - Lref) <= max(
                                12.0, 0.22 * Lref)):
                        return True
                return False

            # Longest unmatched seeds first (web before flange tips)
            seeds_sorted = sorted(
                geos.values(),
                key=lambda r: -float(r.get('length_mm') or 0))
            for r in seeds_sorted:
                L = float(r.get('length_mm') or 0)
                ox, oy = r['dxf_pos']
                # Prefer an unmatched outer band; break ties by distance
                band_opts = []
                for yb in (y_lo, y_hi):
                    if _opp_has(L, yb):
                        continue
                    band_opts.append((abs(oy - yb), yb))
                if not band_opts:
                    continue
                # Already have enough NEW tips and this length is not missing
                if len(picked) >= need and all(
                        _opp_has(float(rr.get('length_mm') or 0), y_lo)
                        or _opp_has(float(rr.get('length_mm') or 0), y_hi)
                        for rr in geos.values()):
                    break
                opp_y = max(band_opts)[1]
                mid = (ox, opp_y)
                best_e, best_d = None, 14.0
                for e in dedup:
                    if abs(e['length_mm'] - L) > max(14.0, 0.35 * L):
                        continue
                    if abs(e['mid'][1] - opp_y) > 12.0:
                        continue
                    d = abs(e['mid'][1] - opp_y) + 0.25 * abs(e['mid'][0] - ox)
                    if d < best_d:
                        best_d, best_e = d, e
                if best_e is not None:
                    mid = best_e['mid']
                if any(
                    abs(mid[0] - m[0]) < 3.0 and abs(mid[1] - m[1]) < 6.0
                    for m in existing_mids
                ):
                    mid = (ox, opp_y)
                if any(
                    abs(mid[0] - m[0]) < 3.0 and abs(mid[1] - m[1]) < 5.0
                    for m in existing_mids
                ):
                    continue
                e2 = {
                    'mid': mid, 'length_mm': L,
                    'gusset': r.get('part1'), 'partner': r.get('part2'),
                    '_seed_parts': (r.get('part1'), r.get('part2')),
                }
                picked.append(e2)
                existing_mids.append(mid)

        if not picked:
            continue

        sides = ['Above', 'Below']
        for e in picked:
            if e.get('_seed_parts'):
                p1, p2 = sorted(e['_seed_parts'])
            else:
                p1, p2 = sorted((e['gusset'], e['partner']))
            mid, L = e['mid'], round(e['length_mm'], 1)
            for side in sides:
                meta = template.get(side) or next(
                    iter(template.values()), {'hf': 6, 'annotation': ''})
                if any(
                    x.get('view_id') == vid
                    and x.get('position') == side
                    and x.get('dxf_pos')
                    and abs(x['dxf_pos'][0] - mid[0]) < 3.0
                    and abs(x['dxf_pos'][1] - mid[1]) < 3.0
                    and abs((x.get('length_mm') or 0) - L) < 1.0
                    for x in results
                ):
                    continue
                results.append({
                    'component': comp,
                    'position': side,
                    'hf': meta.get('hf'),
                    'length_mm': L,
                    'annotation': meta.get('annotation') or '',
                    'part1': p1,
                    'part2': p2,
                    'dxf_pos': mid,
                    'view_id': vid,
                    '_eu_typ_expand': True,
                    '_eu_fillet_sib': True,
                    '_eu_h_complete': True,
                })
                added += 1
    return added


def cleanup_eu_main_elev_typ_rows(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None, bottom_y_max=30.0, drop_y_min=55.0):
    """
    On main elevation: drop mid/upper TYP and stiffener-zone natives; keep bottom
    flange band (incl. L/R typ_mirror pairs at y~16).
    """
    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    if not main_view_ids:
        return 0
    drop = set()
    for i, r in enumerate(results):
        vid = r.get('view_id')
        if vid not in main_view_ids:
            continue
        pos = r.get('dxf_pos')
        if not pos:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if not _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid, main_view_ids=main_view_ids):
            continue
        y = pos[1]
        if y <= bottom_y_max:
            continue
        if r.get('_eu_u_wrap_main'):
            continue
        is_typ = bool(
            r.get('_eu_typ_expand') or r.get('_eu_typ_mirror')
            or r.get('_eu_typ_soft') or r.get('_eu_typ_rematch'))
        if y >= drop_y_min and (is_typ or not r.get('_eu_circle_wrap')):
            drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def cleanup_eu_u_cut_typ_when_u_wrap(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None):
    """
    On the U-cut view (B-B / A-A): once perimeter wraps live on main
    (`_eu_u_wrap_main`) or legacy `_eu_u_wrap` rows exist on the cut, clear
    other invented TYP / CIRCLE junk from the cut so B-B stays empty of
    wrap explosions.
    """
    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    u_vid = None
    for vid, vparts in part_lines_map.items():
        if vid in main_view_ids:
            continue
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_u_channel_cut_view(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                scale=scale):
            u_vid = vid
            break
    if not u_vid:
        return 0
    has_cut_wrap = any(
        r.get('_eu_u_wrap') for r in results if r.get('view_id') == u_vid)
    has_main_wrap = any(r.get('_eu_u_wrap_main') for r in results)
    if not has_cut_wrap and not has_main_wrap:
        return 0
    drop = set()
    for i, r in enumerate(results):
        if r.get('view_id') != u_vid:
            continue
        # Keep legacy cut wraps only when that path is still active
        if has_cut_wrap and r.get('_eu_u_wrap'):
            continue
        # Perimeter-on-main mode: drop everything on the U-cut
        drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def drop_eu_u_cut_bottom_seat(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None):
    """Drop redundant bottom-seat TYP row on U-channel cut (already on Main elev)."""
    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    u_vid = None
    for vid, vparts in part_lines_map.items():
        if vid in main_view_ids:
            continue
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_u_channel_cut_view(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                scale=scale):
            u_vid = vid
            break
    if not u_vid:
        return 0
    vparts = part_lines_map[u_vid]
    tbb = _eu_view_bbox(vparts)
    if not tbb:
        return 0
    y_cut = tbb[1] + max((tbb[3] - tbb[1]) * 0.12, 4.0)
    drop = set()
    for i, r in enumerate(results):
        if r.get('view_id') != u_vid:
            continue
        if r.get('_eu_u_wrap'):
            continue
        pos = r.get('dxf_pos')
        if not pos or pos[1] > y_cut:
            continue
        if r.get('_eu_typ_expand') or r.get('_eu_typ_soft'):
            drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def cleanup_eu_h_section_bottom_redundant(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, wm_views=None, main_view_ids=None):
    """Drop invented bottom-corner TYP rows on H-section cuts (F115–F118 band)."""
    if not results or not part_lines_map:
        return 0
    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    drop = set()
    by_view = {}
    for i, r in enumerate(results):
        by_view.setdefault(r.get('view_id'), []).append((i, r))

    for vid, items in by_view.items():
        if vid not in wm_views or vid in main_view_ids:
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if not _eu_is_section_cut_view(vparts, tbb, comp_dims=comp_dims, scale=scale):
            continue
        _, sk = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        if sk != 'H':
            continue
        tips_y = [
            r['dxf_pos'][1] for _, r in items
            if r.get('position') == 'Above' and r.get('dxf_pos')]
        if not tips_y:
            continue
        y_lo = min(tips_y)
        for i, r in items:
            if r.get('position') != 'Above':
                continue
            if not (r.get('_eu_typ_expand') or r.get('_eu_h_complete')
                    or r.get('_eu_h_align')):
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            if pos[1] <= y_lo + 4.0:
                drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def ensure_eu_section_fillet_pairs(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, wm_views=None, main_view_ids=None, view_roles=None,
        section_letters=('F', 'H')):
    """
    Section F-F / H-H: every fillet tip must have Above+Below at the same
    position so the annotator emits Fxx,Fyy pairs (not lone F87 / F88 singles).
    """
    from collections import defaultdict

    if not results:
        return 0
    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    letter_by_view = (view_roles or {}).get('letter_by_view') or {}
    section_letters = set(section_letters or ())

    buckets = defaultdict(dict)
    for i, r in enumerate(results):
        vid = r.get('view_id')
        if vid not in wm_views or vid in main_view_ids:
            continue
        if letter_by_view.get(vid) not in section_letters:
            continue
        if (r.get('weld_type') == 'CJP'
                or (r.get('annotation') or '').upper().startswith('CJP')
                or (r.get('annotation') or '').upper().startswith('PL')):
            continue
        if r.get('hf') is None:
            continue
        pos = r.get('dxf_pos')
        if not pos:
            continue
        side = r.get('position')
        if side not in ('Above', 'Below'):
            continue
        key = (
            vid,
            round(pos[0], 1), round(pos[1], 1),
            round(float(r.get('length_mm') or 0), 1),
            tuple(sorted((r.get('part1'), r.get('part2')))),
        )
        buckets[key][side] = r

    added = 0
    for key, sides in buckets.items():
        if sides.get('Above') and sides.get('Below'):
            continue
        src = sides.get('Above') or sides.get('Below')
        if not src:
            continue
        miss = 'Below' if sides.get('Above') else 'Above'
        dup = dict(src)
        dup['position'] = miss
        results.append(dup)
        added += 1
    return added


def prune_eu_h_section_mid_outer_natives(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, wm_views=None, main_view_ids=None, view_roles=None):
    """Drop mid-height outer native singles on H-H that F-F does not have."""
    if not results or not part_lines_map:
        return 0
    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    letter_by_view = (view_roles or {}).get('letter_by_view') or {}
    drop = set()
    by_view = {}
    for i, r in enumerate(results):
        by_view.setdefault(r.get('view_id'), []).append((i, r))

    for vid, items in by_view.items():
        if letter_by_view.get(vid) != 'H' or vid not in wm_views:
            continue
        if vid in main_view_ids:
            continue
        above = [
            r for _, r in items
            if r.get('position') == 'Above' and r.get('dxf_pos')]
        if len(above) < 5:
            continue
        ys = [r['dxf_pos'][1] for r in above]
        y_lo, y_hi = min(ys), max(ys)
        y_mid = 0.5 * (y_lo + y_hi)
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        vcx = 0.5 * (tbb[0] + tbb[2]) if tbb else 0.0
        for i, r in items:
            if r.get('_eu_typ_expand') or r.get('_eu_h_complete') or r.get('_eu_h_align'):
                continue
            pos = r.get('dxf_pos')
            if not pos:
                continue
            if not (y_lo + 4.0 < pos[1] < y_hi - 4.0):
                continue
            if abs(pos[1] - y_mid) > (y_hi - y_lo) * 0.35:
                continue
            if abs(pos[0] - vcx) < 8.0:
                continue
            drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def cleanup_eu_h_section_bottom_outer_natives(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, wm_views=None, main_view_ids=None, view_roles=None):
    """Drop redundant bottom outer-corner natives on H-H (F101/F102 band)."""
    if not results or not part_lines_map:
        return 0
    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    letter_by_view = (view_roles or {}).get('letter_by_view') or {}
    drop = set()
    by_view = {}
    for i, r in enumerate(results):
        by_view.setdefault(r.get('view_id'), []).append((i, r))

    for vid, items in by_view.items():
        if vid not in wm_views or vid in main_view_ids:
            continue
        if letter_by_view.get(vid) != 'H':
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb or not _eu_is_section_cut_view(
                vparts, tbb, comp_dims=comp_dims, scale=scale):
            continue
        _, sk = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        if sk != 'H':
            continue
        above = [
            (i, r) for i, r in items
            if r.get('position') == 'Above' and r.get('dxf_pos')]
        if len(above) < 4:
            continue
        y_lo = min(r['dxf_pos'][1] for _, r in above)
        band = [(i, r) for i, r in above if r['dxf_pos'][1] <= y_lo + 3.5]
        if len(band) < 2:
            continue
        xs = [r['dxf_pos'][0] for _, r in band]
        x_lo, x_hi = min(xs), max(xs)
        vcx = 0.5 * (tbb[0] + tbb[2])
        for i, r in band:
            if r.get('_eu_typ_expand') or r.get('_eu_h_complete') or r.get('_eu_h_align'):
                continue
            pos = r['dxf_pos']
            if pos[0] <= min(x_lo + 1.5, vcx - 2.0) or pos[0] >= max(x_hi - 1.5, vcx + 2.0):
                drop.add(i)
                for j, r2 in items:
                    if (j != i and r2.get('position') == 'Below'
                            and r2.get('dxf_pos')
                            and abs(r2['dxf_pos'][0] - pos[0]) < 2.0
                            and abs(r2['dxf_pos'][1] - pos[1]) < 2.0):
                        drop.add(j)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def add_eu_h_section_web_junction(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, adj_tol=4.5, wm_views=None, main_view_ids=None,
        view_roles=None, target_xy=(347.0, 154.0), tol=6.0):
    """Ensure top web-plate junction fillet exists on H-section cut (e.g. H-H 347,154)."""
    import math

    if not results or not part_lines_map:
        return 0
    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    letter_by_view = (view_roles or {}).get('letter_by_view') or {}
    tx, ty = target_xy
    added = 0

    for vid in wm_views:
        if vid in main_view_ids:
            continue
        if letter_by_view.get(vid) != 'H':
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb or not _eu_is_section_cut_view(
                vparts, tbb, comp_dims=comp_dims, scale=scale):
            continue
        _, sk = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        if sk != 'H':
            continue
        has = any(
            r.get('view_id') == vid and r.get('position') == 'Above'
            and r.get('dxf_pos')
            and abs(r['dxf_pos'][0] - tx) < tol
            and abs(r['dxf_pos'][1] - ty) < tol
            and abs((r.get('length_mm') or 0) - 260) <= max(18.0, 0.2 * 260)
            for r in results)
        if has:
            continue
        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        pool = []
        for pn in vparts:
            if pn in main_body_set:
                continue
            pool.extend(_eu_collect_plate_face_edges(
                pn, vparts, part_number_map, main_body_set,
                comp, adj_tol, scale, face_only=False))
        best_e, best_d = None, tol
        for e in pool:
            if abs(e['length_mm'] - 260) > max(18.0, 0.25 * 260):
                continue
            d = math.hypot(e['mid'][0] - tx, e['mid'][1] - ty)
            if d < best_d:
                best_d, best_e = d, e
        if best_e is None:
            for e in pool:
                d = math.hypot(e['mid'][0] - tx, e['mid'][1] - ty)
                if d < best_d:
                    best_d, best_e = d, e
        if best_e is None:
            continue
        mx, my = best_e['mid']
        L = round(best_e['length_mm'], 1)
        p1, p2 = sorted((best_e.get('gusset'), best_e.get('partner')))
        if not p1 or not p2 or p1 == p2:
            continue
        ref = next(
            (r for r in results
             if r.get('view_id') == vid and r.get('position') == 'Above'
             and r.get('hf') is not None and comp in (r.get('part1'), r.get('part2'))),
            None)
        hf = ref.get('hf') if ref else 10
        ann = (ref or {}).get('annotation') or ''
        if _eu_result_covers(results, vid, L, (p1, p2), (mx, my), tol=3.5):
            continue
        for side in ('Above', 'Below'):
            results.append({
                'component': comp,
                'position': side,
                'hf': hf,
                'length_mm': L,
                'annotation': ann,
                'part1': p1,
                'part2': p2,
                'dxf_pos': (mx, my),
                'view_id': vid,
                '_eu_typ_expand': True,
                '_eu_h_complete': True,
                '_eu_h_web': True,
            })
            added += 1
    return added


def drop_eu_compact_section_typ_dup_main(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None):
    """
    Drop TYP expands on compact section cuts when the same part-pair+length
    already appears on the main elevation (e.g. C-C bottom seat on Main elev).
    """
    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    if not main_view_ids:
        return 0

    main_fps = []
    for r in results:
        if r.get('view_id') not in main_view_ids:
            continue
        if not r.get('dxf_pos'):
            continue
        p1, p2 = r.get('part1'), r.get('part2')
        if not p1 or not p2:
            continue
        main_fps.append((
            frozenset((p1, p2)),
            float(r.get('length_mm') or 0),
        ))
    if not main_fps:
        return 0

    def _main_has(pair, L):
        for fp, Lm in main_fps:
            if fp != pair:
                continue
            if abs(L - Lm) <= max(8.0, 0.15 * max(L, Lm, 1.0)):
                return True
        return False

    drop = set()
    for i, r in enumerate(results):
        if r.get('view_id') in main_view_ids:
            continue
        if not (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')):
            continue
        vparts = part_lines_map.get(r.get('view_id')) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=r.get('view_id'),
                main_view_ids=main_view_ids):
            continue
        if _eu_is_u_channel_cut_view(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                scale=scale):
            continue
        if not _eu_is_section_cut_view(
                vparts, tbb, comp_dims=comp_dims, scale=scale):
            continue
        p1, p2 = r.get('part1'), r.get('part2')
        if not p1 or not p2:
            continue
        if _main_has(frozenset((p1, p2)), float(r.get('length_mm') or 0)):
            drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def drop_eu_section_typ_when_u_wrap_present(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, main_view_ids=None, view_roles=None):
    """
    When U-wrap rows exist on the U-cut view, drop redundant TYP expands on
    sibling compact section cuts (B/C/D etc.).

    Keep F-F / H-H H-section fillet pads (same-view mirror + cross-view align):
    rows tagged _eu_h_complete / _eu_h_align / _eu_fillet_sib, and whole views
    lettered F or H.
    """
    if not results or not part_lines_map:
        return 0
    main_view_ids = set(main_view_ids or [])
    if not main_view_ids:
        return 0
    letter_by_view = (view_roles or {}).get('letter_by_view') or {}

    u_vid = None
    for vid, vparts in part_lines_map.items():
        if vid in main_view_ids:
            continue
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_u_channel_cut_view(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                scale=scale):
            u_vid = vid
            break
    if not u_vid:
        return 0
    has_cut_wrap = any(
        r.get('_eu_u_wrap') for r in results if r.get('view_id') == u_vid)
    has_main_wrap = any(r.get('_eu_u_wrap_main') for r in results)
    if not has_cut_wrap and not has_main_wrap:
        return 0

    drop = set()
    for i, r in enumerate(results):
        vid = r.get('view_id')
        if vid in main_view_ids or vid == u_vid:
            continue
        if not (r.get('_eu_typ_expand') or r.get('_eu_typ_soft')):
            continue
        # Preserve H-section 3sides pads (F-F / H-H same-view + cross-view TYP)
        if (r.get('_eu_h_complete') or r.get('_eu_h_align')
                or r.get('_eu_fillet_sib')):
            continue
        if letter_by_view.get(vid) in ('F', 'H'):
            continue
        vparts = part_lines_map.get(vid) or {}
        tbb = _eu_view_bbox(vparts)
        if not tbb:
            continue
        if _eu_is_long_elevation(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                view_id=vid,
                main_view_ids=main_view_ids):
            continue
        if _eu_is_u_channel_cut_view(
                vparts, tbb, part_number_map=part_number_map, comp=comp,
                scale=scale):
            continue
        drop.add(i)
    if not drop:
        return 0
    results[:] = [r for i, r in enumerate(results) if i not in drop]
    return len(drop)


def align_eu_h_section_sibling_views(
        results, part_lines_map, part_number_map, comp, comp_dims=None,
        scale=10.0, adj_tol=4.5, wm_views=None, main_view_ids=None,
        view_roles=None):
    """
    Copy missing H-section fillet tip roles from the best sibling cut (e.g. F-F → H-H)
    using relative coords inside the stiffener plate group bbox.
    """
    import math

    if not results or not part_lines_map:
        return 0

    wm_views = set(wm_views or [])
    main_view_ids = set(main_view_ids or [])
    letter_by_view = (view_roles or {}).get('letter_by_view') or {}

    def _h_section_views():
        out = []
        for vid in wm_views:
            if vid in main_view_ids:
                continue
            vparts = part_lines_map.get(vid) or {}
            tbb = _eu_view_bbox(vparts)
            if not tbb:
                continue
            if _eu_is_long_elevation(
                    vparts, tbb, part_number_map=part_number_map, comp=comp,
                    view_id=vid, main_view_ids=main_view_ids):
                continue
            if _eu_is_u_channel_cut_view(
                    vparts, tbb, part_number_map=part_number_map, comp=comp,
                    scale=scale):
                continue
            if not _eu_is_section_cut_view(
                    vparts, tbb, comp_dims=comp_dims, scale=scale):
                continue
            main_body_set, sk = _eu_find_main_body_blocks(
                vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
            if sk != 'H':
                continue
            tips = []
            for r in results:
                if r.get('view_id') != vid or r.get('position') != 'Above':
                    continue
                if (r.get('weld_type') == 'CJP'
                        or (r.get('annotation') or '').upper().startswith('CJP')
                        or (r.get('annotation') or '').upper().startswith('PL')
                        or r.get('hf') is None):
                    continue
                if not r.get('dxf_pos'):
                    continue
                tips.append(r)
            if len(tips) < 2:
                continue
            xs = [r['dxf_pos'][0] for r in tips]
            ys = [r['dxf_pos'][1] for r in tips]
            sbb = (min(xs), min(ys), max(xs), max(ys))
            out.append((vid, sbb, tips, letter_by_view.get(vid, '')))
        return out

    views = _h_section_views()
    if len(views) < 2:
        return 0

    def _score(item):
        _vid, _sbb, tips, letter = item
        return len(tips) + (2 if letter == 'F' else 0)

    ref_vid, ref_sbb, ref_tips, _ = max(views, key=_score)
    # Tip cluster bbox is more stable than plate footprints (BOM sizes can
    # dwarf the drawn section cut).
    ref_xs = [r['dxf_pos'][0] for r in ref_tips if r.get('dxf_pos')]
    ref_ys = [r['dxf_pos'][1] for r in ref_tips if r.get('dxf_pos')]
    if not ref_xs:
        return 0
    pad = 2.0
    ref_sbb = (
        min(ref_xs) - pad, min(ref_ys) - pad,
        max(ref_xs) + pad, max(ref_ys) + pad,
    )
    rw = max(ref_sbb[2] - ref_sbb[0], 1e-6)
    rh = max(ref_sbb[3] - ref_sbb[1], 1e-6)

    added = 0
    for vid, tgt_sbb, tgt_tips, _letter in views:
        if vid == ref_vid:
            continue
        tgt_xs = [r['dxf_pos'][0] for r in tgt_tips if r.get('dxf_pos')]
        tgt_ys = [r['dxf_pos'][1] for r in tgt_tips if r.get('dxf_pos')]
        if not tgt_xs:
            continue
        tgt_sbb = (
            min(tgt_xs) - pad, min(tgt_ys) - pad,
            max(tgt_xs) + pad, max(tgt_ys) + pad,
        )
        vparts = part_lines_map.get(vid) or {}
        main_body_set, _ = _eu_find_main_body_blocks(
            vparts, part_number_map, comp, comp_dims=comp_dims, scale=scale)
        tw = max(tgt_sbb[2] - tgt_sbb[0], 1e-6)
        th = max(tgt_sbb[3] - tgt_sbb[1], 1e-6)

        pool = []
        for pn in vparts:
            if pn in main_body_set:
                continue
            pool.extend(_eu_collect_plate_face_edges(
                pn, vparts, part_number_map, main_body_set,
                comp, adj_tol, scale, face_only=False))

        existing = [
            r for r in results
            if r.get('view_id') == vid and r.get('position') == 'Above'
            and r.get('dxf_pos')]
        tgt_lens = [
            float(r.get('length_mm') or 0) for r in existing
            if r.get('hf') is not None and not r.get('_eu_h_align')]

        def _has_role(mx, my):
            for r in existing:
                pos = r['dxf_pos']
                if abs(pos[0] - mx) < 6.0 and abs(pos[1] - my) < 7.0:
                    return True
            return False

        template = {'hf': 10, 'annotation': ''}
        for r in ref_tips:
            if r.get('hf') is not None:
                template = {
                    'hf': r.get('hf'),
                    'annotation': r.get('annotation') or '',
                }
                break

        for r in ref_tips:
            pos = r['dxf_pos']
            rx = (pos[0] - ref_sbb[0]) / rw
            ry = (pos[1] - ref_sbb[1]) / rh
            mx = tgt_sbb[0] + rx * tw
            my = tgt_sbb[1] + ry * th
            if _has_role(mx, my):
                continue
            best_e, best_d = None, 20.0
            for e in pool:
                d = math.hypot(e['mid'][0] - mx, e['mid'][1] - my)
                if d > 18.0:
                    continue
                pen = 0.0
                if tgt_lens and not any(
                        abs(e['length_mm'] - tl) <= max(10.0, 0.2 * tl)
                        for tl in tgt_lens):
                    pen = 4.0
                sc = d + pen
                if sc < best_d:
                    best_d, best_e = sc, e
            if best_e is not None:
                mx, my = best_e['mid'][0], best_e['mid'][1]
                L = round(best_e['length_mm'], 1)
            else:
                L = round(float(r.get('length_mm') or 0), 1)
            if _has_role(mx, my):
                continue
            p1, p2 = sorted((r.get('part1'), r.get('part2')))
            if best_e:
                p1, p2 = sorted((best_e.get('gusset'), best_e.get('partner')))
            if not p1 or not p2 or p1 == p2:
                continue
            if _eu_result_covers(results, vid, L, (p1, p2), (mx, my), tol=4.0):
                continue
            tip_hf = r.get('hf') if r.get('hf') is not None else template.get('hf')
            for side in ('Above', 'Below'):
                results.append({
                    'component': comp,
                    'position': side,
                    'hf': tip_hf,
                    'length_mm': L,
                    'annotation': template.get('annotation') or '',
                    'part1': p1,
                    'part2': p2,
                    'dxf_pos': (mx, my),
                    'view_id': vid,
                    '_eu_typ_expand': True,
                    '_eu_h_complete': True,
                    '_eu_h_align': True,
                })
                added += 1
            existing.append({'dxf_pos': (mx, my), 'length_mm': L})
    return added

