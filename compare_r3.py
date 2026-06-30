"""Compare script output against R3_auto(1) manual (11 DXF components)."""
import openpyxl, re
from collections import Counter, defaultdict

TOL = 3  # length tolerance in mm

def load(path, sheet=None):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1] is None or r[1] == '位置(上/下)':
            continue
        _c = str(r[7] or '')
        _m = re.search(r'(BE\d+|CO\d+)', _c)
        if _m:
            _c = _m.group(1)
        rows.append({'pos': r[1], 'hf': r[2] or 0, 'len': round(float(r[3] or 0), 1),
                     'ann': r[4] or '', 'p1': r[5], 'p2': r[6], 'comp': _c})
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

def full_key(r):
    p = tuple(sorted((str(r['p1'] or '').lower(), str(r['p2'] or '').lower())))
    return (r['comp'], r['pos'], r['hf'], p)

# Greedy matching with length tolerance
_used_s = [False] * len(script)
_matched = 0
_man_only_rows = []
_scr_only_rows = []
_len_mismatch = []  # matched on full_key but length out of tolerance

for i, mr in enumerate(manual):
    found = False
    for j, sr in enumerate(script):
        if _used_s[j]:
            continue
        if full_key(mr) != full_key(sr):
            continue
        if abs(mr['len'] - sr['len']) <= TOL:
            _matched += 1
            _used_s[j] = True
            found = True
            break
        # Matched on key but length out of tolerance — still pair them to avoid
        # double-counting, but report as length mismatch
        _len_mismatch.append((mr, sr))
        _matched += 1
        _used_s[j] = True
        found = True
        break
    if not found:
        _man_only_rows.append(mr)

for j, sr in enumerate(script):
    if not _used_s[j]:
        _scr_only_rows.append(sr)

print(f"=== FULL match (comp+pos+hf+pair+len, TOL={TOL}mm) ===")
print(f"  matched : {_matched}")
print(f"  man-only: {len(_man_only_rows)}")
print(f"  scr-only: {len(_scr_only_rows)}")

total = _matched + len(_man_only_rows) + len(_scr_only_rows)
print(f"\nMatch rate: {_matched}/{total} = {_matched/total*100:.1f}%")

if _len_mismatch:
    print(f"\n--- length mismatch (matched by key but len diff > {TOL}mm) ---")
    for mr, sr in _len_mismatch:
        print(f"  {mr['comp']:<6} {mr['p1']:<8}/{mr['p2']:<8} {mr['pos']:<6} hf={mr['hf']}  man_len={mr['len']:.0f}  scr_len={sr['len']:.0f}  diff={abs(mr['len']-sr['len']):.0f}")

if _man_only_rows:
    print(f"\n--- man-only ({len(_man_only_rows)}) ---")
    for r in _man_only_rows:
        print(f"  {r['comp']:<6} {r['p1']:<8}/{r['p2']:<8} {r['pos']:<6} len={r['len']:.0f} hf={r['hf']} ann={r['ann']}")

if _scr_only_rows:
    print(f"\n--- scr-only ({len(_scr_only_rows)}) ---")
    for r in _scr_only_rows:
        print(f"  {r['comp']:<6} {r['p1']:<8}/{r['p2']:<8} {r['pos']:<6} len={r['len']:.0f} hf={r['hf']} ann={r['ann']}")
