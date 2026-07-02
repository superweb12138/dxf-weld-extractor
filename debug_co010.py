"""
独立调试脚本：只对 CO010 进行提取 + 标注
不修改 weld_extractor.py 的默认行为
"""

import sys, os, time, glob
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import importlib, importlib.util as iu

spec = iu.spec_from_file_location('we', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'weld_extractor.py'))
we = iu.module_from_spec(spec)
spec.loader.exec_module(we)

import dxf_annotator as da
importlib.reload(da)
import ezdxf

DXF_PATH = r'C:\Users\15297\Desktop\hanf\361-RC3210-S-01-CO010_02.dxf'
ANNO_PATH = r'C:\Users\15297\Desktop\hanf\annotated\361-RC3210-S-01-CO010_02.dxf'

# ---- 1. 提取 ----
t0 = time.time()
print('=== 提取 CO010 ===')
results, skipped = we.extract_welds(DXF_PATH)
print(f'提取完成：{len(results)} 行焊道，{len(skipped)} 行跳过，用时 {time.time()-t0:.0f}s')
if not results:
    print('无焊道结果，退出。'); sys.exit(0)

# ---- 2. 标注 ----
print('\n=== 标注 CO010 ===')
t0 = time.time()
da.annotate(results, [DXF_PATH])
anno_time = time.time() - t0
print(f'标注用时：{anno_time:.0f}s')

# ---- 3. 验证 ----
print('\n=== 验证 ===')
doc = ezdxf.readfile(ANNO_PATH)
msp = doc.modelspace()

lbs = []
for e in msp:
    if e.dxftype() == 'MTEXT' and e.dxf.layer == 'WELD_LABELS':
        t = e.dxf.text.strip(); ins = e.dxf.insert; att = e.dxf.attachment_point
        w = 2.5 * (6.5 if ',' in t else 3.2)
        bx0, bx1 = (ins.x, ins.x + w) if att in (7,) else (ins.x - w, ins.x)
        lbs.append((t, ins.x, ins.y, bx0, bx1, ins.y, ins.y + 2.5))

# 标签重叠检查
overlaps = 0
for i in range(len(lbs)):
    for j in range(i + 1, len(lbs)):
        t1,x1,y1,x01,x11,y01,y11 = lbs[i]
        t2,x2,y2,x02,x12,y02,y12 = lbs[j]
        ox = max(0, min(x11, x12) - max(x01, x02))
        oy = max(0, min(y11, y12) - max(y01, y02))
        if ox > 0 and oy > 0:
            print(f'  重叠：{t1}({x1:.0f},{y1:.0f}) x {t2}({x2:.0f},{y2:.0f}) {ox:.1f}x{oy:.1f}')
            overlaps += 1

# 统计
fs = sum(1 for t,_,_,_,_,_,_ in lbs if t.startswith('F'))
ws = sum(1 for t,_,_,_,_,_,_ in lbs if t.startswith('W'))
print(f'\nF 标签：{fs}  W 标签：{ws}  合计：{fs+ws}  MTEXT 数：{len(lbs)}')
print(f'标签重叠：{overlaps}')
print(f'运行时：{anno_time:.0f}s（{len(results)} 焊道 × {len(lbs)} 标签）')
print('完成！')
