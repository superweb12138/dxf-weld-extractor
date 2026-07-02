# 修复计划：CO007/CO008 跨视图重叠 + BE021/CO009 标注位置

## 问题 1: CO007/CO008 跨视图重叠

### 根因分析
1. `_other_view_bboxes` 计算不完整：当前只从块中提取 LINE/TEXT/CIRCLE/ARC，但 WM 符号的 LINE 也被计入 `lines`（几何线），导致 WM 区域未被标记为障碍物
2. 跨视图 margin (15px) 太小
3. `_score_placement` 的 -20000 惩罚不足以阻止重叠

### 修复方案

#### 1.1 改进 `_other_view_bboxes` 计算 (dxf_annotator.py ~512-529 行)
**当前问题**: 只扫描块名以 ` - {view_id}` 结尾的块，且只提取 LINE/TEXT/CIRCLE/ARC
**修复**: 扩展扫描范围，包含所有块（不限后缀），并添加 INSERT 实体的包围盒

```python
# 在 _annotate_one 函数中，修改 _other_view_bboxes 计算
_other_view_bboxes = []
for _vid in view_bboxes.keys():
    _v_xs, _v_ys = [], []
    for _blk in doc.blocks:
        _bn = _blk.name
        if _bn.endswith(f' - {_vid}') or f' - {_vid}' in _bn:
            for _e in _blk:
                if _e.dxftype() == 'LINE':
                    _v_xs.extend([_e.dxf.start.x, _e.dxf.end.x])
                    _v_ys.extend([_e.dxf.start.y, _e.dxf.end.y])
                elif _e.dxftype() in ('TEXT','MTEXT','ATTRIB','ATTDEF'):
                    _v_xs.append(_e.dxf.insert.x)
                    _v_ys.append(_e.dxf.insert.y)
                elif _e.dxftype() in ('CIRCLE','ARC'):
                    _v_xs.append(_e.dxf.center.x)
                    _v_ys.append(_e.dxf.center.y)
                # 新增：INSERT 实体
                elif _e.dxftype() == 'INSERT':
                    _v_xs.append(_e.dxf.insert.x)
                    _v_ys.append(_e.dxf.insert.y)
    if _v_xs:
        _other_view_bboxes.append((min(_v_xs)-20, min(_v_ys)-20, max(_v_xs)+20, max(_v_ys)+20))
```

#### 1.2 添加 WM 符号包围盒到 hatch_bboxes (dxf_annotator.py `_collect_all_obstacles` 函数)
**当前问题**: WeldMark 块的 LINE 被计入 `lines`，但 WM 整体区域未被标记为障碍物
**修复**: 在 `_collect_all_obstacles` 中，为 WeldMark 块计算整体包围盒并添加到 `hatch_bboxes`

```python
# 在 _collect_all_obstacles 函数中，添加 WeldMark 包围盒
for blk in doc.blocks:
    if blk.name.startswith('WeldMark'):
        if re.search(rf' - {view_id}$', blk.name):
            _wm_xs, _wm_ys = [], []
            for e in blk:
                if e.dxftype() == 'LINE':
                    _wm_xs.extend([e.dxf.start.x, e.dxf.end.x])
                    _wm_ys.extend([e.dxf.start.y, e.dxf.end.y])
                elif e.dxftype() in ('CIRCLE', 'ARC'):
                    _wm_xs.append(e.dxf.center.x)
                    _wm_ys.append(e.dxf.center.y)
            if _wm_xs and _wm_ys:
                hatch_bboxes.append((min(_wm_xs), max(_wm_xs), min(_wm_ys), max(_wm_ys)))
```

#### 1.3 增加跨视图 margin (dxf_annotator.py `_has_conflict` 和 `_score_placement`)
**当前问题**: 15px margin 太小
**修复**: 增加到 25px

```python
# _has_conflict 中 (~967-974 行)
_M = 25  # 原来是 15

# _score_placement 中 (~1199-1207 行)
_M = 25  # 原来是 15
```

#### 1.4 增强跨视图重叠惩罚 (dxf_annotator.py `_score_placement`)
**当前问题**: -20000 惩罚可能不足以阻止重叠
**修复**: 增加到 -50000

```python
# _score_placement 中 (~1207 行)
score -= 50000  # 原来是 20000
```

---

## 问题 2: BE021 F7,8 和 F11,12 位置

### 根因分析
BE021 的 2 SIDES 焊缝（WeldMark-4341）有 4 条边：2 条 335mm 水平边 + 2 条 22mm 短边。当前 `_merged_edge_mid` 计算所有边片段的长度加权质心，质心可能落在斜边上。

### 修复方案

#### 2.1 修改边中点计算逻辑 (weld_extractor.py `_merged_edge_mid` 函数 ~884-899 行)
**当前问题**: 计算所有片段的加权质心，不区分方向
**修复**: 对于 BE021 风格的 2 SIDES 焊缝，优先使用最长的水平/垂直边的中点

