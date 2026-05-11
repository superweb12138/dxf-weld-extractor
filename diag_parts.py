"""Diagnostic: dump all lines for specified parts across all views in a DXF."""
import ezdxf, sys, math

def seg_len(s, e):
    return math.hypot(e[0]-s[0], e[1]-s[1])

def diag(dxf_path, part_labels):
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()

    # collect view IDs
    views = {}
    for e in msp:
        if e.dxftype() == 'INSERT' and 'Part' in (e.dxf.name or ''):
            hdr = e.dxf.name
            parts = list(e.virtual_entities())
            view_id = hdr.split(' - ')[-1] if ' - ' in hdr else '?'
            views.setdefault(view_id, {}).setdefault(hdr, parts)

    # find Mark blocks for labels
    label_map = {}
    for e in msp:
        if e.dxftype() == 'INSERT' and 'Mark' in (e.dxf.name or ''):
            texts = [t.dxf.text for t in e.virtual_entities() if t.dxftype()=='TEXT']
            lbl = next((t for t in texts if t and not t.startswith('(')), None)
            if not lbl: continue
            # find associated Part name
            for s in texts:
                if 'Part' in s:
                    label_map[s.strip()] = lbl
                    break

    # For each view, find parts matching requested labels
    for vid in sorted(views.keys()):
        printed = False
        for hdr, ents in views[vid].items():
            pname = label_map.get(hdr, '?')
            if pname not in part_labels:
                continue
            lines = []
            for ent in ents:
                if ent.dxftype() == 'LINE':
                    s = (ent.dxf.start.x, ent.dxf.start.y)
                    e = (ent.dxf.end.x, ent.dxf.end.y)
                    lines.append(seg_len(s, e)*10)
            if lines:
                if not printed:
                    print(f"\n=== view {vid} ===")
                    printed = True
                print(f"  Part {hdr} ({pname}): lines={[round(l,1) for l in sorted(lines, reverse=True)]}")

if __name__ == '__main__':
    # BE021: inspect p122 and p123 lines in all views
    diag(r'c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE021_00.dxf',
         ['p122', 'p123'])
