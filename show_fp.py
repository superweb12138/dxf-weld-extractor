"""Show false positives and missed rows per component."""
import openpyxl
from collections import defaultdict, Counter

def load(path, sheet=None):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1] is None: continue
        rows.append({'pos':r[1],'hf':r[2] or 0,'len':round(float(r[3] or 0),1),
                     'ann':r[4] or '','p1':r[5],'p2':r[6],'comp':r[7]})
    return rows

def loose_key(r):
    p = tuple(sorted((str(r['p1'] or ''),str(r['p2'] or ''))))
    return (r['comp'],r['pos'],r['hf'],p)

manual = load('焊缝统计.xlsx', 'Weld_number_list(Assembly)')
script = load('焊缝统计_auto.xlsx', '焊缝统计')

HAVE_DXF = {'BE018','BE019','BE020','BE021','BE022','BE023','CO007','CO008','CO009'}
manual = [r for r in manual if r['comp'] in HAVE_DXF]
script = [r for r in script if r['comp'] in HAVE_DXF]

ml2, sl2 = list(manual), list(script)
used_s = set(); matched_m = set()
for i, m in enumerate(ml2):
    for j, s in enumerate(sl2):
        if j in used_s: continue
        if loose_key(m) == loose_key(s):
            used_s.add(j); matched_m.add(i); break

missed = [r for i,r in enumerate(ml2) if i not in matched_m]
extra  = [r for j,r in enumerate(sl2) if j not in used_s]

def fmt(r): return f"  {r['pos']:<6} hf={r['hf']:<4} len={r['len']:<8} {r['p1']}/{r['p2']}"

for comp in sorted(HAVE_DXF):
    m_rows = [r for r in missed if r['comp']==comp]
    e_rows = [r for r in extra  if r['comp']==comp]
    if not m_rows and not e_rows: continue
    print(f"\n=== {comp} ===")
    if m_rows:
        print("  MISSED:")
        for r in m_rows: print(fmt(r))
    if e_rows:
        print("  FALSE POSITIVE:")
        for r in e_rows: print(fmt(r))