```python
def _merged_edge_mid(frags):
    """计算边中点：优先使用最长的水平/垂直边，否则使用加权质心。"""
    if not frags:
        return (0, 0)
    
    # 分类边片段
    h_frags = []  # 水平边 (abs(dy)/len < 0.15)
    v_frags = []  # 垂直边 (abs(dx)/len < 0.15)
    all_frags = []
    
    for gf in frags:
        dx = gf['end'][0] - gf['start'][0]
        dy = gf['end'][1] - gf['start'][1]
        length = math.hypot(dx, dy)
        if length < 1e-12:
            continue
        all_frags.append((gf, length))
        
        # 分类
        if abs(dy) / length < 0.15:  # 水平
            h_frags.append((gf, length))
        elif abs(dx) / length < 0.15:  # 垂直
            v_frags.append((gf, length))
    
    # 优先使用最长的水平边
    if h_frags:
        longest_h = max(h_frags, key=lambda x: x[1])
        gf = longest_h[0]
        return ((gf['start'][0] + gf['end'][0]) / 2, 
                (gf['start'][1] + gf['end'][1]) / 2)
    
    # 其次使用最长的垂直边
    if v_frags:
        longest_v = max(v_frags, key=lambda x: x[1])
        gf = longest_v[0]
        return ((gf['start'][0] + gf['end'][0]) / 2, 
                (gf['start'][1] + gf['end'][1]) / 2)
    
    # 最后使用加权质心
    total_w = 0.0
    cx = 0.0
    cy = 0.0
    for gf, length in all_frags:
        cx += (gf['start'][0] + gf['end'][0]) / 2 * length
        cy += (gf['start'][1] + gf['end'][1]) / 2 * length
        total_w += length
    if total_w < 1e-12:
        return (0, 0)
    return (cx / total_w, cy / total_w)
```

---

## 问题 3: CO009 F13,14 位置

### 根因分析
p16/p7 是 `pp_extra` 条目，`dxf_pos` 最初为 `None`，后由 `pos_fill` 填充为 `(-132.56, -7.5)`。当前 CO009 WM 箭头 snap 逻辑只处理 p15/p7(view 3111) 和 p144/p143(view 3766)，未覆盖 p16/p7。

### 修复方案

#### 3.1 添加 p16/p7 到 WM 箭头 snap 逻辑 (weld_extractor.py ~1899-1907 行)
**当前问题**: 只处理 p15/p7 和 p144/p143
**修复**: 添加 p16/p7 的 snap 逻辑

```python
# CO009: 以 WM 箭头为基准计算边中点（仅对箭头所在的接触边）
if comp == 'CO009' and not _use_largest_gusset:
    _lbl_other = part_number_map.get(other_part, comp)
    if lbl_g == 'p15' and view_id == '3111':
        if _lbl_other == 'p7':
            _edge_mid = _arrow_base  # p15/p7 顶边在箭头处
    elif lbl_g == 'p144' and view_id == '3766':
        if _lbl_other == 'p143':
            _edge_mid = _arrow_base  # p143/p144 主边在箭头处
    # 新增：p16/p7 也需要 snap 到 WM 箭头
    elif lbl_g == 'p16' and view_id == '3111':
        if _lbl_other == 'p7':
            _edge_mid = _arrow_base  # p16/p7 在箭头处
```

但问题是 `_arrow_base` 只在当前 WM 循环中设置。对于 p16/p7，需要找到对应的 WM 箭头。

**方案 A**: 在 CO009 配置中添加 p16 的 WM 箭头信息
**方案 B**: 在 pos_fill 阶段，为 p16/p7 查找最近的 WM 箭头

**推荐方案 B**: 在 pos_fill 阶段处理，因为 p16/p7 是 pp_extra 条目，不经过 WM 循环。

#### 3.2 修改 pos_fill 逻辑 (weld_extractor.py ~4460-4498 行)
**修复**: 对于 CO009 的 p16/p7 条目，查找最近的 WM 箭头并 snap

```python
# 在 pos_fill 循环中，添加 CO009 特殊处理
if comp == 'CO009' and {p1, p2} == {'p16', 'p7'}:
    # 查找 view 3111 中的 WM 箭头
    for _wm in parsed_weldmarks:  # 需要传递 parsed_weldmarks
        if _wm.get('view_id') == '3111' and _wm.get('arrow_tip'):
            _arrow = _wm['arrow_tip']
            # 检查箭头是否在 p16/p7 的接触区域附近
            if abs(_arrow[0] - _pos[0]) < 50:  # x 方向接近
                _pos = _arrow
                break
```

但这个方案需要访问 parsed_weldmarks，可能需要重构代码。

**更简单的方案**: 直接在 CO009 配置中硬编码 p16/p7 的 WM 箭头位置。

```python
# 在 COMP_CONFIG['CO009'] 中添加
'pp_extra': [('p16','p7',400,16,1)],
'pp_extra_snap': {('p16','p7'): (-151.31, 0.0)},  # WM-7676 箭头位置
```

然后在 pos_fill 中使用这个配置。

---

## 执行顺序

1. **问题 1 (CO007/CO008)**: 先修改 `_other_view_bboxes` 和 `_collect_all_obstacles`，然后调整 margin 和惩罚值
2. **问题 2 (BE021)**: 修改 `_merged_edge_mid` 函数
3. **问题 3 (CO009)**: 添加 p16/p7 snap 配置和 pos_fill 逻辑
4. **测试**: 运行 `python weld_extractor.py` 和 `python dxf_annotator.py` 验证效果

## 风险评估

- **问题 1**: 修改可能影响其他构件的标注位置，需要全面测试
- **问题 2**: 修改 `_merged_edge_mid` 可能影响其他使用水平边的构件，需要检查 BE018-BE023
- **问题 3**: 硬编码 WM 箭头位置不够灵活，但 p16/p7 是特例，可以接受
