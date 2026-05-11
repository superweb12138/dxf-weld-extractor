"""Parse BOM rows from Unknown block - find Mark+Description+Length on same Y row."""
import ezdxf, re, glob, os

def parse_bom(dxf_path):
    doc = ezdxf.readfile(dxf_path)
    comp = re.search(r'-(BE|CO)\d+', dxf_path).group(0)[1:]
    for blk in doc.blocks:
        if not (blk.name.startswith('Unknown') and ' - ' not in blk.name):
            continue
        rows = {}
        for e in blk:
            if e.dxftype() not in ('TEXT','MTEXT'):
                continue
            try:
                txt = (e.dxf.text if e.dxftype()=='TEXT' else e.text).strip()
                x = round(e.dxf.insert.x, 0)
                y = round(e.dxf.insert.y, 1)
                if not txt: continue
                yk = round(y / 4) * 4
                rows.setdefault(yk, {})[x] = txt
            except: pass
        print(f"\n{comp} BOM (from {blk.name}):")
        marks_seen = False
        for yk in sorted(rows, reverse=True):
            row = rows[yk]
            xs = sorted(row)
            vals = [row[x] for x in xs]
            # Check if this row has a part label (pXXX or comp name)
            part_label = next((v for v in vals if re.match(r'^p\d+$', v) or v == comp), None)
            spec = next((v for v in vals if re.search(r'(?:PL|HW|HN|HM)\d+[xX]', v)), None)
            length = next((v for v in vals if re.match(r'^\d+\.?\d*$', v) and float(v) > 50), None)
            qty = next((v for v in vals if re.match(r'^\d{1,2}$', v)), None)
            if part_label and spec:
                marks_seen = True
                print(f"  {part_label:8s}  {spec:20s}  len={length or '?':8s}  qty={qty or '?'}")
        if marks_seen:
            break  # found the BOM block

if __name__ == '__main__':
    folder = r'c:\Users\hp\OneDrive\Desktop\dxf\hanf'
    for f in sorted(glob.glob(os.path.join(folder, '*.dxf'))):
        parse_bom(f)
