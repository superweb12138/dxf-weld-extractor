"""
DXF Weld Annotator — adds weld labels to DXF drawings based on extracted results.

Rules:
  - CJP → W{n} prefix
  - FW/PP/PJP → F{n} prefix
  - Numbering: clockwise within each view, global sequential across views
  - Left-half welds → leader line points left
  - Right-half welds → leader line points right
  - Above/Below pair at same position → one leader, two labels stacked
  - Output: annotated/ directory, new layer WELD_LABELS in magenta
"""

import os, math, re
from collections import defaultdict

import ezdxf

FOLDER = os.path.dirname(os.path.abspath(__file__))
ANNOTATED_DIR = os.path.join(FOLDER, "annotated")

SCALE = 10          # 1 CAD unit = 10 mm
LABEL_HEIGHT = 2.5  # text height in CAD units
LAYER_NAME = "WELD_LABELS"
LABEL_COLOR = 5       # blue (ACI 5)
LABEL_OFFSET = 38.0   # fallback offset for no-coordinate labels

# Two-segment leader line: diagonal + horizontal landing
DIAG_BASE = 10             # base diagonal length in CAD units
DIAG_STEP = 3              # step increment for collision avoidance
HORIZ_LAND = 8             # horizontal landing length
PAIR_GAP = LABEL_HEIGHT * 3.0  # horizontal gap between paired labels
PAIR_HORIZ_LAND = 18         # horizontal landing length for paired labels
MAX_DIAG_LEN = 48            # upper limit for diagonal length

# 8-direction system: (name, base_angle_deg, min_angle, max_angle, dx_mult, dy_mult)
# angles: 0=right, 90=up, -90=down, +/-180=left
DIRECTIONS = [
    ('E',   0,   -20,  20,  1,  0),
    ('NE',  45,   25,  65,  1,  1),
    ('N',   90,   70, 110,  0,  1),
    ('NW',  135, 115, 155, -1,  1),
    ('W',   180, 160, 200, -1,  0),
    ('SW', -135, -155,-115, -1, -1),
    ('S',  -90, -110, -70,  0, -1),
    ('SE', -45,  -65, -25,  1, -1),
]

# ezdxf text alignment: 0=left, 1=center, 2=right
HALIGN_LEFT = 0
HALIGN_CENTER = 1
HALIGN_RIGHT = 2

# MTEXT attachment points
MT_TOP_LEFT = 1
MT_TOP_CENTER = 2
MT_TOP_RIGHT = 3
MT_MIDDLE_LEFT = 4
MT_MIDDLE_CENTER = 5
MT_MIDDLE_RIGHT = 6
MT_BOTTOM_LEFT = 7
MT_BOTTOM_CENTER = 8
MT_BOTTOM_RIGHT = 9


def _direction_priority(weld_x, weld_y, cx, cy):
    """Return priority-ordered direction list based on weld position relative to view center."""
    dx = weld_x - cx
    dy = weld_y - cy
    if abs(dx) >= abs(dy):
        if dx >= 0:
            return ['E', 'NE', 'SE', 'N', 'S', 'NW', 'SW', 'W']
        else:
            return ['W', 'NW', 'SW', 'N', 'S', 'NE', 'SE', 'E']
    else:
        if dy >= 0:
            return ['N', 'NE', 'NW', 'E', 'W', 'SE', 'SW', 'S']
        else:
            return ['S', 'SE', 'SW', 'E', 'W', 'NE', 'NW', 'N']


def _collect_all_obstacles(doc, view_id):
    """Collect all visual obstacles in a view: lines, text bboxes, circles/arcs."""
    lines, text_bboxes, circles = [], [], []

    def add_entity(e):
        t = e.dxftype()
        if t == 'LINE':
            lines.append(((e.dxf.start.x, e.dxf.start.y),
                          (e.dxf.end.x, e.dxf.end.y)))
        elif t == 'TEXT':
            tx, ty = e.dxf.insert.x, e.dxf.insert.y
            th = getattr(e.dxf, 'height', 2.0)
            tw = th * len(e.dxf.text.strip()) * 0.7
            mrg = 1.5
            text_bboxes.append((tx - mrg, tx + tw + mrg, ty - mrg, ty + th + mrg))
        elif t == 'MTEXT':
            tx, ty = e.dxf.insert.x, e.dxf.insert.y
            th = getattr(e.dxf, 'char_height', 2.0)
            txt = e.text.strip() if hasattr(e, 'text') else ''
            lines_txt = txt.split('\\n') if txt else ['']
            nlines = len(lines_txt)
            max_line = max(len(l) for l in lines_txt) if txt else 8
            tw = th * max_line * 0.6
            th_total = th * nlines + (nlines - 1) * th * 0.3
            mrg = 1.5
            text_bboxes.append((tx - mrg, tx + tw + mrg, ty - mrg, ty + th_total + mrg))
        elif t == 'CIRCLE':
            circles.append((e.dxf.center.x, e.dxf.center.y,
                            getattr(e.dxf, 'radius', 1.0)))
        elif t == 'ARC':
            circles.append((e.dxf.center.x, e.dxf.center.y,
                            getattr(e.dxf, 'radius', 1.0)))
        elif t == 'DIMENSION':
            try:
                tx, ty = e.dxf.text_midpoint.x, e.dxf.text_midpoint.y
                th = 2.0
                tw = th * len(str(getattr(e.dxf, 'text', ''))) * 0.7
                mrg = 1.5
                text_bboxes.append((tx - mrg, tx + max(tw, 8) + mrg, ty - mrg, ty + th + mrg))
            except Exception:
                pass
        elif t == 'HATCH':
            pass
        elif t in ('ATTDEF', 'ATTRIB'):
            try:
                tx, ty = e.dxf.insert.x, e.dxf.insert.y
                th = getattr(e.dxf, 'height', 2.0)
                txt = e.dxf.text.strip() if hasattr(e.dxf, 'text') and e.dxf.text else ''
                tw = th * len(txt) * 0.7 if txt else th * 2
                mrg = 1.5
                text_bboxes.append((tx - mrg, tx + tw + mrg, ty - mrg, ty + th + mrg))
            except Exception:
                pass
        elif t == 'MLEADER':
            try:
                from ezdxf.math import Vec2
                txt = str(getattr(e.dxf, 'text', '')) or ''
                if hasattr(e, 'get_leader_lines'):
                    for line in e.get_leader_lines():
                        lines.append(((line[0].x, line[0].y), (line[-1].x, line[-1].y)))
                if hasattr(e, 'text'):
                    tx, ty = e.text[0].x, e.text[0].y if hasattr(e.text, '__iter__') and not isinstance(e.text, str) else (e.dxf.insert.x, e.dxf.insert.y)
                else:
                    tx, ty = e.dxf.insert.x, e.dxf.insert.y
                th = getattr(e.dxf, 'char_height', 2.0) if hasattr(e.dxf, 'char_height') else 2.0
                tw = th * len(txt) * 0.6 if txt else th * 6
                mrg = 1.5
                text_bboxes.append((tx - mrg, tx + tw + mrg, ty - mrg, ty + th + mrg))
            except Exception:
                pass

    def _add_block_entities(blk, depth=0):
        if depth > 5:
            return
        for sub in blk:
            if sub.dxftype() == 'INSERT':
                sub_blk = doc.blocks.get(sub.dxf.name)
                if sub_blk:
                    _add_block_entities(sub_blk, depth + 1)
            else:
                add_entity(sub)

    # 1) Part blocks in this view
    for blk in doc.blocks:
        if blk.name.startswith('Part') and re.search(rf' - {view_id}$', blk.name):
            _add_block_entities(blk)

    # 2) WeldMark and Mark blocks in this view (original symbols, leader lines)
    for blk in doc.blocks:
        if (blk.name.startswith('WeldMark') or blk.name.startswith('Mark')):
            if re.search(rf' - {view_id}$', blk.name):
                _add_block_entities(blk)

    # 3) Modelspace entities (dimensions, title blocks, general annotations)
    for e in doc.modelspace():
        if e.dxftype() == 'INSERT':
            blk = doc.blocks.get(e.dxf.name)
            if blk:
                _add_block_entities(blk)
        else:
            add_entity(e)

    return lines, text_bboxes, circles


