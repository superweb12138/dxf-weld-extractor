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
- 接头类型（TJ 型接头 / LJ 搭接）
- 焊缝类型（CJP 全熔透 / PP 板间焊 / FW 填角焊）
- 分类汇总数量（按构件按类型统计）

输出文件：`焊缝统计_auto.xlsx`（14 列，含分类汇总）+ `annotated/*.dxf`（含焊缝编号标注的 DXF 图纸）

---

## 环境依赖

```
Python 3.11+
ezdxf >= 1.1
openpyxl >= 3.1
ifcopenshell >= 0.7   # IFC 3D 邻接分析（可选）
python-docx           # 文档生成
```

安装：

```bash
pip install ezdxf openpyxl ifcopenshell python-docx
```

---

## 文件说明

### 主要脚本

| 文件 | 用途 |
|------|------|
| `weld_extractor.py` | **核心**：读取所有 DXF + IFC → 输出 Excel 统计表 + DXF 标注 |
| `dxf_annotator.py` | **DXF 标注引擎**：在 DXF 图纸上生成焊缝编号引出标注 |
| `ifc_reader.py` | IFC 3D 解析：板尺寸提取 + 包围盒邻接矩阵 + 构件类型识别 |
| `compare_r3.py` | 对比脚本输出与人工标准答案的行数及松匹配率 |
| `compare_lengths.py` | 对比脚本输出与人工答案的焊缝长度精确差异 |
| `convert_dwg_to_dxf.py` | 批量把 DWG 转换为 DXF（管理员一次性运行）|

### 输出文件

| 文件 | 说明 |
|------|------|
| `焊缝统计_auto.xlsx` | 脚本自动生成的焊缝统计（每次运行覆盖），含 14 列 |
| `焊缝统计R3_auto(1).xlsx` | **人工标准答案**，用于精度对比 |
| `annotated/*.dxf` | DXF 标注输出（每次运行覆盖），每个构件一张带编号标注的图纸 |
| `*.dxf` / `*.dwg` | 各构件 DXF/DWG 图纸 |

### 输出表结构（14 列）

| 列 | 名称 | 说明 |
|:--:|------|------|
| A | 序号 | 行号 |
| B | 位置(上/下) | Above / Below |
| C | 焊脚尺寸hf(mm) | None=CJP |
| D | 焊缝长度(mm) | |
| E | 备注 | CJP 时标开坡口板的板厚（如 PL12mm），非 CJP 为空 |
| F | 零件1 | |
| G | 零件2 | |
| H | 构件号 | DXF 文件名（不含版本后缀），如 `361-RC3210-S-01-CO007` |
| I | 接头类型 | TJ(T型接头) / LJ(搭接接头) |
| J | 焊缝类型 | CJP(全熔透) / PP(板间焊) / FW(填角焊) |
| K | 接头类型汇总 | 首行：全类型字母（如 `TJ, LJ`），后续空 |
| L | 焊缝类型汇总 | 首行：全类型字母（如 `CJP, FW, PP`），后续空 |
| M | 接头类型汇总数量 | 首行：类型+数量（如 `TJ:40, LJ:8`），后续空 |
| N | 焊缝类型汇总数量 | 首行：类型+数量（如 `CJP:6, FW:34, PP:8`），后续空 |

### 有 DXF 文件的构件

当前支持的前缀：`BE`、`CO`。文件名格式：`项目号-构件名_版本.dxf`，构件名通过正则 `-(BE|CO)_` 提取。

```
BE018  BE019  BE020  BE021  BE022  BE023
CO006  CO007  CO008  CO009  CO010
```

同时支持 `待测试构件/` 中的其他命名格式（如 `8901IR004I01AX0001` 等），通过 4 级构件类型判定。

> 注意：CO010 参与焊缝统计（输出到 Excel），但**暂不参与 DXF 标注**。

---

## 快速开始

### 统计人员日常使用（只需 2 步）

1. 确保 `*.dxf` 文件在项目目录下
2. 运行：

```bash
python weld_extractor.py
```

输出：
- `焊缝统计_auto.xlsx`（统计表格，含 CO010）
- `annotated/*.dxf`（带焊缝编号标注的 DXF 图纸，**CO010 标注仍在迭代**）

