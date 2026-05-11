import sys, importlib
sys.path.insert(0, '.')
import weld_extractor as we
importlib.reload(we)

rows, skipped = we.extract_welds('361-RC3210-S-01-BE018_00.dxf')
print()
print('--- Final rows (%d) ---' % len(rows))
for i, r in enumerate(rows, 1):
    print('%2d. %-5s  hf=%5s  len=%6.1fmm  %-6s / %-6s  annot=%r' % (
        i, r['position'], r['hf'], r['length_mm'], r['part1'], r['part2'], r['annotation']))
print()
print('--- Skipped (%d) ---' % len(skipped))
for s in skipped:
    print(' ', s)