def annotate(results, dxf_paths=None):
    """Main entry point. Annotates DXF files with weld labels."""
    os.makedirs(ANNOTATED_DIR, exist_ok=True)

    by_comp = defaultdict(list)
    for r in results:
        by_comp[r['component']].append(r)

    if dxf_paths is None:
        import glob
        dxf_paths = sorted([f for f in glob.glob(os.path.join(FOLDER, "*.dxf")) if '(2)' not in f])

    for dxf_path in dxf_paths:
        comp_m = re.search(r'-(BE\d+|CO\d+)_', os.path.basename(dxf_path), re.I)
        comp = comp_m.group(1).upper() if comp_m else os.path.splitext(os.path.basename(dxf_path))[0]
        comp_full = os.path.splitext(os.path.basename(dxf_path))[0].rsplit('_', 1)[0]

        if comp not in by_comp:
            print(f"  SKIP {comp_full}: no weld data")
            continue

        comp_welds = by_comp[comp]
        print(f"\n  Annotating {comp_full} ({len(comp_welds)} welds) → {os.path.basename(dxf_path)}")

        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:
            print(f"    ERROR reading {dxf_path}: {e}")
            continue

        try:
            _annotate_one(doc, comp_welds)
            out_path = os.path.join(ANNOTATED_DIR, os.path.basename(dxf_path))
            doc.saveas(out_path)

            # Patch DXF header to inject $VIEWCTR and $VIEWSIZE (ezdxf drops them)
            try:
                hv = doc.header.hdrvars
                cx, cy, _ = hv['$VIEWCTR'].value if '$VIEWCTR' in hv else (0, 0, 0)
                view_size = hv['$VIEWSIZE'].value if '$VIEWSIZE' in hv else 1
                _patch_header_viewctr(out_path, cx, cy, view_size)
            except Exception:
                pass

            print(f"    Saved → {out_path}")
        except Exception as e:
            import traceback
            print(f"    ERROR annotating {comp}: {e}")
            traceback.print_exc()


def _compute_drawing_bbox(doc):
    """Compute global drawing extents from all modelspace and block entities."""
    xs, ys = [], []
    def add_pt(pt): xs.append(pt[0]); ys.append(pt[1])
    def add_line(s, e): add_pt(s); add_pt(e)
    msp = doc.modelspace()
    for e in msp:
        t = e.dxftype()
        if t == 'LINE': add_line(e.dxf.start, e.dxf.end)
        elif t in ('TEXT','MTEXT'): add_pt(e.dxf.insert)
        elif t == 'CIRCLE': add_pt(e.dxf.center)
    for blk in doc.blocks:
        for e in blk:
            t = e.dxftype()
            if t == 'LINE': add_line(e.dxf.start, e.dxf.end)
            elif t in ('TEXT','MTEXT'): add_pt(e.dxf.insert)
            elif t in ('CIRCLE','ARC'): add_pt(e.dxf.center)
    if not xs: return None
    return [min(xs), min(ys), max(xs), max(ys)]


def _annotate_one(doc, welds):
    """Annotate a single DXF with weld labels."""
    msp = doc.modelspace()
    _ensure_layer(doc)

    # Compute view bounding boxes from Part blocks for center calculation
    view_bboxes, part_centroids = _compute_view_bboxes(doc)

    # Compute global drawing boundary from union of all view bboxes
    if view_bboxes:
        _all_xs = [bb[i] for bb in view_bboxes.values() for i in (0, 2)]
        _all_ys = [bb[i] for bb in view_bboxes.values() for i in (1, 3)]
        draw_bbox = [min(_all_xs), min(_all_ys), max(_all_xs), max(_all_ys)]
    else:
        draw_bbox = _compute_drawing_bbox(doc)

    # Group welds by view_id
    welds_by_view = defaultdict(list)
    welds_no_view = []
    for w in welds:
        vid = w.get('view_id', '')
        if vid:
            welds_by_view[vid].append(w)
        else:
            welds_no_view.append(w)

    # Global F/W counters (mutable to increment across views)
    f_counter = [0]
    w_counter = [0]

    # Process each view in numerical order
    for view_id in sorted(welds_by_view.keys(), key=lambda v: int(v) if v.isdigit() else 0):
        vw = welds_by_view[view_id]
        bbox = view_bboxes.get(view_id)
        centroids = part_centroids.get(view_id, [])
        part_lines = _collect_all_obstacles(doc, view_id)
        _annotate_view(msp, vw, view_id, bbox, centroids, f_counter, w_counter, part_lines, draw_bbox)

    # Handle welds without view_id
    if welds_no_view:
        _annotate_welds_no_view(msp, welds_no_view, welds, f_counter, w_counter)

    # Zoom modelspace view to extents including all labels
    _set_model_view_to_extents(doc)

    print(f"    F: {f_counter[0]}  W: {w_counter[0]}")


def _ensure_layer(doc):
    """Ensure WELD_LABELS layer exists with red color."""
    if LAYER_NAME not in doc.layers:
        layer = doc.layers.new(name=LAYER_NAME)
    else:
        layer = doc.layers.get(LAYER_NAME)
    layer.color = LABEL_COLOR  # blue


