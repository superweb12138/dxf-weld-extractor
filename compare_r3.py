"""Compare script output against R3_auto(1) manual (11 DXF components)."""
import openpyxl
from collections import Counter, defaultdict

def load(path, sheet=None):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1] is None:
            continue
        rows.append({'pos': r[1], 'hf': r[2] or 0, 'len': round(float(r[3] or 0), 1),
                     'ann': r[4] or '', 'p1': r[5], 'p2': r[6], 'comp': r[7]})
    return rows

manual = load(r'C:\Users\15297\Desktop\hanf\焊缝统计R3_auto(1).xlsx')
script = load(r'C:\Users\15297\Desktop\hanf\焊缝统计_auto.xlsx', '焊缝统计')

HAVE_DXF = {'BE018','BE019','BE020','BE021','BE022','BE023','CO006','CO007','CO008','CO009','CO010'}
manual = [r for r in manual if r['comp'] in HAVE_DXF]
script = [r for r in script if r['comp'] in HAVE_DXF]

mc = Counter(r['comp'] for r in manual)
sc = Counter(r['comp'] for r in script)
print(f"{'Comp':<8} {'Man':>5} {'Scr':>5} {'Diff':>6}")
for c in sorted(HAVE_DXF):
    print(f"{c:<8} {mc.get(c,0):>5} {sc.get(c,0):>5} {sc.get(c,0)-mc.get(c,0):>+6}")
print(f"{'TOTAL':<8} {sum(mc.values()):>5} {sum(sc.values()):>5}")
print()

def loose_key(r):
    p = tuple(sorted((str(r['p1'] or ''), str(r['p2'] or ''))))
    return (r['comp'], r['pos'], r['hf'], p)

ml = Counter(loose_key(r) for r in manual)
sl = Counter(loose_key(r) for r in script)
print("=== LOOSE match (ignore length & annotation) ===")
print(f"  matched : {sum((ml & sl).values())}")
print(f"  man-only: {sum((ml - sl).values())}")
print(f"  scr-only: {sum((sl - ml).values())}")

total = sum((ml & sl).values()) + sum((ml - sl).values()) + sum((sl - ml).values())
matched = sum((ml & sl).values())
print(f"\nMatch rate: {matched}/{total} = {matched/total*100:.1f}%")
