"""Debug: trace find_parts_at_point for BE018 WM arrows."""
import ezdxf, re, math

SNAP_TOL = 1.5
SCALE = 10.0

def dist2d(a, b):
    return math.hypot(b[0]-a[0], b[1]-a[1])

def dist_pt_to_seg(p, a, b):
    ax,ay = a; bx,by = b; px,py = p
    dx,dy = bx-ax, by-ay
    L2 = dx*dx + dy*dy
    if L2 == 0:
        return dist2d(p, a), 0.0
    t = max(0.0, min(1.0, ((px-ax)*dx + (py-ay)*dy) / L2))
    return dist2d(p, (ax+t*dx, ay+t*dy)), t

doc = ezdxf.readfile(r'c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.dxf')

# Build label map
label_map = {}
for blk in doc.blocks:
    if not blk.name.startswith('Mark'):
        continue
    texts, part_ref = [], None
    for e in blk:
        if e.dxftype() == 'TEXT':
            t = e.dxf.text.strip()
            if t:
                texts.append(t)
        elif e.dxftype() == 'INSERT' and 'Part' in e.dxf.name:
            part_ref = e.dxf.name
    label = next((t for t in texts if re.match(r'^p\d+$|^BE018$', t)), None)
    if label and part_ref:
        label_map[part_ref] = label

# Test each view
for view_id in ['1655']:
    print(f'\n=== view {view_id} ===')
    # Collect part lines
    parts = {}
    for blk in doc.blocks:
        if blk.name.startswith('Part') and blk.name.endswith(f'- {view_id}'):
            lns = []
            for e in blk:
                if e.dxftype() == 'LINE':
                    s = (e.dxf.start.x, e.dxf.start.y)
                    ep = (e.dxf.end.x, e.dxf.end.y)
                    L = dist2d(s, ep)
                    if L > 0.5:
                        lns.append({'start': s, 'end': ep, 'length': L})
            lbl = label_map.get(blk.name, '?')
            parts[blk.name] = (lbl, lns)
            print(f'  Part {blk.name[-18:]} ({lbl}): {len(lns)} lines, max={round(max(l["length"]*SCALE for l in lns),1) if lns else 0}mm')

    # Test arrows
    for arrow_name, arrow in [
        ('WM-3927 hf=12', (-394.3, 13.8)),
        ('WM-3936 hf=7',  (-394.3,  2.379)),
        ('WM-3964 hf=12', (-394.3, -15.0)),
    ]:
        print(f'\n  Arrow {arrow_name} at {arrow}:')
        for pname, (lbl, lines) in parts.items():
            best_ep = None
            best_int = None
            for ln in lines:
                ep_d = min(dist2d(arrow, ln['start']), dist2d(arrow, ln['end']))
                d, _ = dist_pt_to_seg(arrow, ln['start'], ln['end'])
                if ep_d <= SNAP_TOL:
                    if best_ep is None or ep_d < best_ep[1]:
                        best_ep = (ln, ep_d)
                elif d <= SNAP_TOL:
                    if best_int is None or ln['length'] < best_int[0]['length']:
                        best_int = (ln, d)
            if best_ep:
                ln = best_ep[0]
                print(f'    EP  {pname[-18:]} ({lbl}) {round(ln["length"]*SCALE,1)}mm ep_d={round(best_ep[1],3)}')
                print(f'        {tuple(round(x,2) for x in ln["start"])} -> {tuple(round(x,2) for x in ln["end"])}')
            elif best_int:
                ln = best_int[0]
                print(f'    INT {pname[-18:]} ({lbl}) {round(ln["length"]*SCALE,1)}mm')
                print(f'        {tuple(round(x,2) for x in ln["start"])} -> {tuple(round(x,2) for x in ln["end"])}')