def _compute_view_bboxes(doc):
    """Compute bounding box and Part block centroids of each view."""
    view_bboxes = {}
    view_part_centroids = defaultdict(list)  # view_id -> [(cx, cy), ...]
    for blk in doc.blocks:
        blk_name = blk.name
        m = re.search(r' - (\d+)$', blk_name)
        if not m:
            continue
        view_id = m.group(1)
        if not blk_name.startswith('Part'):
            continue
        xs, ys = [], []
        for e in blk:
            if e.dxftype() == 'LINE':
                xs.extend([e.dxf.start.x, e.dxf.end.x])
                ys.extend([e.dxf.start.y, e.dxf.end.y])
        if xs:
            if view_id not in view_bboxes:
                view_bboxes[view_id] = [min(xs), min(ys), max(xs), max(ys)]
            else:
                bb = view_bboxes[view_id]
                bb[0] = min(bb[0], min(xs))
                bb[1] = min(bb[1], min(ys))
                bb[2] = max(bb[2], max(xs))
                bb[3] = max(bb[3], max(ys))
            view_part_centroids[view_id].append((sum(xs)/len(xs), sum(ys)/len(ys)))
    return view_bboxes, dict(view_part_centroids)


def _annotate_view(msp, welds, view_id, bbox, part_centroids, f_counter, w_counter, obstacles, draw_bbox=None):
    """Annotate all welds in a single view.
    相同位置的 Above+Below 焊缝对共用一根引线，标号并排在横线末端。CJP(W*) 和 FW(F*) 不混合。"""
    lines, text_bboxes, circles = obstacles
    pos_welds = [(w, w['dxf_pos']) for w in welds if w.get('dxf_pos')]
    no_pos_welds = [w for w in welds if not w.get('dxf_pos')]
    if not pos_welds and not no_pos_welds:
        return

    # 焊缝点禁区：使标签避免覆盖焊缝原始符号区域
    _weld_exclusion_radius = 5.0
    for w, wp in pos_welds:
        circles.append((wp[0], wp[1], _weld_exclusion_radius))

    if bbox:
        cx = (bbox[0] + bbox[2]) / 2
        cy = (bbox[1] + bbox[3]) / 2
        vx0, vy0, vx1, vy1 = bbox[0], bbox[1], bbox[2], bbox[3]
    elif pos_welds:
        xs = [p[0] for _, p in pos_welds]
        ys = [p[1] for _, p in pos_welds]
        cx, cy = sum(xs)/len(xs), sum(ys)/len(ys)
        m = 40
        vx0, vy0, vx1, vy1 = min(xs)-m, min(ys)-m, max(xs)+m, max(ys)+m
    else:
        cx = cy = vx0 = vy0 = vx1 = vy1 = 0

    # 按近邻焊缝数降序排序（密集区优先放置，减少后续冲突）
    _CROWD_RADIUS = 30.0
    _crowd_count = {}
    for i, (_, pi) in enumerate(pos_welds):
        _cnt = 0
        for j, (_, pj) in enumerate(pos_welds):
            if i != j and math.hypot(pi[0]-pj[0], pi[1]-pj[1]) < _CROWD_RADIUS:
                _cnt += 1
        _crowd_count[pi] = _cnt
    pos_welds.sort(key=lambda wp: (-_crowd_count[wp[1]], -wp[1][1], wp[1][0]))

    # ---- 分组：同位配对（CJP(Above)+FW(Below)跨类型可配对，同类型Above+Below配对） ----
    POS_TOL = 1.0
    from itertools import groupby
    groups = []
    for (px, py), g_items in groupby(pos_welds, key=lambda wp: (round(wp[1][0], 0), round(wp[1][1], 0))):
        items = list(g_items)
        n = len(items)
        paired_idx = [False] * n

        # 1. 同类型 Above+Below 配对 (FW+FW)
        #    CJP 不做配对待：CJP 只有单面，始终单独标注
        #    也在同位处理 CJP+CJP（虽然罕见）
        for type_name, type_filter in [('FW', lambda it: it[0].get('weld_type','') != 'CJP'),
                                        ('CJP', lambda it: it[0].get('weld_type','') == 'CJP')]:
            above = [i for i in range(n)
                     if not paired_idx[i]
                     and items[i][0].get('position','') == 'Above'
                     and type_filter(items[i])]
            below = [i for i in range(n)
                     if not paired_idx[i]
                     and items[i][0].get('position','') == 'Below'
                     and type_filter(items[i])]
            npairs = min(len(above), len(below))
            for k in range(npairs):
                groups.append(('pair', [items[above[k]], items[below[k]]]))
                paired_idx[above[k]] = True
                paired_idx[below[k]] = True

        # 3. 剩余的作为单标注
        for i in range(n):
            if not paired_idx[i]:
                groups.append(('single', [items[i]]))

    # ---- 分散多实例：同位有多个组时，分配到不同质心 ----
    if part_centroids and len(set(part_centroids)) >= 2:
        _redistribute_groups(groups, part_centroids)

    placed_bboxes = []          # 引线+文字整体包围盒
    placed_text_bboxes = []     # 纯文字包围盒（用于文字重叠检测）
    _single_pos_count = {}
    _placements = []
    for gtype, items in groups:
        if gtype == 'pair':
            ww_a, wp_a = items[0]
            ww_b, wp_b = items[1]
            labels = [_next_label(ww_a, f_counter, w_counter),
                      _next_label(ww_b, f_counter, w_counter)]
            dname, diag_len, angle = _search_placement(
                wp_a, cx, cy, lines, text_bboxes, circles, placed_bboxes,
                placed_text_bboxes, vx0, vy0, vx1, vy1, draw_bbox, is_pair=True)
            bx0, bx1, by0, by1 = _paired_bbox(wp_a, dname, diag_len, angle)
            bbox = (min(bx0, wp_a[0])-1, max(bx1, wp_a[0])+1,
                    min(by0, wp_a[1])-1, max(by1, wp_a[1])+1)
            _placements.append((gtype, items, labels, wp_a, dname, diag_len, angle, bbox))
            placed_bboxes.append(bbox)
            # 纯文字包围盒
            _rad = math.radians(angle)
            _h_len = PAIR_HORIZ_LAND
            if math.cos(_rad) >= -0.05:
                _tbx = _label_corner(wp_a, dname, diag_len, angle, is_pair=True)[0]
                _tbx0, _tbx1 = _tbx, _tbx + LABEL_HEIGHT * 5.5
            else:
                _tbx = _label_corner(wp_a, dname, diag_len, angle, is_pair=True)[0]
                _tbx0, _tbx1 = _tbx - LABEL_HEIGHT * 5.5, _tbx
            _tby = _label_corner(wp_a, dname, diag_len, angle, is_pair=True)[1]
            placed_text_bboxes.append((_tbx0, _tbx1, _tby, _tby + LABEL_HEIGHT))
        else:
            ww, wp = items[0]
            label = _next_label(ww, f_counter, w_counter)
            _pk = (round(wp[0], 0), round(wp[1], 0))
            _n_prev = _single_pos_count.get(_pk, 0)
            _single_pos_count[_pk] = _n_prev + 1
            _use_cx = cx
            if _n_prev > 0:
                _use_cx = wp[0] + (wp[0] - cx)
            dname, diag_len, angle = _search_placement(
                wp, _use_cx, cy, lines, text_bboxes, circles, placed_bboxes,
                placed_text_bboxes, vx0, vy0, vx1, vy1, draw_bbox)
            bx0, bx1, by0, by1 = _single_bbox(wp, dname, diag_len, angle)
            bbox = (min(bx0, wp[0])-1, max(bx1, wp[0])+1,
                    min(by0, wp[1])-1, max(by1, wp[1])+1)
            _placements.append((gtype, items, [label], wp, dname, diag_len, angle, bbox))
            placed_bboxes.append(bbox)
            # 纯文字包围盒
            _rad = math.radians(angle)
            if math.cos(_rad) >= -0.05:
                _tbx = _label_corner(wp, dname, diag_len, angle, is_pair=False)[0]
                _tbx0, _tbx1 = _tbx, _tbx + LABEL_HEIGHT * 2.5
            else:
                _tbx = _label_corner(wp, dname, diag_len, angle, is_pair=False)[0]
                _tbx0, _tbx1 = _tbx - LABEL_HEIGHT * 2.5, _tbx
            _tby = _label_corner(wp, dname, diag_len, angle, is_pair=False)[1]
            placed_text_bboxes.append((_tbx0, _tbx1, _tby, _tby + LABEL_HEIGHT))

    # ---- 全局后处理：冲突解决（最多3次迭代） ----
    _resolve_label_conflicts(msp, lines, text_bboxes, circles,
                             vx0, vy0, vx1, vy1, draw_bbox, _placements, placed_text_bboxes, 3)

    # ---- 绘制所有标注 ----
    for pd in _placements:
        gtype, items, labels, pos, dname, diag_len, angle = pd[:7]
        if gtype == 'pair':
            _draw_paired_weld_label(msp, labels, pos, dname, diag_len, angle)
        else:
            _draw_weld_label(msp, labels[0], pos, dname, diag_len, angle)

    for w in no_pos_welds:
        label = _next_label(w, f_counter, w_counter)
        _draw_fallback_label(msp, w, label, bbox)


