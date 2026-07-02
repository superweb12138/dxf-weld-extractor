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
MAX_DIAG_LEN = 24            # upper limit for diagonal length
MAX_DIAG_LEN_PAIR = 28       # slightly longer allowed for paired labels

# Preferred leader angles (right side / left side)
PREFERRED_ANGLES_RIGHT = (30, 45)
PREFERRED_ANGLES_LEFT = (135, 150)
ANGLE_MIN = 25
ANGLE_MAX = 75

# Cluster-aware fan-out parameters
CLUSTER_RADIUS = 28.0        # welds within this radius are considered a dense cluster
CLUSTER_MIN_SIZE = 3         # minimum number of labels to trigger fan-out assignment

# Quadrant angle ranges relative to view center (degrees, CCW from +X)
# Q1 (upper-right), Q2 (upper-left), Q3 (lower-left), Q4 (lower-right)
QUAD_ANGLE_RANGES = {
    1: (25, 75),      # Q1 右上方 25-75°
    2: (105, 155),    # Q2 左上方 105-155°
    3: (205, 255),    # Q3 左下方 205-255°
    4: (285, 335),    # Q4 右下方 285-335°
}

QUADRANT_ANGLE_TOL = 3.0
OVERLAP_MARGIN = 2.0
CLUSTER_OVERLAP_MARGIN = 3.0

# Hard exclusion margins
BOM_MARGIN = 0               # margin around BOM table (Unknown-* blocks)
BOUNDARY_MARGIN = 8          # margin around drawing inner frame (hard)

# 8-direction system: (name, base_angle_deg, min_angle, max_angle, dx_mult, dy_mult)
# angles: 0=right, 90=up, -90=down, +/-180=left
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


def _collect_all_obstacles(doc, view_id, view_bbox=None):
    """Collect all visual obstacles in a view: lines, text bboxes, circles/arcs, hatch bboxes.
    view_bbox: (x0,y0,x1,y1) to filter modelspace entities by spatial proximity."""
    lines, text_bboxes, circles, hatch_bboxes = [], [], [], []

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
            try:
                cx, cy = e.dxf.center.x, e.dxf.center.y
                r = getattr(e.dxf, 'radius', 1.0)
                sa = math.radians(e.dxf.start_angle)
                ea = math.radians(e.dxf.end_angle)
                if ea < sa: ea += math.pi * 2
                n_seg = max(4, int((ea - sa) / (math.pi / 8)))
                for i in range(n_seg):
                    a1 = sa + (ea - sa) * i / n_seg
                    a2 = sa + (ea - sa) * (i + 1) / n_seg
                    lines.append(((cx + r * math.cos(a1), cy + r * math.sin(a1)),
                                  (cx + r * math.cos(a2), cy + r * math.sin(a2))))
            except Exception:
                pass
        elif t == 'DIMENSION':
            try:
                tx, ty = e.dxf.text_midpoint.x, e.dxf.text_midpoint.y
                th = 2.0
                tw = th * len(str(getattr(e.dxf, 'text', ''))) * 0.7
                mrg = 1.5
                text_bboxes.append((tx - mrg, tx + max(tw, 12) + mrg, ty - mrg, ty + th + mrg))
            except Exception:
                pass
        elif t == 'HATCH':
            try:
                all_pts = []
                for path in e.paths:
                    for v in path.vertices:
                        all_pts.append((float(v[0]), float(v[1])))
                if all_pts:
                    xs = [p[0] for p in all_pts]
                    ys = [p[1] for p in all_pts]
                    hatch_bboxes.append((min(xs), max(xs), min(ys), max(ys)))
            except Exception:
                pass
        elif t == 'LEADER':
            try:
                txt = str(getattr(e.dxf, 'text', '')) or ''
                if txt:
                    tx, ty = e.dxf.insert.x, e.dxf.insert.y
                    th = 2.0
                    tw = th * len(txt) * 0.7
                    mrg = 1.5
                    text_bboxes.append((tx - mrg, tx + tw + mrg, ty - mrg, ty + th + mrg))
            except Exception:
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
        elif t == 'LWPOLYLINE':
            try:
                pts = list(e.get_points())
                for i in range(len(pts)):
                    j = (i + 1) % len(pts)
                    lines.append(((pts[i][0], pts[i][1]), (pts[j][0], pts[j][1])))
            except Exception:
                pass
        elif t == 'POLYLINE':
            try:
                pts = [(v.dxf.location.x, v.dxf.location.y) for v in e.vertices]
                for i in range(len(pts)):
                    j = (i + 1) % len(pts)
                    lines.append((pts[i], pts[j]))
            except Exception:
                pass
        elif t == 'SOLID':
            try:
                pts = [(e.dxf.vch1.x, e.dxf.vch1.y),
                       (e.dxf.vch2.x, e.dxf.vch2.y),
                       (e.dxf.vch3.x, e.dxf.vch3.y),
                       (e.dxf.vch4.x, e.dxf.vch4.y)]
                xs = [p[0] for p in pts if p[0] is not None]
                ys = [p[1] for p in pts if p[1] is not None]
                if xs and ys:
                    hatch_bboxes.append((min(xs), max(xs), min(ys), max(ys)))
            except Exception:
                pass
        elif t == '3DFACE':
            try:
                pts = [(e.dxf.vch1.x, e.dxf.vch1.y),
                       (e.dxf.vch2.x, e.dxf.vch2.y),
                       (e.dxf.vch3.x, e.dxf.vch3.y),
                       (e.dxf.vch4.x, e.dxf.vch4.y)]
                xs = [p[0] for p in pts if p[0] is not None]
                ys = [p[1] for p in pts if p[1] is not None]
                if xs and ys:
                    hatch_bboxes.append((min(xs), max(xs), min(ys), max(ys)))
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
            # Add Part's overall line bbox to hatch_bboxes (catches I-beam/stiffener overlaps that ray casting misses)
            def _part_lines(blk2, _xs, _ys):
                for _sub in blk2:
                    if _sub.dxftype() == 'LINE':
                        _xs.append(_sub.dxf.start.x); _xs.append(_sub.dxf.end.x)
                        _ys.append(_sub.dxf.start.y); _ys.append(_sub.dxf.end.y)
                    elif _sub.dxftype() == 'INSERT':
                        _sblk = doc.blocks.get(_sub.dxf.name)
                        if _sblk:
                            _part_lines(_sblk, _xs, _ys)
            _pxs, _pys = [], []
            _part_lines(blk, _pxs, _pys)
            if _pxs and _pys:
                _pw = max(_pxs) - min(_pxs); _ph = max(_pys) - min(_pys)
                _pb = (min(_pxs), max(_pxs), min(_pys), max(_pys))
                if _pb[0] < _pb[1] and _pb[2] < _pb[3]:
                    hatch_bboxes.append(_pb)

    # 2) WeldMark and Mark blocks in this view (original symbols, leader lines)
    for blk in doc.blocks:
        if (blk.name.startswith('WeldMark') or blk.name.startswith('Mark')):
            if re.search(rf' - {view_id}$', blk.name):
                _add_block_entities(blk)

    # 3) Modelspace entities (dimensions, title blocks, general annotations)
    _MARGIN = 100  # buffer zone around view bbox
    for e in doc.modelspace():
        if view_bbox:
            # Quick spatial filter: check if entity is near the view
            if e.dxftype() in ('LINE','LWPOLYLINE','POLYLINE'):
                try:
                    if e.dxftype() == 'LINE':
                        pts = [(e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)]
                    else:
                        pts = [(v[0], v[1]) for v in list(e.get_points())[:2]]
                    if (max(p[0] for p in pts) < view_bbox[0] - _MARGIN or
                        min(p[0] for p in pts) > view_bbox[2] + _MARGIN or
                        max(p[1] for p in pts) < view_bbox[1] - _MARGIN or
                        min(p[1] for p in pts) > view_bbox[3] + _MARGIN):
                        continue
                except Exception:
                    pass
            elif e.dxftype() == 'INSERT':
                ix, iy = e.dxf.insert.x, e.dxf.insert.y
                if (ix < view_bbox[0] - _MARGIN or ix > view_bbox[2] + _MARGIN or
                    iy < view_bbox[1] - _MARGIN or iy > view_bbox[3] + _MARGIN):
                    continue
            elif e.dxftype() in ('TEXT','MTEXT','ATTRIB','ATTDEF','MLEADER','DIMENSION'):
                try:
                    ix, iy = e.dxf.insert.x, e.dxf.insert.y
                    if (ix < view_bbox[0] - _MARGIN or ix > view_bbox[2] + _MARGIN or
                        iy < view_bbox[1] - _MARGIN or iy > view_bbox[3] + _MARGIN):
                        continue
                except Exception:
                    pass
            elif e.dxftype() in ('CIRCLE','ARC'):
                try:
                    cx, cy = e.dxf.center.x, e.dxf.center.y
                    if (cx < view_bbox[0] - _MARGIN or cx > view_bbox[2] + _MARGIN or
                        cy < view_bbox[1] - _MARGIN or cy > view_bbox[3] + _MARGIN):
                        continue
                except Exception:
                    pass
        if e.dxftype() == 'INSERT':
            blk = doc.blocks.get(e.dxf.name)
            if blk:
                # 计算 WM 块的整体包围盒，加入 hatch_bboxes
                if blk.name.startswith('WeldMark'):
                    _wmx, _wmy = [], []
                    for _sub in blk:
                        if _sub.dxftype() == 'LINE':
                            _wmx.extend([_sub.dxf.start.x, _sub.dxf.end.x])
                            _wmy.extend([_sub.dxf.start.y, _sub.dxf.end.y])
                        elif _sub.dxftype() in ('CIRCLE', 'ARC'):
                            _wmx.append(_sub.dxf.center.x)
                            _wmy.append(_sub.dxf.center.y)
                    if _wmx:
                        hatch_bboxes.append((min(_wmx), max(_wmx), min(_wmy), max(_wmy)))
                _add_block_entities(blk)
        else:
            add_entity(e)

    return lines, text_bboxes, circles, hatch_bboxes


