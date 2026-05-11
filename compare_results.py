"""Compare manual vs script weld extraction outputs."""
import openpyxl
from collections import Counter, defaultdict


def load(path, sheet=None):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1] is None:
            continue
        rows.append({
            'pos': r[1], 'hf': r[2], 'len': r[3],
            'ann': r[4] or '', 'p1': r[5], 'p2': r[6], 'comp': r[7],
        })
    return rows


def norm_pair(p1, p2):
    """Sort the part pair so ordering doesn't matter for matching."""
    return tuple(sorted((str(p1 or ''), str(p2 or ''))))


def key_strict(r):
    return (r['comp'], r['pos'], r['hf'],
            int(round(r['len'] or 0)), norm_pair(r['p1'], r['p2']))


def key_loose(r):
    """Match without length (since length is the main discrepancy)."""
    return (r['comp'], r['pos'], r['hf'], norm_pair(r['p1'], r['p2']))


manual = load('焊缝统计.xlsx', 'Weld_number_list(Assembly)')
script = load('weld_output.xlsx', '焊缝统计')

mc = Counter(r['comp'] for r in manual)
sc = Counter(r['comp'] for r in script)
all_comps = sorted(set(mc) | set(sc))
print(f'{"Comp":<10} {"Manual":>7} {"Script":>7} {"Diff":>7}')
for c in all_comps:
    print(f'{c:<10} {mc.get(c,0):>7} {sc.get(c,0):>7} {sc.get(c,0)-mc.get(c,0):>+7}')
print(f'{"TOTAL":<10} {sum(mc.values()):>7} {sum(sc.values()):>7}')

# Strict match
ms = Counter(key_strict(r) for r in manual)
ss = Counter(key_strict(r) for r in script)
matched_strict = sum((ms & ss).values())
only_manual = sum((ms - ss).values())
only_script = sum((ss - ms).values())
print(f'\n=== STRICT match (incl. length) ===')
print(f'  matched: {matched_strict}, only_manual: {only_manual}, only_script: {only_script}')

# Loose match (ignore length)
ml = Counter(key_loose(r) for r in manual)
sl = Counter(key_loose(r) for r in script)
matched_loose = sum((ml & sl).values())
only_manual_l = sum((ml - sl).values())
only_script_l = sum((sl - ml).values())
print(f'\n=== LOOSE match (ignore length) ===')
print(f'  matched: {matched_loose}, only_manual: {only_manual_l}, only_script: {only_script_l}')


# Detailed per-component diff for BE018 first
def show_diff(comp):
    print(f'\n--- {comp} ---')
    print('MANUAL:')
    for r in manual:
        if r['comp'] == comp:
            print(f"  {r['pos']:<6} hf={r['hf']:<4} len={r['len']:<7} {r['p1']}/{r['p2']}  {r['ann']}")
    print('SCRIPT:')
    for r in script:
        if r['comp'] == comp:
            print(f"  {r['pos']:<6} hf={r['hf']:<4} len={r['len']:<7} {r['p1']}/{r['p2']}  {r['ann']}")


for c in all_comps:
    show_diff(c)