def _single_bbox(weld_pos, dname, diag_len, angle_deg):
    """计算单标注文字 + 引线完整包围盒。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    ex = wx + diag_len * cos_a
    ey = wy + diag_len * math.sin(rad)
    lx, ly = _label_corner(weld_pos, dname, diag_len, angle_deg, is_pair=False)
    lw = LABEL_HEIGHT * 2.5
    if cos_a >= -0.05:
        bx0, bx1 = lx, lx + lw
    else:
        bx0, bx1 = lx - lw, lx
    # Include leader path
    all_xs = [wx, ex, bx0, bx1]
    all_ys = [wy, ey, ly, ly + LABEL_HEIGHT]
    return min(all_xs), max(all_xs), min(all_ys), max(all_ys)


def _paired_bbox(weld_pos, dname, diag_len, angle_deg):
    """计算配对标注"F1,F2"文字 + 引线完整包围盒。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    ex = wx + diag_len * cos_a
    ey = wy + diag_len * math.sin(rad)
    lx, ly = _label_corner(weld_pos, dname, diag_len, angle_deg, is_pair=True)
    pair_w = LABEL_HEIGHT * 5.5
    if cos_a >= -0.05:
        bx0, bx1 = lx, lx + pair_w
    else:
        bx0, bx1 = lx - pair_w, lx
    all_xs = [wx, ex, bx0, bx1]
    all_ys = [wy, ey, ly, ly + LABEL_HEIGHT]
    return min(all_xs), max(all_xs), min(all_ys), max(all_ys)


def _next_label(w, f_counter, w_counter):
    if w.get('annotation') == 'CJP' or w.get('weld_type') == 'CJP':
        w_counter[0] += 1
        return f'W{w_counter[0]}'
    else:
        f_counter[0] += 1
        return f'F{f_counter[0]}'


def _dir_info(dname):
    for d in DIRECTIONS:
        if d[0] == dname:
            return d
    return DIRECTIONS[0]


def _label_corner(weld_pos, dname, diag_len, angle_deg, is_pair=False):
    """返回标注文字的实际位置：水平接地线末端的 (x, y)。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    ex = wx + diag_len * cos_a
    ey = wy + diag_len * math.sin(rad)
    h_len = PAIR_HORIZ_LAND if is_pair else HORIZ_LAND
    if cos_a >= -0.05:
        hx = ex + h_len
    else:
        hx = ex - h_len
    return (hx, ey)


def _draw_arrow_head(msp, tip, angle_deg, arm_len=2.0):
    """在焊缝起点 tip 处绘制 V 形箭头，指向焊缝位置。"""
    rad = math.radians(angle_deg)
    lx = tip[0] + arm_len * math.cos(rad + 0.35)
    ly = tip[1] + arm_len * math.sin(rad + 0.35)
    rx = tip[0] + arm_len * math.cos(rad - 0.35)
    ry = tip[1] + arm_len * math.sin(rad - 0.35)
    for p in ((lx,ly),(rx,ry)):
        msp.add_line(start=p, end=tip,
                     dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})


def _draw_weld_label(msp, label, weld_pos, dname, diag_len, angle_deg):
    """绘制标注：箭头 → 斜线 → 水平接地短横线 → 文字紧贴横线末端。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    ex = wx + diag_len * cos_a
    ey = wy + diag_len * sin_a

    if cos_a >= -0.05:
        h_land = HORIZ_LAND
    else:
        h_land = -HORIZ_LAND
    hx = ex + h_land
    hy = ey

    # 箭头（指向焊缝起点）
    _draw_arrow_head(msp, (wx, wy), angle_deg)

    # 绘制引线
    msp.add_line(start=(wx, wy), end=(ex, ey),
                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})
    msp.add_line(start=(ex, ey), end=(hx, hy),
                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})

    if h_land >= 0:
        ap = MT_BOTTOM_LEFT
        lx = hx
    else:
        ap = MT_BOTTOM_RIGHT
        lx = hx
    msp.add_mtext(label, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': LABEL_HEIGHT,
        'insert': (lx, hy),
        'attachment_point': ap,
    })


