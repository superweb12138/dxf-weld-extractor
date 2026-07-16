# DXF 工程图焊缝智能统计与标注系统

从结构构件 DXF 图纸中自动提取焊缝信息，输出 Excel 焊缝统计表，并在 DXF 图纸上生成可视化焊缝标注。

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
- [欧标（EU）流水线](#欧标eu流水线)
- [当前精度状态](#当前精度状态)
- [已知遗留问题](#已知遗留问题)

---

## 项目背景

钢结构工程图纸（DWG/DXF 格式）中，焊缝信息分散在各构件图中，需要人工汇总统计。本工具通过解析 DXF 文件内的 `WeldMark` 和 `Part` 块，自动提取每道焊缝的：

- 所属构件（如 BE018、CO007）
- 焊缝位置（Above / Below 箭头侧）
- 焊脚尺寸 hf（mm），CJP 坡口焊为 None
- 焊缝长度（mm）
- 连接零件对（如 `BE020/p175`）
- 接头类型（TJ 型接头 / LJ 搭接）
- 焊缝类型（CJP 全熔透 / PP 板间焊 / FW 填角焊）
- 分类汇总数量（按构件按类型统计）

输出文件：`焊缝统计_auto.xlsx`（14 列，含分类汇总）+ `annotated/gb|eu/*.dxf`（含焊缝编号标注的 DXF 图纸）

---

## 环境依赖

```
Python 3.11+
ezdxf >= 1.1
openpyxl >= 3.1
ifcopenshell >= 0.7   # IFC 3D 邻接分析（可选）
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
| `weld_extractor.py` | **核心**：读取所有 DXF + IFC → 输出 Excel 统计表 + 调用标注（国标/欧标分流） |
| `weld_extractor_eu.py` | **欧标提取**：U 型围焊、TYP 扩展、A-A 剖面 U-cut 重定位、双 U 分流 |
| `dxf_annotator.py` | **DXF 标注引擎**：象限约束、碰撞规避、引出标注 |
| `dxf_annotator_eu.py` | **欧标标注入口**：调用 `dxf_annotator` 并输出至 `annotated/eu/` |
| `eu_catalog_from_pdf.py` | 从欧标型钢 PDF 生成 `eu_sections.json`（截面尺寸） |
| `ifc_reader.py` | IFC 3D 解析：板尺寸、包围盒邻接、构件类型 |
| `convert_dwg_to_dxf.py` | 批量 DWG → DXF（管理员一次性） |
| `compare_r3.py` | 与人工标准答案对比行数 / 松匹配率 |
| `compare_lengths.py` | 与人工答案对比焊缝长度差异 |
| `_check_annotation_quality.py` | 质检：字体重叠、引线交叉、出框、BOM 侵入 |

### 输出文件

| 文件 | 说明 |
|------|------|
| `焊缝统计_auto.xlsx` | 自动焊缝统计（焊缝统计 / 异常报告 / 抽检清单） |
| `焊缝统计R3_auto(1).xlsx` | 人工标准答案（精度对比用） |
| `annotated/gb/*.dxf` | 国标带编号标注的 DXF（每次运行覆盖） |
| `annotated/eu/*.dxf` | 欧标带编号标注的 DXF（每次运行覆盖） |
| `*.dxf` / `*.dwg` | 源图纸 |

### 输出表结构（14 列）

| 列 | 名称 | 说明 |
|:--:|------|------|
| A | 序号 | 行号 |
| B | 位置(上/下) | Above / Below |
| C | 焊脚尺寸hf(mm) | None=CJP |
| D | 焊缝长度(mm) | |
| E | 备注 | CJP 时标开坡口板厚 |
| F / G | 零件1 / 零件2 | |
| H | 构件号 | 如 `361-RC3210-S-01-CO007` |
| I / J | 接头类型 / 焊缝类型 | TJ·LJ / CJP·PP·FW |
| K–N | 汇总列 | 首行写类型字母与数量 |

### 有 DXF 文件的构件

```
BE018  BE019  BE020  BE021  BE022  BE023
CO006  CO007  CO008  CO009  CO010
```

> CO010 参与 Excel 统计，但**暂不参与 DXF 标注**（流水线中跳过）。

### 欧标源图纸（项目根目录 `8901IR004I01*.dxf`）

```
AB0001  AB0002  AB0002_01  AB0003
AC0001  AC0002  AC0003
AP0001  AP0002  AP0003
AT0001  AT0002  AT0003
AX0001  AX0002  AX0003
```

流水线按文件名自动识别 `standard=eu`，输出至 `annotated/eu/`（含同名 PDF 预览）。

---

## 快速开始

### 日常使用

```bash
python weld_extractor.py
```

输出：

- `焊缝统计_auto.xlsx`（含 CO010）
- `annotated/gb/*.dxf`（BE018–CO009 标注；约 3 分钟）

质检（可选）：

```bash
python _check_annotation_quality.py
python compare_r3.py
```

### 管理员初次配置

| 步骤 | 命令 | 说明 |
|------|------|------|
| 1 | `python convert_dwg_to_dxf.py` | DWG → DXF |
| 2 | （可选）将 `*.ifc` 放入 `ifc格式/` | 提升尺寸与邻接精度 |

> IFC 缺失不影响运行，仅凭 DXF 即可完成统计与标注。

---

## 核心脚本详解

### `weld_extractor.py`

| 常量 | 值 | 含义 |
|------|----|------|
| `SCALE` | 10.0 | 1 CAD 单位 = 10 mm |
| `SNAP_TOL` | 1.5 | 箭头到零件线捕捉容差 |
| `MAX_HF` | 20 | hf 上限；过大视为板厚 |

主要特性：CIRCLE 围焊、3-SIDES、pp-mirror 对称板、TYP 分发、抽检清单、柱体通用后处理（CO007/CO008 共用）等。细节见源码 `COMP_CONFIG`。

### `dxf_annotator.py` — DXF 焊缝标注

在提取结果上，为每个构件生成**蓝色两段式引出标注**（斜线 + 水平接地线 + 文字），图层 `WELD_LABELS`。

#### 标注规则

| 规则 | 说明 |
|------|------|
| **编号** | FW/PP → `F{n}`；CJP → `W{n}` |
| **引出** | 斜段 + 短水平线；字在水平线末端 |
| **配对** | 同位置 Above/Below 共用一根引线（`F11,F12`） |
| **象限** | 焊点相对本视图焊点中心分 Q1–Q4；默认同半区放置（左 Q2↔Q3，右 Q1↔Q4） |
| **斜角禁区** | 相对**水平 ±10°**、**垂直 ±10°** 不可作斜引线（有效倾角约 10°–80°） |
| **引线长度** | 斜段硬上限 `MAX_DIAG_LEN = 50`，优先短引线 |
| **走廊空白** | C-C / D-D 等邻视图缝隙可作短距偏好，但不放开跨半区象限 |
| **碰撞** | 避开零件/BOM/WM 文字；全局冲突解决 + 近距字强制分槽 |

#### 输出

```
annotated/
  gb/          # 国标 BE/CO
    361-RC3210-S-01-BE018_00.dxf
    ...
  eu/          # 欧标 AB/AC/AP/AT/AX
    8901IR004I01AC0001_00.dxf
    ...
```

标注写在模型空间；用 CAD 打开 `annotated/gb/` 或 `annotated/eu/` 下 DXF 即可查看。

---

## DXF 结构约定

```
WeldMark-<ID> - <视图ID>   → 焊缝标注块
Part-<ID> - <视图ID>       → 零件几何块
Mark-<ID> - <视图ID>       → 零件编号引线块
Unknown-<ID>               → 材料表（BOM）块
```

同一 `视图ID` 的 WeldMark / Part 属于同一视图。

---

## 关键参数与常量

### 提取（`weld_extractor.py`）

| 常量 | 值 | 含义 |
|------|----|------|
| `SNAP_TOL` | 1.5 | 箭头到零件线 |
| `LABEL_TIP_TOL` | 8.0 | 引线端匹配零件 |
| `MAX_HF` | 20 | hf 上限 |

### 标注（`dxf_annotator.py`）

| 常量 | 值 | 含义 |
|------|----|------|
| `ANGLE_MIN` / `ANGLE_MAX` | 10 / 80 | 相对水平倾角带；禁贴轴 ±10° |
| `MAX_DIAG_LEN` | 50 | 斜引线上限 |
| `CLUSTER_RADIUS` | 36 | 近邻分向 / 交叉修复半径 |
| `OVERLAP_MARGIN` | 4 | 文字互相避让边距 |
| `BOM_MARGIN` | 10 | BOM / 表格禁区边距 |

---

## 算法流程

```
DXF 文件
  ├─ parse_bom / read_ifc / 构件类型判定
  ├─ 按视图分组 WeldMark + Part
  ├─ 普通 WM / 3-SIDES / CIRCLE 分支提取焊缝
  ├─ 后处理：TYP 分发、ARC、pos-fill、列体 cleanup
  ├─ 写入 焊缝统计_auto.xlsx
  └─ dxf_annotator.annotate → annotated/gb/*.dxf
       (EU: dxf_annotator_eu.annotate_eu → annotated/eu/*.dxf)
       ├─ 象限归属 + 同半区分向
       ├─ 短距优先搜索 + 走廊偏好（不并入额外象限）
       ├─ 冲突解决 / 引线交叉修复 / 近距字分槽
       └─ 绘制前角度钳位（水平/垂直 ±10°）
```

---

## 欧标（EU）流水线

欧标与国标共用 `python weld_extractor.py` 入口；`weld_extractor_eu.extract_eu` 完成提取后由 `dxf_annotator_eu.annotate_eu` 标注。

### U 型围焊（CIRCLE / hf5）重定位

主视图上的 U 型零件围焊标记会重定位到 **A-A 剖面**（U-cut 视图），核心函数 `relocate_eu_circle_wraps_to_u_cut`：

| 场景 | A-A 剖面 | 主视图 |
|------|----------|--------|
| **单 U** | 4 对焊缝全保留（3 单焊 + 1 短边分叉） | 剥离 CIRCLE wrap |
| **双 U** | 每 lane 仅保留短边分叉（V 形引线） | 左右各 3 对（共 6 点）回主视图 |

短边分叉使用 `_draw_branched_paired_weld_label`：引线 V 口朝向短边开口端（`_eu_u_short_tips`）。

### 双 U 不对称截面（AC0003）

主视图左右 U 板底边 Y 可能相差约 2 mm（L 板更低、R 板更高）。A-A 剖面有时只露出一块板（如 AC0003 上层仅 R 板）。此时：

- 用主视图 `dual_pairs` 的 L/R bbox 底边识别可见板属于哪一侧；
- L 板 → lane0（右侧开口），R 板 → lane1（左侧开口）；
- 缺失的一侧从主视图对应板 bbox Y + 完整双板截面的 X 模板推断短边 tips（**不**简单镜像 Y）。

对称双 U（AC0002 st3–st7，A-A 双板齐全）仍走原有几何拾取路径，不受影响。

### 其他欧标后处理（`weld_extractor_eu.py`）

- `expand_eu_typ_from_seeds`：相似结构 TYP 扩展
- `mirror_eu_long_elev_native` / `ensure_eu_bottom_flange_lr`：主视图左右端镜像、底法兰三对
- `cleanup_eu_u_cut_typ_when_u_wrap`：A-A 有 U-wrap 后清理冗余 TYP
- `drop_eu_section_typ_when_u_wrap_present`：剖面 B/C/D 冗余 TYP 剔除

### 当前欧标标注状态

| 图纸 | 主视图 | A-A 剖面 | 备注 |
|------|--------|----------|------|
| **AC0002** | 双 U st3–7 各 6 点 + 底部 L/C/R | 单 U 全 4 对；双 U 仅短边 | 对称 U，双板完整 |
| **AC0003** | 双 U st0/st1 各 6 点 | 仅短边分叉；上层单板不对称补全 | F31/F32 下方 tip y≈73 |
| **AB0001** | — | — | 基线锁定，不必每次回归 |
| **AB/AC/AT** 其余 | 持续迭代 | — | 见 `annotated/eu/` |

---

## 当前精度状态

以 `焊缝统计R3_auto(1).xlsx` 为标准，`compare_r3.py` 对比。

### 行数匹配率

| 构件 | 匹配率 | 状态 |
|------|:--:|------|
| BE018–BE023 | 100% | 通过 |
| CO006–CO010 | 100% | 通过 |
| **合计 384/384** | **100%** | 通过 |

松匹配率（忽略 hf/标注）约 **93.5%**。

### 标注效果（BE018–CO009）

- 出框 / BOM 侵入：质检目标为 0（非 CO010）
- 字体重叠：目标为 0
- 斜引线倾角：相对水平约 10°–80°
- 全流水线墙钟：约 3 分钟（10 个构件，跳过 CO010 标注）

CO010 标注仍在迭代，默认跳过。

### 欧标

- 流水线可批量处理根目录全部 `8901IR004I01*.dxf`
- U-cut / 双 U / 底法兰逻辑以 AC0002、AC0003 为回归样例
- 个别 AP/AX 图纸仍在扩展 TYP 与剖面清理规则

---

## 已知遗留问题

### DXF 标注

| 问题 | 状态 |
|------|------|
| 极密区仍可能有少量引线交叉 | 已大幅改善，可继续针对性收 |
| 跨视图文字全局协调有限 | 部分靠邻视图 bbox / cross-view 文字框 |
| CO010 标注未纳入流水线 | 待优化后开启 |

### 焊缝提取

| 问题 | 状态 |
|------|------|
| 个别板间焊几何边界场景依赖 COMP_CONFIG | 可配置覆盖 |
| CJP paired fillet 未覆盖全部非配置板 | 待扩展 |

### 欧标（EU）

| 问题 | 状态 |
|------|------|
| A-A 仅单板时 L/R 不对称需主视图补全 | AC0003 已修；推广至同类图纸 |
| 剖面 B/C/D 偶发冗余 TYP | 部分 cleanup 已加，持续观察 |
| 主视图密区 6 点 wrap 碰撞 | 依赖标注引擎全局冲突解决 |

---

## COMP_CONFIG 与新构件

`weld_extractor.py` 中 `COMP_CONFIG` 为**可选覆盖**；新构件可先零配置运行，再按需补 `hf_map` / `pp_extra` / `cjp_plates` 等。

添加新构件：将 `项目号-构件号_版本.dxf` 放入项目根目录后执行 `python weld_extractor.py`，检查 Excel 与 `annotated/gb/` 或 `annotated/eu/` 即可。
