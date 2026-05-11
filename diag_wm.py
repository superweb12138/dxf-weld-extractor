"""Dump raw texts and lines for specified WeldMark blocks."""
import ezdxf, math, sys

def seg_len(s, e):
    return math.hypot(e[0]-s[0], e[1]-s[1])

def diag_file(dxf_path, wm_keywords=None):
    doc = ezdxf.readfile(dxf_path)
    msp = doc.modelspace()
    for e in msp:
        if e.dxftype() != 'INSERT':
            continue
        name = e.dxf.name or ''
        if 'WeldMark' not in name:
            continue
        if wm_keywords and not any(k in name for k in wm_keywords):
            continue
        ents = list(e.virtual_entities())
        texts = [(t.dxf.text, t.dxf.insert.x, t.dxf.insert.y)
                 for t in ents if t.dxftype() == 'TEXT' and t.dxf.text]
        lines = [(round(seg_len((l.dxf.start.x, l.dxf.start.y),
                                (l.dxf.end.x, l.dxf.end.y))*10, 1),
                  round(l.dxf.start.x,2), round(l.dxf.start.y,2),
                  round(l.dxf.end.x,2), round(l.dxf.end.y,2))
                 for l in ents if l.dxftype() == 'LINE']
        print(f"\n{name}")
        print(f"  insert=({round(e.dxf.insert.x,2)}, {round(e.dxf.insert.y,2)})")
        print(f"  texts={texts}")
        print(f"  lines(len,x1,y1,x2,y2)={lines}")

if __name__ == '__main__':
    # BE022: all WeldMarks
    diag_file(r'c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-CO007_00.dxf')
