import ezdxf, re, math
from collections import defaultdict

dxf_path = r"D:\hanf\361-RC3210-S-01-CO010_02.dxf"
doc = ezdxf.readfile(dxf_path)

key_views = ["1225", "3710", "48091", "50420", "50538", "5891", "9869"]

print("="*80)
print("PART BLOCK DIMENSION SUMMARY FOR KEY VIEWS")
print("="*80)

for vid in key_views:
    parts = [b for b in doc.blocks if b.name.startswith("Part") and b.name.endswith(" - " + vid)]
    if not parts:
        continue
    print("\n--- View %s (%d parts) ---" % (vid, len(parts)))
    for blk in sorted(parts, key=lambda b: b.name):
        xs = []; ys = []
        for e in blk:
            if e.dxftype() == "LINE":
                try:
                    s = e.dxf.start; ep = e.dxf.end
                    xs.extend([s.x, ep.x]); ys.extend([s.y, ep.y])
                except: pass
        if xs and ys:
            w = (max(xs) - min(xs)) * 10
            h = (max(ys) - min(ys)) * 10
            edges = []
            for e in blk:
                if e.dxftype() == "LINE":
                    try:
                        s = e.dxf.start; ep = e.dxf.end
                        ln = math.sqrt((s.x-ep.x)**2 + (s.y-ep.y)**2) * 10
                        edges.append(ln)
                    except: pass
            edges.sort(reverse=True)
            major_edges = [e for e in edges[:4] if e > 10]
            sid = blk.name.split("-")[-1].strip()
            short_name = blk.name.split(" - ")[0].split("-")[-1]
            edge_str = " ".join("%.1f" % e for e in major_edges[:3])
            print("  %s (id=%s): %.1f x %.1f mm, edges=[%s]" % (short_name, sid, w, h, edge_str))

print("\n" + "="*80)
print("MARK BLOCKS WITH sp23, sp22, p182, p183, p184 IN KEY VIEWS")
print("="*80)

key_labels = ["sp23", "sp22", "sp27", "p182", "p183", "p184", "p194", "p195", "p196", "p207", "p212"]
for blk in doc.blocks:
    if not blk.name.startswith("Mark"):
        continue
    texts = []
    for e in blk:
        if e.dxftype() == "TEXT":
            try: texts.append(e.dxf.text)
            except: pass
    if any(t in key_labels for t in texts):
        m = re.search(r" - (\d+)$", blk.name)
        view_id = m.group(1) if m else "unknown"
        print("%s (view %s): %s" % (blk.name, view_id, texts))
