import ezdxf
import re
import math
from collections import defaultdict

DOC_PATH = r'C:\Users\15297\Desktop\hanf\361-RC3210-S-01-CO010_02.dxf'
doc = ezdxf.readfile(DOC_PATH)

wm_by_view = defaultdict(list)
part_by_view = defaultdict(list)

for blk in doc.blocks:
    m = re.search(r' - (\d+)$', blk.name)
    if not m: continue
    view_id = m.group(1)
    if blk.name.startswith('WeldMark'):
        wm_by_view[view_id].append(blk.name)
    elif blk.name.startswith('Part'):
        part_by_view[view_id].append(blk.name)

print("=" * 70)
print("CO010 DXF STRUCTURE ANALYSIS")
print("=" * 70)

print("\n--- VIEWS WITH PARTS ---")
for vid in sorted(part_by_view):
    parts = part_by_view[vid]
    wm_count = len(wm_by_view.get(vid, []))
    print(f"  View {vid}: {len(parts)} Parts, {wm_count} WeldMarks")
    for pn in sorted(parts)[:3]:
        print(f"      {pn}")
    if len(parts) > 3:
        print(f"      ... and {len(parts)-3} more")

print("\n--- VIEWS WITH WELDMARKS ---")
for vid in sorted(wm_by_view):
    print(f"  View {vid}: {len(wm_by_view[vid])} WeldMark(s)")
    for wn in sorted(wm_by_view[vid]):
        print(f"      {wn}")

print("\n--- WELDMARK ANNOTATIONS ---")
def parse_weldmark_simple(blk):
    lines_raw = []
    texts = []
    for e in blk:
        t = e.dxftype()
        if t == 'LINE':
            s = (round(e.dxf.start.x, 4), round(e.dxf.start.y, 4))
            ep = (round(e.dxf.end.x, 4), round(e.dxf.end.y, 4))
            ln = math.hypot(ep[0]-s[0], ep[1]-s[1])
            if ln > 0.01: lines_raw.append((s, ep, ln))
        elif t == 'TEXT':
            try:
                txt = e.dxf.text.strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
                if txt: texts.append((txt, pos))
            except: pass
        elif t == 'MTEXT':
            try:
                txt = e.text.strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
                if txt: texts.append((txt, pos))
            except: pass
    if not lines_raw: return None
    ep_count = defaultdict(int)
    for s, ep, _ in lines_raw:
        ep_count[s] += 1; ep_count[ep] += 1
    dangling = {pt for pt, c in ep_count.items() if c == 1}
    horiz = [(s, ep, ln) for s, ep, ln in lines_raw if abs(s[1]-ep[1]) < 0.05*ln and ln > 3]
    if not horiz: return None
    ref_s, ref_e, _ = max(horiz, key=lambda x: x[2])
    ref_y = (ref_s[1] + ref_e[1]) / 2.0
    candidates = [pt for pt in dangling if abs(pt[1] - ref_y) > 0.5]
    if not candidates: return None
    arrow_tip = max(candidates, key=lambda pt: abs(pt[1] - ref_y))
    arclist = []
    for e in blk:
        if e.dxftype() == 'ARC':
            c = (round(e.dxf.center.x, 4), round(e.dxf.center.y, 4))
            r = round(e.dxf.radius, 4)
            arclist.append((c, r))
    _cc = defaultdict(int)
    for c, r in arclist:
        if 1.0 <= r <= 2.5 and abs(c[1] - ref_y) < 1.0:
            _cc[c] += 1
    has_circle = any(cnt >= 2 for cnt in _cc.values())
    annotation = ''
    for txt, pos in texts:
        if any(kw in txt.upper() for kw in ['SIDE', '\u56f4', '\u5168']):
            annotation = txt
            break
    size_above = None
    size_below = None
    import re as _re
    for txt, pos in texts:
        m = _re.match(r'^(\d+)$', txt.strip())
        if m:
            val = int(m.group(1))
            if abs(pos[1] - ref_y) > 0.3:
                if pos[1] > ref_y:
                    size_above = val
                else:
                    size_below = val
            else:
                size_above = val
    return {
        'arrow_tip': arrow_tip,
        'has_circle': has_circle,
        'ref_y': ref_y,
        'annotation': annotation,
        'size_above': size_above,
        'size_below': size_below,
    }

for vid in sorted(wm_by_view):
    print(f"\n  View {vid}:")
    view_parts = part_by_view.get(vid, [])
    print(f"    Parts in this view: {len(view_parts)}")
    for wn in sorted(wm_by_view[vid]):
        wm_blk = doc.blocks[wn]
        parsed = parse_weldmark_simple(wm_blk)
        if parsed:
            print(f"    WeldMark: {wn}")
            print(f"      Arrow tip: {parsed['arrow_tip']}")
            print(f"      Annotation: '{parsed['annotation']}'")
            print(f"      Size above: {parsed['size_above']}, below: {parsed['size_below']}")
            print(f"      Has circle: {parsed['has_circle']}")
        else:
            print(f"    WeldMark: {wn} (parse failed)")

print("\n--- PART TO LABEL MAPPING ---")
def get_part_lines(blk):
    lines = []
    for e in blk:
        if e.dxftype() == 'LINE':
            s = (round(e.dxf.start.x, 4), round(e.dxf.start.y, 4))
            ep = (round(e.dxf.end.x, 4), round(e.dxf.end.y, 4))
            length = math.hypot(ep[0]-s[0], ep[1]-s[1])
            if length > 0.01:
                lines.append({'start': s, 'end': ep, 'length': length})
    return lines

