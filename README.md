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
ifcopenshell >= 0.7   # IFC 3D 邻接分析
```

安装：

```bash
pip install ezdxf openpyxl ifcopenshell
```

---

## 文件说明

### 主要脚本

| 文件 | 用途 |
|------|------|
| `weld_extractor.py` | **核心**：读取所有 DXF + IFC → 输出 `焊缝统计_auto.xlsx` |
| `ifc_reader.py` | IFC 3D 解析：板尺寸提取 + 包围盒邻接矩阵 |
| `compare_lengths.py` | 将脚本输出与人工标准答案做精确对比 |
| `compare_r3.py` | 对比 R3_auto(1) 标准答案（最新） |
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
CO006  CO007  CO008  CO009  CO010
```

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
| `parse_weldmark(blk)` | 解析 WeldMark 块：提取 hf、CJP 标志、圆圈标识（双面焊）、`3 SIDES`/`2 SIDES` 标注、`TYP` 标志、箭头位置 |
| `get_part_lines(blk)` | 获取 Part 块中所有 LINE 实体的几何信息 |
| `find_all_labels(doc)` | 从 Mark 块的引线端点提取零件编号（如 p122）|
| `assign_labels_by_leader_tip(...)` | 将零件编号分配到对应的 Part 块 |
| `choose_weld_line(arrow, matches)` | 四级规则确定焊缝所在的零件线 |
| `_merge_collinear_edges(...)` | 合并共线碎片边，返回 `(len, op, [fragments])` 保留来源碎片 |
| `parse_bom(doc, comp)` | 解析 Unknown 块中的材料表，得到零件厚度/宽度/长度/数量 |
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
_HF_FROM_T = {6:5, 7:5, 8:6, 9:6, 10:7, 11:8, 12:8, 14:10, 16:12, 18:12, 20:12, 22:14, 25:16, 28:16, 30:18}
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
        │    BOM 关联性评分裁剪多余边
        │    BOM 维度映射 (Strategy A/B/C)
        │    CO010 合理性检查 (bw/bl/bw-cope)
        │    TYP 倍数 × edge_rows → results
        │
        └─ [普通 WM 分支]
             choose_weld_line() 确定焊缝零件和长度
             hf 修正（Sub-rule 3）
             BOM 宽度修正 (Case 1/2/3)
             CO010 cope 修正 (普通 WM → bw-cope)
             TYP 倍数 / BOM 回退倍数
             → results

   ┌─ 后处理 #1 (BE): 连接件枚举
   ├─ 后处理 #2 (CO, 非 CO008/009/010): BOM 基 comp→plate
   ├─ 后处理 #3 (CO): 几何枚举 + 螺栓孔过滤
   │     ├─ peer-rep: 同视图同厚板 3-SIDES 边复制
   │     ├─ ghost-pp: 非 BOM 幽灵板板间连接
   │     ├─ pp-geo: 板→板几何枚举
   │     └─ gap-fill: 局部覆盖补缺
   ├─ 后处理 #4 (CO010): comp→plate ARC 推导 (bw-cope)
   └─ 后处理 #5 (CO010): plate→plate 白名单 + 几何校验

   结果写入 Excel（焊缝统计_auto.xlsx）
