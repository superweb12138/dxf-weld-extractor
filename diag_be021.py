"""Diagnostic for BE021: show all part lines and WeldMark arrows."""
import ezdxf, math

def seg_len(s, e):
    return math.hypot(e[0]-s[0], e[1]-s[1])

dxf_path = r'c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE021_00.dxf'
doc = ezdxf.readfile(dxf_path)
msp = doc.modelspace()

# Gather all Part and Mark inserts
parts_by_hdr = {}
marks = []
for e in msp:
    if e.dxftype() != 'INSERT':
        continue
    name = e.dxf.name or ''
    if 'Part' in name and ' - ' in name:
        parts_by_hdr[name] = e
    elif 'Mark' in name:
        marks.append(e)

# Build label map from Mark blocks
label_map = {}
for m in marks:
    ents = list(m.virtual_entities())
    texts = [t.dxf.text.strip() for t in ents if t.dxftype() == 'TEXT' and t.dxf.text]
    # Find which Part this Mark labels: look for Part header in texts or nearby
    # The label is the first non-bracketed text
    lbl = next((t for t in texts if t and not t.startswith('(')), None)
    if not lbl:
        continue
    # The associated Part: Mark name has same view_id
    view_id = m.dxf.name.split(' - ')[-1] if ' - ' in m.dxf.name else ''
    # Find Part in same view with closest centroid to mark insert
    mi = (m.dxf.insert.x, m.dxf.insert.y)
    # Find the leader endpoint (farthest line endpoint from text centroid)
    line_eps = []
    for ent in ents:
        if ent.dxftype() == 'LINE':
            line_eps.extend([(ent.dxf.start.x, ent.dxf.start.y),
                              (ent.dxf.end.x, ent.dxf.end.y)])
    if not line_eps:
        continue
    txt_pts = [(t.dxf.insert.x, t.dxf.insert.y) for t in ents if t.dxftype() == 'TEXT']
    if txt_pts:
        tc = (sum(p[0] for p in txt_pts)/len(txt_pts), sum(p[1] for p in txt_pts)/len(txt_pts))
        tip = max(line_eps, key=lambda p: math.hypot(p[0]-tc[0], p[1]-tc[1]))
    else:
        tip = line_eps[-1]
    label_map[lbl] = label_map.get(lbl, [])
    label_map[lbl].append({'view': view_id, 'tip': tip})

print("Labels found:", {k: len(v) for k,v in label_map.items()})

# For each view, show Part lines for p122 and p123
for hdr, part_ins in sorted(parts_by_hdr.items()):
    ents = list(part_ins.virtual_entities())
    lines = [(ent.dxf.start.x, ent.dxf.start.y, ent.dxf.end.x, ent.dxf.end.y)
             for ent in ents if ent.dxftype() == 'LINE']
    if not lines:
        continue
    view_id = hdr.split(' - ')[-1]
    # Find label for this part
    # Check if any mark tip is near any line
    matched_lbl = None
    for lbl, tips in label_map.items():
        for tip_info in tips:
            if tip_info['view'] != view_id:
                continue
            tip = tip_info['tip']
            for x1,y1,x2,y2 in lines:
                # distance tip to segment
                dx, dy = x2-x1, y2-y1
                if dx==dy==0: continue
                t = max(0, min(1, ((tip[0]-x1)*dx+(tip[1]-y1)*dy)/(dx*dx+dy*dy)))
                d = math.hypot(tip[0]-(x1+t*dx), tip[1]-(y1+t*dy))
                if d < 2.0:
                    matched_lbl = lbl
                    break
            if matched_lbl:
                break
        if matched_lbl:
            break
    if matched_lbl in ('p122', 'p123', 'BE021'):
        lens = sorted([round(seg_len((x1,y1),(x2,y2))*10, 1) for x1,y1,x2,y2 in lines], reverse=True)
        print(f"  view={view_id}  part={hdr.split(' - ')[0][-10:]}  label={matched_lbl}  lines={lens[:5]}")