def _draw_paired_weld_label(msp, labels, weld_pos, dname, diag_len, angle_deg):
    """绘制配对标注：共享引线 + 较长水平横线 + \"F1,F2\" 一个 MTEXT。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    ex = wx + diag_len * cos_a
    ey = wy + diag_len * sin_a

    h_len = PAIR_HORIZ_LAND
    h_land = h_len if cos_a >= -0.05 else -h_len
    hx = ex + h_land
    hy = ey

    # 箭头（指向焊缝起点）
    _draw_arrow_head(msp, (wx, wy), angle_deg)

    # 绘制引线
    msp.add_line(start=(wx, wy), end=(ex, ey),
                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})
    msp.add_line(start=(ex, ey), end=(hx, hy),
                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})

    # 合并编号："F1,F2"
    paired_text = f"{labels[0]},{labels[1]}"
    if h_land >= 0:
        ap = MT_BOTTOM_RIGHT
        lx = hx
    else:
        ap = MT_BOTTOM_LEFT
        lx = hx
    msp.add_mtext(paired_text, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': LABEL_HEIGHT,
        'insert': (lx, hy),
        'attachment_point': ap,
    })


def _search_placement(weld_pos, cx, cy, lines, text_bboxes, circles, placed_bboxes,
                      placed_text_bboxes, vx0, vy0, vx1, vy1, draw_bbox=None, is_pair=False):
    """两阶段搜索最佳标注位置。"""
    wx, wy = weld_pos

    # 空间索引：将几何线分到 50×50 网格，加速近邻查询
    _GRID = 50
    _line_grid = {}
    for _s, _e in lines:
        _gx0 = int(min(_s[0], _e[0]) / _GRID)
        _gx1 = int(max(_s[0], _e[0]) / _GRID)
        _gy0 = int(min(_s[1], _e[1]) / _GRID)
        _gy1 = int(max(_s[1], _e[1]) / _GRID)
        for _gx in range(_gx0, _gx1 + 1):
            for _gy in range(_gy0, _gy1 + 1):
                _line_grid.setdefault((_gx, _gy), []).append((_s, _e))

    def _fine_tune(dist, angle, _draw_bbox):
        """在给定角度附近以1°步长精调，返回更优结果。"""
        _best = (angle, _score_placement(wx, wy, angle, dist, lines, text_bboxes,
                                         circles, placed_bboxes, placed_text_bboxes,
                                         vx0, vy0, vx1, vy1,
                                         _draw_bbox, is_pair=is_pair, min_score=-999999999,
                                         line_grid=_line_grid))
        for d_angle in [-3, -2, -1, 1, 2, 3]:
            a = angle + d_angle
            s = _score_placement(wx, wy, a, dist, lines, text_bboxes,
                                 circles, placed_bboxes, placed_text_bboxes,
                                 vx0, vy0, vx1, vy1,
                                 _draw_bbox, is_pair=is_pair, min_score=_best[1],
                                 line_grid=_line_grid)
            if s > _best[1]:
                _best = (a, s)
        return _best[0], _best[1]

    def _hemi_penalty(angle_deg):
        """半球约束惩罚（不阻止无冲突正分退出，仅影响候选排名）。"""
        rad = math.radians(angle_deg)
        ca, sa = math.cos(rad), math.sin(rad)
        _vcx = (vx0 + vx1) / 2
        _vcy = (vy0 + vy1) / 2
        p = 0
        if wx < _vcx and ca >= -0.05:
            p += 80
        elif wx >= _vcx and ca < -0.05:
            p += 80
        if wy > _vcy and sa < -0.05:
            p += 40
        elif wy < _vcy and sa > 0.05:
            p += 40
        return p

    def _search_pass(_draw_bbox):
        """搜索：短距离优先，找到无冲突后尽早退出。"""
        priority = _direction_priority(wx, wy, cx, cy)
        distances = [10, 14, 18, 22, 26, 30, 36, 48]
        if is_pair:
            distances = [d + 4 for d in distances]

        _dir_rank = {d: i for i, d in enumerate(priority)}
        _best_score = -999999999
        _best_result = ('E', min(distances), 0)

        for dist_i, dist in enumerate(distances):
            for dname in priority:
                _, base, lo, hi, _, _ = _dir_info(dname)
                angles = [base, max(lo, base-12), min(hi, base+12),
                          max(lo, base-8), min(hi, base+8)]
                for angle_deg in angles:
                    score = _score_placement(wx, wy, angle_deg, dist, lines, text_bboxes,
                                             circles, placed_bboxes, placed_text_bboxes,
                                             vx0, vy0, vx1, vy1,
                                             _draw_bbox, is_pair=is_pair, min_score=_best_score,
                                             line_grid=_line_grid)
                    _hp = _hemi_penalty(angle_deg)
                    if score >= 0:
                        return score, (dname, dist, angle_deg)
                    _dr = _dir_rank.get(dname, 99)
                    _adj_score = score - _hp - dist * 0.5 - _dr * 0.1
                    if _adj_score > _best_score:
                        _best_score = _adj_score
                        _best_result = (dname, dist, angle_deg)

            for dname in [d[0] for d in DIRECTIONS]:
                if dname in priority: continue
                _, base, lo, hi, _, _ = _dir_info(dname)
                angles = [base, max(lo, base-12), min(hi, base+12)]
                for angle_deg in angles:
                    score = _score_placement(wx, wy, angle_deg, dist, lines, text_bboxes,
                                             circles, placed_bboxes, placed_text_bboxes,
                                             vx0, vy0, vx1, vy1,
                                             _draw_bbox, is_pair=is_pair, min_score=_best_score,
                                             line_grid=_line_grid)
                    _hp = _hemi_penalty(angle_deg)
                    if score >= 0:
                        # 无冲突：精调后立即返回
                        _fa, _fs = _fine_tune(dist, angle_deg, _draw_bbox)
                        return _fs, (dname, dist, _fa)
                    # 记录最优负分（含半球惩罚）
                    _dr = _dir_rank.get(dname, 99)
                    _adj_score = score - _hp - dist * 0.5 - _dr * 0.1
                    if _adj_score > _best_score:
                        _best_score = _adj_score
                        _best_result = (dname, dist, angle_deg)
            # 前5个距离档后仍无无冲突位置，接受当前最优
            if dist_i >= 4 and _best_score < 0:
                return _best_score, _best_result

        return _best_score, _best_result

    score, result = _search_pass(draw_bbox)
    # 若所有候选均无冲突正分，选最优负分返回（二级回退已不需要）
    return result[0], result[1], result[2]


def _score_placement(wx, wy, angle_deg, dist, lines, text_bboxes, circles,
                     placed_bboxes, placed_text_bboxes, vx0, vy0, vx1, vy1,
                     draw_bbox=None, is_pair=False, min_score=None, line_grid=None):
    """对 (角度, 距离) 位置评分。分值越高越推荐，正分表示无冲突，负分表示冲突严重。"""
    score = 0
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    ex = wx + dist * cos_a
    ey = wy + dist * sin_a

    h_len = PAIR_HORIZ_LAND if is_pair else HORIZ_LAND
    h_land = h_len if cos_a >= -0.05 else -h_len
    hx = ex + h_land
    hy = ey

    lw = LABEL_HEIGHT * 5.5 if is_pair else LABEL_HEIGHT * 2.5
    lh = LABEL_HEIGHT
    if h_land >= 0:
        bx0, bx1 = hx, hx + lw
    else:
        bx0, bx1 = hx - lw, hx
    by0, by1 = hy, hy + lh

    # 候选标注整体包围盒：用于近邻线过滤（大幅提升性能）
    _cand_x0 = min(wx, ex, hx, bx0)
    _cand_x1 = max(wx, ex, hx, bx1)
    _cand_y0 = min(wy, ey, hy, by0)
    _cand_y1 = max(wy, ey, hy, by1)
    _PROX_MARGIN = 15
    if line_grid is not None:
        _gx0 = int((_cand_x0 - _PROX_MARGIN) / 50)
        _gx1 = int((_cand_x1 + _PROX_MARGIN) / 50)
        _gy0 = int((_cand_y0 - _PROX_MARGIN) / 50)
        _gy1 = int((_cand_y1 + _PROX_MARGIN) / 50)
        _cand = []
        for _gx in range(_gx0, _gx1 + 1):
            for _gy in range(_gy0, _gy1 + 1):
                _cand.extend(line_grid.get((_gx, _gy), []))
        _near_lines = [(s, e) for (s, e) in _cand
                       if not (max(s[0], e[0]) < _cand_x0 - _PROX_MARGIN or
                               min(s[0], e[0]) > _cand_x1 + _PROX_MARGIN or
                               max(s[1], e[1]) < _cand_y0 - _PROX_MARGIN or
                               min(s[1], e[1]) > _cand_y1 + _PROX_MARGIN)]
    else:
        _near_lines = [(s, e) for (s, e) in lines
                       if not (max(s[0], e[0]) < _cand_x0 - _PROX_MARGIN or
                               min(s[0], e[0]) > _cand_x1 + _PROX_MARGIN or
                               max(s[1], e[1]) < _cand_y0 - _PROX_MARGIN or
                               min(s[1], e[1]) > _cand_y1 + _PROX_MARGIN)]

    # 超出视图/图纸边界：渐进重惩罚（超出越多扣越多，确保图内位置永远优先）
    # margin 80 给标注留缓冲空间
    _BOUNDARY_MARGIN = 80
    _extra_x = max(bx1 - (vx1 + _BOUNDARY_MARGIN),
                   vx0 - _BOUNDARY_MARGIN - bx0, 0)
    _extra_y = max(by1 - (vy1 + _BOUNDARY_MARGIN),
                   vy0 - _BOUNDARY_MARGIN - by0, 0)
    _extra = max(_extra_x, _extra_y)
    if _extra > 0:
        score -= _extra * 2000

    # 超出全局图纸边界：margin=0，绝对不可超（打印范围）
    if draw_bbox is not None:
        dbx0, dby0, dbx1, dby1 = draw_bbox
        _extra_x_g = max(bx1 - dbx1, dbx0 - bx0, 0)
        _extra_y_g = max(by1 - dby1, dby0 - by0, 0)
        _extra_g = max(_extra_x_g, _extra_y_g)
        if _extra_g > 0:
            score -= _extra_g * 2000

    # 水平接地线与几何线交叉：扣30
    for (sx, sy), (ex2, ey2) in _near_lines:
        if _segments_cross_((ex, ey), (hx, hy), (sx, sy), (ex2, ey2)):
            score -= 30
  
    # 水平接地线穿过文字框：扣100
    for (tx0, tx1, ty0, ty1) in text_bboxes:
        if _seg_cross_rect((ex, ey), (hx, hy), tx0, tx1, ty0, ty1):
            score -= 100
  
    # 水平接地线穿过已放置标注：扣80
    for (pbx0, pbx1, pby0, pby1) in placed_bboxes:
        if _seg_cross_rect((ex, ey), (hx, hy), pbx0, pbx1, pby0, pby1):
            score -= 80

    # 斜引线穿过文字框：扣60
    for (tx0, tx1, ty0, ty1) in text_bboxes:
        if _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
            score -= 60
  
    # 斜引线穿过已放置标注：扣40
    for (pbx0, pbx1, pby0, pby1) in placed_bboxes:
        if _seg_cross_rect((wx, wy), (ex, ey), pbx0, pbx1, pby0, pby1):
            score -= 40

    # 斜引线靠近文字框但不穿过：扣30
    _DIAG_PROX_MARGIN = 3.0
    for (tx0, tx1, ty0, ty1) in text_bboxes:
        _cx_t = (tx0 + tx1) / 2
        _cy_t = (ty0 + ty1) / 2
        d_diag, _ = _dist_pt_to_seg((_cx_t, _cy_t), (wx, wy), (ex, ey))
        if d_diag < _DIAG_PROX_MARGIN:
            if not _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
                score -= 30

    # 文字与已有文字框重叠：扣120（间距8.0）
    _OVERLAP_MARGIN = 8.0
    for (tx0, tx1, ty0, ty1) in text_bboxes:
        if bx1 > tx0 - _OVERLAP_MARGIN and bx0 < tx1 + _OVERLAP_MARGIN and by1 > ty0 - _OVERLAP_MARGIN and by0 < ty1 + _OVERLAP_MARGIN:
            score -= 120
 
    # 文字与已放置标注文字重叠：扣120（只检测纯文字框，不含引线）
    for (pbx0, pbx1, pby0, pby1) in placed_text_bboxes:
        if bx1 > pbx0 - _OVERLAP_MARGIN and bx0 < pbx1 + _OVERLAP_MARGIN and by1 > pby0 - _OVERLAP_MARGIN and by0 < pby1 + _OVERLAP_MARGIN:
            score -= 120
 
    # 文字与几何线过近：扣30（检测4个角点）
    _LINE_MARGIN = 4.0
    _txt_sample_pts = [(bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1)]
    for (sx, sy), (ex2, ey2) in _near_lines:
        if bx1 < min(sx, ex2) - _LINE_MARGIN: continue
        if bx0 > max(sx, ex2) + _LINE_MARGIN: continue
        if by1 < min(sy, ey2) - _LINE_MARGIN: continue
        if by0 > max(sy, ey2) + _LINE_MARGIN: continue
        for (cx, cy) in _txt_sample_pts:
            d, _ = _dist_pt_to_seg((cx, cy), (sx, sy), (ex2, ey2))
            if d < _LINE_MARGIN:
                score -= 30
                break
 
    # 文字中心点被几何线包围（在结构内部）：扣80
    cx_txt = (bx0 + bx1) / 2
    cy_txt = (by0 + by1) / 2
    _min_center_dist = 999
    for (sx, sy), (ex2, ey2) in _near_lines:
        d, _ = _dist_pt_to_seg((cx_txt, cy_txt), (sx, sy), (ex2, ey2))
        if d < _min_center_dist:
            _min_center_dist = d
    if _min_center_dist < 1.5:
        score -= 80

    # 文字与圆/弧重叠：扣30
    for (ccx, ccy, cr) in circles:
        if bx1 > ccx - cr and bx0 < ccx + cr and by1 > ccy - cr and by0 < ccy + cr:
            score -= 30

    # 距离惩罚：越远越不推荐（避免延伸出图）
    if dist > 30:
        score -= (dist - 30) * 1.5

    return score


def _segments_cross_(p1, p2, p3, p4):
    def ccw(a, b, c):
        return (c[1]-a[1])*(b[0]-a[0]) > (b[1]-a[1])*(c[0]-a[0])
    return ccw(p1,p3,p4) != ccw(p2,p3,p4) and ccw(p1,p2,p3) != ccw(p1,p2,p4)


def _dist_pt_to_seg(pt, s, e):
    dx, dy = e[0]-s[0], e[1]-s[1]
    len_sq = dx*dx + dy*dy
    if len_sq < 1e-12:
        return math.hypot(pt[0]-s[0], pt[1]-s[1]), 0.0
    t = max(0.0, min(1.0, ((pt[0]-s[0])*dx + (pt[1]-s[1])*dy) / len_sq))
    proj = (s[0]+t*dx, s[1]+t*dy)
    return math.hypot(pt[0]-proj[0], pt[1]-proj[1]), t


def _seg_cross_rect(p1, p2, rx0, rx1, ry0, ry1):
    """Check if line segment p1→p2 intersects axis-aligned rectangle (rx0,rx1,ry0,ry1)."""
    # Check if either endpoint is inside
    if rx0 <= p1[0] <= rx1 and ry0 <= p1[1] <= ry1: return True
    if rx0 <= p2[0] <= rx1 and ry0 <= p2[1] <= ry1: return True
    # Check if segment crosses any of the 4 edges
    for (a, b) in [((rx0,ry0),(rx1,ry0)), ((rx1,ry0),(rx1,ry1)),
                   ((rx1,ry1),(rx0,ry1)), ((rx0,ry1),(rx0,ry0))]:
        if _segments_cross_(p1, p2, a, b):
            return True
    return False


def _redistribute_groups(groups, centroids):
    """同位置有多个 group 时，垂直偏移避免完全重叠。支持所有类型（pair/single/CJP）。"""
    if not centroids:
        return
    uniq_c = list(set(centroids))
    pos_map = {}
    for gi, (gtype, items) in enumerate(groups):
        pos = items[0][1]
        key = (round(pos[0], 0), round(pos[1], 0))
        pos_map.setdefault(key, []).append((gi, gtype))
    for pos_key, entries in pos_map.items():
        if len(entries) <= 1:
            continue
        wx, wy = pos_key
        _y_step = LABEL_HEIGHT * 2.5
        for i_idx, (gi, gtype) in enumerate(entries):
            if i_idx == 0:
                continue
            _offset_y = wy + _y_step * i_idx
            _, items = groups[gi]
            groups[gi] = (gtype, [(it[0], (wx, _offset_y)) for it in items])


def _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
    """检查 bbox (x0,x1,y0,y1) 在视图边界内（margin=80）且在图纸边界内（margin=0）。"""
    if not (vx0 - 80 <= nbb[0] and nbb[1] <= vx1 + 80 and
            vy0 - 80 <= nbb[2] and nbb[3] <= vy1 + 80):
        return False
    if draw_bbox is not None:
        dx0, dy0, dx1, dy1 = draw_bbox
        if not (dx0 <= nbb[0] and nbb[1] <= dx1 and
                dy0 <= nbb[2] and nbb[3] <= dy1):
            return False
    return True


def _resolve_label_conflicts(msp, lines, text_bboxes, circles,
                              vx0, vy0, vx1, vy1, draw_bbox, placements, placed_text_bboxes, max_iter=3):
    """全局后处理：检测标注间的文字重叠并进行微调，最多 max_iter 次迭代。"""
    _OVERLAP_MARGIN = 8.0
    for _iter in range(max_iter):
        _any_fix = False
        n = len(placements)
        for i in range(n):
            gi, _, _, _, _, _, _, _ = placements[i]
            tbb_i = placed_text_bboxes[i]
            for j in range(i + 1, n):
                gj, it_j, lb_j, pos_j, dn_j, ds_j, ag_j, _ = placements[j]
                tbb_j = placed_text_bboxes[j]
                if (tbb_i[1] < tbb_j[0] - _OVERLAP_MARGIN or
                    tbb_i[0] > tbb_j[1] + _OVERLAP_MARGIN or
                    tbb_i[3] < tbb_j[2] - _OVERLAP_MARGIN or
                    tbb_i[2] > tbb_j[3] + _OVERLAP_MARGIN):
                    continue
                # 有重叠：微调距离
                _fixed = False
                for d_dist in [2, -2, 4, -4]:
                    nd = ds_j + d_dist
                    if nd < 8 or nd > 55:
                        continue
                    if gj == 'pair':
                        nbb = _paired_bbox(pos_j, dn_j, nd, ag_j)
                    else:
                        nbb = _single_bbox(pos_j, dn_j, nd, ag_j)
                    if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
                        continue
                    _ok = True
                    for k in range(len(placements)):
                        if k == j: continue
                        otb = placed_text_bboxes[k]
                        if not (nbb[1] < otb[0] - _OVERLAP_MARGIN or
                                nbb[0] > otb[1] + _OVERLAP_MARGIN or
                                nbb[3] < otb[2] - _OVERLAP_MARGIN or
                                nbb[2] > otb[3] + _OVERLAP_MARGIN):
                            _ok = False
                            break
                    if _ok:
                        for (tx0, tx1, ty0, ty1) in text_bboxes:
                            if not (nbb[1] < tx0 - _OVERLAP_MARGIN or
                                    nbb[0] > tx1 + _OVERLAP_MARGIN or
                                    nbb[3] < ty0 - _OVERLAP_MARGIN or
                                    nbb[2] > ty1 + _OVERLAP_MARGIN):
                                _ok = False
                                break
                    if _ok:
                        placements[j] = (gj, it_j, lb_j, pos_j, dn_j, nd, ag_j, nbb)
                        _fixed = True
                        break
                if _fixed:
                    _any_fix = True
                    continue
                # 微调角度
                for d_a in [3, -3, 5, -5]:
                    na = ag_j + d_a
                    if gj == 'pair':
                        nbb = _paired_bbox(pos_j, dn_j, ds_j, na)
                    else:
                        nbb = _single_bbox(pos_j, dn_j, ds_j, na)
                    if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
                        continue
                    _ok = True
                    for k in range(len(placements)):
                        if k == j: continue
                        otb = placed_text_bboxes[k]
                        if not (nbb[1] < otb[0] - _OVERLAP_MARGIN or
                                nbb[0] > otb[1] + _OVERLAP_MARGIN or
                                nbb[3] < otb[2] - _OVERLAP_MARGIN or
                                nbb[2] > otb[3] + _OVERLAP_MARGIN):
                            _ok = False
                            break
                    if _ok:
                        for (tx0, tx1, ty0, ty1) in text_bboxes:
                            if not (nbb[1] < tx0 - _OVERLAP_MARGIN or
                                    nbb[0] > tx1 + _OVERLAP_MARGIN or
                                    nbb[3] < ty0 - _OVERLAP_MARGIN or
                                    nbb[2] > ty1 + _OVERLAP_MARGIN):
                                _ok = False
                                break
                    if _ok:
                        placements[j] = (gj, it_j, lb_j, pos_j, dn_j, ds_j, na, nbb)
                        _fixed = True
                        break
                if _fixed:
                    _any_fix = True
            if _any_fix:
                break
        if not _any_fix:
            break
    # 最终安全兜底：强制所有超出边界的标注回到最短距离
    for k, pd in enumerate(placements):
        gk, it_k, lb_k, pk, dnk, dsk, agk, bbk = pd
        if not _bbox_in_boundary(bbk, vx0, vy0, vx1, vy1, draw_bbox):
            nd = 8
            if gk == 'pair':
                nbb = _paired_bbox(pk, dnk, nd, agk)
            else:
                nbb = _single_bbox(pk, dnk, nd, agk)
            if _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
                placements[k] = (gk, it_k, lb_k, pk, dnk, nd, agk, nbb)


def _draw_fallback_label(msp, w, label, bbox):
    """Draw label for a weld without precise coordinates."""
    if bbox:
        cx = (bbox[0] + bbox[2]) / 2
        cy = bbox[3] + LABEL_OFFSET * 2
    else:
        cx = cy = 0

    idx = int(re.sub(r'\D', '', label) or '0')  # extract number from label
    x = cx + (idx * LABEL_HEIGHT * 3)
    y = cy + LABEL_HEIGHT * 2

    # Small marker
    msp.add_circle(center=(x, y), radius=1.5,
                   dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})
    msp.add_mtext(label, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': LABEL_HEIGHT,
        'insert': (x + 3, y),
        'attachment_point': MT_MIDDLE_LEFT,
    })


def _annotate_welds_no_view(msp, welds_no_view, all_welds, f_counter, w_counter):
    """Annotate welds without view_id in a summary area above the drawing."""
    all_pos = [w['dxf_pos'] for w in all_welds if w.get('dxf_pos')]
    if all_pos:
        xs = [p[0] for p in all_pos]
        ys = [p[1] for p in all_pos]
        top_y = max(ys) + LABEL_OFFSET * 3
        mid_x = sum(xs) / len(xs)
    else:
        top_y = LABEL_OFFSET * 3
        mid_x = 0

    # Add section header
    msp.add_mtext('BOM-Derived Welds (no view):', dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': LABEL_HEIGHT * 1.2,
        'insert': (mid_x, top_y),
        'attachment_point': MT_MIDDLE_CENTER,
    })

    x = mid_x - LABEL_HEIGHT * 20
    y = top_y - LABEL_HEIGHT * 4
    for w in welds_no_view:
        label = _next_label(w, f_counter, w_counter)
        parts = f"{w.get('part1','')}/{w.get('part2','')}"
        full_label = f"{label} ({parts})"
        msp.add_mtext(full_label, dxfattribs={
            'layer': LAYER_NAME, 'color': LABEL_COLOR,
            'char_height': LABEL_HEIGHT,
            'insert': (x, y),
            'attachment_point': MT_MIDDLE_LEFT,
        })
        y -= LABEL_HEIGHT * 2

def _set_model_view_to_extents(doc):
    """Set VIEWCTR+VIEWSIZE + EXTMIN/EXTMAX so Model tab shows full content."""
    xs, ys = [], []

    def add_pt(pt):
        xs.append(pt[0]); ys.append(pt[1])

    def add_line(s, e):
        add_pt(s); add_pt(e)

    msp = doc.modelspace()
    for e in msp:
        t = e.dxftype()
        if t == 'LINE':
            add_line(e.dxf.start, e.dxf.end)
        elif t == 'TEXT':
            add_pt(e.dxf.insert)
        elif t == 'CIRCLE':
            add_pt(e.dxf.center)

    for blk in doc.blocks:
        for e in blk:
            t = e.dxftype()
            if t == 'LINE':
                add_line(e.dxf.start, e.dxf.end)
            elif t == 'CIRCLE':
                add_pt(e.dxf.center)
            elif t == 'ARC':
                add_pt(e.dxf.center)
            elif t == 'TEXT':
                add_pt(e.dxf.insert)
            elif t == 'MTEXT':
                add_pt(e.dxf.insert)

    if not xs:
        return

    xmin, xmax = min(xs), max(xs)
    ymin, ymax = min(ys), max(ys)
    pad = max((xmax - xmin) * 0.05, (ymax - ymin) * 0.05, 20.0)
    cx = (xmin + xmax) / 2.0
    cy = (ymin + ymax) / 2.0
    view_size = max(xmax - xmin, ymax - ymin) + pad * 2

    from ezdxf.sections.header import HeaderVar
    var_defs = [
        ('$EXTMIN', (10, (xmin - pad, ymin - pad, 0.0))),
        ('$EXTMAX', (10, (xmax + pad, ymax + pad, 0.0))),
        ('$VIEWCTR', (10, (cx, cy, 0.0))),
        ('$VIEWSIZE', (40, view_size)),
    ]
    for name, (code, val) in var_defs:
        if name in doc.header:
            doc.header[name] = val
        else:
            try:
                doc.header.hdrvars[name] = HeaderVar((code, val))
            except Exception:
                pass

    # Update paper space viewports to show full extents
    from ezdxf.layouts import Paperspace
    ps_list = [l for l in doc.layouts if isinstance(l, Paperspace)]
    for layout in ps_list:
        for vp in layout.query('VIEWPORT'):
            if vp.dxf.status == 0:  # skip the "active" viewport (paper space itself)
                continue
            try:
                vp.dxf.view_center_point = (cx, cy, 0.0)
                vp.dxf.view_height = view_size
            except Exception:
                pass


def _patch_header_viewctr(dxf_path, cx, cy, view_size):
    """Inject $VIEWCTR and $VIEWSIZE into the HEADER section of a saved ASCII DXF."""
    with open(dxf_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    in_header = False
    insert_idx = None
    for i, line in enumerate(lines):
        s = line.strip()
        if s == 'SECTION' and i + 2 < len(lines) and lines[i + 2].strip() == 'HEADER':
            in_header = True
        elif in_header and s == '0' and i + 1 < len(lines) and lines[i + 1].strip() == 'ENDSEC':
            insert_idx = i
            break

    if insert_idx is None:
        return

    header_block = [
        '  9\n', '$VIEWCTR\n',
        ' 10\n', f'{cx:.6f}\n',
        ' 20\n', f'{cy:.6f}\n',
        ' 30\n', '0.0\n',
        '  9\n', '$VIEWSIZE\n',
        ' 40\n', f'{view_size:.6f}\n',
    ]

    lines[insert_idx:insert_idx] = header_block

    with open(dxf_path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


# Standalone entry point for testing
if __name__ == '__main__':
    print("dxf_annotator.py — run via weld_extractor.py")
    print("Usage: import dxf_annotator; dxf_annotator.annotate(all_results, dxf_paths)")
