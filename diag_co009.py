"""Diagnose CO009 and CO007 WeldMark parsing issues."""
import ezdxf, math, re
from collections import Counter

def dist2d(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

def diag_file(path):
    comp = re.search(r'-(CO\d+|BE\d+)_', path).group(1)
    print(f"\n{'='*60}\n{path}  [{comp}]")
    doc = ezdxf.readfile(path)

    for blk in doc.blocks:
        name = blk.name
        if not name.startswith('WeldMark'):
            continue
        lines_raw, texts = [], []
        for e in blk:
            t = e.dxftype()
            if t == 'LINE':
                s  = (round(e.dxf.start.x,4), round(e.dxf.start.y,4))
                ep = (round(e.dxf.end.x,4),   round(e.dxf.end.y,4))
                ln = dist2d(s, ep)
                if ln > 0.01:
                    lines_raw.append((s, ep, ln))
            elif t in ('TEXT','MTEXT'):
                try:
                    txt = (e.dxf.text if t=='TEXT' else e.text).strip()
                    pos = (e.dxf.insert.x, e.dxf.insert.y)
                    if txt:
                        texts.append((txt, pos))
                except: pass

        if not lines_raw:
            continue

        ep_count = Counter()
        for s, ep, _ in lines_raw:
            ep_count[s] += 1; ep_count[ep] += 1
        dangling = {pt for pt, c in ep_count.items() if c == 1}

        horiz = [(s, ep, ln) for s,ep,ln in lines_raw
                 if abs(s[1]-ep[1]) < 0.05*ln and ln > 3]
        if not horiz:
            continue
        ref_s, ref_e, _ = max(horiz, key=lambda x: x[2])
        ref_y = (ref_s[1]+ref_e[1])/2.0

        print(f"\n  {name}")
        print(f"    ref_y={ref_y:.3f}  shelf=({ref_s}→{ref_e})")
        print(f"    texts: {texts}")
        print(f"    lines: {[(round(ln,3), s, ep) for s,ep,ln in sorted(lines_raw, key=lambda x:-x[2])]}")

        # Show which texts are above/below shelf
        for txt, pos in texts:
            rel = 'above' if pos[1] >= ref_y else 'below'
            m = re.match(r'^(\d+(?:\.\d+)?)(?:/(\d+(?:\.\d+)?))?$', txt)
            flag = ' ← SIZE' if m else ''
            print(f"      [{rel}] {txt!r} at y={pos[1]:.3f}{flag}")

import glob, os
FOLDER = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf"
for f in sorted(glob.glob(os.path.join(FOLDER, "*.dxf"))):
    if 'CO009' in f or 'CO007' in f:
        diag_file(f)
