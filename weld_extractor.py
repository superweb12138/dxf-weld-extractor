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

import openpyxl
from openpyxl.styles import Font, PatternFill, Alignment

# ============================================================
# Configuration
# ============================================================
FOLDER   = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf"
OUTPUT   = os.path.join(FOLDER, "焊缝统计_auto.xlsx")

# Scale: 1 CAD unit = SCALE mm  (confirmed: 44.042 CAD = 440.4 mm → scale=10)
SCALE    = 10.0

# Arrow-tip to Part-line snap tolerance (CAD units)
SNAP_TOL  = 1.5
MAX_HF    = 20    # cap; very large annotations are plate thickness proxies, but hf=16 is valid

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
        elif txt.strip() == 'G':
            # 'G' suffix marks a groove weld on that side → hf=0
            if pos[1] >= ref_y:
                groove_above = True
            else:
                groove_below = True
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

def part_centroid(lines):
    if not lines:
        return (0.0, 0.0)
    xs = [(l['start'][0]+l['end'][0])/2 for l in lines]
    ys = [(l['start'][1]+l['end'][1])/2 for l in lines]
    return (sum(xs)/len(xs), sum(ys)/len(ys))

# ============================================================
# Find part labels (text that looks like a part number)
# ============================================================
PART_RE = re.compile(r'^[pP]\d+$|^[A-Z]{2,3}\d+$|^\d{3,}$')

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
_HF_FROM_T = {6:5, 7:5, 8:6, 9:6, 10:7, 11:8, 12:8, 14:10, 16:10, 18:12, 20:12}

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

        # Group into rows by y (bucket 4-unit bands)
        rows = defaultdict(dict)
        for y, x, txt in raw:
            rows[round(y / 4) * 4][x] = txt

        found_any = False
        for yk in sorted(rows, reverse=True):
            rowvals = rows[yk]
            vals = list(rowvals.values())
            mark  = next((v for v in vals if re.match(r'^p\d+$', v) or v == comp), None)
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
            qty_str = next((v for v in vals if re.match(r'^\d{1,2}$', v)), None)
            qty = int(qty_str) if qty_str else 1

            if pm:
                t, w = float(pm.group(1)), float(pm.group(2))
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
    comp_web_t    = comp_dims.get('web_t',    None)   # e.g. 9  for HW250×250×9×14
    comp_flange_t = comp_dims.get('flange_t', None)   # e.g. 14
    print(f"  BOM parts: {list(part_dims.keys())}")
    if comp_dims:
        print(f"  Comp section: {comp_dims}")

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

    # Assign part labels via Mark block leader tips
    all_labels      = find_all_labels(doc)
    part_number_map = assign_labels_by_leader_tip(all_labels, part_lines_map)
    print(f"  Part label candidates: {[x['label'] for x in all_labels]}")
    print(f"  Part→label map : {part_number_map}")

    # Extract welds
    results = []
    skipped = []

    # Determine main view: the view with the most Part-block instances.
    # Used to resolve TYP (typical) multipliers — TYP welds appear once on the
    # drawing but represent every instance of that part in the main assembly view.
    from collections import Counter as _Ctr
    _view_cnt = _Ctr(k.split(' - ')[-1] for k in part_number_map)
    main_view_id = _view_cnt.most_common(1)[0][0] if _view_cnt else ''

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
            is_three_sides = any(kw in parsed['annotation'].upper()
                                 for kw in ['SIDE', '围', '全'])

            if is_three_sides:
                # Gusset = the smallest-line NON-COMP Part at the arrow.
                # The comp (main member) is never the gusset plate.
                comp_part_names = {pn for pn, lbl in part_number_map.items() if lbl == comp}
                non_comp_matches = [m for m in matches if m['part'] not in comp_part_names]
                gusset_pool  = non_comp_matches if non_comp_matches else matches
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

                # Collect edges per gusset block (each gusset processed independently)
                # to support multi-instance assemblies (e.g. haunches on both flanges).
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
                                if d1 < pd_s: pd_s = d1; p_s = pname
                                if d2 < pd_e: pd_e = d2; p_e = pname
                        if p_s and p_e:
                            if p_s == p_e: _edges.append((g_ln['length'], p_s))
                        elif p_s: _edges.append((g_ln['length'], p_s))
                        elif p_e: _edges.append((g_ln['length'], p_e))
                    weld_edges_by_gusset[_gn] = _edges

                weld_edges_all = [(e, op, gn)
                                  for gn, edges in weld_edges_by_gusset.items()
                                  for e, op in edges]
                if not weld_edges_all:
                    skipped.append((wm_name, "3 SIDES: no adjacent edges found"))
                    continue

                lbl_g = part_number_map.get(gusset_name, comp)
                print(f"  [{view_id}] {wm_name.split(' - ')[0]}  [3 SIDES]  gusset={lbl_g}")
                # TYP multiplier: count candidate non-comp labels in the main assembly view.
                # Uses the gusset label and all other-part labels from collected edges.
                typ_mul_3s = 1
                if parsed['is_typ']:
                    _cand = {part_number_map.get(op, comp)
                             for _, op, _ in weld_edges_all
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
                # hf correction for 3-SIDES (sizes annotated on WM may be plate thickness)
                sz3_above = _correct_hf_3s(parsed['size_above'], lbl_g)
                sz3_below = _correct_hf_3s(parsed['size_below'], lbl_g)

                # Dedup by (gusset_block, other_block, edge_len): prevents counting the
                # same physical line twice (symmetric DXF where one Part appears in two
                # views at identical positions) while allowing distinct gusset instances
                # (e.g. two haunch blocks at different positions) to each contribute edges.
                edge_rows = []   # accumulate edge rows; extended by typ_mul_3s at end
                seen_bp = set()
                for edge_len, other_part, cur_gusset in weld_edges_all:
                    _bp = (cur_gusset, other_part, round(edge_len, 2))
                    if _bp in seen_bp:
                        continue
                    seen_bp.add(_bp)
                    lbl_o       = part_number_map.get(other_part, comp)
                    geo_len_mm  = round(edge_len * SCALE, 1)
                    final_edge_mm = geo_len_mm
                    # 3-SIDES bom_len correction: when the gusset plate is drawn
                    # slightly shorter in the cross-section view than its true
                    # length, use bom_len.  Only fires when bom_width is clearly
                    # different from geo (so we're not confusing width with length).
                    if lbl_g in part_dims:
                        _pd3 = part_dims[lbl_g]
                        _bw3 = _pd3.get('width')
                        _bl3 = _pd3.get('bom_len')
                        if (_bw3 and _bl3 and geo_len_mm > 0
                                and abs(geo_len_mm - _bl3) / geo_len_mm < 0.15
                                and abs(geo_len_mm - _bw3) / geo_len_mm > 0.35):
                            final_edge_mm = round(_bl3)
                    # Normalize: comp in part1; if neither is comp, gusset in part1
                    if lbl_o == comp:
                        p1, p2 = lbl_o, lbl_g
                    elif lbl_g == comp:
                        p1, p2 = lbl_g, lbl_o
                    else:
                        p1, p2 = (lbl_g, lbl_o) if lbl_g <= lbl_o else (lbl_o, lbl_g)
                    print(f"    edge geo={geo_len_mm}mm final={final_edge_mm}mm  {p1}/{p2}")
                    if p1 == p2:   # skip self-reference
                        continue
                    # CJP normalization for 3-SIDES edges (same rule as normal WMs)
                    grove_3s_ab = parsed['groove_above']
                    grove_3s_bl = parsed['groove_below']
                    s3_data = []
                    for side, sz, present, is_g in [
                        ('Above', sz3_above, parsed['has_above'], grove_3s_ab),
                        ('Below', sz3_below, parsed['has_below'], grove_3s_bl),
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
                            edge_rows.append({
                                'component': comp, 'position': 'Below',
                                'hf': f3['sz'] if f3['sz'] is not None else 0,
                                'length_mm': final_edge_mm,
                                'annotation': '', 'part1': p1, 'part2': p2,
                            })
                    else:
                        for s3 in s3_data:
                            hf_val = s3['sz'] if s3['sz'] is not None else 0
                            edge_rows.append({
                                'component': comp, 'position': s3['side'],
                                'hf': hf_val, 'length_mm': final_edge_mm,
                                'annotation': '', 'part1': p1, 'part2': p2,
                            })
                results.extend(edge_rows * typ_mul_3s)
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

            # TYP multiplier: when the WM is marked TYP (typical), count instances of
            # lbl_non_comp in the main assembly view and use that as the repeat count,
            # overriding bom_fallback_count (which handles a different comp/comp case).
            if parsed['is_typ'] and lbl_non_comp != comp:
                _typ_n = sum(1 for k, v in part_number_map.items()
                             if v == lbl_non_comp and k.split(' - ')[-1] == main_view_id)
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
            # Two cases where the BOM plate width is authoritative:
            #   Case 1: geo ≈ bom_len (plate drawn by its length → weld is on end face,
            #           width = weld run length).  Threshold: within 5 % of bom_len.
            #   Case 2: geo ≈ bom_width within 25 % (plate drawn slightly smaller than
            #           true dimension in the section view).
            # Skipped when the stiffener override already set the length.
            # All BOM widths are rounded to the nearest mm (engineering convention).
            BOM_WIDTH_TOL = 0.25
            BOM_LEN_TOL   = 0.05
            if (not stiffener_override_applied
                    and lbl_non_comp != comp
                    and lbl_non_comp in part_dims):
                pd_nc = part_dims[lbl_non_comp]
                bw = pd_nc['width']
                bl = pd_nc.get('bom_len')
                if bw and bw > 0 and weld_len_mm > 0:
                    if bl and bl > 0 and abs(weld_len_mm - bl) / weld_len_mm < BOM_LEN_TOL:
                        # Case 1: geo ≈ bom_len → plate end-face weld → use bom_width
                        weld_len_mm = round(bw)
                    elif abs(weld_len_mm - bw) / weld_len_mm < BOM_WIDTH_TOL:
                        # Case 2: geo close to bom_width
                        weld_len_mm = round(bw)

            final_len_mm = weld_len_mm

            sz_above = _correct_hf(parsed['size_above'], lbl1, lbl2)
            sz_below = _correct_hf(parsed['size_below'], lbl1, lbl2)

            print(f"  [{view_id}] {wm_name.split(' - ')[0]}")
            print(f"    arrow={arrow}  geo={weld_len_mm}mm  final={final_len_mm}mm"
                  f"  parts: {lbl1} / {lbl2}"
                  f"  size\u2191{sz_above} \u2193{sz_below}"
                  f"  annot={parsed['annotation']!r}")

            if lbl1 == lbl2:   # skip self-reference
                continue

            # CJP/groove normalization:
            # CJP always output as position='Above' with hf=None and note='CJP'.
            # The paired fillet (if any) is output as position='Below' with hf=value.
            # For pure CJP (no paired fillet) only one row is emitted.
            groove_ab = parsed['groove_above']
            groove_bl = parsed['groove_below']
            sides_data = []
            for side, sz, present, is_groove in [
                ('Above', sz_above, parsed['has_above'], groove_ab),
                ('Below', sz_below, parsed['has_below'], groove_bl),
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
                    # Paired fillet → 'Below', hf=value
                    f = fillet_sides[0]
                    for _rep in range(bom_fallback_count):
                        results.append({
                            'component':  comp,
                            'position':   'Below',
                            'hf':         f['sz'] if f['sz'] is not None else 0,
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