part_lines_map = {}
for view_id, parts in part_by_view.items():
    part_lines_map[view_id] = {}
    for pname in parts:
        blk = doc.blocks[pname]
        lines = get_part_lines(blk)
        if lines: part_lines_map[view_id][pname] = lines

PART_RE = re.compile(r'^(p\d+|sp\d+|CO\d+|BE\d+)$')
def find_all_labels(doc):
    labels = []
    for blk in doc.blocks:
        blk_name = blk.name
        if not blk_name.startswith('Mark') and not blk_name.startswith('Part'):
            continue
        texts = []
        lines = []
        for e in blk:
            if e.dxftype() == 'LINE':
                s = (round(e.dxf.start.x, 4), round(e.dxf.start.y, 4))
                ep = (round(e.dxf.end.x, 4), round(e.dxf.end.y, 4))
                ln = math.hypot(ep[0]-s[0], ep[1]-s[1])
                if ln > 0.01: lines.append((s, ep))
            elif e.dxftype() == 'TEXT':
                try:
                    txt = e.dxf.text.strip()
                    pos = (e.dxf.insert.x, e.dxf.insert.y)
                    if txt: texts.append((txt, pos))
                except: pass
            elif e.dxftype() == 'MTEXT':
                try:
                    txt = e.text.strip()
                    pos = (e.dxf.insert.x, e.dxf.insert.y)
                    if txt: texts.append((txt, pos))
                except: pass
        txt_pos = None; label = None
        for t, p in texts:
            if PART_RE.match(t):
                label = t; txt_pos = p; break
        if not label or not txt_pos or not lines: continue
        all_pts = [p for seg in lines for p in seg]
        leader_tip = max(all_pts, key=lambda p: math.hypot(p[0]-txt_pos[0], p[1]-txt_pos[1]))
        labels.append({'label': label, 'pos': txt_pos, 'leader_tip': leader_tip, 'block': blk_name})
    return labels

all_labels = find_all_labels(doc)
print(f"\n  Found {len(all_labels)} total label candidates")

LABEL_TIP_TOL = 8.0
def dist2d(a, b):
    return math.hypot(a[0]-b[0], a[1]-b[1])
def dist_pt_to_seg(pt, s, e):
    dx, dy = e[0]-s[0], e[1]-s[1]
    if dx==0 and dy==0: return dist2d(pt, s), 0
    t = max(0, min(1, ((pt[0]-s[0])*dx + (pt[1]-s[1])*dy)/(dx*dx+dy*dy)))
    proj = (s[0]+t*dx, s[1]+t*dy)
    return dist2d(pt, proj), t
def part_centroid(lines):
    if not lines: return (0,0)
    cx = sum(ln['start'][0]+ln['end'][0] for ln in lines)/(2*len(lines))
    cy = sum(ln['start'][1]+ln['end'][1] for ln in lines)/(2*len(lines))
    return (cx, cy)

part_number_map = {}
unmatched = []
for lbl in all_labels:
    m = re.search(r' - (\d+)$', lbl['block'])
    if not m: continue
    view_id = m.group(1)
    tip = lbl['leader_tip']
    view_parts = part_lines_map.get(view_id, {})
    best_part = None
    best_score = (LABEL_TIP_TOL, 1e18)
    for pname, lines in view_parts.items():
        line_d = LABEL_TIP_TOL
        for ln in lines:
            d, _ = dist_pt_to_seg(tip, ln['start'], ln['end'])
            d = min(d, dist2d(tip, ln['start']), dist2d(tip, ln['end']))
            if d < line_d: line_d = d
        if line_d < LABEL_TIP_TOL:
            c = part_centroid(lines) if lines else tip
            cd = dist2d(tip, c)
            score = (line_d, cd)
            if score < best_score:
                best_score = score; best_part = pname
    if best_part:
        part_number_map[best_part] = lbl['label']
    else:
        unmatched.append(lbl)

by_view = defaultdict(list)
for pn, lbl in sorted(part_number_map.items(), key=lambda x: (x[0].split(' - ')[-1], x[1])):
    m = re.search(r' - (\d+)$', pn)
    vid = m.group(1) if m else '?'
    by_view[vid].append((pn, lbl))

print("\n  Part->Label by View:")
for vid in sorted(by_view):
    print(f"    View {vid}:")
    labels_in_view = set(lbl for _, lbl in by_view[vid])
    print(f"      Labels: {sorted(labels_in_view)}")
    for pn, lbl in sorted(by_view[vid], key=lambda x: x[1]):
        print(f"        {lbl} <- {pn}")

print(f"\n  Unmatched labels ({len(unmatched)}):")
for lbl in unmatched:
    b = lbl['block']
    m = re.search(r' - (\d+)$', b)
    vid = m.group(1) if m else '?'
    print(f"      {b} -> label={lbl['label']} view={vid}")

# Cross-reference: for each WeldMark view, show which plate labels are present
print("\n--- VIEW CROSS-REFERENCE: PLATE PAIRS PER VIEW ---")
comp = 'CO010'
for vid in sorted(part_by_view):
    labels_in_view = set()
    for pn in part_by_view[vid]:
        lbl = part_number_map.get(pn, comp)
        labels_in_view.add(lbl)
    has_wm = vid in wm_by_view
    wm_count = len(wm_by_view.get(vid, []))
    non_comp = sorted(l for l in labels_in_view if l != comp)
    print(f"  View {vid}: {len(labels_in_view)} labels, {wm_count} WMs{' *HAS WM*' if has_wm else ''}")
    if non_comp:
        print(f"    Plates: {non_comp}")

print("\nDone!")
