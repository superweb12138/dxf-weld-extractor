# 焊缝统计自动提取工具

从结构构件 DXF 图纸中自动提取焊缝信息，输出 Excel 焊缝统计表。

---

## 目录

- [项目背景](#项目背景)
- [环境依赖](#环境依赖)
- [文件说明](#文件说明)
- [快速开始](#快速开始)
- [核心脚本详解](#核心脚本详解)
- [DXF 结构约定](#dxf-结构约定)
- [关键参数与常量](#关键参数与常量)
- [算法流程](#算法流程)
- [当前精度状态](#当前精度状态)
- [已知遗留问题](#已知遗留问题)
- [诊断脚本说明](#诊断脚本说明)

---

## 项目背景

钢结构工程图纸（DWG/DXF 格式）中，焊缝信息分散在各构件图中，需要人工汇总统计。本工具通过解析 DXF 文件内的 `WeldMark` 和 `Part` 块，自动提取每道焊缝的：

- 所属构件（如 BE018、CO007）
- 焊缝位置（Above / Below 箭头侧）
- 焊脚尺寸 hf（mm），CJP 坡口焊为 None
- 焊缝长度（mm）
- 连接零件对（如 `BE020/p175`）

输出文件：`焊缝统计_auto.xlsx`

---

## 环境依赖

```
Python 3.9+
ezdxf >= 1.1
openpyxl >= 3.1
```

安装：

```bash
pip install ezdxf openpyxl
```

---

## 文件说明

### 主要脚本

| 文件 | 用途 |
|------|------|
| `weld_extractor.py` | **核心**：读取所有 DXF → 输出 `焊缝统计_auto.xlsx` |
| `compare_lengths.py` | 将脚本输出与人工标准答案做精确对比 |
| `analyse_diff.py` | 对比脚本输出与人工答案的逐构件详细差异 |
| `convert_dwg_to_dxf.py` | 批量把 DWG 转换为 DXF（只需运行一次）|

### 数据文件

| 文件 | 说明 |
|------|------|
| `焊缝统计_auto.xlsx` | 脚本自动生成的焊缝统计（每次运行覆盖）|
| `焊缝统计R3_auto(1).xlsx` | **人工标准答案**，用于精度对比 |
| `焊缝统计.xlsx` | 另一份人工参考表（`analyse_diff.py` 使用）|
| `*.dxf` | 各构件 DXF 图纸（由 DWG 转换得到）|

### 有 DXF 文件的构件

```
BE018  BE019  BE020  BE021  BE022  BE023
CO007  CO008  CO009
```

> CO006、CO010 已加入处理流程（DXF 文件已导入）。

---

## 快速开始

### 第一步：转换 DWG（如 DXF 已存在可跳过）

```bash
python convert_dwg_to_dxf.py
```

### 第二步：提取焊缝

```bash
python weld_extractor.py
```

输出：`焊缝统计_auto.xlsx`

### 第三步：与标准答案对比（可选）

```bash
python compare_lengths.py
```

输出每行的状态：`OK` / `LEN-DIFF` / `MISSED` / `SCRIPT-ONLY`，并汇总精确匹配数。

---

## 核心脚本详解

### `weld_extractor.py`

#### 顶部配置（修改这里来适配新图纸）

```python
FOLDER   = r"c:\...\hanf"       # DXF 文件所在目录
OUTPUT   = os.path.join(FOLDER, "焊缝统计_auto.xlsx")
SCALE    = 10.0                  # 1 CAD 单位 = 10 mm
SNAP_TOL = 1.5                   # 焊缝箭头与零件线的捕捉容差（CAD 单位）
MAX_HF   = 20                    # hf 上限；超过此值视为板厚标注
LABEL_TIP_TOL = 8.0              # 引线端点匹配零件的容差（CAD 单位）
```

#### 主要函数

| 函数 | 说明 |
|------|------|
| `parse_weldmark(blk)` | 解析 WeldMark 块：提取 hf、CJP 标志、`3 SIDES`/`2 SIDES` 标注、`TYP` 标志、箭头位置 |
| `get_part_lines(blk)` | 获取 Part 块中所有 LINE 实体的几何信息 |
| `find_all_labels(doc)` | 从 Mark 块的引线端点提取零件编号（如 p122）|
| `assign_labels_by_leader_tip(...)` | 将零件编号分配到对应的 Part 块 |
| `choose_weld_line(arrow, matches)` | 四级规则确定焊缝所在的零件线 |
| `parse_bom(doc, comp)` | 解析 Unknown 块中的材料表，得到零件厚度/宽度/长度 |
| `hf_from_thickness(t)` | 按板厚查标准最小填角焊脚尺寸 |
| `extract_welds(dxf_path)` | 单个 DXF 文件的完整提取逻辑（主函数）|

---

## DXF 结构约定

脚本依赖以下 DXF 块命名规则（由 Tekla/AutoCAD 导出）：

```
WeldMark-<ID> - <视图ID>   → 焊缝标注块
Part-<ID> - <视图ID>       → 零件几何块
Mark-<ID> - <视图ID>       → 零件编号引线块
Unknown-<ID>               → 材料表（BOM）块（无视图ID后缀）
```

同一 `视图ID` 的 WeldMark 和 Part 块属于同一个视图，脚本按视图分组处理。

---

## 关键参数与常量

### 几何容差

| 常量 | 值 | 含义 |
|------|----|------|
| `SNAP_TOL` | 1.5 CAD | 箭头端点到零件线的最大距离 |
| `LABEL_TIP_TOL` | 8.0 CAD | 引线端点匹配零件线的最大距离 |
| `MIN_EDGE` | 1.5 CAD | 3-SIDES 中忽略的退化短边（< 15 mm）|
| `ADJ_TOL` | `SNAP_TOL+0.5` | 3-SIDES 中判断零件边邻接的容差 |

### hf 修正（Sub-rule 3）

当 WM 标注的尺寸恰好等于板厚或腹板厚时，按标准表替换为最小填角尺寸：

```python
_HF_FROM_T = {6:5, 7:5, 8:6, 9:6, 10:7, 11:8, 12:8, 14:10, 16:10, 18:12, 20:12}
```

### TYP（典型焊缝）处理

WM 文本包含 `TYP` 时，表示图中只标了一次，但实际有多个对称实例。脚本会：

1. 统计**主视图**（Part 块最多的视图）中该零件的实例数 → `typ_multiplier`
2. 将焊缝行复制 `typ_multiplier` 份输出
3. 对于 3-SIDES TYP：`typ_multiplier ÷ len(gusset_names)`，避免与多筋板逻辑重复计数

### BOM 回退（comp/comp 情形）

当 WM 箭头落在构件本体（comp）自身线上导致两端零件都是 comp 时，脚本扫描 BOM 中的零件宽度，找到与焊缝几何长度最接近的非 comp 零件（容差 15%），并以该零件在 `part_number_map` 中的实例数确定输出行数。

---

## 算法流程

```
DXF 文件
  │
  ├─ parse_bom()           读取材料表 → part_dims, comp_dims
  │
  ├─ 按视图 ID 分组
  │    WeldMark 块 → wm_by_view
  │    Part 块    → part_by_view
  │
  ├─ find_all_labels()     解析引线 → 零件编号
  ├─ assign_labels_by_leader_tip()  → part_number_map
  │
  └─ 对每个视图中的每个 WeldMark：
       │
       ├─ parse_weldmark()   提取 hf/CJP/annotation/is_typ
       │
       ├─ [3-SIDES 分支]
       │    找最小非 comp 零件作为筋板 (gusset)
       │    枚举筋板所有邻接边 → edge_rows
       │    TYP 倍数 × edge_rows → results
       │
       └─ [普通 WM 分支]
            choose_weld_line() 确定焊缝零件和长度
            hf 修正（Sub-rule 3）
            haunch 端面长度修正
            BOM 宽度修正
            TYP 倍数 / BOM 回退倍数
            → results

  结果写入 Excel（焊缝统计_auto.xlsx）
```

---

## 当前精度状态

以 `焊缝统计R3_auto(1).xlsx` 为标准答案：

| 构件 | 行数（AUTO） | 行数（CORR）| 状态 |
|------|:-----------:|:-----------:|------|
| BE018 | 14 | 14 | ✅ 完全匹配 |
| BE019 | 14 | 14 | ✅ 完全匹配 |
| BE020 | 22 | 22 | ✅ 完全匹配 |
| BE021 | 20 | 20 | ✅ 行数匹配（p122/p123 标签、155mm 缺边）|
| BE022 | 30 | 30 | ✅ 完全匹配 |
| BE023 | 22 | 22 | ✅ 完全匹配 |
| CO007 | 37 | 48 | ⚠️ p100/p47 源文件无 WM |
| CO008 | 33 | 42 | ⚠️ 同上 |
| CO009 | 20 | 28 | ⚠️ hf/边数与手册偏差 |
| CO006 | 2 | 12 | ⚠️ DXF 仅 2 个 WM |
| CO010 | 91 | 166 | ⚠️ TYP + 缺 BOM + 自引用 |
| **合计** | **305** | — | |

---

## 已修复问题（按类别汇总）

### BOM 解析与长度映射
- **BOM qty 修正**（`:506-531`）：按 X 坐标排序取第二个 `\d{1,2}` 数字（数量列），修复 qty 取到序号的问题
- **CO BOM 长度回退**（`:1127-1144`）：柱型构件剖面投影失真，geo→bw/bl 容差 35%（原 25%/8%），p124/p125 `90.5→116, 170→220`
- **p200 Strategy B**（`:836-855`）：排序盲配→接近度匹配，BE023 `268→268` 不再错配 `140`
- **p126 Strategy A 歧义守卫**（`:852-854`）：两 BOM 维度距离差 <8% 时跳过，避免 `90.5→110` 误修正
- **p26/p48 case1-skip**（`:1030-1034`）：geo=bl 时不再错误替换为 bw

### CJP / hf 修正
- **CJP 填角移动**（`:217-222`）：CJP+填角同侧时填角移至对侧
- **CJP groove 守卫**（`:1110-1114, :758-762`）：groove 存在时跳过板厚→hf 修正
- **双面同值守卫**（`:1155, :760`）：上下侧 hf 相同且 ≤20 时跳过修正（p125 `9→6` 误修）
- **非 BOM 零件 hf 回退**（`:1065-1072, :1314-1322, :1335-1343`）：用 `comp_web_t` 推算默认填角

### Part 标签与自引用
- **comp-backoff**（`:965-973`）：视图缺 comp Part 时，p124/p47→CO007/p47
- **无标签透传**（`:703-730`）：无标签 Part→最近已标签邻居（CO009 Part-3124→p16）
- **CO 自引用豁免**（`:932, :1160`）：仅 CO 构件的 comp/comp 标签保留，非 comp 及 BE 仍跳过

### 边数与去重
- **跨视图去重**（`:650-652, 889-898`）：同标签+同长度跨视图时去重，仅 BOM qty=1 启用
- **自由边剔除**（`:767-820`）：合并后 >3 边时按连接数剔除，仅 conn=0 丢弃
- **单端点过滤**（`:762-766`）：无标签+无透传的单端点边排除

## 已知遗留问题

| 构件 | 缺口 | 根因 | 可修？ |
|------|------|------|:--:|
| CO007 | p100 全部 ~10行、p47[250×2] | DXF 中无对应 WeldMark | ✗ |
| CO008 | 同上类 + p101/p102 标签 | 同上 + 透传映射偏差 | ✗/⚠ |
| CO009 | hf/边数 ~8行 | 手册边数/长度与几何不完全对应 | ⚠ |
| CO006 | 10行 | DXF 仅 2 个 WM | ✗ |
| CO010 | 75行 | TYP 偏差 + p183/p199 等缺 BOM | ⚠ |
| BE021 | p122/p123 标签、155mm 缺边 | 双端邻接不同零件需架构改动 | ✗ |

---

## 诊断脚本说明

| 脚本 | 用途 |
|------|------|
| `compare_lengths.py` | 精确对比 `焊缝统计_auto.xlsx` 与标准答案，输出 OK/LEN-DIFF/MISSED/SCRIPT-ONLY |
| `analyse_diff.py` | 逐构件列出漏报行和误报行，含长度对比 |
| `diag_wm.py` | 打印指定 DXF 的所有 WeldMark 块文本内容 |
| `diag_be021.py` | BE021 专项：打印各视图的零件→标签映射和 WM 箭头信息 |
| `diag_bom.py` | 打印指定 DXF 的 BOM 解析结果（零件厚度/宽度/长度）|
| `diag_blocks.py` | 列出 DXF 中所有块名及类型 |
| `diag_parts.py` | 打印各视图的 Part 块几何信息 |
| `show_fp.py` | 显示指定构件的焊缝 "false positive"（脚本多出的行）|
| `explore_dxf.py` | 通用 DXF 结构探查工具 |

**典型调试流程**：

```bash
# 1. 查看某构件所有 WM 的文本
python diag_wm.py          # 在脚本内修改 target DXF 路径

# 2. 运行提取并对比
python weld_extractor.py
python compare_lengths.py

# 3. 详细差异
python analyse_diff.py
```
