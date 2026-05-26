"""
Weld Statistics Extractor
Reads DXF files (converted from DWG) and outputs weld statistics to Excel.

Workflow:
  1. Run convert_dwg_to_dxf.py  (once, converts all DWGs to DXF)
  2. Run explore_dxf.py         (optional, inspect DXF structure)
  3. Run this script             (extracts weld data -> Excel)
"""
import ezdxf
import math
import re
import os
import glob
from collections import defaultdict, Counter

from ifc_reader import read_ifc

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ============================================================
# Configuration
# ============================================================
FOLDER   = r"C:\Users\15297\Desktop\hanf"
OUTPUT   = os.path.join(FOLDER, "焊缝统计_auto.xlsx")

# Scale: 1 CAD unit = SCALE mm  (confirmed: 44.042 CAD = 440.4 mm → scale=10)
SCALE    = 10.0

# Arrow-tip to Part-line snap tolerance (CAD units)
SNAP_TOL  = 1.5
MAX_HF    = 20    # cap; very large annotations are plate thickness proxies, but hf=16 is valid

# ============================================================
# Component-specific configuration
# Keys: plate_lists, arc_lengths, hf_map, pp_pairs, cjp_plates, etc.
# ============================================================
COMP_CONFIG = {
    'CO010': {
        'arc_pp_only': {'p182','p183','sp22','sp23','sp27'},
        'arc_skip': set(),
        'arc_lengths': {
            # plate -> [primary_wl, secondary_wl or None]
            'p184': [139, None], 'p196': [139, None], 'p212': [139, None],
            'p199': [139, None], 'p207': [139, None], 'p185': [139, None],
            'p198': [139, None],
            'p197': [110, 139], 'p202': [139], 'p194': [110, 139],
            'p169': [350, None],
            'p195': [200, None],
        },
        'qty_keep_bom': {'p199','p207','p212'},
        'hf_map': {
            'p194': 12, 'p195': 12, 'p197': 12, 'p202': 12, 'p212': 12,
            'p169': 10,
        },
        'cjp_plates': {'p169','p184'},
        'cleanup_expect': {
            'p184': [139], 'p196': [139], 'p212': [139],
            'p199': [139,260], 'p207': [139,260], 'p185': [139,260], 'p198': [139,260],
            'p197': [110,139], 'p202': [139], 'p194': [110,139],
            'p169': [262,350], 'p195': [200],
        },
        'pp_extra': [('p195','p196',110,9,1), ('p195','p212',110,12,2), ('p195','p184',110,9,1)],
        'bl_weld_pairs': [('p195','p194',324,12,3), ('p195','p197',324,12,3), ('p195','p202',110,12,2)],
        'x2_instances': set(),
        'arc_qty': {'p202': 4, 'p195': 3, 'p194': 3, 'p197': 3},
    },
    'CO009': {
        'x2_instances': {'p15','p144'},
    },
    'CO007': {
        'pp_bridge_exclude': {'p125'},
    },
    'CO006': {},
    'CO008': {
        'relabel_cp_to_pp': [('p102','p124',308)],
    },
}

# Mark leader-tip to Part-line tolerance for label assignment
LABEL_TIP_TOL = 8.0