def annotate(results, dxf_paths=None):
    """Main entry point. Annotates DXF files with weld labels.
    Returns list of sampled weld label entries for Excel output."""
    os.makedirs(ANNOTATED_DIR, exist_ok=True)

    by_comp = defaultdict(list)
    for r in results:
        by_comp[r['component']].append(r)

    if dxf_paths is None:
        import glob
        dxf_paths = sorted([f for f in glob.glob(os.path.join(FOLDER, "*.dxf")) if '(2)' not in f])

    all_sampled_labels = []

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
            sampled_labels = _annotate_one(doc, comp_welds)
            all_sampled_labels.extend(sampled_labels)
            out_path = os.path.join(ANNOTATED_DIR, os.path.basename(dxf_path))
            for _retry in range(3):
                try:
                    doc.saveas(out_path)
                    break
                except OSError:
                    if _retry < 2:
                        import time; time.sleep(0.5)
                    else:
                        raise

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

    return all_sampled_labels


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


def _detect_drawing_frames(doc):
    """
    扫描文档中所有 LINE 实体，检测图纸的外框和内框。
    返回 (outer_bbox, inner_bbox) 其中 bbox = [x0, y0, x1, y1]。
    如未检测到则返回 (None, None)。

    原理：框线是图纸中最长的水平线和垂直线，构成外矩形和内矩形。
    找所有最长水平线的 y 范围（上下边界），和最长垂直线的 x 范围（左右边界）。
    """
    from collections import defaultdict
    import math

    min_len = 100
    h_segs = defaultdict(list)  # y -> [(x_start, x_end)]
    v_segs = defaultdict(list)  # x -> [(y_start, y_end)]

    def _scan(e):
        if e.dxftype() != 'LINE':
            return
        s, ep = e.dxf.start, e.dxf.end
        dx = abs(s.x - ep.x)
        dy = abs(s.y - ep.y)
        length = math.hypot(dx, dy)
        if length < min_len:
            return
        if dx > dy and dx > length * 0.9:
            y_rounded = round(s.y, 1)
            h_segs[y_rounded].append((s.y, min(s.x, ep.x), max(s.x, ep.x)))
        elif dy > dx and dy > length * 0.9:
            x_rounded = round(s.x, 1)
            v_segs[x_rounded].append((s.x, min(s.y, ep.y), max(s.y, ep.y)))

    for blk in doc.blocks:
        for e in blk:
            _scan(e)
    for e in doc.modelspace():
        _scan(e)

    if len(h_segs) < 4 or len(v_segs) < 4:
        return None, None

    # 合并聚类：每组取最大覆盖范围
    h_final = {}
    for y_key, segs in h_segs.items():
        x0 = min(s[1] for s in segs)
        x1 = max(s[2] for s in segs)
        h_final[y_key] = (x0, x1, x1 - x0)

    v_final = {}
    for x_key, segs in v_segs.items():
        y0 = min(s[1] for s in segs)
        y1 = max(s[2] for s in segs)
        v_final[x_key] = (y0, y1, y1 - y0)

    # 按范围降序
    h_sorted = sorted(h_final.items(), key=lambda kv: -kv[1][2])  # [(y_key, (x0,x1,w))]
    v_sorted = sorted(v_final.items(), key=lambda kv: -kv[1][2])

    h_max_w = h_sorted[0][1][2]  # 最大水平线宽度
    v_max_h = v_sorted[0][1][2]  # 最大垂直线高度

    # 找外框：所有宽度/高度 ≈ h_max_w/v_max_h 的线
    outer_h = [(k, v) for k, v in h_sorted if abs(v[2] - h_max_w) < 5]
    outer_v = [(k, v) for k, v in v_sorted if abs(v[2] - v_max_h) < 5]

    if len(outer_h) < 2 or len(outer_v) < 2:
        return None, None

    # 外框：取最左/最右/最下/最上
    ox0 = min(k for k, v in outer_v)
    ox1 = max(k for k, v in outer_v)
    oy0 = min(k for k, v in outer_h)
    oy1 = max(k for k, v in outer_h)
    outer = [ox0, oy0, ox1, oy1]

    # 内框：宽度 ≈ 85-95% 外框且在外框内部
    inner = None
    inner_h = [kv for kv in h_sorted if kv[1][2] > h_max_w * 0.8 and kv[1][2] < h_max_w * 0.99
               and kv[0] > oy0 + 5 and kv[0] < oy1 - 5]
    inner_v = [kv for kv in v_sorted if kv[1][2] > v_max_h * 0.8 and kv[1][2] < v_max_h * 0.99
               and kv[0] > ox0 + 5 and kv[0] < ox1 - 5]

    if len(inner_h) >= 2 and len(inner_v) >= 2:
        ix0 = min(k for k, v in inner_v)
        ix1 = max(k for k, v in inner_v)
        iy0 = min(k for k, v in inner_h)
        iy1 = max(k for k, v in inner_h)
        inner = [ix0, iy0, ix1, iy1]

    return outer, inner


def _annotate_one(doc, welds):
    """Annotate a single DXF with weld labels. Returns list of sampled weld entries."""
    msp = doc.modelspace()
    _clean_original_labels(doc)
    _ensure_layer(doc)
    _ensure_style(doc)
    sampled_labels = []

    # Compute view bounding boxes from Part blocks for center calculation
    view_bboxes, part_centroids = _compute_view_bboxes(doc)

    # 全局图纸边界 = 所有视图 Part 包围盒的并集 + 40 单位 margin
    # 这个 margin 让视图上半部分的焊缝标注能向更高处寻找位置，
    # 同时超出 margin 仍会被严厉惩罚（_score_placement 中 _extra_g * 2000）
    if view_bboxes:
        _all_xs = [bb[i] for bb in view_bboxes.values() for i in (0, 2)]
        _all_ys = [bb[i] for bb in view_bboxes.values() for i in (1, 3)]
        _DRAW_MARGIN = 60
        draw_bbox = [min(_all_xs) - _DRAW_MARGIN, min(_all_ys) - _DRAW_MARGIN,
                     max(_all_xs) + _DRAW_MARGIN, max(_all_ys) + _DRAW_MARGIN]
    else:
        draw_bbox = _compute_drawing_bbox(doc)

    # 检测图纸内框，将标注范围缩放到内框内
    _outer_frame, _inner_frame = _detect_drawing_frames(doc)
    if _inner_frame:
        ix0, iy0, ix1, iy1 = _inner_frame
        if draw_bbox is None:
            draw_bbox = [ix0, iy0, ix1, iy1]
        else:
            draw_bbox = [max(draw_bbox[0], ix0), max(draw_bbox[1], iy0),
                         min(draw_bbox[2], ix1), min(draw_bbox[3], iy1)]

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

    # 计算每个视图的真实边界（仅 Part 块，不含 Mark/WeldMark 标注元素）
    _other_view_bboxes = []
    for _vid in view_bboxes.keys():
        _v_xs, _v_ys = [], []
        for _blk in doc.blocks:
            _bn = _blk.name
            if not _bn.startswith('Part'):
                continue
            if _bn.endswith(f' - {_vid}') or f' - {_vid}' in _bn:
                for _e in _blk:
                    if _e.dxftype() == 'LINE':
                        _v_xs.extend([_e.dxf.start.x, _e.dxf.end.x])
                        _v_ys.extend([_e.dxf.start.y, _e.dxf.end.y])
        if _v_xs:
            _other_view_bboxes.append((min(_v_xs), min(_v_ys), max(_v_xs), max(_v_ys)))

    # 检测表格区域（BOM 表格块），作为 hatch_bbox 加入阻挡
    _table_hatch = []
    for _e in doc.modelspace():
        if _e.dxftype() == 'INSERT' and _e.dxf.name.startswith('Unknown-'):
            _blk = doc.blocks.get(_e.dxf.name)
            if _blk:
                _tx, _ty = [], []
                for _sub in _blk:
                    if _sub.dxftype() == 'LINE':
                        _tx.extend([_sub.dxf.start.x, _sub.dxf.end.x])
                        _ty.extend([_sub.dxf.start.y, _sub.dxf.end.y])
                if _tx:
                    _table_hatch.append((min(_tx), max(_tx), min(_ty), max(_ty)))

    # 处理每个视图
    for view_id in sorted(welds_by_view.keys(), key=lambda v: int(v) if v.isdigit() else 0):
        vw = welds_by_view[view_id]
        bbox = view_bboxes.get(view_id)
        centroids = part_centroids.get(view_id, [])
        obs_result = _collect_all_obstacles(doc, view_id, view_bbox=bbox)
        part_lines = obs_result[:3]
        hatch_bboxes = obs_result[3] if len(obs_result) > 3 else []
        # 加入表格阻挡
        if _table_hatch:
            hatch_bboxes = list(hatch_bboxes) + _table_hatch
        _annotate_view(msp, vw, view_id, bbox, centroids, f_counter, w_counter, part_lines, draw_bbox, hatch_bboxes=hatch_bboxes if hatch_bboxes else None, other_view_bboxes=_other_view_bboxes, sampled_labels=sampled_labels)

    # Handle welds without view_id
    if welds_no_view:
        _annotate_welds_no_view(msp, welds_no_view, welds, f_counter, w_counter)

    # Zoom modelspace view to extents including all labels
    _set_model_view_to_extents(doc)

    print(f"    F: {f_counter[0]}  W: {w_counter[0]}")
    return sampled_labels


