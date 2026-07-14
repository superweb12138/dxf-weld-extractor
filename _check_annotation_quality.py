"""Quick quality check for annotated DXF files."""
import os, math, re
import ezdxf
from collections import defaultdict

FOLDER = os.path.dirname(os.path.abspath(__file__))
ANNOTATED_DIR = os.path.join(FOLDER, "annotated")

def _seg_intersect(p1, p2, p3, p4):
    def ccw(a, b, c):
        return (c[1]-a[1])*(b[0]-a[0]) > (b[1]-a[1])*(c[0]-a[0])
    return ccw(p1,p3,p4) != ccw(p2,p3,p4) and ccw(p1,p2,p3) != ccw(p1,p2,p4)

def _leader_segments(pos, dist, angle, h_land):
    rad = math.radians(angle)
    ex = pos[0] + dist * math.cos(rad)
    ey = pos[1] + dist * math.sin(rad)
    hx = ex + h_land
    hy = ey
    return [((pos[0], pos[1]), (ex, ey)), ((ex, ey), (hx, hy))]

def _detect_frames(doc):
    h_segs = defaultdict(list)
    v_segs = defaultdict(list)
    def _scan(e):
        if e.dxftype() != 'LINE': return
        s, ep = e.dxf.start, e.dxf.end
        dx, dy = abs(s.x - ep.x), abs(s.y - ep.y)
        length = math.hypot(dx, dy)
        if length < 100: return
        if dx > dy and dx > length * 0.9:
            h_segs[round(s.y, 1)].append((min(s.x, ep.x), max(s.x, ep.x)))
        elif dy > dx and dy > length * 0.9:
            v_segs[round(s.x, 1)].append((min(s.y, ep.y), max(s.y, ep.y)))
    for blk in doc.blocks:
        for e in blk: _scan(e)
    for e in doc.modelspace(): _scan(e)
    if len(h_segs) < 4 or len(v_segs) < 4: return None, None
    h_final = {k: (min(v[0] for v in segs), max(v[1] for v in segs)) for k, segs in h_segs.items()}
    v_final = {k: (min(v[0] for v in segs), max(v[1] for v in segs)) for k, segs in v_segs.items()}
    h_sorted = sorted(h_final.items(), key=lambda kv: -(kv[1][1]-kv[1][0]))
    v_sorted = sorted(v_final.items(), key=lambda kv: -(kv[1][1]-kv[1][0]))
    h_max_w = h_sorted[0][1][1] - h_sorted[0][1][0]
    v_max_h = v_sorted[0][1][1] - v_sorted[0][1][0]
    outer_h = [(k, v) for k, v in h_sorted if abs((v[1]-v[0]) - h_max_w) < 5]
    outer_v = [(k, v) for k, v in v_sorted if abs((v[1]-v[0]) - v_max_h) < 5]
    if len(outer_h) < 2 or len(outer_v) < 2: return None, None
    ox0, ox1 = min(k for k, v in outer_v), max(k for k, v in outer_v)
    oy0, oy1 = min(k for k, v in outer_h), max(k for k, v in outer_h)
    outer = (ox0, oy0, ox1, oy1)
    inner_h = [kv for kv in h_sorted if h_max_w*0.8 < (kv[1][1]-kv[1][0]) < h_max_w*0.99 and oy0+5 < kv[0] < oy1-5]
    inner_v = [kv for kv in v_sorted if v_max_h*0.8 < (kv[1][1]-kv[1][0]) < v_max_h*0.99 and ox0+5 < kv[0] < ox1-5]
    if len(inner_h) >= 2 and len(inner_v) >= 2:
        ix0, ix1 = min(k for k, v in inner_v), max(k for k, v in inner_v)
        iy0, iy1 = min(k for k, v in inner_h), max(k for k, v in inner_h)
        return outer, (ix0, iy0, ix1, iy1)
    return outer, None