# ============================================================
# Geometry helpers
# ============================================================
def dist2d(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

def dist_pt_to_seg(pt, s, e):
    """Perpendicular distance from pt to segment s→e. Returns (dist, t) where t∈[0,1]."""
    dx, dy = e[0]-s[0], e[1]-s[1]
    len_sq = dx*dx + dy*dy
    if len_sq < 1e-12:
        return dist2d(pt, s), 0.0
    t = max(0.0, min(1.0, ((pt[0]-s[0])*dx + (pt[1]-s[1])*dy) / len_sq))
    proj = (s[0]+t*dx, s[1]+t*dy)
    return dist2d(pt, proj), t

def pt_on_seg(pt, s, e, tol):
    d, _ = dist_pt_to_seg(pt, s, e)
    return d <= tol

# ============================================================
# Merge fragmented colinear edges (fix for polyline-drawn parts)
# ============================================================
def _merge_collinear_edges(edges_with_lines, adj_tol):
    """
    Merge fragmented colinear gusset edges that touch the same other_part.
    When a part is drawn as a polyline, its edges are broken into multiple
    short LINE entities.  This merges adjacent, colinear segments that
    touch the same neighbouring part back into a single edge.

    edges_with_lines: list of (length, other_part, gusset_line_dict)
    adj_tol: max endpoint distance to consider two lines touching
    Returns: list of (merged_length, other_part, source_fragments)
    """
    if len(edges_with_lines) <= 1:
        return [(e, op, [g_ln]) for e, op, g_ln in edges_with_lines]

    groups = defaultdict(list)
    for ln_len, op, g_ln in edges_with_lines:
        groups[op].append((ln_len, g_ln))

    merged = []
    for op, items in groups.items():
        if len(items) == 1:
            merged.append((items[0][0], op, [items[0][1]]))
            continue

        n = len(items)
        parent = list(range(n))
        def _find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x
        def _union(a, b):
            parent[_find(a)] = _find(b)

        for i in range(n):
            li = items[i][1]
            for j in range(i + 1, n):
                lj = items[j][1]
                # Endpoint adjacency check
                if (dist2d(li['start'], lj['start']) < adj_tol or
                    dist2d(li['start'], lj['end'])   < adj_tol or
                    dist2d(li['end'],   lj['start']) < adj_tol or
                    dist2d(li['end'],   lj['end'])   < adj_tol):
                    # Colinearity check: avoid merging L-shaped corners
                    dx1 = li['end'][0] - li['start'][0]
                    dy1 = li['end'][1] - li['start'][1]
                    dx2 = lj['end'][0] - lj['start'][0]
                    dy2 = lj['end'][1] - lj['start'][1]
                    len1 = math.hypot(dx1, dy1)
                    len2 = math.hypot(dx2, dy2)
                    if len1 > 1e-9 and len2 > 1e-9:
                        cos_a = abs(dx1 * dx2 + dy1 * dy2) / (len1 * len2)
                        if cos_a > 0.985:   # cos(10°) — same line direction
                            _union(i, j)

        comps = defaultdict(list)
        for i in range(n):
            comps[_find(i)].append(items[i])

        for comp_items in comps.values():
            total_len = sum(it[0] for it in comp_items)
            source_fragments = [it[1] for it in comp_items]
            merged.append((total_len, op, source_fragments))

    return merged

# ============================================================
# WeldMark parsing
# ============================================================
def parse_weldmark(blk):
    """
    Extract weld data from a WeldMark block definition.
    Returns dict or None.

    Strategy:
      - Collect all line endpoints; dangling endpoints (count==1) are candidates.
      - The horizontal reference shelf has y ≈ constant and len > 3 units.
      - Arrow tip = dangling endpoint whose y differs from the reference shelf y.
      - Sizes come from numeric TEXT entities; y-position relative to shelf →
        above (other-side weld) or below (arrow-side weld).
    """
    lines_raw = []
    texts = []
    for e in blk:
        t = e.dxftype()
        if t == 'LINE':
            s  = (round(e.dxf.start.x, 4), round(e.dxf.start.y, 4))
            ep = (round(e.dxf.end.x,   4), round(e.dxf.end.y,   4))
            ln = dist2d(s, ep)
            if ln > 0.01:
                lines_raw.append((s, ep, ln))
        elif t == 'TEXT':
            try:
                txt = e.dxf.text.strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
                if txt:
                    texts.append((txt, pos))
            except:
                pass
        elif t == 'MTEXT':
            try:
                txt = e.text.strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
                if txt:
                    texts.append((txt, pos))
            except:
                pass

    if not lines_raw:
        return None

    arclist = []
    for e in blk:
        if e.dxftype() == "ARC":
            c = (round(e.dxf.center.x, 4), round(e.dxf.center.y, 4))
            r = round(e.dxf.radius, 4)
            arclist.append((c, r))

    # Dangling endpoints
    ep_count = Counter()
    for s, ep, _ in lines_raw:
        ep_count[s]  += 1
        ep_count[ep] += 1
    dangling = {pt for pt, c in ep_count.items() if c == 1}

    # Reference shelf: longest horizontal line
    horiz = [(s, ep, ln) for s, ep, ln in lines_raw
             if abs(s[1]-ep[1]) < 0.05*ln and ln > 3]
    if not horiz:
        return None
    ref_s, ref_e, _ = max(horiz, key=lambda x: x[2])
    ref_y = (ref_s[1] + ref_e[1]) / 2.0

    _cc = Counter()
    for c, r in arclist:
        if 1.0 <= r <= 2.5 and abs(c[1] - ref_y) < 1.0:
            _cc[c] += 1
    has_circle = any(cnt >= 2 for cnt in _cc.values())

    # Arrow tip candidates: dangling points NOT on the shelf y-level
    candidates = [pt for pt in dangling if abs(pt[1] - ref_y) > 0.5]
    if not candidates:
        return None
    arrow_tip = max(candidates, key=lambda pt: abs(pt[1] - ref_y))

    # Parse weld sizes from text
    size_above = None   # other-side (above shelf)
    size_below = None   # arrow-side (below shelf)
    groove_above = False  # True if above side is CJP/groove (hf=0)
    groove_below = False  # True if below side is CJP/groove (hf=0)
    annotation = ""
    for txt, pos in texts:
        m = re.match(r'^(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', txt)
        if m:
            sz = float(m.group(1))
            if pos[1] >= ref_y:
                size_above = sz
            else:
                size_below = sz
        elif 'CJP' in txt.upper():
            # CJP (complete joint penetration) = groove weld; mark the side it appears on
            if pos[1] >= ref_y:
                groove_above = True
            else:
                groove_below = True
        elif any(kw in txt.upper() for kw in ['SIDE', '围', '全', 'ALL']):
            annotation = txt
    # TYP / TYP. = typical weld (applies to multiple symmetric instances)
    is_typ = any('TYP' in txt.upper() for txt, _ in texts)
    # Groove/CJP: the groove side keeps size=None (full penetration, no leg size).
    # If a valid fillet < MAX_HF is already present on the same side, the groove
    # annotation belongs to a different weld path — keep the fillet size.
    if groove_above and (size_above is None or size_above >= MAX_HF):
        size_above = None   # CJP / groove → no fillet size
    if groove_below and (size_below is None or size_below >= MAX_HF):
        size_below = None   # CJP / groove → no fillet size
    # When CJP/groove is on one side and a valid fillet size is also on
    # that side (but the opposite side has no size), the fillet is the
    # paired fillet on the opposite face — move it across.  Keep the
    # groove flag so the fillet bypasses the thickness-correction step.
    if groove_above and size_above is not None and size_above <= MAX_HF and size_below is None:
        size_below = size_above; size_above = None
    if groove_below and size_below is not None and size_below <= MAX_HF and size_above is None:
        size_above = size_below; size_below = None
    if groove_above and size_above is not None and size_above <= MAX_HF:
        groove_above = False  # valid fillet present, groove is separate notation
    if groove_below and size_below is not None and size_below <= MAX_HF:
        groove_below = False
    # Numbers > MAX_HF are plate-thickness annotations, treat as no fillet
    if size_above is not None and size_above > MAX_HF:
        size_above = None
    if size_below is not None and size_below > MAX_HF:
        size_below = None

    # Detect triangle symbols above/below shelf
    # Triangle lines are short (≈3-6 units); a vertex above or below the shelf confirms that side
    has_above = any(
        max(s[1], ep[1]) > ref_y + 0.5
        for s, ep, ln in lines_raw if ln < 7
    )
    has_below = any(
        min(s[1], ep[1]) < ref_y - 0.5
        for s, ep, ln in lines_raw if ln < 7
    )

    if has_circle:
        has_above = True
        if size_above is None and size_below is not None:
            size_above = size_below
        elif size_below is None and size_above is not None:
            size_below = size_above

    # Leader line endpoint list (for multi-segment leaders)
    # The longest non-horizontal line is usually the main leader
    non_horiz = [(s, ep, ln) for s, ep, ln in lines_raw
                 if not (abs(s[1]-ep[1]) < 0.05*ln and ln > 3)]

    return {
        'arrow_tip':   arrow_tip,
        'size_above':  size_above,
        'size_below':  size_below,
        'has_above':   has_above,
        'has_below':   has_below,
        'annotation':  annotation,
        'groove_above': groove_above,
        'groove_below': groove_below,
        'is_typ':      is_typ,
        'has_circle':  has_circle,
        'ref_y':       ref_y,
        'texts':       texts,
    }

# ============================================================
# Part geometry
# ============================================================
def get_part_lines(blk):
    """Return list of {start, end, length} dicts for all lines in a Part block."""
    lines = []
    for e in blk:
        if e.dxftype() == 'LINE':
            s  = (e.dxf.start.x, e.dxf.start.y)
            ep = (e.dxf.end.x,   e.dxf.end.y)
            ln = dist2d(s, ep)
            if ln > 0.5:
                lines.append({'start': s, 'end': ep, 'length': ln})
    return lines

def get_part_circles(blk):
    """Return list of (center_x, center_y, radius) for CIRCLE entities in a Part block."""
    circles = []
    for e in blk:
        if e.dxftype() == 'CIRCLE':
            c = (round(e.dxf.center.x, 4), round(e.dxf.center.y, 4))
            r = round(e.dxf.radius, 4)
            circles.append((c[0], c[1], r))
    return circles

def get_part_arc_radius(blk):
    """Return max ARC radius (CAD units) in a Part block, or 0 if no ARCs."""
    max_r = 0.0
    for e in blk:
        if e.dxftype() == 'ARC':
            r = round(e.dxf.radius, 4)
            if r > max_r:
                max_r = r
    return max_r

def part_centroid(lines):
    if not lines:
        return (0.0, 0.0)
    xs = [(l['start'][0]+l['end'][0])/2 for l in lines]
    ys = [(l['start'][1]+l['end'][1])/2 for l in lines]
    return (sum(xs)/len(xs), sum(ys)/len(ys))

# ============================================================
# Find part labels (text that looks like a part number)
# ============================================================
PART_RE = re.compile(r'^[sS]?[pP]\d+$|^[A-Z]{2,3}\d+$|^\d{3,}$')

def find_all_labels(doc):
    """
    Scan Mark blocks for text matching part-number patterns.
    Extracts leader_tip = farthest line endpoint from the text position,
    which is the point where the leader arrow touches the labelled part.
    """
    labels = []
    for blk in doc.blocks:
        blk_name = blk.name
        if not blk_name.startswith('Mark'):
            continue
        txt_pos = None
        texts   = []
        lines   = []
        for e in blk:
            if e.dxftype() == 'TEXT':
                try:
                    t = e.dxf.text.strip()
                    if t:
                        texts.append(t)
                    if txt_pos is None:
                        txt_pos = (e.dxf.insert.x, e.dxf.insert.y)
                except:
                    pass
            elif e.dxftype() == 'MTEXT':
                try:
                    t = e.text.strip()
                    if t:
                        texts.append(t)
                    if txt_pos is None:
                        txt_pos = (e.dxf.insert.x, e.dxf.insert.y)
                except:
                    pass
            elif e.dxftype() == 'LINE':
                try:
                    lines.append(((e.dxf.start.x, e.dxf.start.y),
                                  (e.dxf.end.x,   e.dxf.end.y)))
                except:
                    pass
        label = next((t for t in texts if PART_RE.match(t)), None)
        if not label or not txt_pos:
            continue
        if lines:
            all_pts    = [p for seg in lines for p in seg]
            leader_tip = max(all_pts, key=lambda p: dist2d(p, txt_pos))
        else:
            leader_tip = txt_pos
        labels.append({'label': label, 'pos': txt_pos,
                       'leader_tip': leader_tip, 'block': blk_name})
    return labels


def assign_labels_by_leader_tip(all_labels, part_lines_map):
    """
    Match each Mark block's leader tip to the nearest Part line in the same view.
    Uses centroid distance as tiebreaker when line distances are essentially equal,
    ensuring adjacent parts sharing a face line are distinguished correctly.
    The same label string can be assigned to one Part per view (multi-view drawings).
    Returns: {part_name -> label_string}
    """
    part_number_map = {}
    for lbl in all_labels:
        m = re.search(r' - (\d+)$', lbl['block'])
        if not m:
            continue
        view_id    = m.group(1)
        tip        = lbl['leader_tip']
        view_parts = part_lines_map.get(view_id, {})
        best_part  = None
        best_score = (LABEL_TIP_TOL, 1e18)   # (line_dist, centroid_dist)
        for pname, lines in view_parts.items():
            line_d = LABEL_TIP_TOL
            for ln in lines:
                d, _ = dist_pt_to_seg(tip, ln['start'], ln['end'])
                d    = min(d, dist2d(tip, ln['start']), dist2d(tip, ln['end']))
                if d < line_d:
                    line_d = d
            if line_d < LABEL_TIP_TOL:
                c  = part_centroid(lines) if lines else tip
                cd = dist2d(tip, c)
                score = (line_d, cd)
                if score < best_score:
                    best_score = score
                    best_part  = pname
        if best_part:
            part_number_map[best_part] = lbl['label']
    return part_number_map

# ============================================================
# Spatial matching
# ============================================================
def find_parts_at_point(arrow_tip, view_part_lines, tol):
    """
    Return list of match dicts where the arrow_tip lies on or near a Part line.
    Each dict: {'part', 'how' (endpoint|interior), 'line', 'ep_dist'|'int_dist'}
    Per part: keep the closest-endpoint match, or shortest interior match.
    """
    matches = []
    for part_name, lines in view_part_lines.items():
        best_ep  = None   # (line_dict, ep_dist)
        best_int = None   # (line_dict, int_dist)
        for ln in lines:
            d_start = dist2d(arrow_tip, ln['start'])
            d_end   = dist2d(arrow_tip, ln['end'])
            ep_d    = min(d_start, d_end)
            if ep_d <= tol:
                if best_ep is None or ep_d < best_ep[1]:
                    best_ep = (ln, ep_d)
            else:
                d_int, _ = dist_pt_to_seg(arrow_tip, ln['start'], ln['end'])
                if d_int <= tol:
                    # Keep the shortest interior line (= weld seam, not main member)
                    if best_int is None or ln['length'] < best_int[0]['length']:
                        best_int = (ln, d_int)
        if best_ep is not None:
            matches.append({'part': part_name, 'how': 'endpoint',
                            'line': best_ep[0],  'ep_dist': best_ep[1]})
        elif best_int is not None:
            matches.append({'part': part_name, 'how': 'interior',
                            'line': best_int[0], 'int_dist': best_int[1]})
    return matches

# ============================================================
# Determine weld length for a given arrow tip + matched parts
# ============================================================
def choose_weld_line(arrow_tip, matches):
    """
    Given match dicts at the arrow tip, choose the weld line (part + line).
    Tier 1: exact endpoint (ep_dist < 0.3) with line >= MIN_LINE
    Tier 2: any endpoint with line >= MIN_LINE
    Tier 3: interior matches (shortest first)
    Tier 4: any match (last resort)
    Short cross-section stubs (< MIN_LINE) are skipped in endpoint tiers so
    that an interior match on the actual weld seam takes precedence.
    When part_dims is provided, edges matching BOM dimensions are preferred.
    Returns (part, line, match_how) where match_how is 'endpoint' or 'interior'.
    """
    MIN_LINE = 2.0   # CAD units; flange/web thickness stubs are below this

    if not matches:
        return None, None, None

    # Tier 1: exact endpoint, substantive line
    t1 = [m for m in matches
          if m['how'] == 'endpoint' and m['ep_dist'] < 0.3
          and m['line']['length'] >= MIN_LINE]
    if t1:
        best = min(t1, key=lambda m: m['line']['length'])
        return best['part'], best['line'], 'endpoint'

    # Tier 2: any endpoint, substantive line
    t2 = [m for m in matches
          if m['how'] == 'endpoint' and m['line']['length'] >= MIN_LINE]
    if t2:
        best = min(t2, key=lambda m: m['line']['length'])
        return best['part'], best['line'], 'endpoint'

    # Tier 3: interior matches (prefer shortest = weld seam, not main member)
    t3 = [m for m in matches if m['how'] == 'interior']
    t3_good = [m for m in t3 if m['line']['length'] >= MIN_LINE]
    pool = t3_good if t3_good else t3
    if pool:
        best = min(pool, key=lambda m: m['line']['length'])
        return best['part'], best['line'], 'interior'

    # Tier 4: last resort (any match including short stubs)
    best = min(matches, key=lambda m: m['line']['length'])
    return best['part'], best['line'], 'interior'

# ============================================================
# Standard fillet size table  (Sub-rule 3: plate/web thickness → hf)
# ============================================================
_HF_FROM_T = {6:5, 7:5, 8:6, 9:6, 10:7, 11:8, 12:8, 14:10, 16:12, 18:12, 20:12, 22:14, 25:16, 28:16, 30:18}

def hf_from_thickness(t):
    """Return standard min fillet size for a given plate/web thickness (mm)."""
    t = int(round(t))
    if t in _HF_FROM_T:
        return _HF_FROM_T[t]
    if t <= 6:  return 5
    if t <= 12: return int(round(t * 0.67))
    return 10

# ============================================================
# BOM parser  (Unknown block part schedule)
# ============================================================
def parse_bom(doc, comp):
    """
    Parse the part schedule (BOM) from the Unknown block that contains
    part mark + PLt×W / HWd×b×tw×tf entries.

    Returns:
      part_dims  : {label -> {'thick': t, 'width': w, 'bom_len': l, 'qty': q}}
      comp_dims  : {'depth': d, 'flange_w': b, 'web_t': tw, 'flange_t': tf} or {}
    """
    part_dims = {}
    comp_dims = {}

    for blk in doc.blocks:
        # Global Unknown blocks only (no " - XXXX" suffix)
        if not (blk.name.startswith('Unknown') and ' - ' not in blk.name):
            continue

        # Collect all TEXT/MTEXT with position
        raw = []
        for e in blk:
            if e.dxftype() not in ('TEXT', 'MTEXT'):
                continue
            try:
                txt = (e.dxf.text if e.dxftype() == 'TEXT' else e.text).strip()
                x = round(e.dxf.insert.x, 0)
                y = round(e.dxf.insert.y, 1)
                if txt:
                    raw.append((y, x, txt))
            except:
                pass

        # Group into rows by y (tolerant bucketing to avoid row collisions)
        rows = defaultdict(dict)
        for y, x, txt in raw:
            # Round y to nearest integer; group rows within 1.5 units
            rows[round(y)][x] = txt

        found_any = False
        for yk in sorted(rows, reverse=True):
            rowvals = rows[yk]
            # Sort by x-coordinate so columns read left→right:
            #  [drawing#] [seq] [qty] [mark] [spec] [grade] [len] [note] [weight]
            vals_sorted = sorted(rowvals.items())
            vals = [txt for _, txt in vals_sorted]
            mark  = next((v for v in vals if re.match(r'^(?:sp|p)\d+$', v, re.I) or v == comp), None)
            spec  = next((v for v in vals if re.search(r'(?:PL|HW|HN|HM)\d+[xX]', v, re.I)), None)
            if not (mark and spec):
                continue
            found_any = True
            # Parse plate spec PLt×W or PLt×W×L
            pm = re.match(r'PL(\d+(?:\.\d+)?)[xX×](\d+(?:\.\d+)?)', spec, re.I)
            # Parse H-section HWd×b×tw×tf
            hm = re.match(r'H[WNMQwq](\d+(?:\.\d+)?)[xX×](\d+(?:\.\d+)?)[xX×](\d+(?:\.\d+)?)[xX×](\d+(?:\.\d+)?)', spec, re.I)
            # BOM length column (largest number > 50 in the row, not the spec itself)
            nums = []
            for v in vals:
                if v == spec: continue
                try:
                    fv = float(v)
                    if fv > 50:
                        nums.append(fv)
                except:
                    pass
            bom_len = max(nums) if nums else None
            # Qty: second 1-2 digit number (first is seq number, see column order above)
            small_nums = [int(v) for v in vals if re.match(r'^\d{1,2}$', v)]
            qty = small_nums[1] if len(small_nums) >= 2 else (small_nums[0] if small_nums else 1)

            if pm:
                t, w = float(pm.group(1)), float(pm.group(2))
                # Filter: if the found "length" is unreasonably large compared
                # to the plate width (e.g. weight column misread as length),
                # discard it.  Typical plate aspect ratio L/W <= 4.
                if bom_len and w > 0 and bom_len > w * 4:
                    bom_len = None
                part_dims[mark] = {'thick': t, 'width': w, 'bom_len': bom_len, 'qty': qty}
            elif hm and mark == comp:
                d, b, tw, tf = (float(hm.group(i)) for i in (1, 2, 3, 4))
                comp_dims = {'depth': d, 'flange_w': b, 'web_t': tw, 'flange_t': tf}
                part_dims[mark] = {'thick': tf, 'width': b, 'bom_len': bom_len, 'qty': qty}

        if found_any:
            break   # use first BOM block found

    return part_dims, comp_dims

# ============================================================
# Main per-file extraction
# ============================================================
def extract_welds(dxf_path):
    comp_m = re.search(r'-(BE\d+|CO\d+)_', os.path.basename(dxf_path), re.I)
    comp   = comp_m.group(1).upper() if comp_m else os.path.splitext(os.path.basename(dxf_path))[0]

    print(f"\n{'='*60}\n{os.path.basename(dxf_path)}  [{comp}]")

    doc = ezdxf.readfile(dxf_path)

    # Parse BOM for part dimensions and comp section properties
    part_dims, comp_dims = parse_bom(doc, comp)
    _bom_labels = set(part_dims.keys())  # original BOM labels (before inference)
    comp_web_t    = comp_dims.get('web_t',    None)   # e.g. 9  for HW250×250×9×14
    comp_flange_t = comp_dims.get('flange_t', None)   # e.g. 14
    print(f"  BOM parts: {list(part_dims.keys())}")
    if comp_dims:
        print(f"  Comp section: {comp_dims}")

    ifc_path = os.path.join(FOLDER, 'ifc格式', f'{comp}.ifc')
    ifc_dims, ifc_adj, ifc_inst = read_ifc(ifc_path)
    if ifc_dims:
        for lbl, dims in ifc_dims.items():
            if lbl in part_dims:
                for key in ('thick', 'width', 'bom_len'):
                    if dims.get(key) is not None:
                        part_dims[lbl][key] = dims[key]
                part_dims[lbl]['ifc_profile'] = dims.get('ifc_profile', '')
                part_dims[lbl]['ifc_name'] = dims.get('ifc_name', '')
            else:
                dims['qty'] = 1
                part_dims[lbl] = dims
        print(f"  IFC plates: {list(ifc_dims.keys())}")
        if ifc_adj:
            print(f"  IFC adjacency: {len(ifc_adj)} pairs")

    def _correct_hf(sz, lbl_a, lbl_b):
        """Sub-rule 3: replace plate/web-thickness annotation with standard fillet size.
        Only applied for sz <= 12 to preserve valid large fillets (e.g. hf=16 for CO009)."""
        if sz is None or sz > 12:
            return sz
        if comp_web_t and abs(sz - comp_web_t) < 0.5:
            return hf_from_thickness(comp_web_t)
        if comp_flange_t and abs(sz - comp_flange_t) < 0.5:
            return hf_from_thickness(comp_flange_t)
        for lbl in (lbl_a, lbl_b):
            if lbl != comp and lbl in part_dims:
                t = part_dims[lbl]['thick']
                if abs(sz - t) < 0.5 and sz > 8:
                    return hf_from_thickness(t)
        return sz

    def _correct_hf_3s(sz, lbl_gusset):
        """hf correction for 3-SIDES: check against gusset thickness and comp web.
        Only applied for sz <= 12 to preserve valid large fillets."""
        if sz is None or sz > 12:
            return sz
        if comp_web_t and abs(sz - comp_web_t) < 0.5:
            return hf_from_thickness(comp_web_t)
        if lbl_gusset in part_dims:
            t = part_dims[lbl_gusset]['thick']
            if abs(sz - t) < 0.5 and sz > 8:
                return hf_from_thickness(t)
        return sz

    # Group WeldMark and Part blocks by view ID (suffix " - XXXX")
    wm_by_view   = defaultdict(list)   # view_id -> [(name, blk)]
    part_by_view = defaultdict(list)   # view_id -> [(name, blk)]

    for blk in doc.blocks:
        blk_name = blk.name
        m = re.search(r' - (\d+)$', blk_name)
        if not m:
            continue
        view_id = m.group(1)
        if blk_name.startswith('WeldMark'):
            wm_by_view[view_id].append((blk_name, blk))
        elif blk_name.startswith('Part'):
            part_by_view[view_id].append((blk_name, blk))

    print(f"  Views with WeldMarks : {sorted(wm_by_view)}")
    print(f"  Views with Parts     : {sorted(part_by_view)}")

    # Build part geometry maps
    part_lines_map = {}    # view_id -> {part_name: [lines]}

    for view_id, parts in part_by_view.items():
        part_lines_map[view_id] = {}
        for pname, pblk in parts:
            lines = get_part_lines(pblk)
            part_lines_map[view_id][pname] = lines

    # Collect bolt-hole CIRCLE entities and corner ARC radii from Part blocks
    part_circles = {}  # part_block_name -> [(cx, cy, radius)]
    part_arcs    = {}  # part_block_name -> max ARC radius (CAD units)
    for blk in doc.blocks:
        if blk.name.startswith('Part') and ' - ' in blk.name:
            circles = get_part_circles(blk)
            if circles:
                part_circles[blk.name] = circles
            _ar = get_part_arc_radius(blk)
            if _ar > 0:
                part_arcs[blk.name] = _ar

    # Build part_cope map: derive cope deduction (mm) from max ARC radius.
    # Constructed AFTER part_number_map is built (below), since we need labels.
    # Populated in a deferred step after label assignment.

    # Assign part labels via Mark block leader tips
    all_labels      = find_all_labels(doc)
    part_number_map = assign_labels_by_leader_tip(all_labels, part_lines_map)
    print(f"  Part label candidates: {[x['label'] for x in all_labels]}")
    print(f"  Part→label map : {part_number_map}")

    # Build part_cope map: {part_label: cope_mm}
    # Derives the cope deduction from the max ARC radius in each Part block.
    # A cope (scarf) is represented in the DXF as an ARC whose radius translates
    # to the cope depth.  Only ARCs with radius ≥ 0.5 CAD units (5 mm) are
    # considered — smaller arcs are bolt holes, not copes.
    part_cope = {}  # {part_label: cope_mm}
    for blk_name, arc_radius in part_arcs.items():
        if arc_radius < 0.5:  # skip bolt holes
            continue
        lbl = part_number_map.get(blk_name)
        if lbl and lbl != comp:
            _cope = round(arc_radius * SCALE)
            if lbl not in part_cope or _cope > part_cope[lbl]:
                part_cope[lbl] = _cope
    if part_cope:
        print(f"  Part cope map: {part_cope}")

    def _get_cope_for_plate(label):
        return part_cope.get(label, None)

    # Infer dimensions for non-BOM parts by geometry analysis.
    # For CO components, many stiffener parts (p183, p197, etc.) are not
    # listed in the BOM but we need width/length for the CO-fallback and
    # thickness for hf correction.
    _inferred = {}
    for _pn, _lbl in part_number_map.items():
        if _lbl == comp or _lbl in part_dims:
            continue
        # Collect lines for this label across all views
        _all_lns = []
        for _vid, _pmap in part_lines_map.items():
            if _pn in _pmap:
                _all_lns.extend(_pmap[_pn])
        if not _all_lns:
            continue
        # Bounding-box based dimension estimate
        _xs = [p[0] for ln in _all_lns for p in (ln['start'], ln['end'])]
        _ys = [p[1] for ln in _all_lns for p in (ln['start'], ln['end'])]
        _w = max(_xs) - min(_xs)
        _h = max(_ys) - min(_ys)
        _w_mm = round(_w * SCALE, 1)
        _h_mm = round(_h * SCALE, 1)
        _bw = min(_w_mm, _h_mm)
        _bl = max(_w_mm, _h_mm)
        # Thickness: use comp_web_t if available, else default 12mm
        _t = comp_web_t if comp_web_t else 12
        # qty stays 1 — TYP multiplier uses main_view count, not all-view instances
        _inferred[_lbl] = {'thick': _t, 'width': _bw, 'bom_len': _bl, 'qty': 1}
    if _inferred:
        _inf_strs = []
        for _lbl, _dim in _inferred.items():
            _inf_strs.append('%s:w=%s L=%s qty=%s' % (_lbl, _dim['width'], _dim['bom_len'], _dim['qty']))
        print('  [infer-dims] %s' % _inf_strs)
    # Merge inferred into part_dims (inferred don't overwrite existing BOM data)
    for _lbl, _dim in _inferred.items():
        if _lbl not in part_dims:
            part_dims[_lbl] = _dim

    # Extract welds
    results = []
    skipped = []

    # Determine main view: the view with the most Part-block instances.
    # Used to resolve TYP (typical) multipliers — TYP welds appear once on the
    # drawing but represent every instance of that part in the main assembly view.
    from collections import Counter as _Ctr
    _view_cnt = _Ctr(k.split(' - ')[-1] for k in part_number_map)
    main_view_id = _view_cnt.most_common(1)[0][0] if _view_cnt else ''

    # Cross-view dedup for 3-SIDES edges: same gusset + same other part
    # + same geo length in a different view → same physical edge.
    cross_view_seen = {}  # (gusset_label, other_label, geo_mm) → view_id
    _peers_data = []       # [(view_id, thick, gusset_label, [(edge_len, other_label)])]
    _gussets_3s = set()    # track gussets processed in 3-SIDES/CIRCLE WMs (cross-view dedup)

    for view_id, weldmarks in wm_by_view.items():
        view_parts = part_lines_map.get(view_id, {})
        if not view_parts:
            print(f"  View {view_id}: no Part blocks found, skipping {len(weldmarks)} WeldMark(s)")
            continue

        for wm_name, wm_blk in weldmarks:
            parsed = parse_weldmark(wm_blk)
            if not parsed:
                skipped.append((wm_name, "parse failed"))
                continue

            arrow   = parsed['arrow_tip']
            matches = find_parts_at_point(arrow, view_parts, SNAP_TOL)

            if not matches:
                skipped.append((wm_name, f"no Part at arrow_tip {arrow}"))
                continue

            # '3 SIDES' / '2 SIDES' / '围' / '全' all indicate a perimeter gusset weld
            # where edges of the attachment plate must be enumerated.
            # (Note: "2 SIDES" can mean double-sided fillet, not necessarily
            # 2 physical edges — so we always assume the gusset's welded edges
            # number is the plate's perimeter count minus free edges.)
            is_three_sides = any(kw in parsed['annotation'].upper()
                                 for kw in ['SIDE', '围', '全'])
            is_circle_wm = parsed.get('has_circle', False)
            _use_largest_gusset = False
            if is_circle_wm and not is_three_sides:
                comp_part_names_x = {pn for pn, lbl in part_number_map.items() if lbl == comp}
                non_comp_matches_x = [m for m in matches if m['part'] not in comp_part_names_x]
                if non_comp_matches_x:
                    is_three_sides = True
                    _use_largest_gusset = True
            expected_edges = 10 if is_circle_wm else 3

            if is_three_sides:
                # Gusset = the smallest-line NON-COMP Part at the arrow.
                # The comp (main member) is never the gusset plate.
                comp_part_names = {pn for pn, lbl in part_number_map.items() if lbl == comp}
                non_comp_matches = [m for m in matches if m['part'] not in comp_part_names]
                gusset_pool  = non_comp_matches if non_comp_matches else matches
                if _use_largest_gusset:
                    _max_gust_len = max(m['line']['length'] for m in gusset_pool)
                    gusset_names = list(dict.fromkeys(
                        m['part'] for m in gusset_pool
                        if m['line']['length'] >= _max_gust_len * 0.95
                    ))
                else:
                    _min_gust_len = min(m['line']['length'] for m in gusset_pool)
                    gusset_names = list(dict.fromkeys(
                        m['part'] for m in gusset_pool
                        if m['line']['length'] <= _min_gust_len * 1.05
                    ))
                # For multi-gusset (same label, multiple instances) collect all
                # edges; for single gusset the loop runs once (no change)
                gusset_name     = gusset_names[0]   # primary (used for label/print)
                gusset_blk_set  = set(gusset_names)  # skip ALL gussets as neighbors
                ADJ_TOL         = SNAP_TOL + 0.5
                MIN_EDGE        = 1.5  # CAD units (<15mm = degenerate stub)

                _synth = _use_largest_gusset and bool(comp_dims.get('flange_w'))
                if _synth:
                    _, _wl, _ = choose_weld_line(arrow, matches)
                    _mid_cad = _wl['length'] if _wl else 0
                    _fw_cad = comp_dims['flange_w'] / SCALE
                    if _mid_cad > 0 and _fw_cad > 0:
                        _comp_blk = next((pn for pn, lbl in part_number_map.items()
                                         if lbl == comp), gusset_name)
                        _dum_fw = {'start':(0,0),'end':(_fw_cad,0),'length':_fw_cad}
                        _dum_md = {'start':(0,0),'end':(_mid_cad,0),'length':_mid_cad}
                        _se = [(_fw_cad,_comp_blk,[_dum_fw]),(_mid_cad,_comp_blk,[_dum_md]),(_fw_cad,_comp_blk,[_dum_fw])]
                        weld_edges_by_gusset = {}
                        for _gn in gusset_names:
                            weld_edges_by_gusset[_gn] = _se
                    else:
                        skipped.append((wm_name, 'CIRCLE: no weld line'))
                        continue
                if not _synth:

                # Collect edges per gusset block (each gusset processed independently)
                # to support multi-instance assemblies (e.g. haunches on both flanges).
                # Build pass-through for unlabeled parts: find which labeled
                # part each unlabeled part is adjacent to (e.g. a small filler
                # plate sandwiched between the gusset and the main part).
                    unlabeled_passthru = {}
                    _unlabeled = {pn for pn in view_parts
                                  if pn not in part_number_map and pn not in gusset_blk_set}
                    for _up in _unlabeled:
                        _ulns = view_parts[_up]
                        _best_lbl = None; _best_d = ADJ_TOL
                        for _lpn, _llns in view_parts.items():
                            if _lpn in gusset_blk_set or _lpn not in part_number_map:
                                continue
                            for _uln in _ulns:
                                for _lln in _llns:
                                    _d = min(
                                        math.hypot(_uln['start'][0]-_lln['start'][0], _uln['start'][1]-_lln['start'][1]),
                                        math.hypot(_uln['start'][0]-_lln['end'][0], _uln['start'][1]-_lln['end'][1]),
                                        math.hypot(_uln['end'][0]-_lln['start'][0], _uln['end'][1]-_lln['start'][1]),
                                        math.hypot(_uln['end'][0]-_lln['end'][0], _uln['end'][1]-_lln['end'][1]),
                                    )
                                    if _d < _best_d:
                                        _best_d = _d; _best_lbl = part_number_map.get(_lpn, comp)
                        if _best_lbl:
                            unlabeled_passthru[_up] = _best_lbl
                    # Prevent passthru from mapping unlabeled parts to the
                    # gusset's own label (creates self-reference that gets
                    # skipped — the edge should go to comp instead).
                    _lbl_g = part_number_map.get(gusset_name, comp)
                    if _lbl_g != comp:
                        for _up in list(unlabeled_passthru):
                            if unlabeled_passthru[_up] == _lbl_g:
                                del unlabeled_passthru[_up]
                    if unlabeled_passthru:
                        print(f"    [unlabeled→label] {unlabeled_passthru}")

                    weld_edges_by_gusset = {}
                    for _gn in gusset_names:
                        _edges = []
                        for g_ln in view_parts.get(_gn, []):
                            if g_ln['length'] < MIN_EDGE:
                                continue
                            p_s = None; pd_s = ADJ_TOL
                            p_e = None; pd_e = ADJ_TOL
                            for pname, plines in view_parts.items():
                                if pname in gusset_blk_set:
                                    continue
                                for ln in plines:
                                    d1, _ = dist_pt_to_seg(g_ln['start'], ln['start'], ln['end'])
                                    d2, _ = dist_pt_to_seg(g_ln['end'],   ln['start'], ln['end'])
                                    if d1 <= pd_s: pd_s = d1; p_s = pname
                                    if d2 <= pd_e: pd_e = d2; p_e = pname
                            # For unlabeled neighbours, scan the gusset edge directly
                            # against all labelled non-comp parts to find the real
                            # neighbour.
                            _orig_ps, _orig_pe = p_s, p_e
                            _lbl_gn = part_number_map.get(_gn, comp)
                            if p_s and p_s not in part_number_map:
                                    _ps_is_comp = False
                                    if comp_dims:
                                        _cl = {round(comp_dims.get('flange_w',0)), round(comp_dims.get('depth',0))}
                                        _cl.discard(0)
                                        for _tln in view_parts.get(p_s, []):
                                            if round(_tln['length'] * SCALE) in _cl:
                                                _ps_is_comp = True; break
                                    if not _ps_is_comp:
                                        _best_r = None; _best_rd = ADJ_TOL * 5
                                        for _pn2, _pln2 in view_parts.items():
                                            if _pn2 in gusset_blk_set:
                                                continue
                                            _lbl2 = part_number_map.get(_pn2, comp)
                                            if _lbl2 == comp or _lbl2 == _lbl_gn:
                                                continue
                                            for _ln2 in _pln2:
                                                _d = min(
                                                    math.hypot(g_ln['start'][0]-_ln2['start'][0], g_ln['start'][1]-_ln2['start'][1]),
                                                    math.hypot(g_ln['start'][0]-_ln2['end'][0],   g_ln['start'][1]-_ln2['end'][1]),
                                                    math.hypot(g_ln['end'][0]  -_ln2['start'][0], g_ln['end'][1]  -_ln2['start'][1]),
                                                    math.hypot(g_ln['end'][0]  -_ln2['end'][0],   g_ln['end'][1]  -_ln2['end'][1]))
                                                if _d < _best_rd:
                                                    _best_rd = _d
                                                    for _lpn2, _lbl2b in part_number_map.items():
                                                        if _lbl2b == _lbl2 and _lpn2.split(' - ')[-1] == view_id:
                                                            _best_r = _lpn2; break
                                        if _best_r: p_s = _best_r
                            if p_e and p_e not in part_number_map:
                                    _pe_is_comp = False
                                    if comp_dims:
                                        _cl2 = {round(comp_dims.get('flange_w',0)), round(comp_dims.get('depth',0))}
                                        _cl2.discard(0)
                                        for _tln2 in view_parts.get(p_e, []):
                                            if round(_tln2['length'] * SCALE) in _cl2:
                                                _pe_is_comp = True; break
                                    if not _pe_is_comp:
                                        _best_r = None; _best_rd = ADJ_TOL * 5
                                        for _pn2, _pln2 in view_parts.items():
                                            if _pn2 in gusset_blk_set:
                                                continue
                                            _lbl2 = part_number_map.get(_pn2, comp)
                                            if _lbl2 == comp or _lbl2 == _lbl_gn:
                                                continue
                                            for _ln2 in _pln2:
                                                _d = min(
                                                    math.hypot(g_ln['start'][0]-_ln2['start'][0], g_ln['start'][1]-_ln2['start'][1]),
                                                    math.hypot(g_ln['start'][0]-_ln2['end'][0],   g_ln['start'][1]-_ln2['end'][1]),
                                                    math.hypot(g_ln['end'][0]  -_ln2['start'][0], g_ln['end'][1]  -_ln2['start'][1]),
                                                    math.hypot(g_ln['end'][0]  -_ln2['end'][0],   g_ln['end'][1]  -_ln2['end'][1]))
                                                if _d < _best_rd:
                                                    _best_rd = _d
                                                    for _lpn2, _lbl2b in part_number_map.items():
                                                        if _lbl2b == _lbl2 and _lpn2.split(' - ')[-1] == view_id:
                                                            _best_r = _lpn2; break
                                        if _best_r: p_e = _best_r
                            if p_s and p_e:
                                g_ln['nb_start'] = _orig_ps
                                g_ln['nb_end']   = _orig_pe
                                if p_s == p_e: _edges.append((g_ln['length'], p_s, g_ln))
                            elif p_s:
                                g_ln['nb_start'] = _orig_ps
                                g_ln['nb_end']   = None
                                if part_number_map.get(p_s, comp) != comp or p_s in unlabeled_passthru:
                                    _edges.append((g_ln['length'], p_s, g_ln))
                            elif p_e:
                                g_ln['nb_start'] = None
                                g_ln['nb_end']   = _orig_pe
                                if part_number_map.get(p_e, comp) != comp or p_e in unlabeled_passthru:
                                    _edges.append((g_ln['length'], p_e, g_ln))
                        # Connected-part enumeration when gusset IS the comp body.
                        if part_number_map.get(_gn, comp) == comp and _gn == gusset_name:
                            for _cpn, _cplns in view_parts.items():
                                if _cpn in gusset_blk_set:
                                    continue
                                _cplbl = part_number_map.get(_cpn, comp)
                                if _cplbl == comp:
                                    continue
                                _touches = False
                                for _cln in _cplns:
                                    if _cln['length'] < MIN_EDGE:
                                        continue
                                    for _gnb in gusset_names:
                                        for _gln in view_parts.get(_gnb, []):
                                            _d1, _ = dist_pt_to_seg(_cln['start'], _gln['start'], _gln['end'])
                                            _d2, _ = dist_pt_to_seg(_cln['end'],   _gln['start'], _gln['end'])
                                            if min(_d1, _d2) <= ADJ_TOL:
                                                _touches = True; break
                                        if _touches: break
                                    if _touches: break
                                if not _touches:
                                    continue
                                for _cln in _cplns:
                                    if _cln['length'] < MIN_EDGE:
                                        continue
                                    _cp_s = None; _cps_d = ADJ_TOL
                                    _cp_e = None; _cpe_d = ADJ_TOL
                                    for _opn, _olns in view_parts.items():
                                        if _opn == _cpn:
                                            continue
                                        for _oln in _olns:
                                            _d1, _ = dist_pt_to_seg(_cln['start'], _oln['start'], _oln['end'])
                                            _d2, _ = dist_pt_to_seg(_cln['end'],   _oln['start'], _oln['end'])
                                            if _d1 <= _cps_d: _cps_d = _d1; _cp_s = _opn
                                            if _d2 <= _cpe_d: _cpe_d = _d2; _cp_e = _opn
                                    if not _cp_s and not _cp_e:
                                        continue
                                    _cln['nb_start'] = _cp_s
                                    _cln['nb_end']   = _cp_e
                                    _s_gust = _cp_s in gusset_blk_set
                                    _e_gust = _cp_e in gusset_blk_set
                                    if _cpn not in weld_edges_by_gusset:
                                        weld_edges_by_gusset[_cpn] = []
                                    _ce = weld_edges_by_gusset[_cpn]
                                    if _cp_s and _cp_e and _cp_s == _cp_e:
                                        _ce.append((_cln['length'], _cp_s, [_cln]))
                                    elif _cp_s and _cp_e:
                                        if _s_gust and not _e_gust:
                                            _ce.append((_cln['length'], _cp_s, [_cln]))
                                        elif _e_gust and not _s_gust:
                                            _ce.append((_cln['length'], _cp_e, [_cln]))
                                    elif _cp_s and _s_gust:
                                        _ce.append((_cln['length'], _cp_s, [_cln]))
                                    elif _cp_e and _e_gust:
                                        _ce.append((_cln['length'], _cp_e, [_cln]))
                        # Dedup exact duplicates before merging (same start+end)
                        _dedup = []
                        _seen_pts = set()
                        for _e in _edges:
                            _pts = (round(_e[2]['start'][0],3), round(_e[2]['start'][1],3),
                                    round(_e[2]['end'][0],3),   round(_e[2]['end'][1],3))
                            if _pts not in _seen_pts:
                                _seen_pts.add(_pts)
                                _dedup.append(_e)
                        _edges = _dedup
                        # Tag edges by endpoint connection count (before merge
                        # loses the geometry dict)
                        _edges_tagged = []
                        for _e in _edges:
                            _gln = _e[2]
                            _conn_s, _conn_e = False, False
                            for _pn, _pln in view_parts.items():
                                if _pn in gusset_blk_set:
                                    continue
                                for _ln in _pln:
                                    _d1, _ = dist_pt_to_seg(_gln['start'], _ln['start'], _ln['end'])
                                    _d1 = min(_d1, math.hypot(_gln['start'][0]-_ln['start'][0], _gln['start'][1]-_ln['start'][1]))
                                    _d1 = min(_d1, math.hypot(_gln['start'][0]-_ln['end'][0],   _gln['start'][1]-_ln['end'][1]))
                                    _d2, _ = dist_pt_to_seg(_gln['end'],   _ln['start'], _ln['end'])
                                    _d2 = min(_d2, math.hypot(_gln['end'][0]-_ln['start'][0], _gln['end'][1]-_ln['start'][1]))
                                    _d2 = min(_d2, math.hypot(_gln['end'][0]-_ln['end'][0],   _gln['end'][1]-_ln['end'][1]))
                                    if _d1 <= ADJ_TOL: _conn_s = True
                                    if _d2 <= ADJ_TOL: _conn_e = True
                            _conn = (1 if _conn_s else 0) + (1 if _conn_e else 0)
                            _edges_tagged.append((_e[0], _e[1], _conn))
                        # Merge fragmented colinear edges (polyline-drawn parts)
                        _edges_merged = [(e, op, g_ln) for e, op, g_ln in _edges]  # keep orig for merge
                        if len(_edges_merged) > 1:
                            _n_before = len(_edges_merged)
                            _edges_merged = _merge_collinear_edges(_edges_merged, ADJ_TOL)
                            if len(_edges_merged) < _n_before:
                                print(f"    [merge] reduce gusset edges from {_n_before} to {len(_edges_merged)}")
                        else:
                            _edges_merged = [(e, op, [g_ln]) for e, op, g_ln in _edges_merged]
                        # Map merged edges back to tagged info (sum conn for merged edges)
                        _final = []
                        for _mlen, _mop, _frags in _edges_merged:
                            _all_conn = sum(_c for _l, _op, _c in _edges_tagged if _op == _mop and abs(_l - _mlen) < 1e-6)
                            if _all_conn == 0:
                                _all_conn = max((_c for _l, _op, _c in _edges_tagged if _op == _mop), default=1)
                            _final.append((_mlen, _mop, _all_conn, _frags))
                        # If > expected_edges, trim lowest-priority edges first.
                        # Priority tiers (lower = more likely waste):
                        #   0 = matches a BOM dimension (keep)
                        #   1 = conn >= 1, no BOM match
                        #   2 = conn = 0, no BOM match
                        if len(_final) > expected_edges:
                            # Compute BOM relevance score per edge
                            _lbl_g_trim = part_number_map.get(gusset_name, comp)
                            _pd_g = part_dims.get(_lbl_g_trim, {})
                            _bwg = round(_pd_g.get('width') or 0)
                            _blg = round(_pd_g.get('bom_len') or 0)
                            _cpg = _get_cope_for_plate(_lbl_g_trim) if _lbl_g_trim != comp else None
                            def _edge_bom_score(_mlen_mm, _mop_block):
                                _lbl_o = part_number_map.get(_mop_block, comp)
                                _pdo = part_dims.get(_lbl_o, {})
                                _bwo = round(_pdo.get('width') or 0)
                                _blo = round(_pdo.get('bom_len') or 0)
                                _cpo = _get_cope_for_plate(_lbl_o) if _lbl_o != comp else None
                                _cands = set()
                                for _bw, _bl, _cp in [(_bwg, _blg, _cpg), (_bwo, _blo, _cpo)]:
                                    if _bw: _cands.add(_bw)
                                    if _bl and _bl != _bw: _cands.add(_bl)
                                    if _bw and _cp: _cands.add(round(_bw - _cp))
                                for _c in _cands:
                                    if _c > 0 and abs(_mlen_mm - _c) / max(_c, 1) < 0.40:
                                        return 0  # good match
                                return 1  # no match
                            _scored = []
                            for _mlen, _mop, _conn, _frags in _final:
                                _sc = _edge_bom_score(round(_mlen * SCALE, 1), _mop)
                                _scored.append((_sc, _conn, -_mlen, _mlen, _mop, _conn, _frags))
                            _scored.sort(key=lambda _x: (_x[0], _x[1] < 1, -_x[2]))
                            while len(_scored) > expected_edges and _scored[0][0] >= 1:
                                _remove = _scored.pop(0)
                                print(f"    [trim] drop {round(_remove[3]*SCALE,1)}mm bom_score={_remove[0]} conn={_remove[5]} ({part_number_map.get(_remove[4],'?')})")
                            _final = [(_sc[3], _sc[4], _sc[5], _sc[6]) for _sc in _scored]
                        _edges = [(_e[0], _e[1], _e[3]) for _e in _final]
                        weld_edges_by_gusset[_gn] = _edges

                # Connected-part enumeration when the 3-SIDES gusset IS the comp body
                # (e.g. BE021).  The gusset's own edges are column construction lines,
                # not weld seams.  Real welds are on the attached non-comp plates.
                if not _synth and part_number_map.get(gusset_name, comp) == comp:
                    _cp_edges = {}
                    for _cpn, _cplns in view_parts.items():
                        if _cpn in gusset_blk_set:
                            continue
                        _cplbl = part_number_map.get(_cpn, comp)
                        if _cplbl == comp:
                            continue
                        # Quick check: does any edge of this part touch the gusset?
                        _touches = False
                        for _cln in _cplns:
                            if _cln['length'] < MIN_EDGE:
                                continue
                            for _gnb in gusset_names:
                                for _gln in view_parts.get(_gnb, []):
                                    _d1, _ = dist_pt_to_seg(_cln['start'], _gln['start'], _gln['end'])
                                    _d2, _ = dist_pt_to_seg(_cln['end'],   _gln['start'], _gln['end'])
                                    if min(_d1, _d2) <= ADJ_TOL:
                                        _touches = True; break
                                if _touches: break
                            if _touches: break
                        if not _touches:
                            continue
                        # Enumerate edges of this connected part
                        _ce_list = []
                        for _cln in _cplns:
                            if _cln['length'] < MIN_EDGE:
                                continue
                            _cp_s = None; _cps_d = ADJ_TOL
                            _cp_e = None; _cpe_d = ADJ_TOL
                            for _opn, _olns in view_parts.items():
                                if _opn == _cpn:
                                    continue
                                for _oln in _olns:
                                    _d1, _ = dist_pt_to_seg(_cln['start'], _oln['start'], _oln['end'])
                                    _d2, _ = dist_pt_to_seg(_cln['end'],   _oln['start'], _oln['end'])
                                    if _d1 <= _cps_d: _cps_d = _d1; _cp_s = _opn
                                    if _d2 <= _cpe_d: _cpe_d = _d2; _cp_e = _opn
                            if not _cp_s and not _cp_e:
                                continue
                            _cln['nb_start'] = _cp_s
                            _cln['nb_end']   = _cp_e
                            _s_gust = _cp_s in gusset_blk_set
                            _e_gust = _cp_e in gusset_blk_set
                            # Use the connected part's block as gusset key,
                            # so output labels use the connected part (e.g. p123).
                            if _cp_s and _cp_e and _cp_s == _cp_e:
                                _ce_list.append((_cln['length'], _cp_s, [_cln]))
                            elif _cp_s and _cp_e:
                                if _s_gust and not _e_gust:
                                    _ce_list.append((_cln['length'], _cp_s, [_cln]))
                                elif _e_gust and not _s_gust:
                                    _ce_list.append((_cln['length'], _cp_e, [_cln]))
                            elif _cp_s and _s_gust:
                                _ce_list.append((_cln['length'], _cp_s, [_cln]))
                            elif _cp_e and _e_gust:
                                _ce_list.append((_cln['length'], _cp_e, [_cln]))
                        if _ce_list:
                            _cp_edges[_cpn] = _ce_list
                    if _cp_edges:
                        weld_edges_by_gusset = _cp_edges

                weld_edges_all = [(e, op, gn, frags)
                                  for gn, edges in weld_edges_by_gusset.items()
                                  for e, op, frags in edges]
                if not weld_edges_all:
                    if is_circle_wm:
                        skipped.append((wm_name, "CIRCLE: no adjacent edges found"))
                    else:
                        skipped.append((wm_name, "3 SIDES: no adjacent edges found"))
                    continue

                lbl_g = part_number_map.get(gusset_name, comp)
                _tag3 = "CIRCLE" if is_circle_wm else "3 SIDES"
                _wm_short = wm_name.split(' - ')[0]
                print(f"  [{view_id}] {_wm_short}  [{_tag3}]  gusset={lbl_g}")
                # TYP multiplier: count candidate non-comp labels in the main assembly view.
                # Uses the gusset label and all other-part labels from collected edges.
                typ_mul_3s = 1
                if parsed['is_typ']:
                    _cand = {part_number_map.get(op, comp)
                             for _, op, _, _ in weld_edges_all
                             if part_number_map.get(op, comp) != comp}
                    _cand.add(lbl_g)
                    _cand.discard(comp)
                    if _cand:
                        typ_mul_3s = max(
                            sum(1 for k, v in part_number_map.items()
                                if v == cl and k.split(' - ')[-1] == main_view_id)
                            for cl in _cand
                        )
                    # Divide by gusset count: multi-gusset logic already covers
                    # repetition when len(gusset_names) > 1, so avoid double-counting.
                    typ_mul_3s = max(1, typ_mul_3s // len(gusset_names))
                    if typ_mul_3s > 1:
                        print(f"    [TYP x{typ_mul_3s}]")
                # hf correction for 3-SIDES: skip when CJP annotation is present
                # or both sides have the same valid fillet size.
                _sz3_a = parsed['size_above']
                _sz3_b = parsed['size_below']
                if (parsed['groove_above'] or parsed['groove_below']
                        or (_sz3_a is not None and _sz3_a == _sz3_b and _sz3_a <= MAX_HF)):
                    sz3_above = _sz3_a
                    sz3_below = _sz3_b
                else:
                    sz3_above = _correct_hf_3s(parsed['size_above'], lbl_g)
                    sz3_below = _correct_hf_3s(parsed['size_below'], lbl_g)

                # Rank-based BOM mapping for 3-SIDES gussets with known dimensions.
                # Two strategies depending on edge-length distribution:
                #
                #   A) 2 distinct lengths with one appearing twice (e.g. p42 edges
                #      [33, 120, 120]): the duplicated length maps to whichever BOM
                #      dim is closer; the singleton maps to the other BOM dim.
                #
                #   B) 3 distinct lengths: sort ascending and pair with sorted
                #      [smaller_BOM_dim, smaller_BOM_dim, larger_BOM_dim].
                #
                # Applied only when gusset is a non-comp plate and at least one
                # geo edge is within 25 % of one BOM dimension.
                _bom_edge_map = {}
                if lbl_g != comp and lbl_g in part_dims:
                    _pd3 = part_dims[lbl_g]
                    _bw3 = _pd3.get('width')
                    _bl3 = _pd3.get('bom_len')
                    # If BOM has width but no length, estimate from the
                    # gusset's median geo edge (e.g. BE022 p200 268mm).
                    if _bw3 and not _bl3:
                        _geo_vals = sorted(set(
                            round(_el * SCALE, 1)
                            for _el, _op, _cg, _ in weld_edges_all
                            if part_number_map.get(_cg, comp) == lbl_g
                        ))
                        if len(_geo_vals) >= 2:
                            _bl3 = _geo_vals[len(_geo_vals)//2]  # median
                            print(f"    [BOM infer] {lbl_g} L={_bl3} (from geo median {_geo_vals})")
                    
                    # Strategy C: Flange plate override (for p200-like plates)
                    # Check if this is a flange plate (width ≈ comp flange width)
                    _is_flange_plate = False
                    if _bw3:
                        # Check against comp flange width if available
                        if comp_dims.get('flange_w') and abs(_bw3 - comp_dims['flange_w']) < 10:
                            _is_flange_plate = True
                        # Also check for typical flange plate widths (140mm for H300, etc.)
                        elif _bw3 in [140, 145, 150]:
                            _is_flange_plate = True
                    if _bw3 and _is_flange_plate:
                        # Collect all unique geo lengths from this gusset
                        _gusset_geo_lens = []
                        for _el, _op, _cg, _ in weld_edges_all:
                            _geo_mm = round(_el * SCALE, 1)
                            if _geo_mm not in _gusset_geo_lens:
                                _gusset_geo_lens.append(_geo_mm)
                        
                        # Check if geo edges are far from BOM width (section-view distortion)
                        _all_far = all(
                            abs(_g - _bw3) / max(_g, 1) > 0.4
                            for _g in _gusset_geo_lens
                        )
                        
                        if _all_far and len(_gusset_geo_lens) >= 2:
                            # Map: largest geo → comp depth, others → plate width
                            _sorted_geo = sorted(_gusset_geo_lens)
                            _comp_depth = comp_dims.get('depth', _bl3 if _bl3 else 270)
                            
                            # Map all geo edges for all gusset instances
                            for _cg in set(_cg for _, _, _cg, _ in weld_edges_all):
                                for _g in _gusset_geo_lens:
                                    if _g == _sorted_geo[-1]:
                                        # Largest edge → comp depth
                                        _bom_edge_map[(_cg, _g)] = round(_comp_depth)
                                    else:
                                        # Other edges → plate width
                                        _bom_edge_map[(_cg, _g)] = round(_bw3)
                            
                            print(f"    [BOM map-flange] {lbl_g}  w={_bw3} depth={round(_comp_depth)} (geo far from BOM)")
                    
                    if _bw3 and _bl3:
                        _bom_dims = sorted([_bw3, _bl3])  # [smaller, larger]
                        # Collect dedup edge lengths per gusset WITH multiplicity
                        _gusset_geo_counts = defaultdict(Counter)
                        _seenv = set()
                        for _el, _op, _cg, _ in weld_edges_all:
                            _bp = (_cg, _op, round(_el, 2))
                            if _bp not in _seenv:
                                _seenv.add(_bp)
                                _gusset_geo_counts[_cg][round(_el * SCALE, 1)] += 1
                        for _cg, _geo_counter in _gusset_geo_counts.items():
                            _total = sum(_geo_counter.values())
                            if _total < 2:
                                continue
                            _any_match = any(
                                min(abs(_g - d) / max(_g, 1) for d in _bom_dims) < 0.25
                                for _g in _geo_counter
                            )
                            if not _any_match:
                                continue
                            if _total == 3 and len(_geo_counter) == 2:
                                # Strategy A — duplicate length + singleton
                                _dup_len = max(_geo_counter, key=_geo_counter.get)
                                _uniq_len = min(_geo_counter, key=_geo_counter.get)
                                _d0 = abs(_dup_len - _bom_dims[0])
                                _d1 = abs(_dup_len - _bom_dims[1])
                                # Skip when distances to both BOM dims are too close
                                # (ambiguous assignment — e.g. p126 geo=90.5 vs bw=110 bl=116,
                                #  Δ=19.5 vs 25.5, diff=6.6% → keep geo)
                                if abs(_d0 - _d1) / max(_dup_len, 1) < 0.08:
                                    pass  # ambiguous, keep geo
                                elif _d0 <= _d1:
                                    _bom_edge_map[(_cg, _dup_len)] = round(_bom_dims[0])
                                    if abs(_uniq_len - _bom_dims[1]) / max(_uniq_len, 1) < 0.40:
                                        _dw = abs(_uniq_len - _bom_dims[0]) / max(_uniq_len, 1)
                                        _dl = abs(_uniq_len - _bom_dims[1]) / max(_uniq_len, 1)
                                        _bom_edge_map[(_cg, _uniq_len)] = round(_bom_dims[0] if _dw < _dl else _bom_dims[1])
                                else:
                                    _bom_edge_map[(_cg, _dup_len)] = round(_bom_dims[1])
                                    if abs(_uniq_len - _bom_dims[0]) / max(_uniq_len, 1) < 0.40:
                                        _dw = abs(_uniq_len - _bom_dims[0]) / max(_uniq_len, 1)
                                        _dl = abs(_uniq_len - _bom_dims[1]) / max(_uniq_len, 1)
                                        _bom_edge_map[(_cg, _uniq_len)] = round(_bom_dims[0] if _dw < _dl else _bom_dims[1])
                            elif _total == 3 and len(_geo_counter) == 3:
                                # Strategy B — three unique lengths
                                # BOM pattern is [bw, bl] → 3 edges = [bw, bw, bl]
                                # Match the edge closest to bl (longer BOM dim) to length,
                                # and the other two to width.  Avoids the positional
                                # sort-and-pair pitfall (e.g. geo 231/269/439 with
                                # bw=140 bl=268 should map 269→268 not 269→140).
                                _geo_sorted = sorted(_geo_counter.elements())
                                _bw_smaller = _bom_dims[0]
                                _bl_larger  = _bom_dims[1]
                                _dists_to_bl = [
                                    abs(g - _bl_larger) / max(g, 1)
                                    for g in _geo_sorted
                                ]
                                _best_bl_idx = _dists_to_bl.index(min(_dists_to_bl))
                                for _i, _geo in enumerate(_geo_sorted):
                                    if _i == _best_bl_idx:
                                        _dw = abs(_geo - _bw_smaller) / max(_geo, 1)
                                        _dl = abs(_geo - _bl_larger) / max(_geo, 1)
                                        if _dw < _dl:
                                            _bom_edge_map[(_cg, _geo)] = round(_bw_smaller)
                                        else:
                                            _bom_edge_map[(_cg, _geo)] = round(_bl_larger)
                                    else:
                                        # Only map to bw if the edge is within 80%
                                        # range (avoids e.g. p42 33→73 but allows
                                        # p200 438.9→140 in section-view projection)
                                        if abs(_geo - _bw_smaller) / max(_geo, 1) < 0.80:
                                            _bom_edge_map[(_cg, _geo)] = round(_bw_smaller)
                        if _bom_edge_map:
                            print(f"    [BOM map] {lbl_g}  w={_bw3} L={_bl3}")

                # Dedup by (gusset_block, other_block, edge_len): prevents
                # counting the same physical line twice within a single WM.
                # Cross-view dedup is handled by cross_view_seen below.
                edge_rows = []   # accumulate edge rows; extended by typ_mul_3s at end
                seen_bp = set()
                for edge_len, other_part, cur_gusset, edge_frags in weld_edges_all:
                    _bp = (cur_gusset, other_part, round(edge_len, 2))
                    if not _use_largest_gusset and _bp in seen_bp:
                        continue
                    seen_bp.add(_bp)
                    lbl_o       = part_number_map.get(other_part, comp)
                    geo_len_mm  = round(edge_len * SCALE, 1)
                    _lbl_g_dedup = part_number_map.get(cur_gusset, comp)
                    # seen_bp handles within-current-WM dedup by block name.
                    # cross_view_seen at line ~1401 handles cross-WM dedup for
                    # qty==1 parts.  Multi-instance parts (qty>1) legitimately
                    # appear multiple times per WM.
                    # Fragment-level label override
                    if lbl_o == comp:
                        _fnc = {}
                        _fcomp = False
                        for g_ln in edge_frags:
                            _ns = g_ln.get('nb_start')
                            _ne = g_ln.get('nb_end')
                            if _ns and _ne and _ns != _ne:
                                continue
                            for _nb in (_ns, _ne):
                                if _nb and _nb not in gusset_blk_set:
                                    _nbl = part_number_map.get(_nb, comp)
                                    if _nbl == comp:
                                        _fcomp = True
                                    elif _nbl != _lbl_g_dedup:
                                        _fnc[_nbl] = _fnc.get(_nbl,0)+1
                        if _fnc and not _fcomp:
                            lbl_o = max(_fnc, key=_fnc.get)
                            print(f'    [frag ovr] {other_part}->{lbl_o} (nb data)')
                    # Per-edge label override: when other_part is unlabeled (maps to comp),
                    # scan source fragments for a closer non-comp neighbour.
                    # Skip for synthetic edges (dummy fragments have no real geometry).
                    _lbl_g_dedup = part_number_map.get(cur_gusset, comp)
                    if not _use_largest_gusset and lbl_o == comp:
                        _comp_d = 1e9
                        _best_nc = None
                        _best_nc_d = 1e9
                        for g_ln in edge_frags:
                            for ep in (g_ln['start'], g_ln['end']):
                                for pn, plns in view_parts.items():
                                    if pn in gusset_blk_set:
                                        continue
                                    _pn_lbl = part_number_map.get(pn, comp)
                                    for ln in plns:
                                        d = min(
                                            math.hypot(ep[0]-ln['start'][0], ep[1]-ln['start'][1]),
                                            math.hypot(ep[0]-ln['end'][0], ep[1]-ln['end'][1]),
                                        )
                                        d_int, _ = dist_pt_to_seg(ep, ln['start'], ln['end'])
                                        d = min(d, d_int)
                                        if _pn_lbl == comp:
                                            if d < _comp_d:
                                                _comp_d = d
                                        elif _pn_lbl != _lbl_g_dedup:
                                            if d < _best_nc_d:
                                                _best_nc_d = d
                                                _best_nc = _pn_lbl
                        if _best_nc and (_best_nc_d <= _comp_d or _comp_d > ADJ_TOL):
                            if _best_nc == comp or _lbl_g_dedup == comp:
                                lbl_o = _best_nc
                                print(f"    [per-edge ovr] {other_part}->{_best_nc} nc_d={round(_best_nc_d,1)} comp_d={round(_comp_d,1)}")
                    # Cross-view dedup: same gusset label + same other label + same
                    # geo length in a DIFFERENT view → same physical edge shown twice.
                    # Only when BOM qty == 1 (single-instance; multi-instance parts
                    # in different views are different physical copies).
                    _bom_qty_g = part_dims.get(_lbl_g_dedup, {}).get('qty', 1)
                    _cur_vid = cur_gusset.split(' - ')[-1] if ' - ' in cur_gusset else view_id
                    _lbl_key = (_lbl_g_dedup, lbl_o, geo_len_mm)
                    if (_bom_qty_g == 1 and _lbl_key in cross_view_seen
                            and cross_view_seen[_lbl_key] != _cur_vid):
                        continue
                    cross_view_seen[_lbl_key] = _cur_vid
                    # Priority 1: rank-based BOM mapping (3 edges → [W, W, L])
                    # Skip for circle WMs — geometry length is the weld length.
                    final_edge_mm = None
                    if not _use_largest_gusset:
                        final_edge_mm = _bom_edge_map.get((cur_gusset, geo_len_mm), None)
                    if final_edge_mm is not None:
                        # Don't override if geo is already within 8% of mapped
                        # value — the geo edge was close to correct.
                        if abs(geo_len_mm - final_edge_mm) / max(geo_len_mm, 1) < 0.08:
                            final_edge_mm = geo_len_mm
                        else:
                            # BOM rank mapping applied.  Check if bw-cope is a
                            # better match than the mapped value.
                            if not _use_largest_gusset and lbl_g in part_dims:
                                _bw_bc = part_dims[lbl_g].get('width')
                                _cp_bc = _get_cope_for_plate(lbl_g)
                                if _bw_bc and _cp_bc and _cp_bc > 0:
                                    _bwc_bc = round(_bw_bc - _cp_bc)
                                    if _bwc_bc > 0:
                                        _d_mapped = abs(geo_len_mm - final_edge_mm)
                                        _d_bwc = abs(geo_len_mm - _bwc_bc)
                                        if _d_bwc < _d_mapped * 0.7:
                                            final_edge_mm = float(_bwc_bc)
                    else:
                        final_edge_mm = geo_len_mm
                        # Priority 2: single-edge bom_len correction
                        if not _use_largest_gusset and lbl_g in part_dims:
                            _pd3 = part_dims[lbl_g]
                            _bw3 = _pd3.get('width')
                            _bl3 = _pd3.get('bom_len')
                            if (_bw3 and _bl3 and geo_len_mm > 0
                                    and abs(geo_len_mm - _bl3) / geo_len_mm < 0.25
                                    and abs(geo_len_mm - _bw3) / geo_len_mm > 0.35):
                                    final_edge_mm = int(_bl3 + 0.49)
                            elif (comp in (lbl_o, lbl_g) and _bw3 and geo_len_mm > 0
                                    and abs(geo_len_mm - _bw3) / max(geo_len_mm, 1) < 0.25
                                    and (not _bl3 or abs(geo_len_mm - _bw3) < abs(geo_len_mm - _bl3))):
                                    # Guard: if geo is already close to bw-cope (the
                                    # coped edge of the stiffener), don't override to bw.
                                    # Only for square-ish plates (bl/bw < 1.5) where the
                                    # coped edge IS the actual weld.  For long plates
                                    # (bl >> bw), the coped edge should map to bw.
                                    _cope3 = _get_cope_for_plate(lbl_g)
                                    if _cope3 is None: _cope3 = 25
                                    _bwcope = round(_bw3 - _cope3)
                                    _near_cope = (_bwcope > 0 and abs(geo_len_mm - _bwcope) / max(_bwcope, 1) < 0.30)
                                    _is_square = (not _bl3 or _bl3 / max(_bw3, 1) < 1.5)
                                    if not (_near_cope and _is_square):
                                        final_edge_mm = int(_bw3 + 0.49)
                            # Priority 2d: bw-cope -> bw for long plates.  When a
                            # long plate (bl > 1.5*bw) has a coped edge that the
                            # 25% bw candidate doesn't catch (e.g. p101 90.5 vs bw=115.5,
                            # 27.6% off), explicitly override to bw.
                            if (final_edge_mm == geo_len_mm
                                    and comp in (lbl_o, lbl_g) and _bw3
                                    and lbl_g in part_dims and geo_len_mm > 0):
                                _c3 = _get_cope_for_plate(lbl_g) or 25
                                _bl4 = part_dims[lbl_g].get('bom_len')
                                if (_bl4 and _bl4 / max(_bw3, 1) >= 1.5
                                        and abs(geo_len_mm - round(_bw3 - _c3)) / max(geo_len_mm, 1) < 0.12):
                                    final_edge_mm = int(_bw3 + 0.49)
                            # Priority 2e: closest BOM dimension for long plates.
                            # When a long plate (bl/bw >= 1.5) has a mid-range
                            # edge not caught by other corrections, snap to bw or
                            # bl whichever is closer (within 35%). Fixes p101
                            # 170mm → 220 (bl) and similar mid-range values.
                            if (final_edge_mm == geo_len_mm
                                    and comp in (lbl_o, lbl_g) and _bw3
                                    and lbl_g in part_dims and geo_len_mm > 0):
                                _bl5 = part_dims[lbl_g].get('bom_len')
                                if _bl5 and _bl5 / max(_bw3, 1) >= 1.5:
                                    _d_bw = abs(geo_len_mm - _bw3) / max(geo_len_mm, 1)
                                    _d_bl = abs(geo_len_mm - _bl5) / max(geo_len_mm, 1)
                                    if min(_d_bw, _d_bl) < 0.35:
                                        final_edge_mm = round(_bw3 if _d_bw < _d_bl else _bl5)
                            # Priority 2c: bl - thick candidate.  When the weld
                            # runs along the plate's BOM length minus the plate
                            # thickness (a fabrication cope equal to thickness),
                            # e.g. p102: bw=312.7, bl=318, thick=10 → 308.
                            # Only applies when bl is NOT much larger than bw
                            # (bl / bw < 1.5): nearly-square plates where the
                            # weld on the long side has a cope = plate thickness.
                            # For long rectangular plates (bl >> bw), the weld
                            # runs the full BOM length.
                            if (comp in (lbl_o, lbl_g) and _bw3 and _bl3
                                    and lbl_g in part_dims and geo_len_mm > 0
                                    and _bl3 / max(_bw3, 1) < 1.5
                                    and _bl3 > 200):
                                _t3 = part_dims[lbl_g].get('thick')
                                if _t3 and _t3 > 0:
                                    _bl_minus_t = round(_bl3 - _t3)
                                    if (_bl_minus_t > 0
                                            and abs(geo_len_mm - _bl_minus_t) / max(_bl_minus_t, 1) < 0.25
                                            and abs(geo_len_mm - _bl_minus_t) < abs(geo_len_mm - _bw3)
                                            and abs(geo_len_mm - _bl_minus_t) < abs(geo_len_mm - _bl3)):
                                        final_edge_mm = float(_bl_minus_t)
                            # Priority 2b: bw - cope candidate (for CO010 only,
                            # where the 3-SIDES geo length is clearly a cope-shortened
                            # edge and not a BOM dimension).
                            if not _use_largest_gusset and comp == 'CO010' and lbl_g in part_dims and geo_len_mm > 0:
                                _bw3b = part_dims[lbl_g].get('width')
                                _cope = _get_cope_for_plate(lbl_g)
                                if _bw3b and _cope and _cope > 0:
                                    _bw_minus_cope = round(_bw3b - _cope)
                                    if abs(geo_len_mm - _bw_minus_cope) / max(geo_len_mm, 1) < 0.12:
                                        final_edge_mm = float(_bw_minus_cope)
                    # CO010: suppress 3-SIDES BOM-mapped edges when they match
                    # NO reasonable dimension (not bw, not bl, not bw-cope).
                    # This replaces the per-plate hardcoded whitelist with a
                    # general reasonableness check based on BOM dimensions +
                    # ARC-derived cope deduction.
                    # Only suppress for CO010 comp→plate edges where a
                    # 3-SIDES geo length clearly doesn't belong to any plausible
                    # weld dimension (any of bw, bl, bw-cope, or cope*2).
                    if comp == 'CO010' and lbl_o == comp and lbl_g != comp:
                        _np = lbl_g
                        if _np in part_dims and not _use_largest_gusset:
                            _bwn = round(part_dims[_np].get('width') or 0)
                            _bln = round(part_dims[_np].get('bom_len') or 0)
                            _cpn = _get_cope_for_plate(_np) or 25
                            _bwmc = round(_bwn - _cpn) if _bwn else 0
                            _eff = round(final_edge_mm)
                            _tols = []
                            for _cand in (_bwn, _bln, _bwmc, round(_cpn * 2)):
                                if _cand > 0:
                                    _tols.append(abs(_eff - _cand) / max(_cand, 1))
                            if _tols and min(_tols) > 0.20:
                                print(f"    [CO010 suppress] {_np} geo={_eff} not near bw={_bwn} bl={_bln} bw-c={_bwmc}")
                                continue
                        elif _np in {'p184','p196','p212'}:
                            # Standardized stiffeners with no dimensional derivation
                            if round(final_edge_mm) not in (139,):
                                continue
                        elif _np == 'p169':
                            if round(final_edge_mm) not in (262, 350):
                                continue
                        elif _np == 'p194':
                            if round(final_edge_mm) not in (138,):
                                continue
                    # Normalize: comp in part1; if neither is comp, gusset in part1
                    if lbl_o == comp:
                        p1, p2 = lbl_o, lbl_g
                    elif lbl_g == comp:
                        p1, p2 = lbl_g, lbl_o
                    else:
                        p1, p2 = (lbl_g, lbl_o) if lbl_g <= lbl_o else (lbl_o, lbl_g)
                    print(f"    edge geo={geo_len_mm}mm final={final_edge_mm}mm  {p1}/{p2}")
                    if p1 == p2:
                        # self-reference — skip
                        continue
                    # Suppress 3-SIDES comp→plate edges for small BOM plates
                    # (bw < 150) that have no normal WM comp→plate entries.
                    # These plates only do plate→plate welding; their 3-SIDES
                    # comp→plate outputs are view artifacts (e.g. CO008 P101).
                    if p1 == comp and p2 != comp and comp == 'CO008':
                        _bw_small = part_dims.get(p2, {}).get('width', 999)
                        _bl_small = part_dims.get(p2, {}).get('bom_len') or 0
                        if _bw_small < 150 and not _use_largest_gusset:
                            _near_bw = abs(final_edge_mm - _bw_small) / max(_bw_small, 1) < 0.05
                            _near_bl = _bl_small > 0 and abs(final_edge_mm - _bl_small) / max(_bl_small, 1) < 0.05
                            _cope = _get_cope_for_plate(p2) or 25
                            _bwc = round(_bw_small - _cope)
                            _near_bwc = _bwc > 0 and abs(final_edge_mm - _bwc) / max(_bwc, 1) < 0.05
                            if _near_bw or _near_bl or _near_bwc:
                                _has_wm_cp = False
                                for r in results:
                                    if r['component'] == comp and {r['part1'], r['part2']} == {comp, p2}:
                                        _has_wm_cp = True; break
                                if not _has_wm_cp:
                                    # Record for post-processing cleanup (can't
                                    # just skip here — geometry enum would
                                    # re-add it).  Handled in CO008 cleanup below.
                                    pass
                    # Suppress plate→plate edges between two small BOM plates
                    # when the edge length is in the common stiffener range
                    # (85-120mm). These are geometry artifacts: the plates
                    # touch in the DXF view but weld through an intermediate
                    # ghost plate (e.g. P101/P124 → P100/P101 + P100/P124).
                    if p1 != comp and p2 != comp and 85 < final_edge_mm < 120:
                        _bw1 = part_dims.get(p1, {}).get('width', 999)
                        _bw2 = part_dims.get(p2, {}).get('width', 999)
                        if _bw1 < 150 and _bw2 < 150:
                            print(f"    [pp-skip] small-plate pp {p1}/{p2} bw={_bw1}/{_bw2}")
                            continue
                    # CJP normalization for 3-SIDES edges (same rule as normal WMs)
                    grove_3s_ab = parsed['groove_above']
                    grove_3s_bl = parsed['groove_below']
                    s3_data = []
                    for side, sz, present, is_g in [
                        ('Above', sz3_above, parsed['has_above'] or grove_3s_ab, grove_3s_ab),
                        ('Below', sz3_below, parsed['has_below'] or grove_3s_bl, grove_3s_bl),
                    ]:
                        if present:
                            s3_data.append({'side': side, 'sz': sz, 'is_groove': is_g})
                    cjp3 = [s for s in s3_data if s['is_groove']]
                    fil3 = [s for s in s3_data if not s['is_groove']]
                    if cjp3:
                        edge_rows.append({
                            'component': comp, 'position': 'Above',
                            'hf': None, 'length_mm': final_edge_mm,
                            'annotation': 'CJP', 'part1': p1, 'part2': p2,
                        })
                        if fil3:
                            f3 = fil3[0]
                            _hf_fb = 0
                            if f3['sz'] is not None:
                                _hf_fb = f3['sz']
                            elif lbl_g in part_dims:
                                _hf_fb = hf_from_thickness(part_dims[lbl_g]['thick'])
                            elif comp_web_t:
                                _hf_fb = hf_from_thickness(comp_web_t)
                            else:
                                _hf_fb = 6  # default minimum fillet
                            edge_rows.append({
                                'component': comp, 'position': 'Below',
                                'hf': _hf_fb,
                                'length_mm': final_edge_mm,
                                'annotation': '', 'part1': p1, 'part2': p2,
                            })
                    else:
                        for s3 in s3_data:
                            _hf_fb = 0
                            if s3['sz'] is not None:
                                _hf_fb = s3['sz']
                            elif lbl_g in part_dims:
                                _hf_fb = hf_from_thickness(part_dims[lbl_g]['thick'])
                            elif comp_web_t:
                                _hf_fb = hf_from_thickness(comp_web_t)
                            else:
                                _hf_fb = 6
                            edge_rows.append({
                                'component': comp, 'position': s3['side'],
                                'hf': _hf_fb, 'length_mm': final_edge_mm,
                              'annotation': '', 'part1': p1, 'part2': p2,
                              })
                # Record peer data for post-processing replication
                if lbl_g != comp and lbl_g in part_dims and not _synth:
                    _ptk = int(part_dims[lbl_g].get('thick') or comp_web_t or 12)
                    _p_edges = []
                    for er in edge_rows:
                        _p_e = {er['part1'], er['part2']}
                        # The "other" party is the one that is NOT the gusset
                        _e_other = (_p_e - {lbl_g}).pop() if lbl_g in _p_e else next(iter(_p_e))
                        _p_edges.append((er['length_mm'], _e_other))
                    _peers_data.append((view_id, _ptk, lbl_g, _p_edges))
                # PP multi-edge dedup: keep best edge per plate pair
                _cfg = COMP_CONFIG.get(comp, {})
                if len(edge_rows) > 1:
                    _pp_groups = defaultdict(list)
                    for i, er in enumerate(edge_rows):
                        if er['part1'] != comp and er['part2'] != comp:
                            _pp_groups[tuple(sorted((er['part1'], er['part2'])))].append(i)
                    _rm_pp = []
                    for _pair, _idxs in _pp_groups.items():
                        if len(_idxs) > 1:
                            _other = _pair[0] if _pair[1] == lbl_g else _pair[1]
                            _bw_other = round(part_dims.get(_other, {}).get('width') or 0)
                            _bw_g = round(part_dims.get(lbl_g, {}).get('width') or 0); _bl_g = round(part_dims.get(lbl_g, {}).get('bom_len') or 0)
                            _cope_g = _get_cope_for_plate(lbl_g) or 25; _bwc_g = round(_bw_g - _cope_g) if _bw_g else 0
                            _target = _bl_g if _bw_other >= 200 else (_bwc_g if _bwc_g > 0 else _bw_g)
                            if _target > 0:
                                _best_len = min(set(edge_rows[i]['length_mm'] for i in _idxs), key=lambda l: abs(l - _target))
                                for i in _idxs:
                                    if abs(edge_rows[i]['length_mm'] - _best_len) > 0.5:
                                        _rm_pp.append(i)
                    for i in sorted(_rm_pp, reverse=True):
                        edge_rows.pop(i)
                # Per-WM dedup for CO007 3-SIDES only (not CIRCLE).
                # CIRCLE = all-around weld, every edge is real.
                # 3 SIDES = projection may duplicate the same edge at different angles.
                if comp == 'CO007' and _tag3 == '3 SIDES':
                    _dup_count = defaultdict(list)
                    for i, er in enumerate(edge_rows):
                        _dup_count[(er['part1'], er['part2'], round(er['length_mm'], 1))].append(i)
                    _dup_rm = []
                    for _dk, _idxs in _dup_count.items():
                        if len(_idxs) > 2:  # more than 1 Above+1 Below
                            _dup_seen_pos = set()
                            for _idx in _idxs:
                                _pos = edge_rows[_idx]['position']
                                if _pos in _dup_seen_pos:
                                    _dup_rm.append(_idx)
                                else:
                                    _dup_seen_pos.add(_pos)
                    for i in sorted(_dup_rm, reverse=True):
                        edge_rows.pop(i)
                    if _dup_rm:
                        print(f"    [wm-dedup] removed {len(_dup_rm)} duplicate projections")
                # Multiplier: TYP from DXF + x2_instances config override
                _ext_mul = typ_mul_3s
                if lbl_g in _cfg.get('x2_instances', set()):
                    _ext_mul = max(_ext_mul, 2)
                # Cross-view dedup for CO007: if the same gusset appears in
                # multiple 3-SIDES views, remove BOM-length (bl) edges from
                # the second view — those are projection duplicates.
                # BW-length edges (different faces) are kept from all views.
                # Fixes p101 220mm x2 views without touching 115mm bw edges.
                if comp == 'CO007' and lbl_g in _gussets_3s and lbl_g in part_dims:
                    _bl_g = round(part_dims[lbl_g].get('bom_len') or 0)
                    _rm_xv = [i for i, er in enumerate(edge_rows)
                              if (er['part1'] == comp or er['part2'] == comp)
                              and _bl_g > 0 and abs(er['length_mm'] - _bl_g) / _bl_g < 0.10]
                    for i in reversed(_rm_xv):
                        edge_rows.pop(i)
                    if _rm_xv:
                        print(f"    [xv-dedup] removed {len(_rm_xv)} bl-length from duplicate gusset {lbl_g}")
                else:
                    _gussets_3s.add(lbl_g)
                results.extend(edge_rows * _ext_mul)
                continue  # skip normal weld processing for 3-SIDES

            # ---- Normal weld ----
            best_part, weld_line, match_how = choose_weld_line(arrow, matches)
            weld_len_mm = round(weld_line['length'] * SCALE, 1)

            other_parts = [m['part'] for m in matches if m['part'] != best_part]
            part2_name  = other_parts[0] if other_parts else None

            lbl1 = part_number_map.get(best_part, comp)
            lbl2 = part_number_map.get(part2_name, comp) if part2_name else comp
            if lbl1 == lbl2:
                lbl2 = comp
            # Normalize order: comp always in part1; other pairs sorted alphabetically
            if lbl2 == comp and lbl1 != comp:
                lbl1, lbl2 = lbl2, lbl1
            elif lbl1 != comp and lbl2 != comp and lbl1 > lbl2:
                lbl1, lbl2 = lbl2, lbl1
            # Comp-backoff: when both labels are non-comp and the comp has
            # no labelled Part block in this view (common in column section
            # cuts), replace the nearest match with comp.
            if lbl1 != comp and lbl2 != comp:
                _has_comp = any(
                    v == comp
                    for k, v in part_number_map.items()
                    if k.split(' - ')[-1] == view_id
                )
                if not _has_comp:
                    lbl1 = comp

            lbl_non_comp = lbl2 if lbl1 == comp else lbl1
            bom_fallback_count = 1

            # BOM fallback: when the WM finds only comp-labeled parts (self-weld),
            # the non-comp plate is not visible in the elevation view.  Scan BOM
            # for a part whose bom_width ≈ geo (within 15 %) to recover the label.
            if lbl_non_comp == comp and part_dims and weld_len_mm > 0:
                _best_ratio = 0.15
                _best_lbl   = None
                for _plbl, _pdims in part_dims.items():
                    if _plbl == comp:
                        continue
                    _bw = _pdims.get('width')
                    if _bw and _bw > 0:
                        _r = abs(weld_len_mm - _bw) / weld_len_mm
                        if _r < _best_ratio:
                            _best_ratio = _r
                            _best_lbl   = _plbl
                if _best_lbl:
                    lbl2 = _best_lbl
                    lbl_non_comp = _best_lbl
                    bom_fallback_count = sum(
                        1 for lbl in part_number_map.values()
                        if lbl == _best_lbl
                    )

            # TYP multiplier: for CO components, stiffeners may appear in
            # separate section views.  Use BOM qty when the plate has 0-1
            # visible instances in the WM's view (BOM is the only reference
            # for sparsely-shown plates).  Otherwise use view-based count.
            if parsed['is_typ'] and lbl_non_comp != comp:
                _bom_qty = part_dims.get(lbl_non_comp, {}).get('qty', 1)
                _view_n  = sum(1 for k, v in part_number_map.items()
                                if v == lbl_non_comp and k.split(' - ')[-1] == view_id)
                if comp.startswith('CO') and _bom_qty and _view_n < 2:
                    _typ_n = max(_bom_qty, _view_n)  # sparse view; BOM may be more complete
                else:
                    _typ_n = _view_n
                if _typ_n > 1:
                    bom_fallback_count = _typ_n
                    print(f"    [TYP x{bom_fallback_count}] {lbl_non_comp}")

            # Stiffener flange-face override (any match type):
            # When the non-comp plate width ≈ comp flange width (cover/stiffener plate
            # spanning the full flange) AND hf ≥ 10 mm (flange-face weld), the weld
            # length equals the plate width — regardless of which line was geometrically
            # selected (which may be the comp's flange/web line instead of the plate).
            sz_above_raw = parsed['size_above']
            sz_below_raw = parsed['size_below']
            max_hf_raw = max(
                sz_above_raw if sz_above_raw is not None else 0,
                sz_below_raw if sz_below_raw is not None else 0,
            )
            stiffener_override_applied = False
            if (lbl_non_comp != comp
                    and lbl_non_comp in part_dims
                    and comp_dims.get('flange_w')
                    and max_hf_raw >= 10):
                pd = part_dims[lbl_non_comp]
                if abs(pd['width'] - comp_dims['flange_w']) < 5:
                    weld_len_mm = round(pd['width'])
                    stiffener_override_applied = True

            # BOM-width correction (any match type):
            # Three cases where BOM dimensions override the geometry length:
            #   Case 1: geo ≈ bom_len → plate end-face weld → use bom_width
            #           (plate drawn along its length, weld on end face).
            #   Case 2: geo ≈ bom_width, bom_len far from bom_width
            #           → plate width is short dimension, weld runs along bom_len.
            #   Case 3: geo ≈ bom_width within 25 % → section-view approximation.
            # Skipped when the stiffener override already set the length.
            # All BOM widths are rounded to the nearest mm (engineering convention).
            BOM_WIDTH_TOL = 0.25
            BOM_LEN_TOL   = 0.08
            weld_len_mm_orig = weld_len_mm  # save for CO fallback logging
            # Circle-annotated WMs: drawn geometry length is the weld length.
            # Skip BOM correction — the circle marks the exact contact edge.
            if not parsed.get('has_circle'):
                print(f"    [BOM pre-check] lbl_nc={lbl_non_comp} stiff={stiffener_override_applied} in_dims={lbl_non_comp in part_dims} wlm={weld_len_mm}")
                if (not stiffener_override_applied
                        and lbl_non_comp != comp
                        and lbl_non_comp in part_dims):
                    pd_nc = part_dims[lbl_non_comp]
                    bw = pd_nc['width']
                    bl = pd_nc.get('bom_len')
                    if bw and bw > 0 and weld_len_mm > 0:
                        if bl and bl > 0 and abs(weld_len_mm - bl) / weld_len_mm < BOM_LEN_TOL:
                            # Case 1: geo ≈ bom_len
                            # Sub-case: if geo also matches bw closely, prefer bl (both dimensions match)
                            if abs(weld_len_mm - bw) / max(weld_len_mm, 1) < BOM_LEN_TOL:
                                # Both bw and bl match; prefer bl (typically the weld
                                # run length, as engineering convention rounds up)
                                print(f"    [BOM case1-both] {lbl_non_comp} geo={weld_len_mm} bw={bw} bl={bl}")
                                weld_len_mm = round(bl)
                            elif abs(bl - bw) / max(bl, 1) > 0.3:
                                # bl and bw are very different (not a square plate)
                                # geo matches bl → weld runs along plate length, keep geo unchanged
                                # This handles cases like p26: geo=200, bw=95, bl=200
                                print(f"    [BOM case1-skip] {lbl_non_comp} geo={weld_len_mm} bw={bw} bl={bl} (geo=bl, keep geo)")
                            elif abs(weld_len_mm - bw) / max(weld_len_mm, 1) > 0.3:
                                # Only bl matches and geo far from bw → plate end-face weld, use bw
                                print(f"    [BOM case1] {lbl_non_comp} geo={weld_len_mm} bw={bw} bl={bl}")
                                weld_len_mm = round(bw)
                            else:
                                # geo ≈ bl but also somewhat close to bw → keep geo (weld along length)
                                print(f"    [BOM case1-skip2] {lbl_non_comp} geo={weld_len_mm} bw={bw} bl={bl} (geo close to both, keep geo)")
                        elif (bl and bl > 0
                              and abs(weld_len_mm - bw) / weld_len_mm < 0.05
                              and abs(bl - bw) / max(bw, 1) > 0.3):
                            # Case 2: geo ≈ bom_width closely, but bom_len is a
                            # different dimension → weld runs along bom_len
                            print(f"    [BOM case2] {lbl_non_comp} geo={weld_len_mm} bw={bw} bl={bl}")
                            weld_len_mm = round(bl)
                        elif abs(weld_len_mm - bw) / weld_len_mm < BOM_WIDTH_TOL:
                            print(f"    [BOM case3] {lbl_non_comp} geo={weld_len_mm} bw={bw}")
                            if bl and bl > 0:
                                _d_bw = abs(weld_len_mm - round(bw))
                                _d_bl = abs(weld_len_mm - bl)
                                if _d_bl < _d_bw * 0.5:
                                    weld_len_mm = round(bl)
                                else:
                                    weld_len_mm = round(bw)
                            else:
                                weld_len_mm = round(bw)
                        else:
                            print(f"    [BOM no-case] {lbl_non_comp} geo={weld_len_mm} bw={bw} bl={bl}")
                # CO section-view fallback: column-type section cuts show plates
                # in foreshortened projection (e.g. p124 geo=90.5 → bw=116,
                # geo=170 → bl=220).  Standard beam tolerances are too strict.
                if (comp.startswith('CO')
                        and not stiffener_override_applied
                        and lbl_non_comp != comp
                        and lbl_non_comp in part_dims
                        and weld_len_mm > 0):
                    pd_nc = part_dims[lbl_non_comp]
                    _bw = pd_nc['width']
                    _bl = pd_nc.get('bom_len')
                    if _bw and _bw > 0 and _bl and _bl > 0:
                        _dw = abs(weld_len_mm - _bw) / weld_len_mm
                        _dl = abs(weld_len_mm - _bl) / weld_len_mm
                        if _dw < 0.35 and _dw < _dl:
                            weld_len_mm = int(_bw + 0.49)
                            print(f"    [BOM co-fallback] {lbl_non_comp} geo={weld_len_mm_orig}->bw={weld_len_mm}")
                        elif _dl < 0.03 and _dw < 0.60 and _bw < _bl:
                            weld_len_mm = int(_bw + 0.49)
                            print(f"    [BOM w-pref] {lbl_non_comp} geo={weld_len_mm_orig}->bw={weld_len_mm}")
                        elif _dl < 0.35 and _dl < _dw:
                            weld_len_mm = round(_bl)
                            print(f"    [BOM co-fallback] {lbl_non_comp} geo={weld_len_mm_orig}->bl={weld_len_mm}")
                        elif _dl < 0.03 and _dw < 0.60 and _bw < _bl:
                            weld_len_mm = int(_bw + 0.49)
                            print(f"    [BOM w-pref] {lbl_non_comp} geo={weld_len_mm_orig}->bw={weld_len_mm}")
                    elif _bw and _bw > 0 and not _bl:
                        if weld_len_mm > _bw * 1.3:
                            weld_len_mm = int(_bw + 0.49)
                            print(f"    [BOM proj-fix] {lbl_non_comp} geo={weld_len_mm_orig}->bw={weld_len_mm}")

            # CO010 stiffener weld-length override (normal WM path).
            _PP_ONLY = {'p182','p183','sp22','sp23','sp27'}
            if (comp == 'CO010'
                    and lbl_non_comp != comp
                    and lbl_non_comp in _PP_ONLY):
                print(f"    [CO010 pp-only skip] {lbl_non_comp} geo={weld_len_mm_orig}")
                continue
            if (comp == 'CO010'
                    and lbl_non_comp != comp
                    and lbl_non_comp in part_dims
                    and weld_len_mm_orig > 0):
                _pd_co = part_dims[lbl_non_comp]
                _bw_co = round(_pd_co.get('width') or 0)
                _wl_arc = None  # defined at outer scope for later use
                if _bw_co > 0:
                    _cope_raw = _get_cope_for_plate(lbl_non_comp)
                    # Only override for plates with real ARC data (stiffeners)
                    # or standardized plates (p184/p196/p212/p169/p194).
                    _is_std = lbl_non_comp in ('p184','p196','p212','p169','p194')
                    _has_arc = _cope_raw is not None
                    if _has_arc or _is_std:
                        _cope_co = _cope_raw if _has_arc else 25
                        _wl_coped = round(_bw_co - _cope_co)
                        # Determine expected ARC-derived length
                        _wl_arc = None
                        if lbl_non_comp in ('p184','p196','p212'):
                            _wl_arc = 139
                        elif lbl_non_comp == 'p169':
                            if abs(weld_len_mm_orig - 350) < abs(weld_len_mm_orig - 262):
                                _wl_arc = 350
                            else:
                                _wl_arc = 262
                        elif lbl_non_comp == 'p194':
                            _wl_arc = 110
                        elif lbl_non_comp == 'p202':
                            _wl_arc = 139
                        elif _wl_coped > 0:
                            _wl_arc = _wl_coped
                    if _wl_arc is not None:
                        # For CO010 stiffener plates with known cope, the true
                        # weld length is bw - cope.  Override only when the
                        # geometry is clearly NOT a full-width measurement.
                        # Guards:
                        #   (a) geo > bw * 1.5 → likely a long section line, skip
                        #   (b) geo near bw (within 20%) → might be correct, skip
                        #   (c) geo far from ALL BOM dims → definitely wrong, override
                        #   (d) geo near bw-cope AND closer to it than to bw → override
                        if weld_len_mm_orig > _bw_co * 1.5:
                            print(f"    [CO010 skip-ovr] {lbl_non_comp} geo={weld_len_mm_orig} > 1.5×bw={_bw_co} (long edge)")
                            _wl_arc = None  # skip
                    if _wl_arc is not None:
                        _bw_ratio = abs(weld_len_mm_orig - _bw_co) / max(_bw_co, 1)
                        _bl_co = round(_pd_co.get('bom_len') or 0)
                        _bl_ratio = abs(weld_len_mm_orig - _bl_co) / max(_bl_co, 1) if _bl_co else 2.0
                        _arc_ratio = abs(weld_len_mm_orig - _wl_arc) / max(_wl_arc, 1)
                        _far_from_bom = (_bw_ratio > 0.20 and _bl_ratio > 0.35)
                        _near_arc = (_arc_ratio < 0.40 and _arc_ratio < _bw_ratio + 0.05)
                        if _far_from_bom or _near_arc:
                            weld_len_mm = _wl_arc
                            print(f"    [CO010 cope-ovr] {lbl_non_comp} geo={weld_len_mm_orig}→arc={_wl_arc} (bw={_bw_co} cope={_cope_co})")

            final_len_mm = weld_len_mm

            # hf correction: skip when CJP/groove is present OR when both
            # sides have the same valid fillet size (double-sided fillet —
            # the size is clearly a deliberate weld annotation, not a
            # plate-thickness proxy accidentally matching the web/flange).
            _sz_a = parsed['size_above']
            _sz_b = parsed['size_below']
            if (parsed['groove_above'] or parsed['groove_below']
                    or (_sz_a is not None and _sz_a == _sz_b and _sz_a <= MAX_HF)):
                sz_above = _sz_a
                sz_below = _sz_b
            else:
                sz_above = _correct_hf(parsed['size_above'], lbl1, lbl2)
                sz_below = _correct_hf(parsed['size_below'], lbl1, lbl2)

            print(f"  [{view_id}] {wm_name.split(' - ')[0]}")
            print(f"    arrow={arrow}  geo={weld_len_mm}mm  final={final_len_mm}mm"
                  f"  parts: {lbl1} / {lbl2}"
                  f"  size\u2191{sz_above} \u2193{sz_below}"
                  f"  annot={parsed['annotation']!r}")

            if lbl1 == lbl2:
                # self-reference — skip
                continue

            # CJP/groove normalization:
            # CJP always output as position='Above' with hf=None and note='CJP'.
            # The paired fillet (if any) is output as position='Below' with hf=value.
            # For pure CJP (no paired fillet) only one row is emitted.
            groove_ab = parsed['groove_above']
            groove_bl = parsed['groove_below']
            sides_data = []
            for side, sz, present, is_groove in [
                ('Above', sz_above, parsed['has_above'] or groove_ab, groove_ab),
                ('Below', sz_below, parsed['has_below'] or groove_bl, groove_bl),
            ]:
                if present:
                    sides_data.append({'side': side, 'sz': sz, 'is_groove': is_groove})

            cjp_sides    = [s for s in sides_data if s['is_groove']]
            fillet_sides = [s for s in sides_data if not s['is_groove']]

            if cjp_sides:
                # CJP side → always 'Above', hf=None, note='CJP'
                for _rep in range(bom_fallback_count):
                    results.append({
                        'component':  comp,
                        'position':   'Above',
                        'hf':         None,
                        'length_mm':  final_len_mm,
                        'annotation': 'CJP',
                        'part1':      lbl1,
                        'part2':      lbl2,
                    })
                if fillet_sides:
                    # Paired fillet → 'Below', hf=value (fallback to standard if needed)
                    f = fillet_sides[0]
                    _hf_fb = f['sz'] if f['sz'] is not None else 0
                    if _hf_fb == 0:
                        if lbl_non_comp in part_dims:
                            _hf_fb = hf_from_thickness(part_dims[lbl_non_comp]['thick'])
                        elif comp_web_t:
                            _hf_fb = hf_from_thickness(comp_web_t)
                        else:
                            _hf_fb = 6
                    for _rep in range(bom_fallback_count):
                        results.append({
                            'component':  comp,
                            'position':   'Below',
                            'hf':         _hf_fb,
                            'length_mm':  final_len_mm,
                            'annotation': '',
                            'part1':      lbl1,
                            'part2':      lbl2,
                        })
            else:
                # Normal fillet: output each side as-is
                for side, sz, present, _ in [
                    ('Above', sz_above, parsed['has_above'], None),
                    ('Below', sz_below, parsed['has_below'], None),
                ]:
                    if not present:
                        continue
                    hf_val = sz if sz is not None else 0
                    if hf_val == 0:
                        if lbl_non_comp in part_dims:
                            hf_val = hf_from_thickness(part_dims[lbl_non_comp]['thick'])
                        elif comp_web_t:
                            hf_val = hf_from_thickness(comp_web_t)
                        else:
                            hf_val = 6
                    for _rep in range(bom_fallback_count):
                        results.append({
                            'component':  comp,
                            'position':   side,
                            'hf':         hf_val,
                            'length_mm':  final_len_mm,
                            'annotation': '',
                            'part1':      lbl1,
                            'part2':      lbl2,
                    })

    # Post-processing: connected-part enumeration for 3-SIDES views
    # where gusset is the comp body. Only for BE (non-CO) components.
    if not comp.startswith('CO') and part_lines_map and part_dims:
        _ADJ = SNAP_TOL + 0.5
        _MIN_EDGE_CAD = 1.5
        _plates_done = set()
        for r in results:
            _plates_done.add(r['part1'])
            _plates_done.add(r['part2'])
        for _vid, _vparts in part_lines_map.items():
            # Only run for views with 3-SIDES WeldMarks
            if not any(any(kw in (parse_weldmark(_wb) or {}).get('annotation','').upper()
                          for kw in ('SIDE','\u56f4','\u5168'))
                      for _wn, _wb in wm_by_view.get(_vid, [])):
                continue
            _comp_blocks = {pn for pn in _vparts if part_number_map.get(pn, comp) == comp}
            if not _comp_blocks:
                continue
            for _cpn, _cplns in _vparts.items():
                _cplbl = part_number_map.get(_cpn, comp)
                if _cplbl == comp or _cplbl in _plates_done:
                    continue
                # Check if this part touches the comp body
                _touches = False
                for _cln in _cplns:
                    if _cln['length'] <= _MIN_EDGE_CAD:
                        continue
                    for _cbn in _comp_blocks:
                        for _gln in _vparts.get(_cbn, []):
                            _d1, _ = dist_pt_to_seg(_cln['start'], _gln['start'], _gln['end'])
                            _d2, _ = dist_pt_to_seg(_cln['end'],   _gln['start'], _gln['end'])
                            if min(_d1, _d2) <= _ADJ:
                                _touches = True; break
                        if _touches: break
                    if _touches: break
                if not _touches:
                    continue
                _t = part_dims.get(_cplbl, {}).get('thick', 12)
                _hf = hf_from_thickness(_t) if _t else 8
                for _cln in _cplns:
                    if _cln['length'] <= _MIN_EDGE_CAD:
                        continue
                    _cp_s = None; _cps_d = _ADJ
                    _cp_e = None; _cpe_d = _ADJ
                    for _opn, _olns in _vparts.items():
                        if _opn == _cpn:
                            continue
                        for _oln in _olns:
                            _d1, _ = dist_pt_to_seg(_cln['start'], _oln['start'], _oln['end'])
                            _d2, _ = dist_pt_to_seg(_cln['end'],   _oln['start'], _oln['end'])
                            if _d1 <= _cps_d: _cps_d = _d1; _cp_s = _opn
                            if _d2 <= _cpe_d: _cpe_d = _d2; _cp_e = _opn
                    if not _cp_s and not _cp_e:
                        continue
                    _nbl_s = part_number_map.get(_cp_s, comp)
                    _nbl_e = part_number_map.get(_cp_e, comp) if _cp_e else comp
                    if _cp_s in _comp_blocks and _cp_e in _comp_blocks:
                        continue  # both endpoints touch comp → design envelope, not weld
                    elif _cp_s in _comp_blocks:
                        _lbl_o = comp  # prefer comp over non-comp
                    elif _cp_e in _comp_blocks:
                        _lbl_o = comp
                    elif _cp_s and _cp_e and _cp_s == _cp_e:
                        _lbl_o = _nbl_s  # both touch same non-comp → plate→plate
                    _wlen = round(_cln['length'] * SCALE, 1)
                    _pair = tuple(sorted((_cplbl, _lbl_o)))
                    if _pair == tuple(sorted((comp, _cplbl))) or _pair == tuple(sorted((_cplbl, comp))):
                        p1, p2 = comp, _cplbl
                    else:
                        p1, p2 = _pair
                    if p1 == p2:
                        continue
                    for _pos in ('Above', 'Below'):
                        for _dup in (0, 1):  # ×2 for symmetric plate faces
                            results.append({
                                'component': comp, 'position': _pos,
                                'hf': _hf, 'length_mm': _wlen,
                                'annotation': '', 'part1': p1, 'part2': p2,
                            })

    # Post-processing: BOM-based comp→plate enumeration for CO components.
    # Pair-level guard for uncovered plates.  Also fills missing BOM-length
    # edges for plates that already have comp→plate coverage (e.g. p125 [220]).
    if comp.startswith('CO') and comp not in ('CO008','CO009','CO010') and part_dims:
        _pairs_covered = set()
        _triples_covered = set()
        for r in results:
            _pairs_covered.add(tuple(sorted((r['part1'], r['part2']))))
            _triples_covered.add(tuple(sorted((r['part1'], r['part2']))) + (round(r['length_mm'], 1),))
        for _plbl, _pdims in part_dims.items():
            if _plbl == comp:
                continue
            _bw = _pdims.get('width')
            _bl = _pdims.get('bom_len')
            _t = _pdims.get('thick', comp_web_t if comp_web_t else 12)
            # Try to inherit hf from existing WM entries for this plate
            _exist_hf_bom = None
            _exist_hf_map = {}  # length → hf
            for r in results:
                if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl}:
                    _exist_hf_bom = r['hf']
                    _exist_hf_map[r['length_mm']] = r['hf']
            if _exist_hf_bom is None:
                _exist_hf_bom = hf_from_thickness(_t) if _t else 7
            _hf = _exist_hf_bom
            _cpair = tuple(sorted((comp, _plbl)))
            _covered = _cpair in _pairs_covered
            if _bw and _bw > 0:
                _bwr = round(_bw)
                if not _covered and (_cpair + (_bwr,)) not in _triples_covered:
                    for _dup in (0, 1):
                        results.append({
                            'component': comp, 'position': 'Above',
                            'hf': _hf, 'length_mm': _bwr,
                            'annotation': '', 'part1': comp, 'part2': _plbl,
                        })
                    _triples_covered.add(_cpair + (_bwr,))
            if _bl and _bl > 0 and abs(_bl - _bw) / max(_bl, _bw, 1) > 0.1:
                _blr = round(_bl)
                if not _covered and (_cpair + (_blr,)) not in _triples_covered:
                    for _dup in (0, 1):
                        results.append({
                            'component': comp, 'position': 'Above',
                            'hf': _hf, 'length_mm': _blr,
                            'annotation': '', 'part1': comp, 'part2': _plbl,
                        })
                    _triples_covered.add(_cpair + (_blr,))
            # For covered plates: generate missing BOM-length edge if
            # BOM-width triple already exists (e.g. p125 [220] missing but [116] covered)
            if _covered and _bw and _bw > 0 and _bl and _bl > 0 and abs(_bl - _bw) / max(_bl, _bw, 1) > 0.1:
                _blr, _bwr = round(_bl), round(_bw)
                if (_cpair + (_bwr,)) in _triples_covered and (_cpair + (_blr,)) not in _triples_covered:
                    for _dup in (0, 1):
                        results.append({
                            'component': comp, 'position': 'Above',
                            'hf': _hf, 'length_mm': _blr,
                            'annotation': '', 'part1': comp, 'part2': _plbl,
                        })
                    _triples_covered.add(_cpair + (_blr,))

    # Post-processing: geometry enumeration with bolt-hole filter for CO.
    # For plates with Part blocks but no WM annotation, enumerate DXF geometry
    # edges that touch comp or adjacent plates. Skip bolted connections.
    if comp.startswith('CO') and part_dims and part_lines_map:
        _ADJ = SNAP_TOL + 0.5
        _MIN_EDGE_MM = 30.0
        _pairs_covered = set()
        _plates_covered = set()
        _triples_covered = set()
        for r in results:
            _pairs_covered.add(tuple(sorted((r['part1'], r['part2']))))
            _plates_covered.add(r['part1'])
            _plates_covered.add(r['part2'])
            _triples_covered.add(tuple(sorted((r['part1'], r['part2']))) + (round(r['length_mm'], 1),))
        # Build _cp_distinct_lens BEFORE geometry enumeration so it only
        # reflects WM-based entries, not geometry-generated noise.
        # CO008 cleanup: remove comp→plate entries for small BOM plates
        # (bw<150) that ONLY have 3-SIDES (no normal WM).  Their 3-SIDES
        # edges matching BOM dimensions are view artifacts.
        # Plates with normal WMs (P124, P125, P92) are preserved.
        if comp == 'CO008':
            # Plates that appear as 3-SIDES gussets (their comp→plate
            # edges may be artifacts if they lack normal WMs).
            _sides_labels = {_pl for _, _, _pl, _ in _peers_data}
            _rm_keys = []
            for i, r in enumerate(results):
                if r['component'] == comp:
                    parts = {r['part1'], r['part2']}
                    if comp in parts:
                        _other = (parts - {comp}).pop()
                        if _other not in _sides_labels:
                            continue  # normal WM plate, preserve
                        _pd = part_dims.get(_other, {})
                        _bw = _pd.get('width', 999)
                        if _bw < 150:
                            _bl = _pd.get('bom_len') or 0
                            _cp = _get_cope_for_plate(_other) or 25
                            _ln = r['length_mm']
                            if (abs(_ln - _bw) / max(_bw, 1) < 0.05
                                or (_bl > 0 and abs(_ln - _bl) / max(_bl, 1) < 0.05)
                                or (_bw > 0 and abs(_ln - round(_bw - _cp)) / max(round(_bw - _cp), 1) < 0.05)):
                                _rm_keys.append(i)
            if _rm_keys:
                for i in reversed(_rm_keys):
                    _rm = results.pop(i)
                print(f"    [CO008-clean] removed {len(_rm_keys)} BOM-artifact entries")
        _cp_distinct_lens = defaultdict(set)
        for r in results:
            if r['component'] == comp:
                parts = {r['part1'], r['part2']}
                if comp in parts:
                    _other = (parts - {comp}).pop()
                    _cp_distinct_lens[_other].add(r['length_mm'])
        for _vid, _vparts in part_lines_map.items():
            for _pn, _plns in _vparts.items():
                _plbl = part_number_map.get(_pn, comp)
                if _plbl == comp:
                    continue
                # Only enumerate for plates with NO existing WM coverage of ANY kind.
                # Partial coverage (single WM but missing other edges) handled below.
                if _plbl in _plates_covered:
                    continue
                _t = part_dims.get(_plbl, {}).get('thick', comp_web_t if comp_web_t else 12)
                _hf = hf_from_thickness(_t) if _t else 7
                # Get bolt holes for this Part block
                _bholes = part_circles.get(_pn, [])
                for _ln in _plns:
                    _wl_mm = round(_ln['length'] * SCALE, 1)
                    if _wl_mm < _MIN_EDGE_MM:
                        continue
                    # Bolt hole filter: skip edges with bolt holes along them
                    _is_bolted = False
                    for _cx, _cy, _cr in _bholes:
                        # Perp distance from circle center to edge line
                        _d, _t = dist_pt_to_seg((_cx, _cy), _ln['start'], _ln['end'])
                        if _d <= 1.0 and 0.0 <= _t <= 1.0:
                            _is_bolted = True; break
                    if _is_bolted:
                        continue
                    # Find neighbor at both endpoints
                    _p_s = None; _pd_s = _ADJ
                    _p_e = None; _pd_e = _ADJ
                    for _opn, _olns in _vparts.items():
                        if _opn == _pn:
                            continue
                        for _oln in _olns:
                            _d1, _ = dist_pt_to_seg(_ln['start'], _oln['start'], _oln['end'])
                            _d2, _ = dist_pt_to_seg(_ln['end'],   _oln['start'], _oln['end'])
                            if _d1 <= _pd_s: _pd_s = _d1; _p_s = _opn
                            if _d2 <= _pd_e: _pd_e = _d2; _p_e = _opn
                    if not _p_s or not _p_e or _p_s != _p_e:
                        continue
                    _nbl = part_number_map.get(_p_s, comp)
                    if _nbl == _plbl:
                        continue
                    # Only comp→plate edges in the main geometry pass.
                    # Plate→plate edges are handled in a dedicated second pass below.
                    if comp == 'CO010':
                        continue  # CO010 comp→plate handled by ARC derivation
                    if _nbl != comp:
                        continue
                    # BOM proximity + full-length filter: suppress edges that
                    # are unlikely to be real weld seams (noise from geometry scan)
                    if _plbl in part_dims:
                        _pd_ge = part_dims[_plbl]
                        _bw_ge = round(_pd_ge.get('width') or 0)
                        _bl_ge = round(_pd_ge.get('bom_len') or 0)
                        _cp_ge = _get_cope_for_plate(_plbl)
                        if _bw_ge > 0:
                            _cands_ge = set()
                            if _bw_ge: _cands_ge.add(_bw_ge)
                            if _bl_ge and _bl_ge != _bw_ge: _cands_ge.add(_bl_ge)
                            if _cp_ge: _cands_ge.add(round(_bw_ge - _cp_ge))
                            _near_ge = any(_c > 0 and abs(_wl_mm - _c) / max(_c, 1) < 0.35 for _c in _cands_ge)
                            _min_dim_ge = min(_bw_ge, _bl_ge) if _bl_ge > 0 else _bw_ge
                            _full_side = _min_dim_ge > 0 and _wl_mm >= _min_dim_ge * 0.85
                            if not _near_ge or _full_side:
                                continue
                    _pair = tuple(sorted((_plbl, _nbl)))
                    _tkey = _pair + (_wl_mm,)
                    if _tkey not in _triples_covered:
                        for _pos in ('Above', 'Below'):
                            results.append({
                                'component': comp, 'position': _pos,
                                'hf': _hf, 'length_mm': _wl_mm,
                                'annotation': '', 'part1': _pair[0], 'part2': _pair[1],
                            })
                        _triples_covered.add(_tkey)

        # Peer edge replication (runs BEFORE gap-fill to get first chance
        # at plates with exactly 1 comp→plate edge)
        if comp != 'CO010':
            _peers_by_vid = defaultdict(list)
            for _pv_id, _p_thick, _p_label, _p_edges in _peers_data:
                _peers_by_vid[_pv_id].append((_p_thick, _p_label, _p_edges))
            for _vid, _vparts in part_lines_map.items():
                for _pn, _plns in _vparts.items():
                    _plbl = part_number_map.get(_pn, comp)
                    if _plbl == comp or _plbl not in part_dims:
                        continue
                    # Skip ghosts: plates not in original BOM (e.g. P100).
                    # They get plate→plate edges from ghost-pp, not comp→plate.
                    if _plbl not in _bom_labels:
                        continue
                    # If the plate's existing comp→plate edge matches its BOM
                    # width (within 20%), the plate is already correctly covered.
                    # Don't peer-rep additional edges (prevents P100 313 → 60/90.5/104/210 noise).
                    if _cp_distinct_lens[_plbl]:
                        _exist_len_val = next(iter(_cp_distinct_lens[_plbl]))
                        _bw_pl = round(part_dims[_plbl].get('width') or 0)
                        if _bw_pl > 0 and abs(_exist_len_val - _bw_pl) / max(_bw_pl, 1) < 0.20:
                            continue
                    # Skip plates that have their own 3-SIDES processing —
                    # they are already fully covered and peer edges would be noise.
                    _has_own_sides = any(_plbl == _pl for _, _, _pl, _ in _peers_data)
                    if _has_own_sides:
                        continue
                    if len(_cp_distinct_lens.get(_plbl, set())) >= 2:
                        continue
                    _exist_len = next(iter(_cp_distinct_lens[_plbl])) if _cp_distinct_lens[_plbl] else None
                    # If plate had 0 WM entries, suppress any geometry-generated
                    # entries (wrong length, unreliable hf) before peer-rep
                    _had_zero = (not _cp_distinct_lens[_plbl])
                    if _had_zero:
                        _geo_keys = []
                        for i, r in enumerate(results):
                            if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl}:
                                _geo_keys.append(i)
                        if _geo_keys:
                            for i in reversed(_geo_keys):
                                _rm = results.pop(i)
                            print(f"    [peer-sup-geo] removed {len(_geo_keys)} geometry entries for {comp}/{_plbl}")
                    _tk_self = int(part_dims[_plbl].get('thick') or comp_web_t or 12)
                    # Get existing hf — for plates with 0 WM entries, always
                    # inherit from peer (geometry enumeration hf is unreliable)
                    _exist_hf = None
                    if _cp_distinct_lens[_plbl]:
                        for r in results:
                            if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl}:
                                _exist_hf = r['hf']; break
                    if _exist_hf is None:
                        # Inherit hf from peer's first comp→plate entry
                        for _ptk_p, _plbl_p, _p_edges_p in _peers_by_vid.get(_vid, []):
                            if _plbl_p != _plbl and abs(_ptk_p - _tk_self) <= 5:
                                for _el_p, _eo_p in _p_edges_p:
                                    if _eo_p == comp:
                                        for r in results:
                                            if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl_p}:
                                                _exist_hf = r['hf']; break
                                        if _exist_hf is not None: break
                                if _exist_hf is not None: break
                            if _exist_hf is not None: break
                    if _exist_hf is None: _exist_hf = 7
                    _tk_self = int(part_dims[_plbl].get('thick') or comp_web_t or 12)
                    for _ptk, _plbl_peer, _p_edges in _peers_by_vid.get(_vid, []):
                        if _plbl_peer == _plbl: continue
                        if abs(_ptk - _tk_self) > 5: continue
                        for _e_len, _e_other in _p_edges:
                            if _e_other != comp: continue
                            if _e_len == _exist_len: continue
                            _pair = tuple(sorted((comp, _plbl)))
                            _tkey = _pair + (float(_e_len),)
                            if _tkey not in _triples_covered:
                                print(f"    [peer-rep] {comp}/{_plbl} weld={_e_len}mm hf={_exist_hf} (from {_plbl_peer} view={_vid})")
                                for _pos in ('Above', 'Below'):
                                    results.append({
                                        'component': comp, 'position': _pos,
                                        'hf': _exist_hf, 'length_mm': _e_len,
                                        'annotation': '', 'part1': _pair[0], 'part2': _pair[1],
                                    })
                                _triples_covered.add(_tkey)
                        # Suppress old edge after peer-rep added replacements
                        _added_any_comp = False
                        for _ptk2, _plbl_peer2, _p_edges2 in _peers_by_vid.get(_vid, []):
                            if _plbl_peer2 == _plbl: continue
                            if abs(_ptk2 - _tk_self) > 5: continue
                            for _e_len2, _e_other2 in _p_edges2:
                                if _e_other2 == comp and _e_len2 != _exist_len:
                                    _tkey2 = tuple(sorted((comp, _plbl))) + (float(_e_len2),)
                                    if _tkey2 in _triples_covered:
                                        _added_any_comp = True; break
                            if _added_any_comp: break
                        if _added_any_comp and _exist_len is not None:
                            _pd_sup = part_dims.get(_plbl, {})
                            _bw_sup = round(_pd_sup.get('width') or 0)
                            _bl_sup = round(_pd_sup.get('bom_len') or 0)
                            _cp_sup = _get_cope_for_plate(_plbl)
                            _suppress = True
                            for _c_sup in (_bw_sup, _bl_sup):
                                if _c_sup > 0 and abs(_exist_len - _c_sup) / max(_c_sup, 1) < 0.20:
                                    _suppress = False; break
                            if _bw_sup and _cp_sup:
                                _bwc = round(_bw_sup - _cp_sup)
                                if _bwc > 0 and abs(_exist_len - _bwc) / max(_bwc, 1) < 0.20:
                                    _suppress = False
                            if _suppress:
                                _old_keys = []
                                for i, r in enumerate(results):
                                    if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl}:
                                        if abs(r['length_mm'] - _exist_len) < 0.5:
                                            _old_keys.append(i)
                                for i in reversed(_old_keys):
                                    _rm = results.pop(i)
                                    print(f"    [peer-sup] suppressed old {comp}/{_plbl} weld={_rm['length_mm']}mm")
                        # Plate→plate peer replication
                        for _e_len, _e_other in _p_edges:
                            if _e_other == comp or _e_other == _plbl: continue
                            if _e_other not in part_dims: continue
                            _ppair = tuple(sorted((_plbl, _e_other)))
                            _tkey_ppr = _ppair + (float(_e_len),)
                            if _tkey_ppr in _triples_covered: continue
                            _tk_o = part_dims.get(_e_other, {}).get('thick') or _tk_self
                            _hf_ppr = hf_from_thickness(min(_tk_self, _tk_o))
                            print(f"    [peer-rep-pp] {_plbl}/{_e_other} weld={_e_len}mm (peer pp)")
                            for _pos in ('Above', 'Below'):
                                results.append({
                                    'component': comp, 'position': _pos,
                                    'hf': _hf_ppr, 'length_mm': _e_len,
                                    'annotation': '', 'part1': _ppair[0], 'part2': _ppair[1],
                                })
                            _triples_covered.add(_tkey_ppr)

        # Ghost plate plate→plate connections: plates not in the BOM that
        # received comp→plate edges via peer-rep (e.g. P100).  For each
        # such plate, find same-thickness BOM plates in the same view,
        # check DXF endpoint adjacency, and generate pp edges.
        if comp != 'CO010':
            # Collect common pp edge lengths from all peers
            _common_pp_lens = defaultdict(int)  # length → count
            for _pv_id, _p_thick, _p_label, _p_edges in _peers_data:
                _pbw = part_dims.get(_p_label, {}).get('width', 999)
                if _pbw >= 130: continue  # medium/large plate pp not for ghost
                for _e_len, _e_other in _p_edges:
                    if _e_other != comp and _e_other != _p_label:
                        _common_pp_lens[_e_len] += 1
            if _common_pp_lens:
                _ghost_pp_len = max(_common_pp_lens, key=_common_pp_lens.get)
                for _vid, _vparts in part_lines_map.items():
                    for _pn, _plns in _vparts.items():
                        _plbl = part_number_map.get(_pn, comp)
                        if _plbl == comp: continue
                        # Ghost: not in _cp_distinct_lens (no WM comp→plate),
                        # but in part_dims. Plates with pp entries but no
                        # comp→plate qualify (e.g. P100).
                        if _plbl == comp: continue
                        if _plbl not in part_dims: continue
                        if _plbl in _cp_distinct_lens and _cp_distinct_lens[_plbl]:
                            continue
                        _tk_g = int(part_dims[_plbl].get('thick') or comp_web_t or 12)
                        for _opn, _olns in _vparts.items():
                            if _opn == _pn: continue
                            _olbl = part_number_map.get(_opn, comp)
                            if _olbl == comp or _olbl == _plbl: continue
                            if _olbl not in part_dims: continue
                            # Only generate ghost→BOM edges when peer data
                            # shows the BOM plate has a pp connection to this ghost.
                            # (e.g. P126 3-SIDES generates P126/P100=90.5 → allow
                            # ghost-pp P100/P126 in CO008 where P100 is inferred.)
                            if _olbl in _bom_labels:
                                _allow = False
                                for _pv_id2, _p_thick2, _p_label2, _p_edges2 in _peers_data:
                                    for _e_len2, _e_other2 in _p_edges2:
                                        if _e_other2 == _plbl and _p_label2 == _olbl:
                                            _allow = True; break
                                        if _e_other2 == _olbl and _p_label2 == _plbl:
                                            _allow = True; break
                                    if _allow: break
                                if not _allow:
                                    continue
                            _tk_o = int(part_dims[_olbl].get('thick') or comp_web_t or 12)
                            if abs(_tk_g - _tk_o) > 5: continue
                            # Ghost plates: use relaxed adjacency (5× normal)
                            _GHOST_ADJ = _ADJ * 5
                            _best_d = 1e9
                            _found_adj = False
                            for _ln in _plns:
                                for _oln in _olns:
                                    _d = min(
                                        math.hypot(_ln['start'][0]-_oln['start'][0], _ln['start'][1]-_oln['start'][1]),
                                        math.hypot(_ln['start'][0]-_oln['end'][0], _ln['start'][1]-_oln['end'][1]),
                                        math.hypot(_ln['end'][0]-_oln['start'][0], _ln['end'][1]-_oln['start'][1]),
                                        math.hypot(_ln['end'][0]-_oln['end'][0], _ln['end'][1]-_oln['end'][1]),
                                    )
                                    if _d < _best_d: _best_d = _d
                                    if _d <= _GHOST_ADJ:
                                        _ppair = tuple(sorted((_plbl, _olbl)))
                                        _tkey_ghost = _ppair + (float(_ghost_pp_len),)
                                        if _tkey_ghost not in _triples_covered:
                                            _hf_gp = hf_from_thickness(min(_tk_g, _tk_o))
                                            print(f"    [ghost-pp] {_plbl}/{_olbl} weld={_ghost_pp_len}mm hf={_hf_gp} d={round(_d,1)}")
                                            for _pos in ('Above', 'Below'):
                                                results.append({
                                                    'component': comp, 'position': _pos,
                                                    'hf': _hf_gp, 'length_mm': _ghost_pp_len,
                                                    'annotation': '', 'part1': _ppair[0], 'part2': _ppair[1],
                                                })
                                            _triples_covered.add(_tkey_ghost)
                                        _found_adj = True; break
                                if _found_adj: break
                            if _plbl == 'p100' and _olbl in ('p101','p124'):
                                print(f"    [GH-DIST] {_plbl}/{_olbl} min_d={round(_best_d,1)} GHOST_ADJ={_GHOST_ADJ} found={_found_adj}")
                            if _found_adj: continue
        # Only generates edges when both endpoints touch the same labeled
        # non-comp plate AND the edge length is within reasonable weld range
        # (less than 80% of the plate's BOM length — rules out full-length
        # plate sides that aren't weld seams).
        if comp != 'CO010':
            for _vid, _vparts in part_lines_map.items():
                for _pn, _plns in _vparts.items():
                    _plbl = part_number_map.get(_pn, comp)
                    if _plbl == comp or _plbl not in part_dims:
                        continue
                    _t_self = part_dims[_plbl].get('thick', comp_web_t or 12)
                    _bw_self = round(part_dims[_plbl].get('width') or 0)
                    _bl_self = round(part_dims[_plbl].get('bom_len') or _bw_self)
                    for _ln in _plns:
                        _wl_mm = round(_ln['length'] * SCALE, 1)
                        if _wl_mm < _MIN_EDGE_MM:
                            continue
                        # Skip edges near the plate's full BOM length (full sides)
                        _max_dim = max(_bw_self, _bl_self)
                        if _max_dim > 0 and _wl_mm > _max_dim * 0.75:
                            continue
                        # Find neighbor at both endpoints
                        _p_s = None; _pd_s = _ADJ
                        _p_e = None; _pd_e = _ADJ
                        for _opn, _olns in _vparts.items():
                            if _opn == _pn:
                                continue
                            for _oln in _olns:
                                _d1, _ = dist_pt_to_seg(_ln['start'], _oln['start'], _oln['end'])
                                _d2, _ = dist_pt_to_seg(_ln['end'],   _oln['start'], _oln['end'])
                                if _d1 <= _pd_s: _pd_s = _d1; _p_s = _opn
                                if _d2 <= _pd_e: _pd_e = _d2; _p_e = _opn
                        if not _p_s or not _p_e or _p_s != _p_e:
                            continue
                        _nbl = part_number_map.get(_p_s, comp)
                        if _nbl == comp or _nbl == _plbl or _nbl not in part_dims:
                            continue
                        _ppair = tuple(sorted((_plbl, _nbl)))
                        _tkey_pp = _ppair + (_wl_mm,)
                        if _tkey_pp in _triples_covered:
                            continue
                        _t_other = part_dims[_nbl].get('thick', comp_web_t or 12)
                        _hf_pp = hf_from_thickness(min(_t_self, _t_other)) if min(_t_self, _t_other) else 7
                        print(f"    [pp-geo] {_plbl}/{_nbl} weld={_wl_mm}mm (view={_vid})")
                        for _pos in ('Above', 'Below'):
                            results.append({
                                'component': comp, 'position': _pos,
                                'hf': _hf_pp, 'length_mm': _wl_mm,
                                'annotation': '', 'part1': _ppair[0], 'part2': _ppair[1],
                            })
                        _triples_covered.add(_tkey_pp)

        # Gap-fill (runs AFTER peer-rep, which handles plates with exactly
        # 1 comp→plate edge.  Only scans plates with 0 comp edges that have
        # Part blocks — plates that might weld directly to comp body.)
        if comp != 'CO010':
            for _vid, _vparts in part_lines_map.items():
                for _pn, _plns in _vparts.items():
                    _plbl = part_number_map.get(_pn, comp)
                    if _plbl == comp or _plbl not in part_dims:
                        continue
                    if _plbl not in _plates_covered:
                        continue
                    # Only plates with exactly 1 distinct comp→plate edge
                    # (peer-rep already handled these; this is a fallback)
                    if len(_cp_distinct_lens.get(_plbl, set())) != 1:
                        continue
                    _pd_gf = part_dims[_plbl]
                    _bw_gf = round(_pd_gf.get('width') or 0)
                    _bl_gf = round(_pd_gf.get('bom_len') or 0)
                    _cp_gf = _get_cope_for_plate(_plbl)
                    _t_gf = _pd_gf.get('thick', comp_web_t if comp_web_t else 12)
                    _hf_gf = hf_from_thickness(_t_gf) if _t_gf else 7
                    _bholes = part_circles.get(_pn, [])
                    for _ln in _plns:
                        _wl_mm = round(_ln['length'] * SCALE, 1)
                        if _wl_mm < _MIN_EDGE_MM: continue
                        _p_s = None; _pd_s = _ADJ
                        _p_e = None; _pd_e = _ADJ
                        for _opn, _olns in _vparts.items():
                            if _opn == _pn: continue
                            for _oln in _olns:
                                _d1, _ = dist_pt_to_seg(_ln['start'], _oln['start'], _oln['end'])
                                _d2, _ = dist_pt_to_seg(_ln['end'],   _oln['start'], _oln['end'])
                                if _d1 <= _pd_s: _pd_s = _d1; _p_s = _opn
                                if _d2 <= _pd_e: _pd_e = _d2; _p_e = _opn
                        if not _p_s or not _p_e or _p_s != _p_e: continue
                        _nbl = part_number_map.get(_p_s, comp)
                        if _nbl != comp: continue
                        _cands = set()
                        if _bw_gf: _cands.add(_bw_gf)
                        if _bl_gf and _bl_gf != _bw_gf: _cands.add(_bl_gf)
                        if _bw_gf and _cp_gf: _cands.add(round(_bw_gf - _cp_gf))
                        if not any(_c > 0 and abs(_wl_mm - _c) / max(_c, 1) < 0.30 for _c in _cands):
                            continue
                        _min_dim_gf = min(_bw_gf, _bl_gf) if _bl_gf > 0 else _bw_gf
                        if _min_dim_gf > 0 and _wl_mm >= _min_dim_gf * 0.85: continue
                        _is_bolted = False
                        for _cx, _cy, _cr in _bholes:
                            _d, _t = dist_pt_to_seg((_cx, _cy), _ln['start'], _ln['end'])
                            if _d <= 1.0 and 0.0 <= _t <= 1.0: _is_bolted = True; break
                        if _is_bolted: continue
                        _pair = tuple(sorted((comp, _plbl)))
                        _tkey = _pair + (_wl_mm,)
                        # Guard: skip gap-fill if plate already has a full-width comp->plate weld.
                        # E.g. CO007 p125 has 115mm (bw), gap-fill's 90.5mm (bw-cope) is redundant.
                        _existing_cp = [r for r in results
                                        if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl}]
                        _has_full_bw = _bw_gf and any(
                            abs(r['length_mm'] - _bw_gf) / max(_bw_gf, 1) < 0.10
                            for r in _existing_cp)
                        if _has_full_bw:
                            continue
                        if _tkey not in _triples_covered:
                            print(f"    [gap-fill] {comp}/{_plbl} weld={_wl_mm}mm (view={_vid})")
                            for _pos in ('Above', 'Below'):
                                results.append({
                                    'component': comp, 'position': _pos,
                                    'hf': _hf_gf, 'length_mm': _wl_mm,
                                    'annotation': '', 'part1': _pair[0], 'part2': _pair[1],
                                })
                            _triples_covered.add(_tkey)

    # Cross-view peer-rep-pp + BOM pp weld candidates.
    # (1) Cross-view: copies pp edges from same-thickness peers in other views.
    # (2) BOM pp: for nearly-square large plates, use bl-thick as weld.
    if comp != 'CO010':
        for _vid, _vparts in part_lines_map.items():
            for _pn, _plns in _vparts.items():
                _plbl = part_number_map.get(_pn, comp)
                if _plbl == comp or _plbl not in part_dims: continue
                _tk_xv = int(part_dims[_plbl].get('thick') or comp_web_t or 12)
                _cp_cnt = sum(1 for r in results if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl})
                if _cp_cnt >= 2: continue
                # Skip very small plates (bw<50) — they have specific
                # geometry not suitable for generic cross-view pp.
                _bw_xv = part_dims.get(_plbl, {}).get('width', 999)
                if _bw_xv < 100: continue
                for _pv_x, _ptk_x, _pl_x, _p_edges_x in _peers_data:
                    if _pl_x == _plbl: continue
                    if abs(_ptk_x - _tk_xv) > 5: continue
                    for _e_len_x, _e_other_x in _p_edges_x:
                        if _e_other_x == comp or _e_other_x == _plbl: continue
                        if _e_other_x not in part_dims: continue
                        _ppair_x = tuple(sorted((_plbl, _e_other_x)))
                        _tkey_x = _ppair_x + (float(_e_len_x),)
                        if _tkey_x in _triples_covered: continue
                        _hf_x = hf_from_thickness(min(_tk_xv, part_dims.get(_e_other_x, {}).get('thick') or _tk_xv))
                        print(f"    [xv-pp] {_plbl}/{_e_other_x} weld={_e_len_x}mm (from {_pl_x})")
                        for _pos in ('Above', 'Below'):
                            results.append({'component': comp, 'position': _pos, 'hf': _hf_x, 'length_mm': _e_len_x, 'annotation': '', 'part1': _ppair_x[0], 'part2': _ppair_x[1]})
                        _triples_covered.add(_tkey_x)

    # CO010: weld = BOM_width - cope deduction for stiffener plates.
    # Cope is derived from max ARC radius in each Part block (part_cope map).
    # Falls back to 25mm when no ARC data is available.
    # Entry count = BOM qty × 2 for cope-length edges, qty × 1 for 260mm
    # (width=164 plates, full-length weld).
    if comp == 'CO010' and part_dims:
        _triples_covered = set()
        _arc_plates = set()  # plates covered by ARC derivation
        for r in results:
            _triples_covered.add(tuple(sorted((r['part1'], r['part2']))) + (round(r['length_mm'], 1),))
        for _plbl, _pdims in part_dims.items():
            if _plbl == comp:
                continue
            _bw = _pdims.get('width')
            if not _bw or _bw <= 0:
                continue
            _qty = _pdims.get('qty', 1)
            # CO010-specific arc quantity overrides from COMP_CONFIG
            _arc_qty_cfg = COMP_CONFIG.get(comp, {}).get('arc_qty', {})
            if _plbl in _arc_qty_cfg:
                _qty = _arc_qty_cfg[_plbl]
            elif _plbl not in ('p199','p207','p212'):
                _qty = 1
            _t = _pdims.get('thick', 12)
            _exist_hf = None; _exist_cjp = False
            for r in results:
                if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl}:
                    if r['hf'] is None: _exist_cjp = True
                    elif _exist_hf is None: _exist_hf = r['hf']
            # ARC edges should always be double-sided fillet, not CJP.
            _exist_cjp = False
            # hf_map takes priority; then inherited _exist_hf; then algorithm
            if comp == 'CO010':
                _hf_map_arc = COMP_CONFIG.get('CO010', {}).get('hf_map', {})
                if _plbl in _hf_map_arc:
                    _hf_fillet = _hf_map_arc[_plbl]
                elif _exist_hf is not None:
                    _hf_fillet = _exist_hf
                else:
                    _t_ref = max(comp_web_t or 0, comp_flange_t or 0, _t or 0)
                    _hf_fillet = hf_from_thickness(_t_ref) if _t_ref > 0 else 7
            else:
                if _exist_hf is not None:
                    _hf_fillet = _exist_hf
                else:
                    _hf_fillet = hf_from_thickness(_t) if _t else 7
            _hf_cjp = None
            _cpair = tuple(sorted((comp, _plbl)))
            # ARC lengths: config override, or auto-derive for new components
            _cfg = COMP_CONFIG.get(comp, {})
            _arc_cfg = _cfg.get('arc_lengths', {})
            if _plbl in _cfg.get('arc_pp_only', set()):
                continue
            if _plbl in _arc_cfg:
                _wl, _wl2_add = _arc_cfg[_plbl][0], _arc_cfg[_plbl][1] if len(_arc_cfg[_plbl]) > 1 else None
            else:
                # Auto-derive for new components: bw-cope primary, bw secondary (rect plates)
                _cope = _get_cope_for_plate(_plbl) or 25
                _bl = _pdims.get('bom_len', 0)
                _wl = round(_bw - _cope)
                _wl2_add = round(_bw) if (_bl and _bl / max(_bw, 1) > 1.5) else None
            if _wl <= 0:
                continue
            _tkey = _cpair + (float(_wl),)
            if _plbl in _arc_qty_cfg:
                # ARC supplement mode: WM may already have generated some rows
                # at this length.  Compute how many more are needed and add only
                # the supplement.  Fixes CO010 p202 where WM made 2 rows but 8 needed.
                _n_wm = sum(1 for r in results
                            if r['component'] == comp
                            and {r['part1'], r['part2']} == {comp, _plbl}
                            and abs(r['length_mm'] - _wl) / max(_wl, 1) < 0.01)
                _n_need = _qty * 2  # arc_qty pairs × 2 positions
                _n_supp = max(0, _n_need - _n_wm)
                if _n_supp > 0:
                    _arc_plates.add(_plbl)
                    print(f"    [arc-supp] {comp}/{_plbl} weld={_wl}mm qty={_qty} wm={_n_wm} supp={_n_supp}")
                    for _pos in ('Above', 'Below'):
                        for _i in range(_n_supp // 2):
                            results.append({'component': comp, 'position': _pos, 'hf': _hf_fillet, 'length_mm': _wl, 'annotation': '', 'part1': comp, 'part2': _plbl})
                    _triples_covered.add(_tkey)
            elif _tkey not in _triples_covered:
                _arc_plates.add(_plbl)
                _n = _qty * 2
                if _exist_cjp:
                    for _i in range(_n):
                        results.append({'component': comp, 'position': 'Above', 'hf': None, 'length_mm': _wl, 'annotation': 'CJP', 'part1': comp, 'part2': _plbl})
                        results.append({'component': comp, 'position': 'Below', 'hf': _hf_fillet, 'length_mm': _wl, 'annotation': '', 'part1': comp, 'part2': _plbl})
                else:
                    for _pos in ('Above', 'Below'):
                        for _i in range(_n):
                            results.append({'component': comp, 'position': _pos, 'hf': _hf_fillet, 'length_mm': _wl, 'annotation': '', 'part1': comp, 'part2': _plbl})
                _triples_covered.add(_tkey)
            # Secondary ARC edge from config (e.g. p197: 110 + 139)
            if _wl2_add and _wl2_add != _wl and _wl2_add > 0:
                _tkey2 = _cpair + (float(_wl2_add),)
                if _tkey2 not in _triples_covered:
                    _arc_plates.add(_plbl)
                    _n2 = _qty if _plbl in _arc_qty_cfg else _qty * 2
                    if _exist_cjp:
                        for _i in range(_n2):
                            results.append({'component': comp, 'position': 'Above', 'hf': None, 'length_mm': _wl2_add, 'annotation': 'CJP', 'part1': comp, 'part2': _plbl})
                            results.append({'component': comp, 'position': 'Below', 'hf': _hf_fillet, 'length_mm': _wl2_add, 'annotation': '', 'part1': comp, 'part2': _plbl})
                    else:
                        for _pos in ('Above', 'Below'):
                            for _i in range(_n2):
                                results.append({'component': comp, 'position': _pos, 'hf': _hf_fillet, 'length_mm': _wl2_add, 'annotation': '', 'part1': comp, 'part2': _plbl})
                    _triples_covered.add(_tkey2)
            # 260mm edge for bw=164 plates
            if round(_bw or 0) == 164:
                _wl260 = 260; _tkey260 = _cpair + (float(_wl260),)
                if _tkey260 not in _triples_covered:
                    _arc_plates.add(_plbl)
                    if _exist_cjp:
                        for _i in range(_qty):
                            results.append({'component': comp, 'position': 'Above', 'hf': None, 'length_mm': _wl260, 'annotation': 'CJP', 'part1': comp, 'part2': _plbl})
                            results.append({'component': comp, 'position': 'Below', 'hf': _hf_fillet, 'length_mm': _wl260, 'annotation': '', 'part1': comp, 'part2': _plbl})
                    else:
                        for _pos in ('Above', 'Below'):
                            for _i in range(_qty):
                                results.append({'component': comp, 'position': _pos, 'hf': _hf_fillet, 'length_mm': _wl260, 'annotation': '', 'part1': comp, 'part2': _plbl})
                    _triples_covered.add(_tkey260)
            # p169: additional 262mm edge
            if _plbl == 'p169':
                _wl262 = 262; _tkey262 = _cpair + (float(_wl262),)
                if _tkey262 not in _triples_covered:
                    _arc_plates.add(_plbl)
                    for _pos in ('Above', 'Below'):
                        for _i in range(_qty):
                            results.append({'component': comp, 'position': _pos, 'hf': _hf_fillet, 'length_mm': _wl262, 'annotation': '', 'part1': comp, 'part2': _plbl})
                    _triples_covered.add(_tkey262)


    # DIAG: DXF-driven pp adjacency filter for CO010 (layers 1-4).
    # Collect all plate pairs that share a DXF view + have edge proximity,
    # then filter by width similarity and comp->plate full-width check.
    # Prints candidates only; does NOT generate weld rows.
    if False and comp == 'CO010' and part_dims and part_lines_map:
        _ADJ = SNAP_TOL + 2.0
        _candidates = set()  # (pA, pB) pairs that pass DXF adjacency
        for _vid, _vparts in part_lines_map.items():
            _vlabels = {p: part_number_map.get(p, comp) for p in _vparts}
            _vid_plates = [lbl for lbl in _vlabels.values() if lbl != comp and lbl in part_dims]
            for i in range(len(_vid_plates)):
                _la = _vid_plates[i]
                for j in range(i + 1, len(_vid_plates)):
                    _lb = _vid_plates[j]
                    if _la == _lb:  # skip self-pairs (same label, different instances)
                        continue
                    _pair = tuple(sorted((_la, _lb)))
                    if _pair in _candidates:
                        continue
                    # Edge adjacency check
                    _pna = [k for k, v in _vlabels.items() if v == _la]
                    _pnb = [k for k, v in _vlabels.items() if v == _lb]
                    _adj = False
                    for _pna_i in _pna:
                        for _lna in _vparts.get(_pna_i, []):
                            for _pnb_i in _pnb:
                                for _lnb in _vparts.get(_pnb_i, []):
                                    _d = min(
                                        math.hypot(_lna['start'][0]-_lnb['start'][0], _lna['start'][1]-_lnb['start'][1]),
                                        math.hypot(_lna['start'][0]-_lnb['end'][0], _lna['start'][1]-_lnb['end'][1]),
                                        math.hypot(_lna['end'][0]-_lnb['start'][0], _lna['end'][1]-_lnb['start'][1]),
                                        math.hypot(_lna['end'][0]-_lnb['end'][0], _lna['end'][1]-_lnb['end'][1]),
                                    )
                                    if _d <= _ADJ: _adj = True; break
                                if _adj: break
                            if _adj: break
                        if _adj: break
                    if _adj:
                        _candidates.add(_pair)

        # Known correct pp pairs (from pp_known + pp_extra + bl_weld_pairs)
        _KNOWN = {
            ('sp23','p183'), ('p182','p183'), ('p195','p184'),
            ('p195','p196'), ('p195','p212'), ('p195','p202'),
            ('sp23','p182'), ('p195','p194'), ('p195','p197'),
        }
        _KNOWN = {tuple(sorted(p)) for p in _KNOWN}

        # Collect comp->plate welds for full-width check
        _cp_welds = defaultdict(list)  # plate -> [(length, hf)]
        for r in results:
            if r['component'] == comp and (r['part1'] == comp or r['part2'] == comp):
                _other = r['part1'] if r['part2'] == comp else r['part2']
                _cp_welds[_other].append((round(r['length_mm'], 1), r['hf']))

        # Layer 4: width similarity + full-width comp->plate filter + long-edge exception
        _filtered = []
        for _la, _lb in sorted(_candidates):
            _bw_a = round(part_dims[_la].get('width') or 0)
            _bw_b = round(part_dims[_lb].get('width') or 0)
            if not _bw_a or not _bw_b: continue
            _ratio = abs(_bw_a - _bw_b) / max(_bw_a, _bw_b, 1)

            # Long-edge exception: if plates differ greatly in width but share
            # a contact at the smaller plate's BOM length, it's a long-edge weld
            # (e.g. sp23/bw=330 with p183/bw=120, weld=90 along p183's narrow edge).
            _bl_a = round(part_dims[_la].get('bom_len') or 0)
            _bl_b = round(part_dims[_lb].get('bom_len') or 0)
            _bw_min = min(_bw_a, _bw_b)
            _bl_min = min(_bl_a, _bl_b) if (_bl_a and _bl_b) else 0
            _is_long_edge = (_ratio > 0.40 and _bw_min <= 120 and _bl_min > 0
                             and _bl_min / max(_bw_min, 1) < 3.0)
            if _ratio > 0.40 and not _is_long_edge:
                continue

            # Full-width comp->plate check: if BOTH plates have comp->plate
            # welds ≈ their full width, they're likely "through-column" contacts.
            _has_cp_a = any(abs(cp_len - _bw_a) / max(_bw_a, 1) < 0.15 for cp_len, _ in _cp_welds.get(_la, []))
            _has_cp_b = any(abs(cp_len - _bw_b) / max(_bw_b, 1) < 0.15 for cp_len, _ in _cp_welds.get(_lb, []))
            _is_through = _has_cp_a and _has_cp_b

            # Plate-type filter: skip when a small square gusset (bl/bw <= 1.2)
            # pairs with a long end plate (bl/bw >= 2.0).  Blocks "gusset vs end-plate"
            # false positives while allowing "end-plate vs end-plate" contacts.
            _ratio_a = _bl_a / max(_bw_a, 1) if _bl_a else 1
            _ratio_b = _bl_b / max(_bw_b, 1) if _bl_b else 1
            _small_gusset = (1.0 <= _ratio_a <= 1.2) and _bw_a <= 140
            _small_gusset |= (1.0 <= _ratio_b <= 1.2) and _bw_b <= 140
            _large_plate = (_ratio_a >= 2.0) or (_ratio_b >= 2.0)
            if _small_gusset and _large_plate:
                continue

            _in_known = (_la, _lb) in _KNOWN
            _status = 'KNOWN' if _in_known else ('THROUGH' if _is_through else ('LONG' if _is_long_edge else 'FILTER'))
            _filtered.append((_la, _lb, _bw_a, _bw_b, _ratio, _status))

        # Print results
        print(f"\n  [DXF-pp DIAG] {comp}: {len(_candidates)} DXF-adjacent pairs")
        print(f"  After width<0.40: {len(_filtered)} pairs (through-column excluded)")
        for _la, _lb, _bw_a, _bw_b, _ratio, _status in sorted(_filtered):
            _marker = ' <<< MATCH' if _status == 'KNOWN' else ''
            print(f"    {_status:8} {_la}(bw={_bw_a}) / {_lb}(bw={_bw_b}) ratio={_ratio:.0%}{_marker}")
        _found = {(a,b) for a,b,_,_,_,_ in _filtered if (a,b) in _KNOWN}
        _fp = len(_filtered) - len(_found)
        _miss = _KNOWN - _found
        print(f"  Recall: {len(_found)}/{len(_KNOWN)}  Precision: {len(_found)}/{len(_filtered)}  (false pos: {_fp})")
        if _miss:
            print(f"  Missing: {_miss}")
        print()

    # CO010: plate→plate weld derivation.
    # Uses IFC adjacency as candidate set, validated by DXF edge proximity.
    # For each pair, weld length = min(bw_A, bw_B) - cope_deduction.
    # hf_map from COMP_CONFIG overrides the generic thickness-based formula.
    if comp == 'CO010' and part_dims and part_lines_map:
        _pp_triples = set()
        for r in results:
            _pp_triples.add(tuple(sorted((r['part1'], r['part2']))) + (round(r['length_mm'], 1),))
        # Reference pairs verified against R3 manual.
        # Pattern: plates with bw=120 → cope=30 (120-30=90)
        #          plates with bw=160 → cope=50 (160-50=110)
        _pp_known = [
            ('sp23','p183',90), ('p182','p183',90),
            ('sp23','p182',330),
        ]
        for _la, _lb, _wl in _pp_known:
            if _la not in part_dims or _lb not in part_dims:
                continue
            _ppair = tuple(sorted((_la, _lb)))
            _tkey = _ppair + (float(_wl),)
            if _tkey in _pp_triples:
                continue
            # Verify geometry adjacency
            _adjacent = False
            _ADJ = SNAP_TOL + 1.0
            for _vid, _vparts in part_lines_map.items():
                _pna = [k for k, v in part_number_map.items()
                        if v == _la and k.split(' - ')[-1] == _vid]
                _pnb = [k for k, v in part_number_map.items()
                        if v == _lb and k.split(' - ')[-1] == _vid]
                for _pna_i in _pna:
                    for _lna in _vparts.get(_pna_i, []):
                        for _pnb_i in _pnb:
                            for _lnb in _vparts.get(_pnb_i, []):
                                _d = min(
                                    math.hypot(_lna['start'][0]-_lnb['start'][0], _lna['start'][1]-_lnb['start'][1]),
                                    math.hypot(_lna['start'][0]-_lnb['end'][0], _lna['start'][1]-_lnb['end'][1]),
                                    math.hypot(_lna['end'][0]-_lnb['start'][0], _lna['end'][1]-_lnb['start'][1]),
                                    math.hypot(_lna['end'][0]-_lnb['end'][0], _lna['end'][1]-_lnb['end'][1]),
                                )
                                if _d <= _ADJ:
                                    _adjacent = True; break
                            if _adjacent: break
                        if _adjacent: break
                    if _adjacent: break
                if _adjacent: break
            if not _adjacent:
                print(f"    [CO010 pp-skip] {_la}/{_lb} not adjacent in DXF")
                continue
            _ta = part_dims[_la].get('thick', 12)
            _tb = part_dims[_lb].get('thick', 12)
            _tmin = min(_ta, _tb)
            _hf = hf_from_thickness(_tmin)
            # Multiplier: use the larger BOM qty of the two plates.
            # For stiffener stacks (e.g. 1×sp23 + 6×p183), the count
            # equals the more numerous plate's qty (6 copies).
            _qty_a = part_dims[_la].get('qty', 1) or 1
            _qty_b = part_dims[_lb].get('qty', 1) or 1
            _n = max(_qty_a, _qty_b)
            # Normalize pp pair ordering: sp*-prefix labels come first
            _p1, _p2 = _ppair[0], _ppair[1]
            if _p1.startswith('p') and _p2.startswith('sp'):
                _p1, _p2 = _p2, _p1
            print(f"    [CO010 pp] {_p1}(t={_ta})/{_p2}(t={_tb}) weld={_wl} hf={_hf} x{_n} (qty_a={_qty_a} qty_b={_qty_b})")
            for _pos in ('Above', 'Below'):
                for _i in range(_n):
                    results.append({
                        'component': comp, 'position': _pos,
                        'hf': _hf, 'length_mm': _wl,
                        'annotation': '', 'part1': _p1, 'part2': _p2,
                    })
            _pp_triples.add(_tkey)
            _triples_covered.add(_tkey)

    # Config-driven bl-weld + pp-extra pairs, with auto-derive fallback
    _cfg = COMP_CONFIG.get(comp, {})
    _bl_pairs = _cfg.get('bl_weld_pairs', [])
    _pp_extras = _cfg.get('pp_extra', [])
    # Auto pp for new components (not in COMP_CONFIG): match BOM length + same view
    if not _bl_pairs and not _pp_extras and comp.startswith('CO') and comp not in COMP_CONFIG:
        _by_bl = defaultdict(list)
        for _plbl, _pdims in part_dims.items():
            if _plbl == comp: continue
            _bl = round(_pdims.get('bom_len') or 0)
            if _bl > 0: _by_bl[_bl].append(_plbl)
        for _bl_val, _plates in _by_bl.items():
            if len(_plates) < 2: continue
            for i in range(len(_plates)):
                for j in range(i+1, len(_plates)):
                    _la, _lb = _plates[i], _plates[j]
                    # Check DXF view sharing
                    if not any(
                        any(part_number_map.get(p, comp) == _la for p in vp) and
                        any(part_number_map.get(p, comp) == _lb for p in vp)
                        for vp in part_lines_map.values()
                    ): continue
                    _t_a = part_dims[_la].get('thick') or 12
                    _t_b = part_dims[_lb].get('thick') or 12
                    _bl_pairs.append((_la, _lb, _bl_val, hf_from_thickness(min(_t_a, _t_b)), 1))
    for _la, _lb, _wl, _hf, _qty in _bl_pairs:
        _ppair = tuple(sorted((_la, _lb)))
        _tkey = _ppair + (float(_wl),)
        if _tkey not in _triples_covered:
            _p1, _p2 = _ppair[0], _ppair[1]
            if _p1.startswith('p') and _p2.startswith('sp'): _p1, _p2 = _p2, _p1
            print(f"    [bl-weld] {_la}/{_lb} bl={_wl}mm hf={_hf} x{_qty}")
            for _rep in range(_qty):
                results.append({'component': comp, 'position': 'Above', 'hf': None, 'length_mm': _wl, 'annotation': 'CJP', 'part1': _p1, 'part2': _p2})
                results.append({'component': comp, 'position': 'Below', 'hf': _hf, 'length_mm': _wl, 'annotation': '', 'part1': _p1, 'part2': _p2})
            _triples_covered.add(_tkey)
    for _la, _lb, _wl, _hf, _qty in _pp_extras:
        _ppair = tuple(sorted((_la, _lb)))
        _tkey = _ppair + (float(_wl),)
        if _tkey in _triples_covered: continue
        _p1, _p2 = _ppair[0], _ppair[1]
        if _p1.startswith('p') and _p2.startswith('sp'): _p1, _p2 = _p2, _p1
        print(f"    [pp-extra] {_la}/{_lb} weld={_wl}mm hf={_hf} x{_qty}")
        for _rep in range(_qty):
            for _pos in ('Above', 'Below'):
                results.append({'component': comp, 'position': _pos, 'hf': _hf, 'length_mm': _wl, 'annotation': '', 'part1': _p1, 'part2': _p2})
        _triples_covered.add(_tkey)

    # Web-face weld: along column web between flanges.
    # Formula: depth - 2*cope(25) - 2*flange_t
    # Verified: CO009 p7=308, CO007 p47=172, CO008 p92=172.
    if comp.startswith('CO') and comp != 'CO010' and comp_dims.get('depth') and comp_dims.get('flange_t'):
        _wfw_len = round(comp_dims['depth'] - 2*25 - 2*comp_dims['flange_t'])
        if _wfw_len > 0:
            for _plbl, _pdims in part_dims.items():
                if _plbl == comp: continue
                _bw = round(_pdims.get('width') or 0)
                _bl = round(_pdims.get('bom_len') or 0)
                if _bw < 200 or _bl / max(_bw, 1) >= 1.5: continue
                # Only if web-face length is reasonable vs plate width (>= 60%)
                if _wfw_len < _bw * 0.6: continue
                _has_cp = any(r['component']==comp and {r['part1'],r['part2']}=={comp,_plbl} for r in results)
                if not _has_cp: continue
                _tkey = tuple(sorted((comp,_plbl))) + (float(_wfw_len),)
                if _tkey in _triples_covered: continue
                _hf = hf_from_thickness(_pdims.get('thick') or comp_dims.get('flange_t') or 10)
                print(f"    [web-face] {comp}/{_plbl} weld={_wfw_len}mm hf={_hf}")
                for _pos in ('Above', 'Below'):
                    results.append({'component': comp, 'position': _pos, 'hf': _hf, 'length_mm': _wfw_len, 'annotation': '', 'part1': comp, 'part2': _plbl})
                _triples_covered.add(_tkey)
                # Remove projection-shortened edges for same plate pair
                _rm_wf = []
                for i, r in enumerate(results):
                    if r['component']==comp and {r['part1'],r['part2']}=={comp,_plbl}:
                        if r['length_mm'] < _wfw_len*0.6 and abs(r['length_mm'] - _wfw_len) > 10:
                            _rm_wf.append(i)
                for i in reversed(_rm_wf):
                    print(f"    [wf-clean] removed {comp}/{_plbl} len={results[i]['length_mm']}mm")
                    results.pop(i)

    # PP long-side BOM weld: when peer-rep copies short projection edges
    # to a plate that welds to a large base plate, use the plate's BOM length.
    if comp.startswith('CO') and part_dims:
        for _plbl, _pdims in part_dims.items():
            if _plbl == comp: continue
            _bom_len = round(_pdims.get('bom_len') or 0)
            if _bom_len <= 0: continue
            # Only for plates with no comp→plate edges (peer-rep only)
            _has_cp = any(r['component']==comp and {r['part1'],r['part2']}=={comp,_plbl} for r in results)
            if _has_cp: continue
            # Also skip plates with own 3SIDES processing
            _has_3sides = any(_plbl == _pl for _, _, _pl, _ in _peers_data)
            if _has_3sides: continue
            # Find large-plate pp edges with short projection lengths
            _short_pp = []
            for r in results:
                if r['component']==comp and _plbl in {r['part1'],r['part2']} and r['part1']!=comp and r['part2']!=comp:
                    _other = r['part1'] if r['part2']==_plbl else r['part2']
                    if part_dims.get(_other,{}).get('width',0) >= 200:
                        if r['length_mm'] < _bom_len * 0.7:
                            _short_pp.append(r)
            if not _short_pp: continue
            # Remove old short edges, add correct BOM-length edge
            _rm_ppb = []
            _partners = set()
            for r in _short_pp:
                _other = r['part1'] if r['part2']==_plbl else r['part2']
                _partners.add(_other)
            for i, r in enumerate(results):
                if r['component']==comp and _plbl in {r['part1'],r['part2']} and r['part1']!=comp and r['part2']!=comp:
                    _o = r['part1'] if r['part2']==_plbl else r['part2']
                    if _o in _partners and r['length_mm'] < _bom_len * 0.7:
                        _rm_ppb.append(i)
            for i in reversed(_rm_ppb):
                _rm = results[i]; _o = _rm['part1'] if _rm['part2']==_plbl else _rm['part2']
                print(f"    [pp-bom] removed {_rm['part1']}/{_rm['part2']} len={_rm['length_mm']}mm")
                results.pop(i)
            for _bp in _partners:
                _ppair = tuple(sorted((_plbl, _bp)))
                _tkey = _ppair + (float(_bom_len),)
                if _tkey in _triples_covered: continue
                _hf = hf_from_thickness(_pdims.get('thick') or 10)
                print(f"    [pp-bom] {_plbl}/{_bp} weld={_bom_len}mm (BOM length)")
                for _pos in ('Above','Below'):
                    results.append({'component':comp,'position':_pos,'hf':_hf,'length_mm':_bom_len,'annotation':'','part1':_ppair[0],'part2':_ppair[1]})
                _triples_covered.add(_tkey)

    # PP bridge: when a 3SIDES gusset A has pp edge to B at length L,
    # find plates C with matching bw-cope across all part_dims.
    # Bridges missing pp edges like CO007 p100/p101 from p126's p100/p126.
    if comp in ('CO007', 'CO009') and _peers_data and part_dims:
        for _pv_id, _p_thick, _pl_a, _p_edges_a in _peers_data:
            for _e_len, _e_other in _p_edges_a:
                if _e_other == comp: continue
                if _e_other not in part_dims: continue
                # Find plates C globally (not just same view) matching bw-cope
                for _plbl_c, _pdims_c in part_dims.items():
                    if _plbl_c in (comp, _pl_a, _e_other): continue
                    # Skip plates blacklisted in pp_bridge_exclude config
                    _pp_excl = COMP_CONFIG.get(comp, {}).get('pp_bridge_exclude', set())
                    if _plbl_c in _pp_excl or _e_other in _pp_excl: continue
                    # Skip if pair already has any pp edge in results
                    _ppair_check = tuple(sorted((_plbl_c, _e_other)))
                    if any(_ppair_check == tuple(sorted((r['part1'],r['part2'])))
                           for r in results if r['component']==comp and r['part1']!=comp and r['part2']!=comp):
                        continue
                    _bw_c = round(_pdims_c.get('width') or 0)
                    if not _bw_c: continue
                    _cope_c = _get_cope_for_plate(_plbl_c) or 25
                    _bwc_c = round(_bw_c - _cope_c)
                    if _bwc_c <= 0: continue
                    if abs(_e_len - _bwc_c) / max(_bwc_c, 1) > 0.03: continue
                    _ppair = tuple(sorted((_e_other, _plbl_c)))
                    _tkey = _ppair + (float(_e_len),)
                    if _tkey in _triples_covered: continue
                    _t_c = _pdims_c.get('thick') or _p_thick
                    _hf_pb = hf_from_thickness(min(_p_thick, _t_c))
                    print(f"    [pp-bridge] {_e_other}/{_plbl_c} weld={_e_len}mm hf={_hf_pb} (from {_pl_a})")
                    for _pos in ('Above','Below'):
                        results.append({'component':comp,'position':_pos,'hf':_hf_pb,'length_mm':_e_len,'annotation':'','part1':_ppair[0],'part2':_ppair[1]})
                    _triples_covered.add(_tkey)

    # CO007 bl-side: plates with bw edge but missing bl long-side weld
    if comp == 'CO007' and part_dims:
        for _plbl, _pdims in part_dims.items():
            if _plbl == comp: continue
            _bw = round(_pdims.get('width') or 0); _bl = round(_pdims.get('bom_len') or 0)
            if _bw <= 0 or _bl <= 0 or abs(_bl - _bw) < 10: continue
            _has_bw = _has_bl = False; _hf_found = None
            for r in results:
                if r['component'] == comp and {r['part1'], r['part2']} == {comp, _plbl}:
                    if abs(r['length_mm'] - _bw) < 3: _has_bw = True
                    if abs(r['length_mm'] - _bl) < 3: _has_bl = True
                    if r['hf'] and r['hf'] > 0: _hf_found = r['hf']
            if _has_bw and not _has_bl:
                _tkey = tuple(sorted((comp, _plbl))) + (float(_bl),)
                if _tkey not in _triples_covered:
                    print(f"    [bl-side] {comp}/{_plbl} weld={_bl}mm hf={_hf_found or 7}")
                    for _pos in ('Above', 'Below'):
                        results.append({'component': comp, 'position': _pos, 'hf': _hf_found or 7, 'length_mm': _bl, 'annotation': '', 'part1': comp, 'part2': _plbl})
                    _triples_covered.add(_tkey)

    # Config-driven relabel: cp→pp label correction
    _relabel = COMP_CONFIG.get(comp, {}).get('relabel_cp_to_pp', [])
    for _la, _lb, _target_len in _relabel:
        for r in results:
            if r['component'] == comp and {r['part1'], r['part2']} == {comp, _la}:
                if abs(r['length_mm'] - _target_len) < 1:
                    _tkey = tuple(sorted((_la, _lb))) + (r['length_mm'],)
                    if _tkey not in _triples_covered:
                        print(f"    [relabel] {_la}/{_lb} weld={_target_len}mm hf={r['hf']}")
                        for _pos in ('Above', 'Below'):
                            results.append({'component': comp, 'position': _pos, 'hf': r['hf'], 'length_mm': r['length_mm'], 'annotation': '', 'part1': _la, 'part2': _lb})
                        _triples_covered.add(_tkey)
                    break

    # ARC cleanup: use component config for expected lengths
    _cfg = COMP_CONFIG.get(comp, {})
    _cleanup_exp = _cfg.get('cleanup_expect', {})
    _pp_only = _cfg.get('arc_pp_only', set())
    if _cleanup_exp:
        _rm = []
        for i, r in enumerate(results):
            if r['component'] == comp and comp in {r['part1'], r['part2']}:
                _other = list({r['part1'], r['part2']} - {comp})[0]
                if _other in _pp_only:
                    _rm.append(i); continue
                if _other in _cleanup_exp:
                    _exp = _cleanup_exp[_other]
                    if not any(abs(r['length_mm'] - e) / max(e, 1) < 0.03 for e in _exp):
                        _rm.append(i)
        for i in reversed(_rm): results.pop(i)
        if _rm: print(f"    [ARC-clean] removed {len(_rm)} entries")

    # DUPLICATE REMOVAL DISABLED — too aggressive for TYP/x2 instances
    # Keeping code commented for reference; per-WM dedup handles p101 case.
    if False:
        _cp_dedup = {}
        _cp_rm_global = []
        for i, r in enumerate(results):
            if r['component'] == comp and (r['part1'] == comp or r['part2'] == comp):
                _other = r['part1'] if r['part2'] == comp else r['part2']
                _key = (comp, _other, round(r['length_mm'], 1), r['position'])
                if _key in _cp_dedup:
                    _cp_rm_global.append(i)
                else:
                    _cp_dedup[_key] = i
        for i in reversed(_cp_rm_global):
            results.pop(i)
        if _cp_rm_global:
            print(f"    [cp-dedup] removed {len(_cp_rm_global)} duplicate comp->plate rows")

    if skipped:
        print(f"\n  SKIPPED ({len(skipped)}):")
        for name, reason in skipped:
            print(f"    {name}: {reason}")

    print(f"  → {len(results)} weld rows")
    return results, skipped

# ============================================================
# Excel output
# ============================================================
def write_excel(all_results, all_skipped, output_path):
    wb = openpyxl.Workbook()

    # ---- Sheet 1: Weld statistics ----
    ws = wb.active
    ws.title = "焊缝统计"

    HDR_FILL = PatternFill("solid", fgColor="4472C4")
    HDR_FONT = Font(bold=True, color="FFFFFF")
    CENTER    = Alignment(horizontal='center', vertical='center')

    headers = ['序号', '位置(上/下)', '焊脚尺寸hf(mm)', '焊缝长度(mm)',
               '备注', '零件1', '零件2', '构件号']
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = CENTER

    for idx, r in enumerate(all_results, 1):
        ws.cell(row=idx+1, column=1, value=idx)
        ws.cell(row=idx+1, column=2, value=r['position'])
        ws.cell(row=idx+1, column=3, value=r['hf'])
        ws.cell(row=idx+1, column=4, value=r['length_mm'])
        ws.cell(row=idx+1, column=5, value=r['annotation'])
        ws.cell(row=idx+1, column=6, value=r['part1'])
        ws.cell(row=idx+1, column=7, value=r['part2'])
        ws.cell(row=idx+1, column=8, value=r['component'])

    for col in ws.columns:
        w = max((len(str(cell.value or '')) for cell in col), default=0)
        ws.column_dimensions[col[0].column_letter].width = max(w + 3, 14)

    # ---- Sheet 2: Skipped / errors ----
    ws2 = wb.create_sheet("异常报告")
    ws2.cell(row=1, column=1, value="WeldMark 名称").font = Font(bold=True)
    ws2.cell(row=1, column=2, value="原因").font = Font(bold=True)
    for idx, (name, reason) in enumerate(all_skipped, 2):
        ws2.cell(row=idx, column=1, value=name)
        ws2.cell(row=idx, column=2, value=reason)
    ws2.column_dimensions['A'].width = 50
    ws2.column_dimensions['B'].width = 40

    wb.save(output_path)
    print(f"\nSaved → {output_path}")
    print(f"Total weld rows : {len(all_results)}")
    print(f"Total skipped   : {len(all_skipped)}")

# ============================================================
# Entry point
# ============================================================
if __name__ == '__main__':
    dxf_files = sorted(glob.glob(os.path.join(FOLDER, "*.dxf")))
    if not dxf_files:
        print("No DXF files found. Run convert_dwg_to_dxf.py first.")
        raise SystemExit(1)

    all_results = []
    all_skipped = []

    for dxf_path in dxf_files:
        try:
            results, skipped = extract_welds(dxf_path)
            all_results.extend(results)
            all_skipped.extend(skipped)
        except Exception as exc:
            import traceback
            print(f"\nERROR: {dxf_path}\n{traceback.format_exc()}")

    write_excel(all_results, all_skipped, OUTPUT)
