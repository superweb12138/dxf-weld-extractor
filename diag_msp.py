"""
Inspect modelspace of a DXF for:
1. Standalone LINE/LWPOLYLINE/ARC entities (potential visible weld lines)
2. TEXT/MTEXT with part dimension specs (e.g. PL16x461.4, HN450x200x9x14)
"""
import ezdxf, re, sys

def diag_msp(dxf_path, max_lines=60):
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    lines_out = []
    texts_out = []
    blocks_out = []

    for e in msp:
        t = e.dxftype()
        if t == 'LINE':
            s = e.dxf.start; ep = e.dxf.end
            import math
            ln = math.hypot(ep.x-s.x, ep.y-s.y)
            if ln > 0.5:  # skip micro-lines
                lines_out.append(f"  LINE  len={round(ln*10,1)}mm  ({round(s.x,1)},{round(s.y,1)})->({round(ep.x,1)},{round(ep.y,1)})")
        elif t == 'LWPOLYLINE':
            try:
                pts = list(e.get_points())
                import math
                total = sum(math.hypot(pts[i+1][0]-pts[i][0], pts[i+1][1]-pts[i][1])
                            for i in range(len(pts)-1))
                lines_out.append(f"  LWPOLY  len={round(total*10,1)}mm  pts={len(pts)}")
            except: pass
        elif t == 'ARC':
            import math
            r = e.dxf.radius
            a1, a2 = e.dxf.start_angle, e.dxf.end_angle
            span = (a2-a1) % 360
            arc_len = r * math.radians(span)
            lines_out.append(f"  ARC  len={round(arc_len*10,1)}mm  r={round(r*10,1)}")
        elif t in ('TEXT', 'MTEXT'):
            txt = (e.dxf.text if t=='TEXT' else e.text).strip()
            if txt:
                texts_out.append(f"  {t}  {txt!r}")
        elif t == 'INSERT':
            blocks_out.append(f"  INSERT  {e.dxf.name}")

    print(f"\n=== {dxf_path.split(chr(92))[-1]} ===")
    print(f"Standalone LINE/LWPOLY/ARC ({len(lines_out)} total):")
    for ln in lines_out[:max_lines]:
        print(ln)
    if len(lines_out) > max_lines:
        print(f"  ... {len(lines_out)-max_lines} more")
    print(f"\nTEXT/MTEXT ({len(texts_out)} total):")
    # Show only those matching plate/section spec patterns
    dim_texts = [t for t in texts_out if re.search(r'[xX×]\d', t)]
    for t in dim_texts[:max_lines]:
        print(t)
    if not dim_texts:
        print("  (none matching dimension pattern)")
    print(f"\nTotal INSERT blocks: {len(blocks_out)}")

if __name__ == '__main__':
    import glob, os
    folder = r'c:\Users\hp\OneDrive\Desktop\dxf\hanf'
    for f in sorted(glob.glob(os.path.join(folder, '*.dxf')))[:3]:
        diag_msp(f)