def _clean_original_labels(doc):
    """Remove part/dimension label INSERT references to reduce annotation overlap.
    Removes Mark-, MarkSet-, StraightDimension-, AngleDimension- block inserts.
    Preserves SectionMark, WeldMark, Unknown (BOM/title block), Part blocks."""
    msp = doc.modelspace()
    _remove_prefixes = ('Mark-', 'MarkSet-', 'StraightDimension-', 'AngleDimension-')
    _removed = 0
    for e in list(msp):
        if e.dxftype() == 'INSERT':
            blk_name = e.dxf.name
            if any(blk_name.startswith(p) for p in _remove_prefixes):
                msp.delete_entity(e)
                _removed += 1
    if _removed:
        print(f"    [clean] removed {_removed} part/dimension labels")
    return _removed


def _ensure_layer(doc):
    """Ensure WELD_LABELS layer exists with red color."""
    if LAYER_NAME not in doc.layers:
        layer = doc.layers.new(name=LAYER_NAME)
    else:
        layer = doc.layers.get(LAYER_NAME)
    layer.color = LABEL_COLOR  # blue


def _ensure_style(doc, name='Arial Narrow', font='ARIALN.TTF'):
    """Ensure a text style exists in the document (match WM symbol style)."""
    if name not in doc.styles:
        style = doc.styles.new(name=name, dxfattribs={'font': font})


def _compute_view_bboxes(doc):
    """Compute bounding box and Part block centroids of each view.
    扩展视图边界：含 Part/WeldMark/Mark/SectionMark 等全部块类型。"""
    view_bboxes = {}
    view_part_centroids = defaultdict(list)  # view_id -> [(cx, cy), ...]
    _BLOCK_PREFIXES = ('Part', 'WeldMark', 'Mark', 'SectionMark')
    for blk in doc.blocks:
        blk_name = blk.name
        m = re.search(r' - (\d+)$', blk_name)
        if not m:
            continue
        view_id = m.group(1)
        if not any(blk_name.startswith(p) for p in _BLOCK_PREFIXES):
            continue
        xs, ys = [], []
        for e in blk:
            if e.dxftype() == 'LINE':
                xs.extend([e.dxf.start.x, e.dxf.end.x])
                ys.extend([e.dxf.start.y, e.dxf.end.y])
            elif e.dxftype() in ('TEXT','MTEXT','ATTRIB','ATTDEF'):
                try:
                    xs.append(e.dxf.insert.x)
                    ys.append(e.dxf.insert.y)
                except Exception:
                    pass
            elif e.dxftype() in ('CIRCLE','ARC'):
                try:
                    xs.append(e.dxf.center.x)
                    ys.append(e.dxf.center.y)
                except Exception:
                    pass
        if xs:
            if view_id not in view_bboxes:
                view_bboxes[view_id] = [min(xs), min(ys), max(xs), max(ys)]
            else:
                bb = view_bboxes[view_id]
                bb[0] = min(bb[0], min(xs))
                bb[1] = min(bb[1], min(ys))
                bb[2] = max(bb[2], max(xs))
                bb[3] = max(bb[3], max(ys))
            if blk_name.startswith('Part'):
                view_part_centroids[view_id].append((sum(xs)/len(xs), sum(ys)/len(ys)))
    return view_bboxes, dict(view_part_centroids)


