import openpyxl, re
from collections import defaultdict

def load(path, sheet=None):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1] is None: continue
        _c = str(r[7] or '')
        _m = re.search(r'(BE\d+|CO\d+)', _c)
        if _m: _c = _m.group(1)
        rows.append({
            'pos': r[1], 'hf': r[2], 'len': round(float(r[3] or 0), 1),
            'ann': r[4] or '', 'p1': str(r[5] or '').upper(), 'p2': str(r[6] or '').upper(),
            'comp': _c
        })
    return rows

manual = load('焊缝统计R3_auto(1).xlsx')
script = load('焊缝统计_auto.xlsx', '焊缝统计')

def key(r):
    parts = tuple(sorted((r["p1"], r["p2"])))
    hf_key = -1 if r["hf"] is None else r["hf"]
    return (r["comp"], r["pos"], hf_key, parts)

def key_hf(orig_hf):
    return -1 if orig_hf is None else orig_hf

man_map = defaultdict(list)
scr_map = defaultdict(list)
for r in manual:
    k = key(r)
    man_map[k].append(r["len"])
for r in script:
    k = key(r)
    scr_map[k].append(r["len"])

all_keys = sorted(set(list(man_map.keys()) + list(scr_map.keys())))
print("CO008 mismatches:")
hits = 0
for k in all_keys:
    comp, pos, hf_key, parts = k
    if comp != "CO008": continue
    hf = None if hf_key == -1 else hf_key
    mlens = sorted(man_map.get(k, []))
    slens = sorted(scr_map.get(k, []))
    if mlens != slens:
        hf_s = "CJP" if hf is None else str(hf)
        print("  pos=%-8s hf=%-5s %-22s man=%-30s scr=%s" % (pos, hf_s, "/".join(parts), str(mlens), str(slens)))
        hits += 1
if hits == 0: print("  (none)")

print()
print("CO009 mismatches:")
hits = 0
for k in all_keys:
    comp, pos, hf_key, parts = k
    if comp != "CO009": continue
    hf = None if hf_key == -1 else hf_key
    mlens = sorted(man_map.get(k, []))
    slens = sorted(scr_map.get(k, []))
    if mlens != slens:
        hf_s = "CJP" if hf is None else str(hf)
        print("  pos=%-8s hf=%-5s %-22s man=%-30s scr=%s" % (pos, hf_s, "/".join(parts), str(mlens), str(slens)))
        hits += 1
if hits == 0: print("  (none)")

print()
print("CO010 mismatches:")
hits = 0
for k in all_keys:
    comp, pos, hf_key, parts = k
    if comp != "CO010": continue
    hf = None if hf_key == -1 else hf_key
    mlens = sorted(man_map.get(k, []))
    slens = sorted(scr_map.get(k, []))
    if mlens != slens:
        hf_s = "CJP" if hf is None else str(hf)
        print("  pos=%-8s hf=%-5s %-22s man=%-30s scr=%s" % (pos, hf_s, "/".join(parts), str(mlens), str(slens)))
        hits += 1
if hits == 0: print("  (none)")

print()
print("CO010 scr-only:")
for k in sorted(set(scr_map.keys()) - set(man_map.keys())):
    comp, pos, hf_key, parts = k
    if comp != "CO010": continue
    slens = sorted(scr_map[k])
    hf = None if hf_key == -1 else hf_key
    hf_s = "CJP" if hf is None else str(hf)
    print("  pos=%-8s hf=%-5s %-22s scr=%s" % (pos, hf_s, "/".join(parts), str(slens)))

print()
print("CO010 man-only:")
for k in sorted(set(man_map.keys()) - set(scr_map.keys())):
    comp, pos, hf_key, parts = k
    if comp != "CO010": continue
    mlens = sorted(man_map[k])
    hf = None if hf_key == -1 else hf_key
    hf_s = "CJP" if hf is None else str(hf)
    print("  pos=%-8s hf=%-5s %-22s man=%s" % (pos, hf_s, "/".join(parts), str(mlens)))