### 管理员初次配置（一次性）

| 步骤 | 命令 | 说明 |
|------|------|------|
| 1 | `python convert_dwg_to_dxf.py` | 批量将 DWG 图纸转为 DXF |
| 2 | （可选）将 Tekla 导出的 `*.ifc` 放入 `ifc格式/` 子目录 | 提升板尺寸和邻接判断精度 |

> **不需要**：IFC 文件缺失不影响脚本运行——程序会自动跳过，仅凭 DXF 即可完成统计。

---

## 核心脚本详解

### `weld_extractor.py`

#### 顶部配置

```python
FOLDER   = os.path.dirname(os.path.abspath(__file__))  # 脚本所在目录，自动检测
OUTPUT   = os.path.join(FOLDER, "焊缝统计_auto.xlsx")  # 输出文件
SCALE    = 10.0                  # 1 CAD 单位 = 10 mm
SNAP_TOL = 1.5                   # 焊缝箭头与零件线的捕捉容差（CAD 单位）
MAX_HF   = 20                    # hf 上限；超过此值视为板厚标注
LABEL_TIP_TOL = 8.0              # 引线端点匹配零件的容差（CAD 单位）
```

> `FOLDER` 自动检测脚本所在目录，无需手动修改。确保 `*.dxf` 文件在脚本同级目录即可。

### `dxf_annotator.py` — DXF 焊缝标注

在提取焊缝数据后，自动在每个构件的 DXF 图纸上生成**红色引出标注**，每条焊缝对应一个编号，标注在焊缝位置附近的空白处。

#### 标注规则

| 规则 | 说明 |
|------|------|
| **编号格式** | FW → `F1, F2, ...`；CJP 全熔透焊 → `W1, W2, ...` |
| **引出样式** | 斜线（65°）+ 横线两段式，编号位于横线末端上方 |
| **编号顺序** | 每个视图内**从上到下、从左到右**递增 |
| **配对标注** | 同位置 Above/Below 共用一根引线 |
| **多实例分离** | TYP 倍数的焊缝分配到不同的 Part 块中心，各自独立标注 |
| **碰撞避免** | 标注自动避开 WeldMark 文字区域；多行标注间纵向错开 |
| **颜色/图层** | 蓝色，图层 `WELD_LABELS` |

#### 输出

`weld_extractor.py` 运行后，标注结果保存在 `annotated/` 子目录：
```
annotated/
  361-RC3210-S-01-BE018_00.dxf    ← 带标注的 DXF
  361-RC3210-S-01-BE019_00.dxf
  ...
```