def _annotate_view(msp, welds, view_id, bbox, part_centroids, f_counter, w_counter, obstacles, draw_bbox=None, hatch_bboxes=None, other_view_bboxes=None, sampled_labels=None):
    """Annotate all welds in a single view.
    相同位置的 Above+Below 焊缝对共用一根引线，标号并排在横线末端。CJP(W*) 和 FW(F*) 不混合。"""
    if sampled_labels is None:
        sampled_labels = []
    lines, text_bboxes, circles = obstacles
    # 扫描已有 WELD_LABELS MTEXT（跨视图重叠保护）
    for e in msp:
        if e.dxftype() == 'MTEXT' and e.dxf.layer == LAYER_NAME:
            ins = e.dxf.insert
            w = LABEL_HEIGHT * (6.5 if ',' in e.dxf.text else 3.2)
            text_bboxes.append((ins.x - w, ins.x, ins.y, ins.y + LABEL_HEIGHT))
    pos_welds = [(w, w['dxf_pos']) for w in welds if w.get('dxf_pos')]
    no_pos_welds = [w for w in welds if not w.get('dxf_pos')]
    if not pos_welds and not no_pos_welds:
        return

    # 焊缝点禁区：使标签避免覆盖焊缝原始符号区域
    _weld_exclusion_radius = 5.0
    for w, wp in pos_welds:
        circles.append((wp[0], wp[1], _weld_exclusion_radius))

    if bbox:
        vx0, vy0, vx1, vy1 = bbox[0], bbox[1], bbox[2], bbox[3]
        if pos_welds:
            xs = sorted([p[0] for _, p in pos_welds])
            ys = sorted([p[1] for _, p in pos_welds])
            n = len(xs)
            cx = xs[n//2] if n % 2 else (xs[n//2-1] + xs[n//2]) / 2
            cy = ys[n//2] if n % 2 else (ys[n//2-1] + ys[n//2]) / 2
        else:
            cx = (vx0 + vx1) / 2
            cy = (vy0 + vy1) / 2
    elif pos_welds:
        xs = [p[0] for _, p in pos_welds]
        ys = [p[1] for _, p in pos_welds]
        cx, cy = sum(xs)/len(xs), sum(ys)/len(ys)
        m = 40
        vx0, vy0, vx1, vy1 = min(xs)-m, min(ys)-m, max(xs)+m, max(ys)+m
    else:
        cx = cy = vx0 = vy0 = vx1 = vy1 = 0

    # 扇形分区排序：跨扇区交替放置，避免跨半球引线交叉
    _vcx = cx
    _vcy = cy
    _N_SECTORS = 12
    _sectors = [[] for _ in range(_N_SECTORS)]
    for i, (w, pi) in enumerate(pos_welds):
        _ang = math.degrees(math.atan2(pi[1] - _vcy, pi[0] - _vcx))
        _sid = int((_ang + 15 + 360) % 360 / 30)
        _sectors[_sid].append((i, w, pi))
    for _s in _sectors:
        _s.sort(key=lambda x: math.hypot(x[2][0]-_vcx, x[2][1]-_vcy))
    _new_order = []
    _max_len = max(len(_s) for _s in _sectors)
    for _round in range(_max_len):
        for _sid in range(_N_SECTORS):
            if _round < len(_sectors[_sid]):
                _new_order.append(_sectors[_sid][_round])
    pos_welds = [(w, p) for _, w, p in _new_order]

    # ---- 分组：同位配对（CJP(Above)+FW(Below)跨类型可配对，同类型Above+Below配对） ----
    POS_TOL = 1.0
    from collections import defaultdict
    _pos_map = defaultdict(list)
    for wp in pos_welds:
        _key = (round(wp[1][0], 0), round(wp[1][1], 0))
        _pos_map[_key].append(wp)
    groups = []
    for _key, items in _pos_map.items():
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
        _redistribute_groups(groups, part_centroids, (vx0, vy0, vx1, vy1))

    placed_bboxes = []          # 引线+文字整体包围盒
    placed_text_bboxes = []     # 纯文字包围盒（用于文字重叠检测）
    _placements = []
    _quadrant_used_angles = {1: [], 2: [], 3: [], 4: []}

    def _next_hint_for_quadrant(q, used_angles):
        a0, a1 = QUAD_ANGLE_RANGES[q]
        n = len(used_angles)
        if n == 0:
            return a1
        return a1 - (a1 - a0) * n / (n + 1)

    for gtype, items in groups:
        _hint = None
        if gtype == 'pair':
            ww_a, wp_a = items[0]
            ww_b, wp_b = items[1]
            labels = [_next_label(ww_a, f_counter, w_counter),
                      _next_label(ww_b, f_counter, w_counter)]
            _home_q = _weld_home_quadrant(wp_a[0], wp_a[1], _vcx, _vcy)
            if _quadrant_used_angles.get(_home_q):
                _hint = _next_hint_for_quadrant(_home_q, _quadrant_used_angles[_home_q])
            dname, diag_len, angle = _search_placement(
                wp_a, lines, text_bboxes, circles, placed_bboxes,
                placed_text_bboxes, vx0, vy0, vx1, vy1, draw_bbox, is_pair=True,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=_home_q, quad_cx=cx, quad_cy=cy)
            bx0, bx1, by0, by1 = _paired_bbox(wp_a, dname, diag_len, angle)
            bbox = (min(bx0, wp_a[0])-1, max(bx1, wp_a[0])+1,
                    min(by0, wp_a[1])-1, max(by1, wp_a[1])+1)
            _placements.append((gtype, items, labels, wp_a, dname, diag_len, angle, bbox))
            placed_bboxes.append(bbox)
            placed_text_bboxes.append(_text_bbox(wp_a, dname, diag_len, angle, is_pair=True))
        else:
            ww, wp = items[0]
            label = _next_label(ww, f_counter, w_counter)
            _home_q = _weld_home_quadrant(wp[0], wp[1], _vcx, _vcy)
            if _quadrant_used_angles.get(_home_q):
                _hint = _next_hint_for_quadrant(_home_q, _quadrant_used_angles[_home_q])
            dname, diag_len, angle = _search_placement(
                wp, lines, text_bboxes, circles, placed_bboxes,
                placed_text_bboxes, vx0, vy0, vx1, vy1, draw_bbox,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=_home_q, quad_cx=cx, quad_cy=cy)
            bx0, bx1, by0, by1 = _single_bbox(wp, dname, diag_len, angle)
            bbox = (min(bx0, wp[0])-1, max(bx1, wp[0])+1,
                    min(by0, wp[1])-1, max(by1, wp[1])+1)
            _placements.append((gtype, items, [label], wp, dname, diag_len, angle, bbox))
            placed_bboxes.append(bbox)
            placed_text_bboxes.append(_text_bbox(wp, dname, diag_len, angle, is_pair=False))

        _quadrant_used_angles.setdefault(_home_q, []).append(angle)

    # ---- 全局后处理：冲突解决（最多8次迭代） ----
    _resolve_label_conflicts(msp, lines, text_bboxes, circles,
                             vx0, vy0, vx1, vy1, draw_bbox, _placements, placed_text_bboxes, 8,
                             hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes)

    # ---- 绘制所有标注 ----
    for pd in _placements:
        gtype, items, labels, pos, dname, diag_len, angle = pd[:7]
        _smp = items[0][0].get('_sampled', False)
        if gtype == 'pair':
            _draw_paired_weld_label(msp, labels, pos, dname, diag_len, angle, sampled=_smp)
        else:
            _draw_weld_label(msp, labels[0], pos, dname, diag_len, angle, sampled=_smp)
        if _smp:
            if gtype == 'pair':
                for i in range(2):
                    ww = items[i][0]
                    lb = labels[i]
                    sampled_labels.append({
                        'component': ww.get('comp_full', ww['component']), 'label': lb,
                        'weld_type': ww.get('weld_type', ''), 'part1': ww['part1'], 'part2': ww['part2'],
                        'position': ww.get('position', ''), 'length': ww.get('length_mm', 0),
                        'hf': ww.get('hf', ''), 'annotation': ww.get('annotation', ''),
                    })
            else:
                ww = items[0][0]; lb = labels[0]
                sampled_labels.append({
                    'component': ww['component'], 'label': lb,
                    'weld_type': ww.get('weld_type', ''), 'part1': ww['part1'], 'part2': ww['part2'],
                    'position': ww.get('position', ''), 'length': ww.get('length_mm', 0),
                    'hf': ww.get('hf', ''), 'annotation': ww.get('annotation', ''),
                })

    for w in no_pos_welds:
        label = _next_label(w, f_counter, w_counter)
        _draw_fallback_label(msp, w, label, bbox)
        if w.get('_sampled'):
            sampled_labels.append({
                'component': w.get('comp_full', w['component']), 'label': label,
                'weld_type': w.get('weld_type', ''), 'part1': w['part1'], 'part2': w['part2'],
                'position': w.get('position', ''), 'length': w.get('length_mm', 0),
                'hf': w.get('hf', ''), 'annotation': w.get('annotation', ''),
            })
    return sampled_labels


def _weld_home_quadrant(wx, wy, vcx, vcy):
    """焊点相对视图中心归属象限 Q1–Q4。"""
    if wx >= vcx and wy >= vcy:
        return 1
    if wx < vcx and wy >= vcy:
        return 2
    if wx < vcx and wy < vcy:
        return 3
    return 4


def _pos_home_quadrant(px, py, vcx, vcy):
    return _weld_home_quadrant(px, py, vcx, vcy)


def _allowed_quadrants(home_q, allow_adjacent=False):
    if allow_adjacent:
        _half = {1: {1, 4}, 2: {2, 3}, 3: {2, 3}, 4: {1, 4}}
        _adj = {1: {1, 2, 4}, 2: {2, 1, 3}, 3: {3, 2, 4}, 4: {4, 1, 3}}
        return _adj.get(home_q, {home_q}) & _half.get(home_q, {home_q})
    return {home_q}


def _angle_in_quadrant(angle_deg, quad, tol=QUADRANT_ANGLE_TOL):
    a0, a1 = QUAD_ANGLE_RANGES[quad]
    a = angle_deg % 360
    return (a0 - tol) <= a <= (a1 + tol)


def _quadrant_angle_offsets(home_q, ideal_ang, step=3):
    """归属象限内的角度 offset 列表（相对 ideal_ang）。"""
    a0, a1 = QUAD_ANGLE_RANGES[home_q]
    return [a - ideal_ang for a in range(int(a0), int(a1) + 1, step)]


def _label_text_width(labels, is_pair=False):
    if is_pair:
        if isinstance(labels, (list, tuple)):
            text = f"{labels[0]},{labels[1]}"
        else:
            text = str(labels)
    else:
        text = labels if isinstance(labels, str) else str(labels)
    return max(LABEL_HEIGHT, len(text) * LABEL_HEIGHT * 0.6)


def _single_bbox(weld_pos, dname, diag_len, angle_deg):
    """计算单标注文字 + 引线完整包围盒。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    ex = wx + diag_len * cos_a
    ey = wy + diag_len * math.sin(rad)
    lx, ly = _label_corner(weld_pos, dname, diag_len, angle_deg, is_pair=False)
    lw = LABEL_HEIGHT * 3.2
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
    pair_w = LABEL_HEIGHT * 6.5
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


def _text_bbox(weld_pos, dname, diag_len, angle_deg, is_pair=False):
    """计算纯文字包围盒 (x0,x1,y0,y1)，用于重叠检测。"""
    _rad = math.radians(angle_deg)
    _tbx, _tby = _label_corner(weld_pos, dname, diag_len, angle_deg, is_pair=is_pair)
    _width = LABEL_HEIGHT * (6.5 if is_pair else 3.2)
    if math.cos(_rad) >= -0.05:    # BR: text extends LEFT from hx
        _tbx0 = _tbx - _width
        _tbx1 = _tbx
    else:                            # BL: text extends RIGHT from hx
        _tbx0 = _tbx
        _tbx1 = _tbx + _width
    return (_tbx0, _tbx1, _tby, _tby + LABEL_HEIGHT)


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


def _draw_weld_label(msp, label, weld_pos, dname, diag_len, angle_deg, sampled=False):
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
        ap = MT_BOTTOM_RIGHT
        lx = hx
    else:
        ap = MT_BOTTOM_LEFT
        lx = hx
    msp.add_mtext(label, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': LABEL_HEIGHT,
        'insert': (lx, hy),
        'attachment_point': ap,
        'style': 'Arial Narrow',
        'lineweight': 30,
    })

    if sampled:
        _tw = len(label) * LABEL_HEIGHT * 0.6
        if h_land >= 0:
            _cx = lx - _tw / 2
        else:
            _cx = lx + _tw / 2
        _cy = hy + LABEL_HEIGHT / 2
        _rx = _tw / 2 + 1.3
        _ry = LABEL_HEIGHT / 2 + 1.3
        msp.add_ellipse(center=(_cx, _cy), major_axis=(_rx, 0),
                        ratio=_ry / max(_rx, 0.01),
                        dxfattribs={'layer': LAYER_NAME, 'color': 1})


def _draw_paired_weld_label(msp, labels, weld_pos, dname, diag_len, angle_deg, sampled=False):
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
        'style': 'Arial Narrow',
        'lineweight': 30,
    })

    if sampled:
        _tw = len(paired_text) * LABEL_HEIGHT * 0.6
        if h_land >= 0:
            _cx = lx - _tw / 2
        else:
            _cx = lx + _tw / 2
        _cy = hy + LABEL_HEIGHT / 2
        _rx = _tw / 2 + 1.3
        _ry = LABEL_HEIGHT / 2 + 1.3
        msp.add_ellipse(center=(_cx, _cy), major_axis=(_rx, 0),
                        ratio=_ry / max(_rx, 0.01),
                        dxfattribs={'layer': LAYER_NAME, 'color': 1})


def _search_placement(weld_pos, lines, text_bboxes, circles, placed_bboxes,
                      placed_text_bboxes, vx0, vy0, vx1, vy1, draw_bbox=None, is_pair=False,
                      hatch_bboxes=None, other_view_bboxes=None,
                      home_q=None, quad_cx=None, quad_cy=None):
    """在360°连续角度中搜索最佳标注位置。"""
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

    # 预计算理想引线方向（象限用）
    _vcx = quad_cx if quad_cx is not None else (vx0 + vx1) / 2
    _vcy = quad_cy if quad_cy is not None else (vy0 + vy1) / 2
    _ideal_ang = math.degrees(math.atan2(wy - _vcy, wx - _vcx)) % 360
    if home_q is None:
        home_q = _weld_home_quadrant(wx, wy, _vcx, _vcy)
    _allowed_quads = _allowed_quadrants(home_q, allow_adjacent=False)

    def _has_conflict(angle_deg, dist, _db):
        """True if position has any critical conflict (text overlap, line cross, boundary, quadrant)."""
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        # 引线必须有倾角：拒水平和垂直
        if abs(sin_a) < math.sin(math.radians(ANGLE_MIN)):
            return True
        if abs(cos_a) < math.cos(math.radians(ANGLE_MAX)):
            return True
        # 象限检查：角度必须在允许象限内
        if not any(_angle_in_quadrant(angle_deg, q) for q in _allowed_quads):
            return True
        ex = wx + dist * cos_a
        ey = wy + dist * sin_a
        h_len = PAIR_HORIZ_LAND if is_pair else HORIZ_LAND
        h_land = h_len if cos_a >= -0.05 else -h_len
        hx = ex + h_land; hy = ey
        lw = LABEL_HEIGHT * (6.5 if is_pair else 3.2)
        bx0 = hx - lw if h_land >= 0 else hx
        bx1 = hx if h_land >= 0 else hx + lw
        by0, by1 = hy, hy + LABEL_HEIGHT
        nbb = (min(wx, ex, bx0), max(wx, ex, bx1),
               min(wy, ey, by0), max(wy, ey, by1))
        if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, _db):
            return True
        # 跨视图边界检查：标注不能与其他视图区域重叠
        if other_view_bboxes:
            for _ovb in other_view_bboxes:
                if _ovb == (vx0, vy0, vx1, vy1):
                    continue
                _M = 40
                if nbb[1] > _ovb[0] - _M and nbb[0] < _ovb[2] + _M and nbb[3] > _ovb[1] - _M and nbb[2] < _ovb[3] + _M:
                    return True
        for otb in placed_text_bboxes:
            if not (bx1 < otb[0] - OVERLAP_MARGIN or bx0 > otb[1] + OVERLAP_MARGIN or
                    by1 < otb[2] - OVERLAP_MARGIN or by0 > otb[3] + OVERLAP_MARGIN):
                return True
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if not (bx1 < tx0 - OVERLAP_MARGIN or bx0 > tx1 + OVERLAP_MARGIN or
                    by1 < ty0 - OVERLAP_MARGIN or by0 > ty1 + OVERLAP_MARGIN):
                return True
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
                return True
        for otb in placed_text_bboxes:
            if _seg_cross_rect((wx, wy), (ex, ey), otb[0], otb[1], otb[2], otb[3]):
                return True
        for otb in placed_text_bboxes:
            if _seg_cross_rect((ex, ey), (hx, hy), otb[0], otb[1], otb[2], otb[3]):
                return True
        for (pbx0, pbx1, pby0, pby1) in placed_bboxes:
            if _seg_cross_rect((wx, wy), (ex, ey), pbx0, pbx1, pby0, pby1):
                return True
            if _seg_cross_rect((ex, ey), (hx, hy), pbx0, pbx1, pby0, pby1):
                return True
        _min_x = min(wx, ex, hx, bx0); _max_x = max(wx, ex, hx, bx1)
        _min_y = min(wy, ey, hy, by0); _max_y = max(wy, ey, hy, by1)
        _mrg = 15
        _gx0 = int((_min_x - _mrg) / 50); _gx1 = int((_max_x + _mrg) / 50)
        _gy0 = int((_min_y - _mrg) / 50); _gy1 = int((_max_y + _mrg) / 50)
        _near = []
        for _gx in range(_gx0, _gx1 + 1):
            for _gy in range(_gy0, _gy1 + 1):
                _near.extend(_line_grid.get((_gx, _gy), []))
        for (sx, sy), (ex2, ey2) in _near:
            if not (max(sx, ex2) < _min_x - _mrg or min(sx, ex2) > _max_x + _mrg or
                    max(sy, ey2) < _min_y - _mrg or min(sy, ey2) > _max_y + _mrg):
                if _segments_cross_((ex, ey), (hx, hy), (sx, sy), (ex2, ey2)):
                    return True
        # 文字与几何线过近
        _line_mrg = 3.0
        _txt_pts = [(bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1),
                    ((bx0+bx1)/2, by0), ((bx0+bx1)/2, by1),
                    (bx0, (by0+by1)/2), (bx1, (by0+by1)/2)]
        for (sx, sy), (ex2, ey2) in _near:
            for (cx, cy) in _txt_pts:
                if _dist_pt_to_seg((cx, cy), (sx, sy), (ex2, ey2))[0] < _line_mrg:
                    return True
        _cx_txt = (bx0 + bx1) / 2
        _cy_txt = (by0 + by1) / 2
        for (sx, sy), (ex2, ey2) in _near:
            if _dist_pt_to_seg((_cx_txt, _cy_txt), (sx, sy), (ex2, ey2))[0] < 1.5:
                return True
        # 射线法：检查文字中心是否在几何线围成的封闭形状内
        # 使用全部 lines（非 _near），配合 y/x 范围过滤保证性能
        _odd = 0
        for _rx, _ry in [(_cx_txt + 99999, _cy_txt), (_cx_txt - 99999, _cy_txt)]:
            _cnt = sum(1 for (sx, sy), (ex2, ey2) in lines
                       if (sy <= _cy_txt <= ey2 or ey2 <= _cy_txt <= sy)
                       and _segments_cross_((_cx_txt, _cy_txt), (_rx, _ry), (sx, sy), (ex2, ey2)))
            if _cnt % 2 == 1:
                _odd += 1
        for _rx, _ry in [(_cx_txt, _cy_txt + 99999), (_cx_txt, _cy_txt - 99999)]:
            _cnt = sum(1 for (sx, sy), (ex2, ey2) in lines
                       if (sx <= _cx_txt <= ex2 or ex2 <= _cx_txt <= sx)
                       and _segments_cross_((_cx_txt, _cy_txt), (_rx, _ry), (sx, sy), (ex2, ey2)))
            if _cnt % 2 == 1:
                _odd += 1
        if _odd >= 3:
            return True
        _txt_edges = [((bx0, by0), (bx1, by0)), ((bx1, by0), (bx1, by1)),
                      ((bx1, by1), (bx0, by1)), ((bx0, by1), (bx0, by0))]
        for (sx, sy), (ex2, ey2) in _near:
            for (_s, _e) in _txt_edges:
                if _segments_cross_(_s, _e, (sx, sy), (ex2, ey2)):
                    return True
        for (ccx, ccy, cr) in circles:
            if not (bx1 < ccx - cr or bx0 > ccx + cr or by1 < ccy - cr or by0 > ccy + cr):
                return True
        if hatch_bboxes:
            for (hx0, hx1, hy0, hy1) in hatch_bboxes:
                if not (bx1 < hx0 or bx0 > hx1 or by1 < hy0 or by0 > hy1):
                    return True
        return False

    def _fine_tune(dist, angle, _db):
        """Local angle fix, then reverse-sweep from shortest distance to find the shortest leader."""
        _ang = angle
        for da in range(-60, 61, 3):
            na2 = angle + da
            r2 = math.radians(na2 % 360)
            if abs(math.sin(r2)) < math.sin(math.radians(20)): continue
            if abs(math.cos(r2)) < math.cos(math.radians(70)): continue
            if not _has_conflict(na2, dist, _db):
                _ang = na2; break
        # 反向扫描：从最短距离(8)向上，试全部角度找最短引线
        _full_ao = [0, 10, -10, 20, -20, 30, -30, 40, -40, 50, -50,
                    60, -60, 70, -70, 80, -80, 90, -90,
                    100, -100, 110, -110, 120, -120, 130, -130,
                    140, -140, 150, -150, 160, -160, 170, -170, 180]
        for nd in range(8, dist, 2):
            for offset in _full_ao:
                na = (_ang + offset) % 360
                r3 = math.radians(na)
                if abs(math.sin(r3)) < math.sin(math.radians(20)): continue
                if abs(math.cos(r3)) < math.cos(math.radians(70)): continue
                if not _has_conflict(na, nd, _db):
                    for da in range(-30, 31, 3):
                        na3 = na + da
                        r3b = math.radians(na3 % 360)
                        if abs(math.sin(r3b)) < math.sin(math.radians(20)): continue
                        if abs(math.cos(r3b)) < math.cos(math.radians(70)): continue
                        if not _has_conflict(na3, nd, _db):
                            return na3, nd, 0
                    return na, nd, 0
        # 没更短距离，轻微延长
        for nd in range(dist + 2, min(dist + 14, 61), 2):
            for offset in _full_ao:
                na = (_ang + offset) % 360
                r4 = math.radians(na)
                if abs(math.sin(r4)) < math.sin(math.radians(20)): continue
                if abs(math.cos(r4)) < math.cos(math.radians(70)): continue
                if not _has_conflict(na, nd, _db):
                    for da in range(-30, 31, 3):
                        na4 = na + da
                        r4b = math.radians(na4 % 360)
                        if abs(math.sin(r4b)) < math.sin(math.radians(20)): continue
                        if abs(math.cos(r4b)) < math.cos(math.radians(70)): continue
                        if not _has_conflict(na4, nd, _db):
                            return na4, nd, 0
                    return na, nd, 0
        return _ang, dist, 0

    def _search_pass(_db, allow_adjacent=False):
        """三阶段搜索，归属象限内搜索最佳位置。"""
        nonlocal _allowed_quads
        _allowed_quads = _allowed_quadrants(home_q, allow_adjacent=allow_adjacent)
        distances = list(range(8, 56, 2))
        if is_pair:
            distances = [d + 4 for d in distances]
        _full_ao = [0, 5, -5, 10, -10, 15, -15, 20, -20, 25, -25, 30, -30,
                     35, -35, 40, -40, 45, -45, 50, -50, 55, -55, 60, -60,
                     65, -65, 70, -70, 75, -75, 80, -80, 85, -85, 90, -90,
                     95, -95, 100, -100, 105, -105, 110, -110, 115, -115,
                     120, -120, 125, -125, 130, -130, 135, -135, 140, -140,
                     145, -145, 150, -150, 155, -155, 160, -160, 165, -165,
                     170, -170, 175, -175, 180]

        def _try_place(dist, ao_list):
            for offset in ao_list:
                angle = (_ideal_ang + offset) % 360
                rad = math.radians(angle)
                if abs(math.sin(rad)) < math.sin(math.radians(ANGLE_MIN)): continue
                if abs(math.cos(rad)) < math.cos(math.radians(ANGLE_MAX)): continue
                if not _has_conflict(angle, dist, _db):
                    _fa, _fd, _fs = _fine_tune(dist, angle, _db)
                    return _fs, (_fa, _fd, 0)
            return None

        # Phase A: 短距离 8→24, 放宽角度 ±60°（空间大时优先用短引线）
        _wide_ao = [0,5,-5,10,-10,15,-15,20,-20,25,-25,30,-30,
                    35,-35,40,-40,45,-45,50,-50,55,-55,60,-60]
        for dist in distances:
            if dist > 24: break
            result = _try_place(dist, _wide_ao)
            if result: return result

        # Phase B: 中距离 26→38, 正常角度 ±45°（常规区域）
        _mid_ao = [0,8,-8,15,-15,22,-22,30,-30,38,-38,45,-45]
        for dist in distances:
            if dist < 26 or dist > 38: continue
            result = _try_place(dist, _mid_ao)
            if result: return result

        # Phase C: 长距离 40→54, 全角度兜底
        for dist in distances:
            if dist < 40: continue
            result = _try_place(dist, _full_ao)
            if result: return result

        _best_score = -999999999
        _best_result = (_ideal_ang, min(distances), 0)
        for dist in distances:
            for _off in range(0, 360, 30):
                angle = (_ideal_ang + _off) % 360
                rad = math.radians(angle)
                if abs(math.sin(rad)) < math.sin(math.radians(20)): continue
                if abs(math.cos(rad)) < math.cos(math.radians(70)): continue
                score = _score_placement(wx, wy, angle, dist, lines, text_bboxes,
                                         circles, placed_bboxes, placed_text_bboxes,
                                         vx0, vy0, vx1, vy1,
                                         _db, is_pair=is_pair, min_score=_best_score,
                                         line_grid=_line_grid,
                                         hatch_bboxes=hatch_bboxes,
                                         other_view_bboxes=other_view_bboxes)
                if score > _best_score:
                    _best_score = score
                    _best_result = (angle, dist, 0)
        _bd, _bdst, _ = _best_result
        _fa, _fd, _fs = _fine_tune(_bdst, _bd, _db)
        return _fs, (_fa, _fd, 0)

    score, result = _search_pass(draw_bbox, allow_adjacent=False)
    _fa, _fd, _ = result
    if _score_placement(wx, wy, _fa, _fd, lines, text_bboxes,
                        circles, placed_bboxes, placed_text_bboxes,
                        vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                        line_grid=_line_grid, hatch_bboxes=hatch_bboxes,
                        other_view_bboxes=other_view_bboxes,
                        home_q=home_q) < -10000:
        print(f"    [warn] quadrant-fallback at ({wx:.1f},{wy:.1f}) home_q={home_q}")
        score, result = _search_pass(draw_bbox, allow_adjacent=True)
    # 硬象限钳制：确保返回的角度一定在 home_q 象限范围内
    _fa, _fd = result[0], result[1]
    _a0, _a1 = QUAD_ANGLE_RANGES[home_q]
    if not (_a0 - QUADRANT_ANGLE_TOL <= _fa % 360 <= _a1 + QUADRANT_ANGLE_TOL):
        _fa = _a1 if (_fa % 360) < (_a0 + _a1) / 2 else _a0
    return 0, _fd, _fa


def _leader_crosses_leader(pos_a, dist_a, angle_a, h_land_a,
                           pos_b, dist_b, angle_b, h_land_b):
    """检测两条引线（斜线+水平线）是否交叉。返回 (crosses, cross_angle)。"""
    rad_a = math.radians(angle_a)
    ex_a = pos_a[0] + dist_a * math.cos(rad_a)
    ey_a = pos_a[1] + dist_a * math.sin(rad_a)
    hx_a = ex_a + h_land_a; hy_a = ey_a

    rad_b = math.radians(angle_b)
    ex_b = pos_b[0] + dist_b * math.cos(rad_b)
    ey_b = pos_b[1] + dist_b * math.sin(rad_b)
    hx_b = ex_b + h_land_b; hy_b = ey_b

    def _seg_intersect(p1, p2, p3, p4):
        d1 = (p4[1]-p3[1])*(p2[0]-p3[0]) - (p4[0]-p3[0])*(p2[1]-p3[1])
        d2 = (p4[1]-p3[1])*(p1[0]-p3[0]) - (p4[0]-p3[0])*(p1[1]-p3[1])
        if d1*d2 >= 0: return False
        d3 = (p2[1]-p1[1])*(p4[0]-p1[0]) - (p2[0]-p1[0])*(p4[1]-p1[1])
        d4 = (p2[1]-p1[1])*(p3[0]-p1[0]) - (p2[0]-p1[0])*(p3[1]-p1[1])
        return d3*d4 < 0

    lines_a = [(pos_a, (ex_a, ey_a)), ((ex_a, ey_a), (hx_a, hy_a))]
    lines_b = [(pos_b, (ex_b, ey_b)), ((ex_b, ey_b), (hx_b, hy_b))]
    for (p1, p2) in lines_a:
        for (p3, p4) in lines_b:
            if _seg_intersect(p1, p2, p3, p4):
                return True, abs(angle_a - angle_b) % 180
    return False, None


def _score_placement(wx, wy, angle_deg, dist, lines, text_bboxes, circles,
                     placed_bboxes, placed_text_bboxes, vx0, vy0, vx1, vy1,
                     draw_bbox=None, is_pair=False, min_score=None, line_grid=None,
                     hatch_bboxes=None, other_view_bboxes=None,
                     home_q=None, quad_cx=None, quad_cy=None):
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

    lw = LABEL_HEIGHT * 6.5 if is_pair else LABEL_HEIGHT * 3.2
    lh = LABEL_HEIGHT
    if h_land >= 0:
        bx0, bx1 = hx - lw, hx
    else:
        bx0, bx1 = hx, hx + lw
    by0, by1 = hy, hy + lh

    # 视图中点（用于象限判断）
    _vcx_s = quad_cx if quad_cx is not None else (vx0 + vx1) / 2.0
    _vcy_s = quad_cy if quad_cy is not None else (vy0 + vy1) / 2.0
    _cx_cand = (bx0 + bx1) / 2.0
    _cy_cand = (by0 + by1) / 2.0
    if home_q is not None:
        _allowed = _allowed_quadrants(home_q, allow_adjacent=False)
        if not any(_angle_in_quadrant(angle_deg, q) for q in _allowed):
            score -= 50000
        if _pos_home_quadrant(_cx_cand, _cy_cand, _vcx_s, _vcy_s) not in _allowed:
            score -= 50000
        elif _pos_home_quadrant(_cx_cand, _cy_cand, _vcx_s, _vcy_s) == home_q:
            score += 80

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

    # 跨视图重叠惩罚（完整标注包围盒 vs 其他视图边界）
    if other_view_bboxes:
        nbb = (min(wx, ex, bx0), max(wx, ex, bx1),
               min(wy, ey, by0), max(wy, ey, by1))
        for _ovb in other_view_bboxes:
            if _ovb == (vx0, vy0, vx1, vy1):
                continue
            if nbb[1] > _ovb[0] - 10 and nbb[0] < _ovb[2] + 10 and nbb[3] > _ovb[1] - 10 and nbb[2] < _ovb[3] + 10:
                score -= 50000

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
  
    # 斜引线穿过已放置标注：扣200（防止引线与文字交错）
    for (pbx0, pbx1, pby0, pby1) in placed_bboxes:
        if _seg_cross_rect((wx, wy), (ex, ey), pbx0, pbx1, pby0, pby1):
            score -= 200

    # 斜引线穿过已放置标注文字：扣2000（硬性惩罚）
    for k, otb in enumerate(placed_text_bboxes):
        if _seg_cross_rect((wx, wy), (ex, ey), otb[0], otb[1], otb[2], otb[3]):
            score -= 2000

    # 斜引线靠近文字框但不穿过：扣30
    _DIAG_PROX_MARGIN = 3.0
    for (tx0, tx1, ty0, ty1) in text_bboxes:
        _cx_t = (tx0 + tx1) / 2
        _cy_t = (ty0 + ty1) / 2
        d_diag, _ = _dist_pt_to_seg((_cx_t, _cy_t), (wx, wy), (ex, ey))
        if d_diag < _DIAG_PROX_MARGIN:
            if not _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
                score -= 30

    # 文字与已有文字框重叠：扣2000（不可接受）
    _OVERLAP_MARGIN = 4.0
    for (tx0, tx1, ty0, ty1) in text_bboxes:
        if bx1 > tx0 - _OVERLAP_MARGIN and bx0 < tx1 + _OVERLAP_MARGIN and by1 > ty0 - _OVERLAP_MARGIN and by0 < ty1 + _OVERLAP_MARGIN:
            score -= 2000

    # 文字与已放置标注文字重叠：扣2000（不可接受）
    for (pbx0, pbx1, pby0, pby1) in placed_text_bboxes:
        if bx1 > pbx0 - _OVERLAP_MARGIN and bx0 < pbx1 + _OVERLAP_MARGIN and by1 > pby0 - _OVERLAP_MARGIN and by0 < pby1 + _OVERLAP_MARGIN:
            score -= 2000
 
    # 文字与几何线过近：扣30（检测4个角点+4条边中点）
    _LINE_MARGIN = 4.0
    _txt_sample_pts = [(bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1),
                       ((bx0+bx1)/2, by0), ((bx0+bx1)/2, by1),
                       (bx0, (by0+by1)/2), (bx1, (by0+by1)/2)]
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
 
    # 文字边穿越几何线：扣500
    _txt_edges = [((bx0, by0), (bx1, by0)), ((bx1, by0), (bx1, by1)),
                  ((bx1, by1), (bx0, by1)), ((bx0, by1), (bx0, by0))]
    for (sx, sy), (ex2, ey2) in _near_lines:
        for (_s, _e) in _txt_edges:
            if _segments_cross_(_s, _e, (sx, sy), (ex2, ey2)):
                score -= 500
                break

    # 射线法（优化）：只检查可能跨过射线的线
    cx_txt = (bx0 + bx1) / 2
    cy_txt = (by0 + by1) / 2
    _odd = 0
    for _rx, _ry in [(cx_txt + 99999, cy_txt), (cx_txt - 99999, cy_txt)]:
        _cnt = sum(1 for (sx, sy), (ex2, ey2) in lines
                   if (sy <= cy_txt <= ey2 or ey2 <= cy_txt <= sy)
                   and _segments_cross_((cx_txt, cy_txt), (_rx, _ry), (sx, sy), (ex2, ey2)))
        if _cnt % 2 == 1:
            _odd += 1
    for _rx, _ry in [(cx_txt, cy_txt + 99999), (cx_txt, cy_txt - 99999)]:
        _cnt = sum(1 for (sx, sy), (ex2, ey2) in lines
                   if (sx <= cx_txt <= ex2 or ex2 <= cx_txt <= sx)
                   and _segments_cross_((cx_txt, cy_txt), (_rx, _ry), (sx, sy), (ex2, ey2)))
        if _cnt % 2 == 1:
            _odd += 1
    if _odd >= 3:
        score -= 2000

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

    # 文字与 HATCH/SOLID 填充区重叠：扣2000
    if hatch_bboxes:
        for (hx0, hx1, hy0, hy1) in hatch_bboxes:
            if bx1 > hx0 and bx0 < hx1 and by1 > hy0 and by0 < hy1:
                score -= 5000

    # 文字与已放置标注重叠：扣20000（硬性惩罚，防止标签叠在一起）
    _OV_MARGIN = 8.0
    for (pbx0, pbx1, pby0, pby1) in placed_text_bboxes:
        if bx1 > pbx0 - _OV_MARGIN and bx0 < pbx1 + _OV_MARGIN and by1 > pby0 - _OV_MARGIN and by0 < pby1 + _OV_MARGIN:
            score -= 20000

    # 动态距离惩罚：根据冲突严重程度调整
    # 无冲突→近处优先（强惩罚远距离），严重冲突→可以走远找干净位置（弱惩罚）
    _non_pen = -score  # 非距离惩罚总绝对值
    if _non_pen < 30:        # 无冲突/极轻微 → 强烈反对远距离
        if dist > 30: score -= (dist - 30) * 4
    elif _non_pen < 500:     # 轻微冲突 → 适中
        if dist > 50: score -= (dist - 50) * 3
    else:                    # 严重冲突（hatch/标注重叠）→ 允许走远找干净位置
        if dist > 70: score -= (dist - 70) * 2

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


def _redistribute_groups(groups, centroids, view_bbox=None):
    """Offset duplicate groups toward view center to avoid overlap."""
    if not centroids:
        return
    _vy_center = (view_bbox[1] + view_bbox[3]) / 2.0 if view_bbox else None
    uniq_c = list(set(centroids))
    pos_map = {}
    for gi, (gtype, items) in enumerate(groups):
        pos = items[0][1]
        key = (round(pos[0], 0), round(pos[1], 0))
        pos_map.setdefault(key, []).append((gi, gtype))
    for pos_key, entries in pos_map.items():
        if len(entries) <= 1:
            continue
        # Skip redistribution if groups represent different weld pairs
        _all_pairs = set()
        for _gi, _gt in entries:
            _w = groups[_gi][1][0][0]
            _all_pairs.add(tuple(sorted((_w.get('part1',''), _w.get('part2','')))))
        if len(_all_pairs) > 1:
            continue
        wx, wy = pos_key
        _y_step = LABEL_HEIGHT * 5.0
        for i_idx, (gi, gtype) in enumerate(entries):
            if i_idx == 0:
                continue
            if _vy_center is not None and wy > _vy_center:
                _offset_y = wy - _y_step * i_idx
            else:
                _offset_y = wy + _y_step * i_idx
            _, items = groups[gi]
            groups[gi] = (gtype, [(it[0], (wx, _offset_y)) for it in items])


def _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
    """检查 bbox (x0,x1,y0,y1) 在视图边界内（margin=30）且在图纸边界内（margin=0）。"""
    if not (vx0 - 30 <= nbb[0] and nbb[1] <= vx1 + 30 and
            vy0 - 30 <= nbb[2] and nbb[3] <= vy1 + 30):
        return False
    if draw_bbox is not None:
        dx0, dy0, dx1, dy1 = draw_bbox
        if not (dx0 <= nbb[0] and nbb[1] <= dx1 and
                dy0 <= nbb[2] and nbb[3] <= dy1):
            return False
    return True


def _resolve_label_conflicts(msp, lines, text_bboxes, circles,
                              vx0, vy0, vx1, vy1, draw_bbox, placements, placed_text_bboxes, max_iter=8,
                              hatch_bboxes=None, other_view_bboxes=None):
    """全局后处理：检测标注间的文字重叠并进行综合微调（距离/角度/方向翻转/双向调整）。"""
    _OVERLAP_MARGIN = 4.0

    def _leader_crosses_text(pos, dist, angle, gtype, tb):
        """检查斜引线或水平接地线是否穿过文字框 tb。"""
        rad = math.radians(angle)
        ex = pos[0] + dist * math.cos(rad)
        ey = pos[1] + dist * math.sin(rad)
        h_len = PAIR_HORIZ_LAND if gtype == 'pair' else HORIZ_LAND
        h_land = h_len if math.cos(rad) >= -0.05 else -h_len
        hx = ex + h_land
        hy = ey
        if _seg_cross_rect(pos, (ex, ey), tb[0], tb[1], tb[2], tb[3]):
            return True
        if _seg_cross_rect((ex, ey), (hx, hy), tb[0], tb[1], tb[2], tb[3]):
            return True
        return False

    def _adjust_safe(idx, pos, dname, dist, angle, gtype, skip_idx):
        """检查调整后的位置是否安全（文字无重叠、引线不穿几何线/文字）。"""
        if gtype == 'pair':
            nbb = _paired_bbox(pos, dname, dist, angle)
        else:
            nbb = _single_bbox(pos, dname, dist, angle)
        if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
            return None

        _tbb = _text_bbox(pos, dname, dist, angle, is_pair=(gtype == 'pair'))
        # 纯文字 vs 已放置文字框（跳过自身）
        for k, otb in enumerate(placed_text_bboxes):
            if k == skip_idx: continue
            if not (_tbb[1] < otb[0] - _OVERLAP_MARGIN or
                    _tbb[0] > otb[1] + _OVERLAP_MARGIN or
                    _tbb[3] < otb[2] - _OVERLAP_MARGIN or
                    _tbb[2] > otb[3] + _OVERLAP_MARGIN):
                return None

        # 纯文字 vs 图纸原有文字框
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if not (_tbb[1] < tx0 - _OVERLAP_MARGIN or
                    _tbb[0] > tx1 + _OVERLAP_MARGIN or
                    _tbb[3] < ty0 - _OVERLAP_MARGIN or
                    _tbb[2] > ty1 + _OVERLAP_MARGIN):
                return None

        wx, wy = pos
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        ex = wx + dist * cos_a
        ey = wy + dist * sin_a
        h_len = PAIR_HORIZ_LAND if gtype == 'pair' else HORIZ_LAND
        h_land = h_len if cos_a >= -0.05 else -h_len
        hx = ex + h_land
        hy = ey

        # 水平接地线与几何线交叉
        _cx0, _cx1 = min(nbb[0], hx, ex), max(nbb[1], hx, ex)
        _cy0, _cy1 = min(nbb[2], hy, ey), max(nbb[3], hy, ey)
        for (sx, sy), (ex2, ey2) in lines:
            if max(sx, ex2) < _cx0 - 5 or min(sx, ex2) > _cx1 + 5: continue
            if max(sy, ey2) < _cy0 - 5 or min(sy, ey2) > _cy1 + 5: continue
            if _segments_cross_((ex, ey), (hx, hy), (sx, sy), (ex2, ey2)):
                return None

        # 斜引线与图纸文字框交叉
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
                return None

        # 斜引线与已放置标注文字框交叉
        for k, otb in enumerate(placed_text_bboxes):
            if k == skip_idx: continue
            if _seg_cross_rect((wx, wy), (ex, ey), otb[0], otb[1], otb[2], otb[3]):
                return None

        # 水平接地线与已放置标注文字框交叉
        for k, otb in enumerate(placed_text_bboxes):
            if k == skip_idx: continue
            if _seg_cross_rect((ex, ey), (hx, hy), otb[0], otb[1], otb[2], otb[3]):
                return None

        # 文字与圆/弧重叠
        for (ccx, ccy, cr) in circles:
            if not (_tbb[1] < ccx - cr or _tbb[0] > ccx + cr or
                    _tbb[3] < ccy - cr or _tbb[2] > ccy + cr):
                return None

        # 文字与 HATCH 填充区重叠
        if hatch_bboxes:
            for (hx0, hx1, hy0, hy1) in hatch_bboxes:
                if not (_tbb[1] < hx0 or _tbb[0] > hx1 or
                        _tbb[3] < hy0 or _tbb[2] > hy1):
                    return None

        return nbb, _tbb

    for _iter in range(max_iter):
        _any_fix = False
        n = len(placements)
        for i in range(n):
            gi, it_i, lb_i, pos_i, dn_i, ds_i, ag_i, _ = placements[i]
            tbb_i = placed_text_bboxes[i]
            for j in range(i + 1, n):
                gj, it_j, lb_j, pos_j, dn_j, ds_j, ag_j, _ = placements[j]
                tbb_j = placed_text_bboxes[j]
                if (tbb_i[1] < tbb_j[0] - _OVERLAP_MARGIN or
                    tbb_i[0] > tbb_j[1] + _OVERLAP_MARGIN or
                    tbb_i[3] < tbb_j[2] - _OVERLAP_MARGIN or
                    tbb_i[2] > tbb_j[3] + _OVERLAP_MARGIN):
                    # 文字不重叠，检查引线是否穿过对方文字
                    if not (_leader_crosses_text(pos_i, ds_i, ag_i, gi, tbb_j) or
                            _leader_crosses_text(pos_j, ds_j, ag_j, gj, tbb_i)):
                        # Also check leader-leader crossing (<45° must fix)
                        _h_i = PAIR_HORIZ_LAND if gi == 'pair' else HORIZ_LAND
                        _h_j = PAIR_HORIZ_LAND if gj == 'pair' else HORIZ_LAND
                        _crosses, _cross_ang = _leader_crosses_leader(
                            pos_i, ds_i, ag_i, _h_i, pos_j, ds_j, ag_j, _h_j)
                        if not _crosses or (_cross_ang and _cross_ang >= 45):
                            continue
                        # Cross angle < 45° → try to fix
                        _cross_fixed = False
                        for target, g, it, lb, pos, dn, ds, ag in [
                            (j, gj, it_j, lb_j, pos_j, dn_j, ds_j, ag_j),
                            (i, gi, it_i, lb_i, pos_i, dn_i, ds_i, ag_i),
                        ]:
                            for d_a in [15, -15, 30, -30, 45, -45]:
                                na = ag + d_a
                                ra = math.radians(na % 360)
                                if abs(math.sin(ra)) < math.sin(math.radians(20)): continue
                                if abs(math.cos(ra)) < math.cos(math.radians(70)): continue
                                result = _adjust_safe(target, pos, dn, ds, na, g, target)
                                if result:
                                    nbb, tbb = result
                                    placements[target] = (g, it, lb, pos, dn, ds, na, nbb)
                                    placed_text_bboxes[target] = tbb
                                    _cross_fixed = True
                                    break
                            if _cross_fixed: break
                        if _cross_fixed:
                            _fixed = True
                            break
                        continue

                _fixed = False

                # 依次尝试调整 j → 调整 i
                for target, g, it, lb, pos, dn, ds, ag in [
                    (j, gj, it_j, lb_j, pos_j, dn_j, ds_j, ag_j),
                    (i, gi, it_i, lb_i, pos_i, dn_i, ds_i, ag_i),
                ]:
                    # --- 距离微调 ---
                    for d_dist in [1, -1, 2, -2, 4, -4, 8, -8, 12, -12, 16, -16, 20, -20, 24]:
                        nd = ds + d_dist
                        if nd < 8 or nd > 60: continue
                        result = _adjust_safe(target, pos, dn, nd, ag, g, target)
                        if result:
                            nbb, tbb = result
                            placements[target] = (g, it, lb, pos, dn, nd, ag, nbb)
                            placed_text_bboxes[target] = tbb
                            _fixed = True
                            break
                    if _fixed: break

                    # --- 角度微调 ---
                    for d_a in [1, -1, 2, -2, 3, -3, 5, -5, 8, -8, 12, -12]:
                        na = ag + d_a
                        r5 = math.radians(na % 360)
                        if abs(math.sin(r5)) < math.sin(math.radians(20)): continue
                        if abs(math.cos(r5)) < math.cos(math.radians(70)): continue
                        result = _adjust_safe(target, pos, dn, ds, na, g, target)
                        if result:
                            nbb, tbb = result
                            placements[target] = (g, it, lb, pos, dn, ds, na, nbb)
                            placed_text_bboxes[target] = tbb
                            _fixed = True
                            break
                    if _fixed: break

                    # --- 方向翻转（角度+180°）---
                    _opp_ang = (ag + 180) % 360
                    for _oa in [_opp_ang, (_opp_ang-12)%360, (_opp_ang+12)%360,
                                (_opp_ang-24)%360, (_opp_ang+24)%360]:
                        r6 = math.radians(_oa)
                        if abs(math.sin(r6)) < math.sin(math.radians(20)): continue
                        if abs(math.cos(r6)) < math.cos(math.radians(70)): continue
                        for od in [ds, ds+4, ds-4, ds+8, ds-8]:
                            if od < 8 or od > 60: continue
                            result = _adjust_safe(target, pos, _opp_ang, od, _oa, g, target)
                            if result:
                                nbb, tbb = result
                                placements[target] = (g, it, lb, pos, _opp_ang, od, _oa, nbb)
                                placed_text_bboxes[target] = tbb
                                _fixed = True
                                break
                        if _fixed: break
                    if _fixed: break

                    # --- 全局重新搜索（兜底）---
                    _dn, _ds, _ag = _search_placement(
                        pos, lines, text_bboxes, circles,
                        [p[7] for p in placements], placed_text_bboxes,
                        vx0, vy0, vx1, vy1, draw_bbox,
                        is_pair=(g == 'pair'), hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes)
                    _re_result = _adjust_safe(target, pos, _dn, _ds, _ag, g, target)
                    if _re_result:
                        _nbb, _tbb = _re_result
                        placements[target] = (g, it, lb, pos, _dn, _ds, _ag, _nbb)
                        placed_text_bboxes[target] = _tbb
                        _fixed = True

                if _fixed:
                    _any_fix = True

        if not _any_fix:
            break

    # 最终安全兜底：超出边界的标注逐步缩短距离，找到不超边界且不重叠 hatch 的最短距离
    for k, pd in enumerate(placements):
        gk, it_k, lb_k, pk, dnk, dsk, agk, bbk = pd
        if _bbox_in_boundary(bbk, vx0, vy0, vx1, vy1, draw_bbox):
            continue
        for nd in range(8, dsk + 1, 2):
            if gk == 'pair':
                nbb = _paired_bbox(pk, dnk, nd, agk)
            else:
                nbb = _single_bbox(pk, dnk, nd, agk)
            if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
                continue
            _tb = _text_bbox(pk, dnk, nd, agk, is_pair=(gk == 'pair'))
            _hatch_ok = True
            if hatch_bboxes:
                for (hx0, hx1, hy0, hy1) in hatch_bboxes:
                    if not (_tb[1] < hx0 or _tb[0] > hx1 or _tb[3] < hy0 or _tb[2] > hy1):
                        _hatch_ok = False; break
            if _hatch_ok:
                placements[k] = (gk, it_k, lb_k, pk, dnk, nd, agk, nbb)
                break


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
        'style': 'Arial Narrow',
        'lineweight': 30,
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
        'style': 'Arial Narrow',
        'lineweight': 30,
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
            'style': 'Arial Narrow',
            'lineweight': 30,
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