def _bom_bbox(doc):
    bboxes = []
    BOM_KEYWORDS = ('LENGTH', 'WIDTH', 'HEIGHT', 'WEIGHT', 'QTY', 'QUANTITY',
                    'DESCRIPTION', 'PART', 'MARK', 'MATERIAL', 'REMARK',
                    'ASSEMBLY BOLT LIST', 'BOLT LIST', 'PAY CODE', 'PAY CAT',
                    'PART LIST', 'NO.', 'DIA.', 'GRADE', 'SITE/SHOP',
                    'MEMBERS LOCATION')
    TITLE_KEYWORDS = ('STEEL STRUCTURE DRAWING', 'PROJECT DOCUMENT',
                      'VENDOR DOCUMENT', 'DOCUMENT CLASS', 'REVISION',
                      'ISSUE FOR', 'DRAWN BY', 'CHECKED', 'APPROVED')
    for e in doc.modelspace():
        if e.dxftype() == 'INSERT' and e.dxf.name.startswith('Unknown-'):
            if re.search(r' - \d+$', e.dxf.name):
                continue
            blk = doc.blocks.get(e.dxf.name)
            if not blk: continue
            xs, ys = [], []
            texts = []
            for sub in blk:
                if sub.dxftype() == 'LINE':
                    xs.extend([sub.dxf.start.x, sub.dxf.end.x])
                    ys.extend([sub.dxf.start.y, sub.dxf.end.y])
                elif sub.dxftype() in ('TEXT','MTEXT','ATTRIB','ATTDEF'):
                    try:
                        xs.append(sub.dxf.insert.x); ys.append(sub.dxf.insert.y)
                        txt = (sub.text if hasattr(sub, 'text') else sub.dxf.text) or ''
                        texts.append(txt.upper())
                    except: pass
            if not xs: continue
            all_text = ' '.join(texts)
            bom_hits = sum(1 for kw in BOM_KEYWORDS if kw in all_text)
            title_hits = sum(1 for kw in TITLE_KEYWORDS if kw in all_text)
            if bom_hits >= 2 and title_hits == 0:
                bboxes.append((min(xs), max(xs), min(ys), max(ys)))
    return bboxes

def check_file(path):
    doc = ezdxf.readfile(path)
    _, inner = _detect_frames(doc)
    boms = _bom_bbox(doc)
    labels = []
    for e in doc.modelspace():
        if e.dxftype() == 'MTEXT' and e.dxf.layer == 'WELD_LABELS':
            text = e.text if hasattr(e, 'text') else e.dxf.text
            if not text or 'Derived' in text: continue
            ins = e.dxf.insert
            h = getattr(e.dxf, 'char_height', 2.5)
            w = h * len(text.strip().replace(',', '')) * 0.6
            # attachment point handling roughly
            ap = getattr(e.dxf, 'attachment_point', 1)
            if ap in (1, 4, 7):  # left
                x0, x1 = ins.x, ins.x + w
            elif ap in (3, 6, 9):  # right
                x0, x1 = ins.x - w, ins.x
            else:
                x0, x1 = ins.x - w/2, ins.x + w/2
            if ap in (1, 2, 3):  # top
                y0, y1 = ins.y - h, ins.y
            elif ap in (7, 8, 9):  # bottom
                y0, y1 = ins.y, ins.y + h
            else:
                y0, y1 = ins.y - h/2, ins.y + h/2
            labels.append({'text': text, 'bbox': (x0, x1, y0, y1)})

    out_of_frame = 0
    bom_overlap = 0
    for lb in labels:
        x0, x1, y0, y1 = lb['bbox']
        if inner:
            if x0 < inner[0] or x1 > inner[2] or y0 < inner[1] or y1 > inner[3]:
                out_of_frame += 1
        for bx0, bx1, by0, by1 in boms:
            if not (x1 < bx0 or x0 > bx1 or y1 < by0 or y0 > by1):
                bom_overlap += 1
                break

    # count leader line crossings: reconstruct from WELD_LABELS layer lines
    leaders = []
    lines = []
    for e in doc.modelspace():
        if e.dxftype() == 'LINE' and e.dxf.layer == 'WELD_LABELS':
            lines.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)))
    # group lines into leader sets: arrow-V (2 short) + diagonal + horizontal
    # simplistic: consider all non-V line segments; V segments are very short
    segs = [s for s in lines if math.hypot(s[1][0]-s[0][0], s[1][1]-s[0][1]) > 3]
    crossings = 0
    for i in range(len(segs)):
        for j in range(i+1, len(segs)):
            if _seg_intersect(segs[i][0], segs[i][1], segs[j][0], segs[j][1]):
                crossings += 1
    return len(labels), crossings, out_of_frame, bom_overlap, inner

print(f"{'File':<45} {'Labels':>8} {'Cross':>8} {'OutFrame':>9} {'BOM':>5}")
print("-" * 80)
for fn in sorted(os.listdir(ANNOTATED_DIR)):
    if not fn.endswith('.dxf'): continue
    path = os.path.join(ANNOTATED_DIR, fn)
    try:
        n, c, o, b, inner = check_file(path)
        print(f"{fn:<45} {n:>8} {c:>8} {o:>9} {b:>5}")
    except Exception as e:
        print(f"{fn:<45} ERROR: {e}")
