"""Check what block types exist and look for dimension text in Part/Mark blocks."""
import ezdxf, re, glob, os

def diag(dxf_path):
    doc = ezdxf.readfile(dxf_path)
    comp = re.search(r'-(BE|CO)\d+', dxf_path).group(0)[1:]

    # Collect block name prefixes
    prefixes = {}
    for blk in doc.blocks:
        m = re.match(r'^([A-Za-z]+)', blk.name)
        if m:
            p = m.group(1)
            prefixes[p] = prefixes.get(p, 0) + 1

    print(f"\n{comp}: block prefixes = {dict(sorted(prefixes.items()))}")

    # Look for dimension-like text in ANY block
    dim_pat = re.compile(r'(?:PL|HN|HW|HM|HQ|UC|UB|W|HP)\s*\d+[xX×]', re.I)
    found_dims = []
    for blk in doc.blocks:
        for e in blk:
            if e.dxftype() in ('TEXT', 'MTEXT'):
                try:
                    txt = (e.dxf.text if e.dxftype()=='TEXT' else e.text).strip()
                    if dim_pat.search(txt):
                        found_dims.append(f"  [{blk.name[:40]}] {txt!r}")
                except: pass

    if found_dims:
        print("  Dimension texts found:")
        for d in found_dims[:20]:
            print(d)
    else:
        print("  No dimension texts found in any block")

if __name__ == '__main__':
    folder = r'c:\Users\hp\OneDrive\Desktop\dxf\hanf'
    for f in sorted(glob.glob(os.path.join(folder, '*.dxf'))):
        diag(f)
