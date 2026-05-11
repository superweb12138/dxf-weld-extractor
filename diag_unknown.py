"""Dump all TEXT/MTEXT in Unknown blocks to see part number + spec pairing."""
import ezdxf, re, glob, os

def diag(dxf_path):
    doc = ezdxf.readfile(dxf_path)
    comp = re.search(r'-(BE|CO)\d+', dxf_path).group(0)[1:]
    for blk in doc.blocks:
        if not blk.name.startswith('Unknown'):
            continue
        texts = []
        for e in blk:
            if e.dxftype() in ('TEXT','MTEXT'):
                try:
                    txt = (e.dxf.text if e.dxftype()=='TEXT' else e.text).strip()
                    x = round(e.dxf.insert.x, 1)
                    y = round(e.dxf.insert.y, 1)
                    if txt:
                        texts.append((y, x, txt))
                except: pass
        if texts:
            texts.sort()
            print(f"\n{comp}  [{blk.name}]:")
            for y, x, t in texts:
                print(f"  y={y:8.1f}  x={x:8.1f}  {t!r}")

if __name__ == '__main__':
    folder = r'c:\Users\hp\OneDrive\Desktop\dxf\hanf'
    for f in sorted(glob.glob(os.path.join(folder, '*.dxf'))):
        diag(f)
