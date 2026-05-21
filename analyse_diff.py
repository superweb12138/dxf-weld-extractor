"""Detailed diff of manual vs script for the 9 available DXFs."""
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

manual = load('焊缝统计.xlsx', 'Weld_number_list(Assembly)')
script = load('焊缝统计_new.xlsx', '焊缝统计')

HAVE_DXF = {'BE018','BE019','BE020','BE021','BE022','BE023','CO007','CO008','CO009'}
manual = [r for r in manual if r['comp'] in HAVE_DXF]
script = [r for r in script if r['comp'] in HAVE_DXF]

# ── per-component count ──────────────────────────────────────────────────────
mc = Counter(r['comp'] for r in manual)
sc = Counter(r['comp'] for r in script)
print(f"{'Comp':<8} {'Man':>5} {'Scr':>5} {'Diff':>6}")
for c in sorted(HAVE_DXF):
    print(f"{c:<8} {mc.get(c,0):>5} {sc.get(c,0):>5} {sc.get(c,0)-mc.get(c,0):>+6}")
print(f"{'TOTAL':<8} {sum(mc.values()):>5} {sum(sc.values()):>5}")

# ── loose-match (ignore length, ignore annotation) ───────────────────────────
def loose_key(r):
    p = tuple(sorted((str(r['p1'] or ''), str(r['p2'] or ''))))
    return (r['comp'], r['pos'], r['hf'], p)

ml = Counter(loose_key(r) for r in manual)
sl = Counter(loose_key(r) for r in script)
print(f"\n=== LOOSE match (ignore length & annotation) ===")
print(f"  matched : {sum((ml & sl).values())}")
print(f"  man-only: {sum((ml - sl).values())}")
print(f"  scr-only: {sum((sl - ml).values())}")

# ── per-comp detailed diff ────────────────────────────────────────────────────
def fmt(r):
    return (f"  {r['pos']:<6} hf={r['hf']:<4} len={r['len']:<8} "
            f"{r['p1']}/{r['p2']:<8}  {r['ann']}")

print("\n" + "="*70)
print("ROWS ONLY IN MANUAL (missed by script)")
print("="*70)
ml2 = list(manual)
sl2 = list(script)
# Match greedily on loose key
used_s = set()
matched_m = set()
for i, m in enumerate(ml2):
    for j, s in enumerate(sl2):
        if j in used_s:
            continue
        if loose_key(m) == loose_key(s):
            used_s.add(j)
            matched_m.add(i)
            break

missed = [r for i, r in enumerate(ml2) if i not in matched_m]
by_comp = defaultdict(list)
for r in missed:
    by_comp[r['comp']].append(r)
for c in sorted(by_comp):
    print(f"\n  [{c}]")
    for r in by_comp[c]:
        print(fmt(r))

print("\n" + "="*70)
print("ROWS ONLY IN SCRIPT (false positives)")
print("="*70)
extra = [r for j, r in enumerate(sl2) if j not in used_s]
by_comp2 = defaultdict(list)
for r in extra:
    by_comp2[r['comp']].append(r)
for c in sorted(by_comp2):
    print(f"\n  [{c}]")
    for r in by_comp2[c]:
        print(fmt(r))

# ── hf errors: matched pairs with wrong hf ───────────────────────────────────
print("\n" + "="*70)
print("MATCHED ROWS WITH WRONG LENGTH (loose key matches, length differs)")
print("="*70)
def strict_key(r):
    p = tuple(sorted((str(r['p1'] or ''), str(r['p2'] or ''))))
    return (r['comp'], r['pos'], r['hf'], p, r['len'])

ml3 = Counter(strict_key(r) for r in manual)
sl3 = Counter(strict_key(r) for r in script)
# For each loose match, find length discrepancy
from itertools import groupby
m_by_loose = defaultdict(list)
s_by_loose = defaultdict(list)
for r in manual: m_by_loose[loose_key(r)].append(r['len'])
for r in script: s_by_loose[loose_key(r)].append(r['len'])

for k in sorted(set(m_by_loose) & set(s_by_loose)):
    ml_lens = sorted(m_by_loose[k])
    sl_lens = sorted(s_by_loose[k])
    if ml_lens != sl_lens:
        comp, pos, hf, parts = k
        print(f"  {comp} {pos} hf={hf} {'/'.join(parts)}")
        print(f"    manual: {ml_lens}")
        print(f"    script: {sl_lens}")
