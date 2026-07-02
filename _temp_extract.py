import openpyxl
from collections import defaultdict

def load(path, sheet=None):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[1] is None: continue
        rows.append({
            'pos': r[1], 'hf': r[2], 'len': round(float(r[3] or 0), 1),
            'ann': r[4] or '', 'p1': str(r[5] or '').upper(), 'p2': str(r[6] or '').upper(),
            'comp': str(r[7] or '')
        })
    return rows

manual = load('焊缝统计R3_auto(1).xlsx')
script = load('焊缝统计_auto.xlsx', '焊缝统计')
manual = [r for r in manual if r['comp'] in ('CO008','CO009','CO010')]
script = [r for r in script if r['comp'] in ('CO008','CO009','CO010')]

print('=== MANUAL CO008 ===')
for r in manual:
    if r['comp']=='CO008':
        print(f"{r['pos']:8} hf={str(r['hf']):5} len={r['len']:7} {r['ann']:10} {r['p1']:12} {r['p2']:12}")

print()
print('=== SCRIPT CO008 ===')
for r in script:
    if r['comp']=='CO008':
        print(f"{r['pos']:8} hf={str(r['hf']):5} len={r['len']:7} {r['ann']:10} {r['p1']:12} {r['p2']:12}")

print()
print('=== MANUAL CO009 ===')
for r in manual:
    if r['comp']=='CO009':
        print(f"{r['pos']:8} hf={str(r['hf']):5} len={r['len']:7} {r['ann']:10} {r['p1']:12} {r['p2']:12}")

print()
print('=== SCRIPT CO009 ===')
for r in script:
    if r['comp']=='CO009':
        print(f"{r['pos']:8} hf={str(r['hf']):5} len={r['len']:7} {r['ann']:10} {r['p1']:12} {r['p2']:12}")

print()
print('=== MANUAL CO010 ===')
for r in manual:
    if r['comp']=='CO010':
        print(f"{r['pos']:8} hf={str(r['hf']):5} len={r['len']:7} {r['ann']:10} {r['p1']:12} {r['p2']:12}")

print()
print('=== SCRIPT CO010 ===')
for r in script:
    if r['comp']=='CO010':
        print(f"{r['pos']:8} hf={str(r['hf']):5} len={r['len']:7} {r['ann']:10} {r['p1']:12} {r['p2']:12}")