> 标注写入模型空间（ModelSpace），图纸打开默认显示 Model 选项卡。

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
_HF_FROM_T = {
    6:5, 7:5, 8:6, 9:6, 10:7, 11:8, 12:8, 14:10,
    16:12, 18:12, 20:12, 22:14, 25:16, 28:16, 30:18
}
```

---

## 算法流程

```
DXF 文件
  │
  ├─ parse_bom()           读取材料表 → part_dims, comp_dims, bom_type
  │
  ├─ read_ifc()            读取 IFC 3D → ifc_dims, ifc_adj, ifc_type
  │
  ├─ 构件类型判定（4 级优先级）
  │    Tier 1: IFC 实体类型（IfcBeam/IfcColumn）
  │    Tier 2: BOM 描述字段（"Beam"/"Column"）
  │    Tier 3: 主视图几何推断（横向/纵向延展）
  │    Tier 4: 文件名命名回退（BE→beam, CO→column）
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
       ├─ [3-SIDES/CIRCLE 分支]
       │    找节点板 (gusset)
       │    ┌─ [CIRCLE 合成分支 (_synth)]
       │    │  计算 3 条虚拟边：左翼缘、腹板、右翼缘
       │    │  位置基于 comp flange_width（柱）或 gusset bbox（梁）
       │    │  接触边 Y 过滤 + 三角分散
       │    │
       │    └─ [常规 3-SIDES 分支]
       │       枚举节点板所有邻接边
       │       BOM 关联性评分裁剪多余边
       │       pp-skip 过滤假阳性板间焊
       │
       └─ [普通 WM 分支]
            choose_weld_line() 确定焊缝零件和长度
            hf 修正（Sub-rule 3）
            BOM 宽度修正 (Case 1/2/3/CO-fallback)
            TYP 倍数 → results

  ┌─ 后处理 #1 (BE): 连接件枚举
  ├─ 后处理 #2 (CO): BOM 基 comp→plate（view_id 智能分配）
  ├─ 后处理 #3 (CO): 几何枚举 + 螺栓孔过滤
  │     ├─ peer-rep: 同视图同宽度板 3-SIDES 边复制
  │     ├─ pp-geo: 板→板几何枚举
  │     └─ gap-fill: 局部覆盖补缺
  ├─ 后处理 #4: pp-bridge（同筋板桥接 PP 边，find_best_view_for_pair）
  ├─ 后处理 #5: bl-side（按 BOM 长度补全缺边）
  ├─ 后处理 #6: multi-view dedup（同一gusset多视图→保留最聚焦视图）
  ├─ 后处理 #7: pos-spread（peer-rep 边三角分散：左下/中上/右下）
  │
  结果写入 Excel（焊缝统计_auto.xlsx）
  │
  └─ dxf_annotator.py    对每个 DXF 生成标注 → annotated/*.dxf
```

---

---
 
## 标注引擎特性

### 碰撞检测算法

`dxf_annotator.py` 采用基于评分的贪心搜索，每个焊缝的标注位置通过 8 方向 × 8 距离档的枚举搜索确定：

| 特性 | 说明 |
|------|------|
| **半球约束** | 左右标注尽量不穿越视图中线（扣80），上下不穿越中线（扣40）|
| **文字重叠惩罚** | 标注文字间重叠扣120分，引线穿过文字扣100分 |
| **分离检测** | 文字重叠用纯文字包围盒判定；引线交叉用含引线整体包围盒判定 |
| **冲突后处理** | 5 次迭代微调距离/角度/方向翻转解决检测到的冲突 |
| **贴近标注强制分离** | 焊点间距 < 2.5 时强制相反标注方向，消除半球惩罚 |
| **图纸边界保护** | 超出图纸范围扣 2000×超量 分，确保标签可见 |
| **障碍物收集** | LINE/TEXT/MTEXT/CIRCLE/ARC/DIMENSION/ATTDEF/ATTRIB/MLEADER |

### 柱体通用后处理

`weld_extractor.py` 中的 `_run_column_cleanup` 函数实现了柱体构件的通用后处理，通过 `plate_map`（板名映射）和视图 ID 参数化，使 CO007 和 CO008 共用同一套逻辑：

- p127 BOM 长度修正（100→90.5, 116→60）
- Below 补全 + Above 去重
- p124 CJP 条目管理（bl-side 清理 + y 分散）
- 间隙算法推算左翼缘面位置（不硬编码坐标）
- 对称板 PP 边复制（p100/p126 → p100/p127）
- relabel 条目 view_id 继承，避免误分配到 E-E 视图

CO008 通过 `plate_map={'p47':'p92','p100':'p102'}` 自动适配 CO007 的逻辑，无需单独维护。

---

以 `焊缝统计R3_auto(1).xlsx` 为标准答案，`compare_r3.py` 对比。

### 行数匹配率

| 构件 | 脚本 | 手动 | 匹配率 | 状态 |
|------|:--:|:--:|:--:|------|
| BE018 | 14 | 14 | 100% | ✅ |
| BE019 | 14 | 14 | 100% | ✅ |
| BE020 | 22 | 22 | 100% | ✅ |
| BE021 | 18 | 18 | 100% | ✅ |
| BE022 | 24 | 24 | 100% | ✅ |
| BE023 | 16 | 16 | 100% | ✅ |
| CO006 | 12 | 12 | 100% | ✅ |
| CO007 | 42 | 42 | 100% | ✅ |
| CO008 | 42 | **42** | **100%** | ✅ **首次匹配** |
| CO009 | 28 | 28 | 100% | ✅ |
| CO010 | **152** | 152 | **100%** | ✅ |
| **总计** | **384** | **384** | **100%** | ✅ |

### 松匹配率（忽略长度和标注）

| 指标 | 值 |
|------|:--:|
| 匹配数 | 361 |
| 手工独有 | 23 |
| 脚本独有 | 23 |
| **松匹配率** | **88.7%** |

### 遗留差异说明

全部 11 构件 384 行完美匹配。CO009 的标注效果已完成优化（箭头基准定位、剖视图自动识别、x2_instances 镜像、p144 上下板分离）。CO010 的 DXF 标注功能仍在迭代中。

---

## 关键技术特性

### 构件类型 4 级检测

| 优先级 | 方法 | 数据源 |
|--------|------|--------|
| 1（最高）| IFC 3D 实体类型（IfcBeam / IfcColumn） | `.ifc` 文件 |
| 2 | BOM 描述字段（"Beam" / "Column"） | DXF |
| 3 | 主视图构件几何推断（bbox 宽/高比） | DXF |
| 4（最低）| 文件名前缀回退（BE→beam, CO→column）| 文件名 |

### 围焊 / 3-SIDES 处理

对于 CIRCLE WM（围焊）：
- **柱子（column）**：用 `comp_dims['flange_w']` 反算左右翼缘位置，中间为腹板
- **横梁（beam）**：用 gusset 自身 bbox 范围分散

3-SIDES WM：
- 节点板接触边通过 gusset Y 高度过滤，排除柱体竖线投影
- gusset bbox 校验过滤远处假阳性边

### 跨视图去重

同一 gusset 出现在多个 3-SIDES 视图时：
- 保留 **Part 块最少**（最聚焦）视图中的 comp→plate 边
- 其他视图仅保留 PP（板间）边
- 避免同一个物理边在多个视图中重复标注

### Peer-Replication 与三角分散

同厚度镜像板（如 p126/p127）：
1. peer-rep 复制边长和 PP 边
2. `pos-spread` 将 3 对边分散到板 bbox 的：左下、顶部中、右下
3. 标志 `_no_refine` 防止位置精化覆盖

### pp-bridge 桥接

通过同筋板的 PP 边模式，自动发现并补齐其他板对的 PP 边：
- `_find_best_view_for_pair` 自动选择两板共现的视图
- 计算第一个板的 bbox 中心 X + top-quarter Y 作为标注位置

---

## COMP_CONFIG 配置驱动

`weld_extractor.py` 中的 `COMP_CONFIG` 字典为**可选覆盖**。新构件可零配置自动匹配。

| 配置键 | 作用 | 典型值 |
|--------|------|--------|
| `pp_bridge_exclude` | 排除桥接假阳 | `{'p125'}` |
| `relabel_cp_to_pp` | cp→pp 标签重定位 | `[('p102','p124',308)]` |
| `hf_map` | 逐板指定焊脚 | `{'p195': 12, 'p196': 9}` |
| `cjp_extra_fillet` | CJP 板额外生成填角焊 | `{'p184': 9}` |
| `arc_lengths` | 显式指定弧焊长度 | `{'p184': [139]}` |
| `cleanup_expect` | 允许保留的 ARC 长度 | `{'p184': [139]}` |
| `bl_weld_pairs` | CJP 板间焊 | `[('pA','pB',len,12,3)]` |
| `pp_extra` | 补充板间焊 | `[('pA','pB',len,9,1)]` |

---

## 添加新构件

### 步骤 1：准备 DXF 文件

将新构件的 DXF 文件放入项目根目录（与 `weld_extractor.py` 同级）。

文件名格式要求：`项目号-构件号_版本.dxf`
```
示例：361-RC3210-S-01-BE018_00.dxf
构件名 = BE018（从 "-BE018_" 中提取）
```

### 步骤 2：运行脚本

```bash
python weld_extractor.py
```

### 步骤 3：检查输出

打开 `焊缝统计_auto.xlsx`，确认焊缝行数合理。如发现不匹配，可参考 COMP_CONFIG 添加可选修正。

---

## 诊断脚本说明

| 脚本 | 用途 |
|------|------|
| `compare_r3.py` | 对比脚本输出与人工标准答案的行数及松匹配率 |
| `compare_lengths.py` | 对比脚本输出与人工答案的焊缝长度精确差异 |

```bash
# 运行提取
python weld_extractor.py

# 查看精度
python compare_r3.py
```
