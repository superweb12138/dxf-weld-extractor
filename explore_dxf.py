"""
Explore the DXF structure after conversion.
Dumps all block names, WeldMark details, Part geometries, and text labels.
Run this AFTER convert_dwg_to_dxf.py to understand the full drawing structure.
Output is saved to explore_dxf_output.txt
"""
import ezdxf
import math
import re
import sys
import os

DXF = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.dxf"

out = open(r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\explore_dxf_output.txt", "w", encoding="utf-8")

def p(*args):
    line = " ".join(str(a) for a in args)
    print(line)
    out.write(line + "\n")

def dist(a, b):
    return math.sqrt((a[0]-b[0])**2 + (a[1]-b[1])**2)

doc = ezdxf.readfile(DXF)

# ---- 1. List ALL block names grouped by prefix ----
p("=" * 70)
p("ALL BLOCK NAMES")
p("=" * 70)
from collections import defaultdict
prefix_map = defaultdict(list)
for blk in doc.blocks:
    blk_name = blk.name
    prefix = blk_name.split('-')[0] if '-' in blk_name else blk_name
    prefix_map[prefix].append(blk_name)

for prefix in sorted(prefix_map):
    names = prefix_map[prefix]
    p(f"\n[{prefix}]  count={len(names)}")
    for n in sorted(names)[:20]:
        p(f"  {n}")
    if len(names) > 20:
        p(f"  ... ({len(names)-20} more)")

# ---- 2. WeldMark details ----
p("\n" + "="*70)
p("WELDMARK BLOCKS - FULL DETAIL")
p("="*70)

for blk in sorted(doc.blocks, key=lambda b: b.name):
    blk_name = blk.name
    if not blk_name.startswith("WeldMark"):
        continue
    p(f"\n{blk_name}")
    for e in blk:
        etype = e.dxftype()
        if etype == 'LINE':
            s = e.dxf.start
            ep = e.dxf.end
            ln = dist((s.x, s.y), (ep.x, ep.y))
            p(f"  LINE ({s.x:.3f},{s.y:.3f}) -> ({ep.x:.3f},{ep.y:.3f})  len={ln:.3f}")
        elif etype == 'TEXT':
            try:
                p(f"  TEXT {e.dxf.text!r}  at ({e.dxf.insert.x:.3f},{e.dxf.insert.y:.3f})")
            except:
                pass
        elif etype == 'MTEXT':
            try:
                p(f"  MTEXT {e.text!r}  at ({e.dxf.insert.x:.3f},{e.dxf.insert.y:.3f})")
            except:
                pass
        elif etype == 'INSERT':
            p(f"  INSERT {e.dxf.name!r}  at ({e.dxf.insert.x:.3f},{e.dxf.insert.y:.3f})")
        elif etype in ('HATCH', 'WIPEOUT'):
            p(f"  {etype}")

# ---- 3. Part blocks - ALL lines ----
p("\n" + "="*70)
p("PART BLOCKS - ALL LINES > 5 units")
p("="*70)

# Group Parts by view
from collections import defaultdict
view_parts = defaultdict(list)
for blk in doc.blocks:
    blk_name = blk.name
    if not blk_name.startswith("Part"):
        continue
    m = re.search(r' - (\d+)$', blk_name)
    view_id = m.group(1) if m else "unknown"
    view_parts[view_id].append(blk_name)

for view_id in sorted(view_parts):
    p(f"\n--- View {view_id} ---")
    for blk_name in sorted(view_parts[view_id]):
        blk = doc.blocks[blk_name]
        lines = []
        for e in blk:
            if e.dxftype() == 'LINE':
                s = e.dxf.start
                ep = e.dxf.end
                ln = dist((s.x, s.y), (ep.x, ep.y))
                if ln > 5:
                    lines.append((ln, (s.x, s.y), (ep.x, ep.y)))
        if lines:
            p(f"  {blk_name}")
            for ln, s, ep in sorted(lines, key=lambda x: -x[0])[:15]:
                p(f"    len={ln:8.3f}  ({s[0]:.3f},{s[1]:.3f}) -> ({ep[0]:.3f},{ep[1]:.3f})")

# ---- 4. All text in ALL blocks (find part numbers) ----
p("\n" + "="*70)
p("ALL TEXT ENTITIES IN ALL BLOCKS")
p("="*70)

part_re = re.compile(r'^[pP]\d+$|^[A-Z]{2}\d+$|^\d+$')
for blk in sorted(doc.blocks, key=lambda b: b.name):
    blk_name = blk.name
    texts = []
    for e in blk:
        etype = e.dxftype()
        txt = None
        pos = None
        if etype == 'TEXT':
            try:
                txt = e.dxf.text.strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
            except:
                pass
        elif etype == 'MTEXT':
            try:
                txt = e.text.strip()
                pos = (e.dxf.insert.x, e.dxf.insert.y)
            except:
                pass
        if txt and pos and txt not in ('', ' '):
            texts.append((txt, pos))
    if texts:
        p(f"\n  [{blk_name}]")
        for txt, pos in texts[:30]:
            p(f"    {txt!r}  at ({pos[0]:.3f},{pos[1]:.3f})")

# ---- 5. INSERT hierarchy in model space ----
p("\n" + "="*70)
p("MODEL SPACE INSERT HIERARCHY")
p("="*70)
ms = doc.modelspace()
for e in ms:
    if e.dxftype() == 'INSERT':
        p(f"  INSERT {e.dxf.name!r}  at ({e.dxf.insert.x:.3f},{e.dxf.insert.y:.3f})  "
          f"xscale={getattr(e.dxf, 'xscale', 1.0):.3f}  yscale={getattr(e.dxf, 'yscale', 1.0):.3f}")

# ---- 6. Check scale of Part inserts in view blocks ----
p("\n" + "="*70)
p("INSERT SCALE FACTORS FOR PART/WELDMARK INSERTS IN VIEW BLOCKS")
p("="*70)
for blk in doc.blocks:
    for e in blk:
        if e.dxftype() == 'INSERT':
            ref = e.dxf.name
            if ref.startswith('Part') or ref.startswith('WeldMark'):
                try:
                    xs = getattr(e.dxf, 'xscale', 1.0)
                    ys = getattr(e.dxf, 'yscale', 1.0)
                    ix = e.dxf.insert.x
                    iy = e.dxf.insert.y
                    p(f"  In [{blk_name}]: INSERT {ref!r}  at ({ix:.3f},{iy:.3f})  scale=({xs},{ys})")
                except:
                    pass

out.close()
print(f"\nOutput saved to explore_dxf_output.txt")