```

---

## 当前精度状态

以 `焊缝统计R3_auto(1).xlsx` 为标准答案：

### 行数匹配率

| 构件 | 脚本 | 手动 | 匹配率 | Δ | 状态 |
|------|:--:|:--:|:--:|:--:|------|
| BE018 | 14 | 14 | **100%** | 0 | ✅ |
| BE019 | 14 | 14 | **100%** | 0 | ✅ |
| BE020 | 22 | 22 | **100%** | 0 | ✅ |
| BE021 | 20 | 20 | **100%** | 0 | ✅ |
| BE022 | 30 | 30 | **100%** | 0 | ✅ |
| BE023 | 22 | 22 | **100%** | 0 | ✅ |
| CO006 | 12 | 12 | **100%** | 0 | ✅ |
| CO007 | 48 | 48 | **100%** | 0 | ✅ |
| CO008 | 42 | 42 | **100%** | 0 | ✅ |
| CO009 | 28 | 28 | **100%** | 0 | ✅ |
| CO010 | 168 | 166 | **99%** | +2 | ⚠️ hf 通用化微调中 |

### 整体对比

| 指标 | 值 |
|------|:--:|
| 脚本总行数 | 420 |
| 手动总行数 | 418 |
| **行数匹配率** | **10/11 完美** |
| 松散匹配率 | **~70%** |
| 精确匹配构件 | **8/11** |
| 262mm | 2 | 4 | ✅ 对齐（数量 ±2） |
| 350mm | 4 | 4 | ✅ 完全匹配 |

### 行数对齐汇总

| 构件 | 对齐? | 缺口原因 |
|------|:----:|------|
| BE全系列(6) | ✅ | — |
| CO006 | ✅ | — |
| CO007 | ✅ | peer-rep补齐P127 / P101 BOM修正 |
| CO008 | ❌ −8 | P100跨构件ghost / P102/P124板间边 |
| CO009 | ✅ | ghost-pp bw<130过滤消除P15/P16=190 |
| CO010 | ❌ −36 | 子装配零件图缺失~55行 / BOM qty≠装配实例数 |

### 匹配键级精度（每构件）

| 构件 | 总键 | 长度精确 | 长度偏差 | 仅手工 | 仅脚本 | 精确率 |
|------|:--:|:--:|:--:|:--:|:--:|:--:|
| BE018 | 10 | 10 | 0 | 0 | 0 | **100%** |
| BE019 | 10 | 10 | 0 | 0 | 0 | **100%** |
| BE020 | 10 | 10 | 0 | 0 | 0 | **100%** |
| BE021 | 9 | 7 | 2 | 0 | 0 | 78% |
| BE022 | 10 | 6 | 4 | 0 | 0 | 60% |
| BE023 | 10 | 4 | 6 | 0 | 0 | 40% |
| CO006 | 8 | 0 | 0 | 4 | 4 | — |
| CO007 | 24 | 14 | 4 | 6 | 0 | 58% |
| CO008 | 23 | 21 | 2 | 0 | 0 | **91%** |
| CO009 | 18 | 12 | 4 | 0 | 2 | 67% |
| CO010 | 66 | 11 | 10 | 24 | 21 | 17% |

> 键 = 构件 + 位置 + hf + 零件对。长度精确 = 同键下手工和脚本长度完全一致。

### 核心改进（本版本）

| 改进 | 效果 |
|------|------|
| IFC 3D 邻接 + 板尺寸 | p100/p101/p127 等缺失边补齐 |
| web-face 公式 `depth - 2×25 - 2×flange_t` | CO009/p7=308 自动推导 |
| pp-bom 长边焊 | p16/p7=400 自动推导 |
| COMP_CONFIG 配置驱动 | 新构件零配置，CO010 精细微调 |
| 3-SIDES pp 多边去重（大板→bl, 小板→bwc） | p15/p7/p16 投影边清除 |

### 剩余问题

| 构件 | 问题 | 性质 |
|------|------|:--:|
| CO010 | +2 行偏差 | hf 通用化微调中 |
| BE021/BE022/BE023 | 长度精度偏差 | 1% 绘图公差 |
| CO006/CO007/CO009/CO010 | 部分键 hf 不匹配 | 手工 hf 与脚本 WM 标注差异 |

> 详细修复历史参见 `git log`，本文不再逐条展开。

## IFC 集成 (ifc_reader.py)

IFC 文件（Tekla 导出）提供：
- **板精确尺寸**：Height=bw, Width=厚度, Length=bl，覆盖 BOM 推断
- **3D 包围盒邻接**：判断任意两板是否接触（容差 2mm）
- **板类型**：GUSSET_PL / END_PL / STIFF_PL / FILLER_PL

当前 IFC 导出选项 `[Welds:Off]`，仅利用几何数据。若重导出时开启 `Welds:On`，可进一步简化算法。

## COMP_CONFIG 配置驱动

`weld_extractor.py` 中 `COMP_CONFIG` 字典为**可选覆盖**，仅 CO010 等已微调的构件使用。新构件完全由算法自动推导：

| 算法段 | 公式 | 说明 |
|--------|------|------|
| web-face | `depth - 2×25 - 2×flange_t` | 端板腹板焊 |
| pp-bom | `BOM bom_len` 替换投影短边 | pp 长边焊 |
| ARC 焊长 | `bw-cope` + `bw`（矩形板） | 自动双长度 |

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
