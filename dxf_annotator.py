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
LABEL_HEIGHT = 2.5  # text height in CAD units (default / sparse views)
LABEL_HEIGHT_DENSE = 1.6
LABEL_HEIGHT_VERY_DENSE = 1.35
DENSE_VIEW_N = 5              # label groups >= this → shrink text
VERY_DENSE_VIEW_N = 10
DENSE_CLUSTER_N = 2           # local weld cluster size → dense
VERY_DENSE_CLUSTER_N = 3      # larger local cluster → very dense
_ACTIVE_LABEL_HEIGHT = LABEL_HEIGHT
LAYER_NAME = "WELD_LABELS"
LABEL_COLOR = 5       # blue (ACI 5)
LABEL_OFFSET = 38.0   # fallback offset for no-coordinate labels

# Two-segment leader line: diagonal + horizontal landing
DIAG_BASE = 15             # base diagonal length in CAD units (increased from 10)
DIAG_STEP = 3              # step increment for collision avoidance
MAX_DIAG_LEN = 50            # upper limit for diagonal length
MAX_DIAG_LEN_PAIR = 50       # paired labels share the same hard cap

# Preferred leader angles (right side / left side) — stay inside 10°–80° band
PREFERRED_ANGLES_RIGHT = (35, 45, 55)
PREFERRED_ANGLES_LEFT = (125, 135, 145)
ANGLE_MIN = 10               # ban within ±10° of horizontal
ANGLE_MAX = 80               # ban within ±10° of vertical

# Cluster-aware fan-out parameters
CLUSTER_RADIUS = 36.0        # welds within this radius are considered a dense cluster
CLUSTER_MIN_SIZE = 2         # min near-group size for divergent prefer_ang pairing
DIVERGE_ANGLE_MIN = 30.0     # near neighbors with smaller angle gap → re-search
DIVERGE_SCORE_CLOSE = 35.0   # angle gap below this → score penalty

# Quadrant angle ranges relative to view center (degrees, CCW from +X)
# Synced with ±10° horizontal/vertical bans → usable band ~10°–80° per quadrant
QUAD_ANGLE_RANGES = {
    1: (10, 80),      # Q1 右上方
    2: (100, 170),    # Q2 左上方
    3: (190, 260),    # Q3 左下方
    4: (280, 350),    # Q4 右下方
}

# Same-half divergent pairs (no cross half-plane): right Q1↔Q4, left Q2↔Q3
DIVERGE_PREF_RIGHT = (55.0, 305.0)   # Q1 upper, Q4 lower
DIVERGE_PREF_LEFT = (135.0, 225.0)   # Q2 upper, Q3 lower

QUADRANT_ANGLE_TOL = 1.0
OVERLAP_MARGIN = 4.0
CLUSTER_OVERLAP_MARGIN = 4.0
LINE_CLEARANCE = 6.5       # min distance from label text to part edges

MIN_DIAG_LEN = 10                      # 斜段绝对硬下限
PREFERRED_DIAG_MIN = 18                # 低于此视为过短
PREFERRED_DIAG_SOFT = 24               # 甜区中心偏短侧
PREFERRED_DIAG_HARD = 38               # 超过开始明显偏长
WM_TEXT_MARGIN = 8.0                   # WM / 3 SIDES TYP 避让边距
WM_SYMBOL_RADIUS = 18.0              # 焊缝符号禁区半径（紧凑符号区，不含长引线）
WM_SYMBOL_LINE_MAX = 35.0            # 计入符号 AABB 的短线最大长度
WM_SYMBOL_CLUSTER_R = 45.0           # 短线相对 WM 文字中心的聚类半径
SECTION_TITLE_MARGIN = 8.0           # D-D / 1:10 等剖面标题硬禁区边距
LEADER_CROSS_MIN_DEG = 45.0          # 蓝×蓝交叉：优先不相交；否则夹角须 > 此值

# Hard exclusion margins
BOM_MARGIN = 10              # margin around BOM / title / bolt tables
BOUNDARY_MARGIN = 8          # margin around drawing inner frame (hard)

_SECTION_TITLE_RE = re.compile(
    r'^(?:[A-Z]\s*[-–—]\s*[A-Z]|\d+\s*:\s*\d+)$', re.I)


def _lh():
    """Current view label text height (may shrink in dense views)."""
    return _ACTIVE_LABEL_HEIGHT


def _set_active_label_height(h):
    global _ACTIVE_LABEL_HEIGHT
    _ACTIVE_LABEL_HEIGHT = h


def _y_slot_height():
    return _lh() * 3.0


def _max_local_cluster_size(positions, radius=None):
    """Largest number of points within radius of each other (inclusive self)."""
    if not positions:
        return 0
    r = CLUSTER_RADIUS if radius is None else radius
    n = len(positions)
    best = 1
    for i in range(n):
        xi, yi = positions[i]
        cnt = 1
        for j in range(n):
            if i == j:
                continue
            xj, yj = positions[j]
            if math.hypot(xj - xi, yj - yi) <= r:
                cnt += 1
        if cnt > best:
            best = cnt
    return best


def _local_cluster_size_at(pos, positions, radius=None):
    """How many welds (incl. self) lie within radius of pos."""
    if not positions:
        return 0
    r = CLUSTER_RADIUS if radius is None else radius
    px, py = pos[0], pos[1]
    return sum(1 for x, y in positions if math.hypot(x - px, y - py) <= r)


def _height_from_cluster_n(n):
    if n >= VERY_DENSE_CLUSTER_N:
        return LABEL_HEIGHT_VERY_DENSE
    if n >= DENSE_CLUSTER_N:
        return LABEL_HEIGHT_DENSE
    return LABEL_HEIGHT


def _choose_view_label_height(groups):
    """Pick label height from group count and local weld density."""
    positions = []
    for _gtype, items in groups:
        pos = items[0][1]
        positions.append((pos[0], pos[1]))
    n = len(groups)
    cluster = _max_local_cluster_size(positions)
    if n >= VERY_DENSE_VIEW_N or cluster >= VERY_DENSE_CLUSTER_N:
        return LABEL_HEIGHT_VERY_DENSE
    if n >= DENSE_VIEW_N or cluster >= DENSE_CLUSTER_N:
        return LABEL_HEIGHT_DENSE
    return LABEL_HEIGHT


def _group_label_heights(groups):
    """Per-group height from local cluster; busy views also floor the size."""
    positions = [(items[0][1][0], items[0][1][1]) for _gtype, items in groups]
    n = len(groups)
    if n >= VERY_DENSE_VIEW_N:
        view_floor = LABEL_HEIGHT_VERY_DENSE
    elif n >= DENSE_VIEW_N:
        view_floor = LABEL_HEIGHT_DENSE
    else:
        view_floor = LABEL_HEIGHT
    heights = []
    for pos in positions:
        local_h = _height_from_cluster_n(_local_cluster_size_at(pos, positions))
        heights.append(min(local_h, view_floor))
    return heights, positions


def _angle_delta_deg(a, b):
    """Smallest absolute angle difference in degrees [0, 180]."""
    d = abs((a - b) % 360)
    return d if d <= 180 else 360 - d


def _diverge_prefer_angs(pos_a, pos_b, vcx, vcy):
    """Same-half divergent prefer angles: high→upper quad, low→lower quad."""
    (xa, ya), (xb, yb) = pos_a, pos_b
    right = ((xa + xb) / 2.0) >= vcx
    up_ang, dn_ang = DIVERGE_PREF_RIGHT if right else DIVERGE_PREF_LEFT
    if ya >= yb:
        return up_ang, dn_ang
    return dn_ang, up_ang


def _halfplane_complement(ang):
    """Opposite corner on the nearest same-half diverge pair (no cross half)."""
    comps = {
        135.0: 225.0, 225.0: 135.0,   # left Q2↔Q3
        65.0: 295.0, 295.0: 65.0,     # right Q1↔Q4
        55.0: 305.0, 305.0: 55.0,
        45.0: 315.0, 315.0: 45.0,
    }
    best_k, best_gap = 135.0, 999.0
    for k in comps:
        g = _angle_delta_deg(ang, k)
        if g < best_gap:
            best_gap = g
            best_k = k
    return comps[best_k]


def _leader_half_band(ang):
    """Same-half band: upper (Q1/Q2) vs lower (Q4/Q3) vs other."""
    a = ang % 360
    if 10 <= a <= 80 or 100 <= a <= 170:
        return 'up'
    if 190 <= a <= 260 or 280 <= a <= 350:
        return 'dn'
    return 'other'


def _text_clears_obstacles(ttbb, others_tb, wm_text_bboxes=None, hatch_bboxes=None):
    """Hard gates used by post-passes: label overlap + WM + hatch."""
    if any(_text_overlaps(ttbb, otb, OVERLAP_MARGIN) for otb in others_tb):
        return False
    if wm_text_bboxes and any(
            _text_overlaps(ttbb, wtb, WM_TEXT_MARGIN) for wtb in wm_text_bboxes):
        return False
    if hatch_bboxes and any(
            _text_overlaps(ttbb, htb, OVERLAP_MARGIN) for htb in hatch_bboxes):
        return False
    return True


def _leader_crosses_view_mid(pos, dist, angle, cx, cy):
    """True if diagonal tip crosses past the view center from the weld's half."""
    rad = math.radians(angle % 360)
    tip_x = pos[0] + dist * math.cos(rad)
    tip_y = pos[1] + dist * math.sin(rad)
    # horizontal mid cross (left↔right)
    if (pos[0] - cx) * (tip_x - cx) < 0 and abs(tip_x - pos[0]) > 8:
        return True
    # vertical mid cross for very long leaders (top↔bottom far jump)
    if dist >= PREFERRED_DIAG_HARD and (pos[1] - cy) * (tip_y - cy) < 0 and abs(tip_y - pos[1]) > 12:
        return True
    return False


def _shorten_long_same_angle(placements, placed_bboxes, placed_text_bboxes,
                             draw_bbox, cross_view_text_bboxes,
                             wm_text_bboxes=None, hatch_bboxes=None):
    """Same-angle shorten when diagonal exceeds soft length; text moves toward weld.

    Keeps WM/hatch hard gates. Skips unsafe shortens (leaves OK poses alone).
    """
    n = len(placements)
    _short_cap = int(PREFERRED_DIAG_SOFT)
    for i in range(n):
        gi, iti, lbi, pi, lti, dsi, agi = placements[i][:7]
        if dsi <= _short_cap:
            continue
        tbb = placed_text_bboxes[i]
        if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
            continue
        is_pair = (gi == 'pair')
        others_tb = ([placed_text_bboxes[k] for k in range(n) if k != i]
                     + list(cross_view_text_bboxes or []))
        best = None
        _cands = list(range(PREFERRED_DIAG_MIN, min(int(dsi), _short_cap) + 1, 2))
        if int(dsi) > _short_cap + 2:
            _cands += list(range(_short_cap + 2, int(dsi), 2))
        for nd in _cands:
            ttbb = _text_bbox(pi, nd, agi, lti, is_pair=is_pair)
            if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                continue
            if not _text_clears_obstacles(ttbb, others_tb, wm_text_bboxes, hatch_bboxes):
                continue
            best = nd
            break
        if best is None or best >= dsi - 0.5:
            continue
        nbb = (_paired_bbox(pi, best, agi, lti) if is_pair
               else _single_bbox(pi, best, agi, lti))
        ttbb = _text_bbox(pi, best, agi, lti, is_pair=is_pair)
        placements[i] = (gi, iti, lbi, pi, lti, best, agi, nbb)
        placed_bboxes[i] = nbb
        placed_text_bboxes[i] = ttbb


def _fix_overlong_crossing_leaders(placements, placed_bboxes, placed_text_bboxes,
                                     lines, text_bboxes, circles,
                                     vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                                     other_view_bboxes, other_view_part_bboxes,
                                     wm_text_bboxes, cx, cy, part_bbox,
                                     line_grid, cross_view_text_bboxes):
    """Long leaders that cross the view mid-plane: re-search same-side (no cross half)."""
    n = len(placements)
    for i in range(n):
        gi, iti, lbi, pi, lti, dsi, agi = placements[i][:7]
        # also catch extreme length even if mid-cross is weak
        if dsi < PREFERRED_DIAG_HARD and not _leader_crosses_view_mid(pi, dsi, agi, cx, cy):
            continue
        if dsi < PREFERRED_DIAG_SOFT:
            continue
        if (not _leader_crosses_view_mid(pi, dsi, agi, cx, cy)
                and dsi < PREFERRED_DIAG_HARD + 2):
            continue
        is_pair = (gi == 'pair')
        hq = _weld_home_quadrant(pi[0], pi[1], cx, cy)
        others_bb = [placed_bboxes[k] for k in range(n) if k != i]
        others_tb = ([placed_text_bboxes[k] for k in range(n) if k != i]
                     + list(cross_view_text_bboxes or []))
        nbrs = [(placements[k][3], placements[k][6]) for k in range(n) if k != i]
        _max_len = min(MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN,
                       int(PREFERRED_DIAG_HARD))
        # prefer home-side mid angles (stay same half, shorter)
        if hq in (1, 4):
            prefers = [55.0, 45.0, 65.0, 305.0, 315.0]
        else:
            prefers = [135.0, 145.0, 225.0, 215.0]
        # bias toward keeping vertical side of home (upper if currently up-ish weld)
        if pi[1] >= cy:
            prefers = ([p for p in prefers if _leader_half_band(p) == 'up']
                       + [p for p in prefers if _leader_half_band(p) != 'up'])
        else:
            # isolated lower welds: still prefer UP first (W5-class) then down
            prefers = ([p for p in prefers if _leader_half_band(p) == 'up']
                       + [p for p in prefers if _leader_half_band(p) == 'dn'])
        applied = False
        for prefer in prefers:
            _, nd, na = _search_placement(
                pi, lines, text_bboxes, circles, others_bb, others_tb,
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=lti, wm_text_bboxes=wm_text_bboxes,
                part_bbox=part_bbox, prefer_down=False,
                line_grid=line_grid, allow_adjacent=True,
                prefer_ang=prefer % 360, neighbor_angles=nbrs,
                max_dist=_max_len, cross_ok=False)
            if nd >= dsi - 1 and _leader_crosses_view_mid(pi, nd, na, cx, cy):
                continue
            if _leader_crosses_view_mid(pi, nd, na, cx, cy) and nd > PREFERRED_DIAG_SOFT:
                continue
            ttbb = _text_bbox(pi, nd, na, lti, is_pair=is_pair)
            if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                continue
            if not _text_clears_obstacles(ttbb, others_tb, wm_text_bboxes, hatch_bboxes):
                continue
            # keep near-neighbor angle gap
            if any(math.hypot(npos[0] - pi[0], npos[1] - pi[1]) <= CLUSTER_RADIUS
                   and _angle_delta_deg(na, nang) < DIVERGE_ANGLE_MIN
                   for npos, nang in nbrs):
                continue
            nbb = (_paired_bbox(pi, nd, na, lti) if is_pair
                   else _single_bbox(pi, nd, na, lti))
            placements[i] = (gi, iti, lbi, pi, lti, nd, na, nbb)
            placed_bboxes[i] = nbb
            placed_text_bboxes[i] = ttbb
            applied = True
            break
        if applied:
            continue


def _maximin_corner_ang(neighbor_angs, prefer=None, home_q=None):
    """Same-half corner maximizing min angle gap to neighbors."""
    # stay in one half-plane to avoid leaders crossing the part
    if home_q in (1, 4) or (prefer is not None and (
            _angle_delta_deg(prefer, 45) < 50 or _angle_delta_deg(prefer, 315) < 50
            or _angle_delta_deg(prefer, 65) < 50 or _angle_delta_deg(prefer, 295) < 50)):
        corners = [65.0, 295.0, 45.0, 315.0]
    elif home_q in (2, 3) or (prefer is not None and (
            _angle_delta_deg(prefer, 135) < 50 or _angle_delta_deg(prefer, 225) < 50)):
        corners = [135.0, 225.0]
    else:
        # infer from first neighbor angle
        seed = neighbor_angs[0] if neighbor_angs else (prefer or 135.0)
        if _angle_delta_deg(seed, 45) < 90 or _angle_delta_deg(seed, 315) < 90:
            corners = [65.0, 295.0, 45.0, 315.0]
        else:
            corners = [135.0, 225.0]
    if prefer is not None:
        corners = [prefer % 360] + [c for c in corners if _angle_delta_deg(c, prefer) > 1]
    if not neighbor_angs:
        return corners[0]
    best_a, best_sc = corners[0], -1.0
    for c in corners:
        sc = min(_angle_delta_deg(c, a) for a in neighbor_angs)
        if sc > best_sc:
            best_sc, best_a = sc, c
    if best_sc >= DIVERGE_ANGLE_MIN:
        return best_a
    seed = neighbor_angs[0]
    for da in (40, -40, 55, -55, 70, -70):
        c = (seed + da) % 360
        # keep candidate in same half as corners[0]
        half_right = corners[0] in (45.0, 65.0, 295.0, 315.0)
        c_right = (0 <= (c % 360) < 90) or (270 < (c % 360) <= 360)
        if half_right != c_right:
            continue
        sc = min(_angle_delta_deg(c, a) for a in neighbor_angs)
        if sc > best_sc:
            best_sc, best_a = sc, c
    return best_a


def _pair_near_groups(groups, vcx, vcy, radius=None):
    """Greedy near-neighbor pairing → (prefer_ang map, partner index map)."""
    r = CLUSTER_RADIUS if radius is None else radius
    n = len(groups)
    if n < CLUSTER_MIN_SIZE:
        return {}, {}
    positions = [(items[0][1][0], items[0][1][1]) for _gtype, items in groups]
    candidates = []
    for i in range(n):
        xi, yi = positions[i]
        for j in range(i + 1, n):
            xj, yj = positions[j]
            d = math.hypot(xj - xi, yj - yi)
            if d <= r and d > 0.5:
                candidates.append((d, i, j))
    candidates.sort()
    used = set()
    prefer = {}
    partners = {}
    for _d, i, j in candidates:
        if i in used or j in used:
            continue
        ai, aj = _diverge_prefer_angs(positions[i], positions[j], vcx, vcy)
        prefer[i] = ai
        prefer[j] = aj
        partners[i] = j
        partners[j] = i
        used.add(i)
        used.add(j)
    return prefer, partners


def _txt_sample_points(bx0, bx1, by0, by1):
    """Dense sample points on text bbox for line-clearance checks."""
    mx, my = (bx0 + bx1) / 2.0, (by0 + by1) / 2.0
    qx0, qx1 = bx0 + (bx1 - bx0) * 0.25, bx0 + (bx1 - bx0) * 0.75
    qy0, qy1 = by0 + (by1 - by0) * 0.25, by0 + (by1 - by0) * 0.75
    return [
        (bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1),
        (mx, by0), (mx, by1), (bx0, my), (bx1, my), (mx, my),
        (qx0, by0), (qx1, by0), (qx0, by1), (qx1, by1),
        (bx0, qy0), (bx0, qy1), (bx1, qy0), (bx1, qy1),
        (qx0, qy0), (qx1, qy0), (qx0, qy1), (qx1, qy1),
    ]


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


def _leader_cross_acute_deg(angle_a, angle_b):
    """Acute angle between two leader directions (0..90)."""
    d = abs(float(angle_a) - float(angle_b)) % 180.0
    return min(d, 180.0 - d)


def _leader_entry(pos, dist, angle, label_text, is_pair):
    """Pack placed blue-leader geometry for cross checks."""
    h_len = _horiz_land(label_text, is_pair)
    rad = math.radians(angle)
    h_land = h_len if math.cos(rad) >= -0.05 else -h_len
    return (pos, dist, angle, h_land)


def _blue_leader_shallow_cross(pos, dist, angle, h_land, placed_leaders,
                               min_deg=None):
    """True if candidate blue leader crosses a placed one at ≤ min_deg."""
    if not placed_leaders:
        return False
    thr = LEADER_CROSS_MIN_DEG if min_deg is None else min_deg
    for ppos, pdist, pang, phland in placed_leaders:
        crosses, _ = _leader_crosses_leader(
            pos, dist, angle, h_land, ppos, pdist, pang, phland)
        if crosses and _leader_cross_acute_deg(angle, pang) <= thr:
            return True
    return False


def _add_bolt_hard_zones(blk, circles, hatch_bboxes):
    """Hard pads around Bolt block holes (CAD radius is often ~1mm)."""
    for _sub in blk:
        _st = _sub.dxftype()
        if _st not in ('CIRCLE', 'ARC'):
            continue
        try:
            _cx, _cy = _sub.dxf.center.x, _sub.dxf.center.y
            _cr = float(getattr(_sub.dxf, 'radius', 1.0) or 1.0)
        except Exception:
            continue
        _pad = max(_cr * 2.5, 3.5)
        circles.append((_cx, _cy, _pad))
        hatch_bboxes.append((
            _cx - _pad, _cx + _pad, _cy - _pad, _cy + _pad,
        ))


def _add_section_title_zones(blk, hard_bboxes):
    """Hard exclusion for SectionMark / Unknown split titles ('D','-','D','1:10')."""
    frags = []
    for _sub in blk:
        _st = _sub.dxftype()
        _txt, _tx, _ty, _th = '', None, None, 2.0
        try:
            if _st in ('TEXT', 'ATTDEF', 'ATTRIB'):
                _tx, _ty = _sub.dxf.insert.x, _sub.dxf.insert.y
                _th = getattr(_sub.dxf, 'height', 2.0)
                _txt = (_sub.dxf.text or '').strip() if hasattr(_sub.dxf, 'text') else ''
            elif _st == 'MTEXT':
                _tx, _ty = _sub.dxf.insert.x, _sub.dxf.insert.y
                _th = getattr(_sub.dxf, 'char_height', 2.0)
                _txt = (_sub.text or '').strip() if hasattr(_sub, 'text') else ''
                _txt = _txt.replace('\\n', ' ')
            else:
                continue
        except Exception:
            continue
        if not _txt or _tx is None:
            continue
        _cmp = _txt.replace(' ', '')
        _is_title = bool(
            _SECTION_TITLE_RE.match(_txt) or _SECTION_TITLE_RE.match(_cmp)
            or (_txt in ('-', '–', '—'))
            or (len(_txt) == 1 and _txt.isalpha()))
        if not _is_title:
            continue
        frags.append((_txt, _tx, _ty, _th))
        _mrg = SECTION_TITLE_MARGIN
        if _SECTION_TITLE_RE.match(_cmp) or _SECTION_TITLE_RE.match(_txt):
            _mrg = SECTION_TITLE_MARGIN * 1.5
        _tw = _th * max(len(_txt), 1) * 0.95
        hard_bboxes.append((
            _tx - _mrg, _tx + _tw + _mrg,
            _ty - _mrg, _ty + _th + _mrg,
        ))
    # Union AABB for split titles (letter + dash + scale) so labels cannot
    # park between 'D' and '1:10'.
    if len(frags) >= 2:
        _has_scale = any(
            _SECTION_TITLE_RE.match(t.replace(' ', ''))
            or _SECTION_TITLE_RE.match(t)
            for t, _, _, _ in frags)
        _has_letter = any(len(t) == 1 and t.isalpha() for t, _, _, _ in frags)
        if _has_scale or _has_letter:
            _mrg = SECTION_TITLE_MARGIN * 2.0
            xs0, xs1, ys0, ys1 = [], [], [], []
            for t, tx, ty, th in frags:
                tw = th * max(len(t), 1) * 0.95
                xs0.append(tx - _mrg)
                xs1.append(tx + tw + _mrg)
                ys0.append(ty - _mrg)
                ys1.append(ty + th + _mrg)
            hard_bboxes.append((min(xs0), max(xs1), min(ys0), max(ys1)))


def _add_wm_hard_zones(blk, wm_text_bboxes, circles, hatch_bboxes):
    """Hard zones for WeldMark block TEXT + compact symbol (no long leaders)."""
    _wm_pts = []
    _short = []
    for _sub in blk:
        _st = _sub.dxftype()
        if _st in ('TEXT', 'ATTDEF', 'ATTRIB'):
            try:
                _atx, _aty = _sub.dxf.insert.x, _sub.dxf.insert.y
                _ath = getattr(_sub.dxf, 'height', 2.0)
                _atxt = (_sub.dxf.text or '').strip() if hasattr(_sub.dxf, 'text') else ''
                if _atxt:
                    _wm_pts.append((_atx, _aty, _ath, _atxt))
            except Exception:
                pass
        elif _st == 'MTEXT':
            try:
                _atx, _aty = _sub.dxf.insert.x, _sub.dxf.insert.y
                _ath = getattr(_sub.dxf, 'char_height', 2.0)
                _atxt = (_sub.text or '').strip() if hasattr(_sub, 'text') else ''
                if _atxt:
                    _wm_pts.append((_atx, _aty, _ath, _atxt.replace('\\n', ' ')))
            except Exception:
                pass
        elif _st == 'LINE':
            try:
                _s, _e = _sub.dxf.start, _sub.dxf.end
                _ln = math.hypot(_e.x - _s.x, _e.y - _s.y)
                if _ln <= WM_SYMBOL_LINE_MAX:
                    _short.append((_s.x, _s.y, _e.x, _e.y, (_s.x + _e.x) / 2, (_s.y + _e.y) / 2))
            except Exception:
                pass
    if not _wm_pts:
        return
    _axs, _ays = [], []
    for _atx, _aty, _ath, _atxt in _wm_pts:
        _atw = _ath * max(len(_atxt), 1) * 0.95
        _mrg = WM_TEXT_MARGIN
        _axs.extend([_atx - _mrg, _atx + _atw + _mrg])
        _ays.extend([_aty - _mrg, _aty + _ath + _mrg])
    wm_text_bboxes.append((min(_axs), max(_axs), min(_ays), max(_ays)))

    _cx = sum(p[0] for p in _wm_pts) / len(_wm_pts)
    _cy = sum(p[1] for p in _wm_pts) / len(_wm_pts)
    _sx, _sy = list(_axs), list(_ays)
    for _x0, _y0, _x1, _y1, _mx, _my in _short:
        if math.hypot(_mx - _cx, _my - _cy) <= WM_SYMBOL_CLUSTER_R:
            _sx.extend([_x0, _x1])
            _sy.extend([_y0, _y1])
    _pad = max(2.0, WM_TEXT_MARGIN * 0.5)
    _hb = (min(_sx) - _pad, max(_sx) + _pad, min(_sy) - _pad, max(_sy) + _pad)
    hatch_bboxes.append(_hb)
    _scx = (_hb[0] + _hb[1]) / 2
    _scy = (_hb[2] + _hb[3]) / 2
    _r = max(WM_SYMBOL_RADIUS, 0.5 * max(_hb[1] - _hb[0], _hb[3] - _hb[2]))
    circles.append((_scx, _scy, min(_r, WM_SYMBOL_RADIUS * 1.4)))


def _collect_all_obstacles(doc, view_id, view_bbox=None):
    """Collect all visual obstacles in a view: lines, text bboxes, circles/arcs, hatch bboxes.
    view_bbox: (x0,y0,x1,y1) to filter modelspace entities by spatial proximity."""
    lines, text_bboxes, wm_text_bboxes, circles, hatch_bboxes = [], [], [], [], []

    def add_entity(e):
        t = e.dxftype()
        if t == 'LINE':
            lines.append(((e.dxf.start.x, e.dxf.start.y),
                          (e.dxf.end.x, e.dxf.end.y)))
        elif t == 'TEXT':
            tx, ty = e.dxf.insert.x, e.dxf.insert.y
            th = getattr(e.dxf, 'height', 2.0)
            tw = th * len(e.dxf.text.strip()) * 0.8
            mrg = 1.5
            text_bboxes.append((tx - mrg, tx + tw + mrg, ty - mrg, ty + th + mrg))
        elif t == 'MTEXT':
            tx, ty = e.dxf.insert.x, e.dxf.insert.y
            th = getattr(e.dxf, 'char_height', 2.0)
            txt = e.text.strip() if hasattr(e, 'text') else ''
            lines_txt = txt.split('\\n') if txt else ['']
            nlines = len(lines_txt)
            max_line = max(len(l) for l in lines_txt) if txt else 8
            tw = th * max_line * 0.7
            th_total = th * nlines + (nlines - 1) * th * 0.3
            mrg = 1.5
            text_bboxes.append((tx - mrg, tx + tw + mrg, ty - mrg, ty + th_total + mrg))
        elif t == 'CIRCLE':
            # Tiny Mark/Bolt holes still need a readable exclusion pad
            _cr = max(float(getattr(e.dxf, 'radius', 1.0) or 1.0), 2.5)
            circles.append((e.dxf.center.x, e.dxf.center.y, _cr))
        elif t == 'ARC':
            _cr = max(float(getattr(e.dxf, 'radius', 1.0) or 1.0), 2.5)
            circles.append((e.dxf.center.x, e.dxf.center.y, _cr))
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
                    _hb = (min(xs), max(xs), min(ys), max(ys))
                    # Skip tiny fill fragments — they flood hot-path conflict scans
                    _hw, _hh = _hb[1] - _hb[0], _hb[3] - _hb[2]
                    if _hw * _hh >= 4.0 and max(_hw, _hh) >= 2.0:
                        hatch_bboxes.append(_hb)
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
                tw = th * len(txt) * 0.8 if txt else th * 2
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
            # SOLID fill edges as line obstacles (no full AABB hard box)
            try:
                pts = []
                for _vn in ('vtx0', 'vtx1', 'vtx2', 'vtx3'):
                    _pt = getattr(e.dxf, _vn, None)
                    if _pt is not None and getattr(_pt, 'x', None) is not None:
                        pts.append((_pt.x, _pt.y))
                if len(pts) >= 3:
                    for i in range(len(pts)):
                        j = (i + 1) % len(pts)
                        lines.append((pts[i], pts[j]))
            except Exception:
                pass
        elif t == '3DFACE':
            try:
                pts = []
                for _vn in ('vtx0', 'vtx1', 'vtx2', 'vtx3'):
                    _pt = getattr(e.dxf, _vn, None)
                    if _pt is not None and getattr(_pt, 'x', None) is not None:
                        pts.append((_pt.x, _pt.y))
                if len(pts) >= 3:
                    for i in range(len(pts)):
                        j = (i + 1) % len(pts)
                        lines.append((pts[i], pts[j]))
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
                tw = th * len(txt) * 0.7 if txt else th * 6
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

    def _accum_geom_xy(blk2, _xs, _ys, depth=0):
        """Collect geometry extents from LINE/LWPOLYLINE/POLYLINE/SOLID/CIRCLE/ARC/TEXT."""
        if depth > 5:
            return
        for _sub in blk2:
            _st = _sub.dxftype()
            if _st == 'LINE':
                _xs.append(_sub.dxf.start.x); _xs.append(_sub.dxf.end.x)
                _ys.append(_sub.dxf.start.y); _ys.append(_sub.dxf.end.y)
            elif _st == 'LWPOLYLINE':
                try:
                    for _pt in _sub.get_points():
                        _xs.append(_pt[0]); _ys.append(_pt[1])
                except Exception:
                    pass
            elif _st == 'POLYLINE':
                try:
                    for _v in _sub.vertices:
                        _xs.append(_v.dxf.location.x); _ys.append(_v.dxf.location.y)
                except Exception:
                    pass
            elif _st in ('SOLID', '3DFACE'):
                try:
                    for _vn in ('vtx0', 'vtx1', 'vtx2', 'vtx3'):
                        _pt = getattr(_sub.dxf, _vn, None)
                        if _pt is not None and _pt[0] is not None:
                            _xs.append(_pt[0]); _ys.append(_pt[1])
                except Exception:
                    pass
            elif _st in ('CIRCLE', 'ARC'):
                try:
                    _ccx, _ccy = _sub.dxf.center.x, _sub.dxf.center.y
                    _cr = getattr(_sub.dxf, 'radius', 1.0)
                    _xs.extend([_ccx - _cr, _ccx + _cr])
                    _ys.extend([_ccy - _cr, _ccy + _cr])
                except Exception:
                    pass
            elif _st in ('TEXT', 'ATTDEF', 'ATTRIB'):
                try:
                    _tx, _ty = _sub.dxf.insert.x, _sub.dxf.insert.y
                    _th = getattr(_sub.dxf, 'height', 2.0)
                    _txt = ''
                    if hasattr(_sub.dxf, 'text') and _sub.dxf.text:
                        _txt = _sub.dxf.text.strip()
                    _tw = _th * max(len(_txt), 2) * 0.8
                    _xs.extend([_tx, _tx + _tw]); _ys.extend([_ty, _ty + _th])
                except Exception:
                    pass
            elif _st == 'MTEXT':
                try:
                    _tx, _ty = _sub.dxf.insert.x, _sub.dxf.insert.y
                    _th = getattr(_sub.dxf, 'char_height', 2.0)
                    _txt = _sub.text.strip() if hasattr(_sub, 'text') else ''
                    _max_line = max((len(l) for l in _txt.split('\\n')), default=4)
                    _tw = _th * _max_line * 0.7
                    _xs.extend([_tx, _tx + _tw]); _ys.extend([_ty, _ty + _th])
                except Exception:
                    pass
            elif _st == 'INSERT':
                _sblk = doc.blocks.get(_sub.dxf.name)
                if _sblk:
                    _accum_geom_xy(_sblk, _xs, _ys, depth + 1)

    # 1) Part blocks in this view — lines/text only; do NOT hard-block with Part AABB
    for blk in doc.blocks:
        if blk.name.startswith('Part') and re.search(rf' - {view_id}$', blk.name):
            _add_block_entities(blk)

    # 2) WeldMark / Mark / Bolt / SectionMark / Unknown in this view
    for blk in doc.blocks:
        if (blk.name.startswith('WeldMark') or blk.name.startswith('Mark')
                or blk.name.startswith('Bolt')
                or blk.name.startswith('SectionMark')
                or blk.name.startswith('Unknown')):
            if re.search(rf' - {view_id}$', blk.name):
                if not blk.name.startswith('Unknown'):
                    _add_block_entities(blk)
                if blk.name.startswith('WeldMark'):
                    _add_wm_hard_zones(blk, wm_text_bboxes, circles, hatch_bboxes)
                elif blk.name.startswith('Bolt'):
                    # Bolt holes: expand tiny CAD radii into readable hard pads
                    _add_bolt_hard_zones(blk, circles, hatch_bboxes)
                elif (blk.name.startswith('SectionMark')
                      or blk.name.startswith('Unknown')):
                    # D-D / 1:10 titles: hard ban (same channel as WM text)
                    _add_section_title_zones(blk, wm_text_bboxes)

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
                _add_block_entities(blk)
                # 提取 WM INSERT 上的 ATTRIB（少见）；块内 TEXT 已由 _add_wm_hard_zones 覆盖
                if blk.name.startswith('WeldMark'):
                    _wm_pts = []
                    for _attrib in e.attribs:
                        try:
                            _atx, _aty = _attrib.dxf.insert.x, _attrib.dxf.insert.y
                            _ath = getattr(_attrib.dxf, 'height', 2.0)
                            _atxt = _attrib.dxf.text.strip() if hasattr(_attrib.dxf, 'text') and _attrib.dxf.text else ''
                            if _atxt:
                                _wm_pts.append((_atx, _aty, _ath, _atxt))
                        except Exception:
                            pass
                    if _wm_pts:
                        _axs = []; _ays = []
                        for _atx, _aty, _ath, _atxt in _wm_pts:
                            _atw = _ath * len(_atxt) * 0.95
                            _mrg = WM_TEXT_MARGIN
                            _axs.extend([_atx - _mrg, _atx + _atw + _mrg])
                            _ays.extend([_aty - _mrg, _aty + _ath + _mrg])
                        wm_text_bboxes.append((min(_axs), max(_axs), min(_ays), max(_ays)))
                        _cx = sum(p[0] for p in _wm_pts) / len(_wm_pts)
                        _cy = sum(p[1] for p in _wm_pts) / len(_wm_pts)
                        circles.append((_cx, _cy, WM_SYMBOL_RADIUS))
                        _r = WM_SYMBOL_RADIUS
                        hatch_bboxes.append((_cx - _r, _cx + _r, _cy - _r, _cy + _r))
        else:
            add_entity(e)
            # Modelspace section titles / scales near the view → hard ban
            if e.dxftype() in ('TEXT', 'MTEXT'):
                try:
                    _mt = ''
                    if e.dxftype() == 'TEXT':
                        _mt = (e.dxf.text or '').strip()
                        _mx, _my = e.dxf.insert.x, e.dxf.insert.y
                        _mh = getattr(e.dxf, 'height', 2.0)
                    else:
                        _mt = (e.text or '').strip().replace('\\n', ' ')
                        _mx, _my = e.dxf.insert.x, e.dxf.insert.y
                        _mh = getattr(e.dxf, 'char_height', 2.0)
                    _mt_cmp = _mt.replace(' ', '')
                    if _mt and (_SECTION_TITLE_RE.match(_mt)
                                or _SECTION_TITLE_RE.match(_mt_cmp)):
                        _mm = SECTION_TITLE_MARGIN * 1.5
                        _mw = _mh * max(len(_mt), 1) * 0.95
                        wm_text_bboxes.append((
                            _mx - _mm, _mx + _mw + _mm,
                            _my - _mm, _my + _mh + _mm,
                        ))
                except Exception:
                    pass

    return lines, text_bboxes, wm_text_bboxes, circles, hatch_bboxes


def _build_line_grid(lines, cell=50):
    """Spatial hash of lines for near-neighbor conflict checks."""
    grid = {}
    for _s, _e in lines:
        _gx0 = int(min(_s[0], _e[0]) / cell)
        _gx1 = int(max(_s[0], _e[0]) / cell)
        _gy0 = int(min(_s[1], _e[1]) / cell)
        _gy1 = int(max(_s[1], _e[1]) / cell)
        for _gx in range(_gx0, _gx1 + 1):
            for _gy in range(_gy0, _gy1 + 1):
                grid.setdefault((_gx, _gy), []).append((_s, _e))
    return grid


def annotate(results, dxf_paths=None):
    """GB-only annotation entry. For EU drawings use dxf_annotator_eu.annotate_eu."""
    import time
    from weld_extractor import is_eu_comp

    bad = sorted({r.get('component', '') for r in results if is_eu_comp(r.get('component', ''))})
    if bad:
        raise ValueError(
            f"annotate() is GB-only; refused EU components {bad}. "
            f"Use dxf_annotator_eu.annotate_eu instead.")

    os.makedirs(ANNOTATED_DIR, exist_ok=True)

    by_comp = defaultdict(list)
    for r in results:
        by_comp[r['component']].append(r)

    if dxf_paths is None:
        import glob
        dxf_paths = sorted([f for f in glob.glob(os.path.join(FOLDER, "*.dxf")) if '(2)' not in f])

    # Only process GB drawings in this entry
    dxf_paths = [p for p in dxf_paths
                 if re.search(r'-(BE\d+|CO\d+)_', os.path.basename(p), re.I)]

    all_sampled_labels = []
    _t_all0 = time.perf_counter()

    for dxf_path in dxf_paths:
        comp_m = re.search(r'-(BE\d+|CO\d+)_', os.path.basename(dxf_path), re.I)
        if comp_m:
            comp = comp_m.group(1).upper()
        else:
            continue
        comp_full = os.path.splitext(os.path.basename(dxf_path))[0].rsplit('_', 1)[0]

        if comp not in by_comp:
            print(f"  SKIP {comp_full}: no weld data")
            continue

        # Prefer welds extracted from this exact DXF (multi-revision _00/_01).
        _base = os.path.basename(dxf_path)
        _all = by_comp[comp]
        if any(r.get('source_dxf') for r in _all):
            comp_welds = [r for r in _all if r.get('source_dxf') == _base]
        else:
            comp_welds = _all
        if not comp_welds:
            print(f"  SKIP {comp_full}: no weld data for {_base}")
            continue
        print(f"\n  [GB] Annotating {comp_full} ({len(comp_welds)} welds) → {_base}")

        try:
            doc = ezdxf.readfile(dxf_path)
        except Exception as e:
            print(f"    ERROR reading {dxf_path}: {e}")
            continue

        try:
            if hasattr(_search_placement, '_fb_seen'):
                _search_placement._fb_seen.clear()
            _t0 = time.perf_counter()
            sampled_labels = _annotate_one(doc, comp_welds)
            print(f"    annotate wall: {time.perf_counter() - _t0:.1f}s")
            all_sampled_labels.extend(sampled_labels)
            out_path = os.path.join(ANNOTATED_DIR, os.path.basename(dxf_path))
            for _retry in range(3):
                try:
                    doc.saveas(out_path)
                    break
                except OSError:
                    if _retry < 2:
                        time.sleep(0.5)
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

    print(f"  [GB] All annotate wall: {time.perf_counter() - _t_all0:.1f}s")
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
    view_bboxes, part_centroids, view_part_bboxes = _compute_view_bboxes(doc)

    # 内框 = 标注硬边界；Part 视图 bbox 仅用于空白环评分
    _outer_frame, _inner_frame = _detect_drawing_frames(doc)
    if _inner_frame:
        draw_bbox = list(_inner_frame)
    elif view_bboxes:
        _all_xs = [bb[i] for bb in view_bboxes.values() for i in (0, 2)]
        _all_ys = [bb[i] for bb in view_bboxes.values() for i in (1, 3)]
        _DRAW_MARGIN = 60
        draw_bbox = [min(_all_xs) - _DRAW_MARGIN, min(_all_ys) - _DRAW_MARGIN,
                     max(_all_xs) + _DRAW_MARGIN, max(_all_ys) + _DRAW_MARGIN]
    else:
        draw_bbox = _compute_drawing_bbox(doc)

    _global_wm_text_bboxes = []
    _global_hatch_bboxes = []
    _drawn_label_registry = []

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

    # 计算每个视图的边界（Part块用于硬阻挡，全块用于渐进惩罚）
    _other_view_part_bboxes = []   # Part-only，_has_conflict 用
    _other_view_bboxes = []        # Part+WM+Mark+SectionMark，_score_placement 用
    for _vid in view_bboxes.keys():
        _v_px, _v_py = [], []
        _v_ax, _v_ay = [], []
        for _blk in doc.blocks:
            _bn = _blk.name
            if not (_bn.startswith('Part') or _bn.startswith('WeldMark') or
                    _bn.startswith('Mark') or _bn.startswith('SectionMark')):
                continue
            if _bn.endswith(f' - {_vid}') or f' - {_vid}' in _bn:
                for _e in _blk:
                    _et = _e.dxftype()
                    if _et == 'LINE':
                        _v_ax.extend([_e.dxf.start.x, _e.dxf.end.x])
                        _v_ay.extend([_e.dxf.start.y, _e.dxf.end.y])
                        if _bn.startswith('Part'):
                            _v_px.extend([_e.dxf.start.x, _e.dxf.end.x])
                            _v_py.extend([_e.dxf.start.y, _e.dxf.end.y])
                    elif _et == 'LWPOLYLINE':
                        try:
                            for _pt in _e.get_points():
                                _v_ax.append(_pt[0]); _v_ay.append(_pt[1])
                        except: pass
                    elif _et in ('TEXT', 'MTEXT', 'ATTRIB', 'ATTDEF'):
                        try:
                            _v_ax.append(_e.dxf.insert.x); _v_ay.append(_e.dxf.insert.y)
                        except: pass
                    elif _et in ('CIRCLE', 'ARC'):
                        try:
                            _v_ax.append(_e.dxf.center.x); _v_ay.append(_e.dxf.center.y)
                        except: pass
        if _v_px:
            _other_view_part_bboxes.append((min(_v_px) - 5, min(_v_py) - 5,
                                            max(_v_px) + 5, max(_v_py) + 5))
        if _v_ax:
            _other_view_bboxes.append((min(_v_ax) - 5, min(_v_ay) - 5,
                                       max(_v_ax) + 5, max(_v_ay) + 5))

    # 检测表格区域（BOM / 材料表 / 螺栓表 / 标题栏），作为 hatch_bbox 加入阻挡
    _table_hatch = []
    _BOM_KEYWORDS = ('LENGTH', 'WIDTH', 'HEIGHT', 'WEIGHT', 'QTY', 'QUANTITY',
                     'DESCRIPTION', 'PART', 'MARK', 'MATERIAL', 'REMARK',
                     'ASSEMBLY BOLT LIST', 'BOLT LIST', 'PAY CODE', 'PAY CAT',
                     'PART LIST', 'NO.', 'DIA.', 'GRADE', 'SITE/SHOP',
                     'MEMBERS LOCATION')
    _TITLE_KEYWORDS = ('STEEL STRUCTURE DRAWING', 'PROJECT DOCUMENT',
                       'VENDOR DOCUMENT', 'DOCUMENT CLASS', 'REVISION',
                       'ISSUE FOR', 'DRAWN BY', 'CHECKED', 'APPROVED')
    for _e in doc.modelspace():
        if _e.dxftype() != 'INSERT':
            continue
        _blk = doc.blocks.get(_e.dxf.name)
        if not _blk:
            continue
        # 跳过带视图ID后缀的块（剖面标记、视图标签等）
        if re.search(r' - \d+$', _e.dxf.name):
            continue
        _tx, _ty = [], []
        _texts = []
        for _sub in _blk:
            if _sub.dxftype() == 'LINE':
                _tx.extend([_sub.dxf.start.x, _sub.dxf.end.x])
                _ty.extend([_sub.dxf.start.y, _sub.dxf.end.y])
            elif _sub.dxftype() == 'LWPOLYLINE':
                try:
                    for _v in _sub.get_points():
                        _tx.append(_v[0]); _ty.append(_v[1])
                except Exception:
                    pass
            elif _sub.dxftype() in ('TEXT', 'MTEXT', 'ATTRIB', 'ATTDEF'):
                try:
                    _ix = _sub.dxf.insert.x
                    _iy = _sub.dxf.insert.y
                    if _sub.dxftype() == 'MTEXT':
                        _th = getattr(_sub.dxf, 'char_height', 2.0)
                        _txt = (_sub.text if hasattr(_sub, 'text') else '') or ''
                    else:
                        _th = getattr(_sub.dxf, 'height', 2.0)
                        _txt = (_sub.dxf.text if hasattr(_sub.dxf, 'text') else '') or ''
                    _txt_u = _txt.upper()
                    _texts.append(_txt_u)
                    _tw = _th * max(len(_txt.strip()), 2) * 0.6
                    _tx.extend([_ix, _ix + _tw])
                    _ty.extend([_iy, _iy + _th])
                except Exception:
                    pass
        if not _tx:
            continue
        _all_text = ' '.join(_texts)
        _bom_hits = sum(1 for _kw in _BOM_KEYWORDS if _kw in _all_text)
        _title_hits = sum(1 for _kw in _TITLE_KEYWORDS if _kw in _all_text)
        _is_bom = (_bom_hits >= 2 and _title_hits == 0) or (
            _e.dxf.name.startswith('Unknown-') and _bom_hits >= 1 and _title_hits == 0)
        _is_title = _title_hits >= 1
        _is_bolt = 'BOLT LIST' in _all_text
        if _is_bom or _is_title or _is_bolt or (
                _e.dxf.name.startswith('Unknown-') and (_bom_hits >= 1 or _title_hits >= 1)):
            _m = BOM_MARGIN
            _table_hatch.append((min(_tx) - _m, max(_tx) + _m,
                                 min(_ty) - _m, max(_ty) + _m))

    # 处理每个视图
    for view_id in sorted(welds_by_view.keys(), key=lambda v: int(v) if v.isdigit() else 0):
        vw = welds_by_view[view_id]
        bbox = view_bboxes.get(view_id)
        centroids = part_centroids.get(view_id, [])
        obs_result = _collect_all_obstacles(doc, view_id, view_bbox=bbox)
        lines, text_bboxes, wm_text_bboxes, circles, hatch_bboxes = obs_result
        _global_wm_text_bboxes.extend(wm_text_bboxes)
        part_lines = (lines, text_bboxes, circles)
        hatch_bboxes = list(hatch_bboxes) if hatch_bboxes else []
        # 加入表格阻挡
        if _table_hatch:
            hatch_bboxes = hatch_bboxes + _table_hatch
        if hatch_bboxes:
            _global_hatch_bboxes.extend(hatch_bboxes)
        part_only = view_part_bboxes.get(view_id)
        _annotate_view(msp, vw, view_id, bbox, centroids, f_counter, w_counter, part_lines,
                       draw_bbox, hatch_bboxes=hatch_bboxes if hatch_bboxes else None,
                       other_view_bboxes=_other_view_bboxes, sampled_labels=sampled_labels,
                       other_view_part_bboxes=_other_view_part_bboxes if _other_view_part_bboxes else None,
                       wm_text_bboxes=wm_text_bboxes, part_bbox=bbox,
                       part_only_bbox=part_only,
                       drawn_registry=_drawn_label_registry)

    # Handle welds without view_id
    if welds_no_view:
        _annotate_welds_no_view(msp, welds_no_view, welds, f_counter, w_counter)

    _fix_global_label_overlaps(msp, _collect_part_lines(doc), draw_bbox,
                               _global_wm_text_bboxes, _drawn_label_registry,
                               hatch_bboxes=_global_hatch_bboxes)

    _enforce_inner_frame_labels(msp, draw_bbox, _collect_part_lines(doc),
                                _global_wm_text_bboxes, _drawn_label_registry,
                                hatch_bboxes=_global_hatch_bboxes)

    # Zoom modelspace view to extents including all labels
    _set_model_view_to_extents(doc)

    print(f"    F: {f_counter[0]}  W: {w_counter[0]}")
    return sampled_labels


def _clean_original_labels(doc):
    """Remove part/dimension label INSERT references to reduce annotation overlap.
    Removes Mark-, MarkSet-, StraightDimension-, AngleDimension- block inserts.
    Preserves SectionMark, WeldMark, Unknown (BOM/title block), Part blocks."""
    msp = doc.modelspace()
    _remove_prefixes = ('Mark-', 'MarkSet-', 'StraightDimension-', 'AngleDimension-', 'Cloud-', 'Text-')
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
    view_part_bboxes = {}
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
                if view_id not in view_part_bboxes:
                    view_part_bboxes[view_id] = [min(xs), min(ys), max(xs), max(ys)]
                else:
                    pbb = view_part_bboxes[view_id]
                    pbb[0] = min(pbb[0], min(xs))
                    pbb[1] = min(pbb[1], min(ys))
                    pbb[2] = max(pbb[2], max(xs))
                    pbb[3] = max(pbb[3], max(ys))
    return view_bboxes, dict(view_part_centroids), view_part_bboxes


def _annotate_view(msp, welds, view_id, bbox, part_centroids, f_counter, w_counter, obstacles, draw_bbox=None, hatch_bboxes=None, other_view_bboxes=None, sampled_labels=None, other_view_part_bboxes=None, wm_text_bboxes=None, part_bbox=None, part_only_bbox=None, drawn_registry=None):
    """Annotate all welds in a single view.
    相同位置的 Above+Below 焊缝对共用一根引线，标号并排在横线末端。CJP(W*) 和 FW(F*) 不混合。"""
    if sampled_labels is None:
        sampled_labels = []
    lines, text_bboxes, circles = obstacles
    # 扫描已有 WELD_LABELS MTEXT（跨视图重叠保护）
    cross_view_text_bboxes = []
    for e in msp:
        if e.dxftype() == 'MTEXT' and e.dxf.layer == LAYER_NAME:
            ins = e.dxf.insert
            txt = e.text.strip() if hasattr(e, 'text') else ''
            w = _label_text_width(txt, ',' in txt)
            att = getattr(e.dxf, 'attachment_point', MT_BOTTOM_RIGHT)
            if att in (MT_BOTTOM_RIGHT, MT_TOP_RIGHT, MT_MIDDLE_RIGHT):
                bx0, bx1 = ins.x - w, ins.x
            else:
                bx0, bx1 = ins.x, ins.x + w
            _eh = getattr(e.dxf, 'char_height', LABEL_HEIGHT)
            cross_view_text_bboxes.append((bx0, bx1, ins.y, ins.y + _eh))
    if wm_text_bboxes is None:
        wm_text_bboxes = []
    pos_welds = [(w, w['dxf_pos']) for w in welds if w.get('dxf_pos')]
    no_pos_welds = [w for w in welds if not w.get('dxf_pos')]
    if not pos_welds and not no_pos_welds:
        return

    # 焊缝点禁区：使标签避免覆盖焊缝原始符号区域。
    # Keep small so adjacent face tips (~4–5mm apart) do not block each other.
    _weld_exclusion_radius = 2.5
    for w, wp in pos_welds:
        circles.append((wp[0], wp[1], _weld_exclusion_radius))

    if bbox:
        vx0, vy0, vx1, vy1 = bbox[0], bbox[1], bbox[2], bbox[3]
        if pos_welds:
            xs = sorted([p[0] for _, p in pos_welds])
            ys = sorted([p[1] for _, p in pos_welds])
            n = len(xs)
            cx = (xs[0] + xs[-1]) / 2
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

    part_view_bbox = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _down_bbox = part_only_bbox if part_only_bbox else part_view_bbox
    # 空白环评分必须用 Part-only bbox，不是整视图框
    _score_part_bbox = part_only_bbox if part_only_bbox else part_view_bbox
    _view_line_grid = _build_line_grid(lines, 50)
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
    # Zigzag sector order: alternate dense/sparse to spread leader lines
    _sector_occ = [(len(_sectors[i]), i) for i in range(_N_SECTORS)]
    _sector_occ.sort(key=lambda x: x[0], reverse=True)
    _zigzag_order = []
    _half = (_N_SECTORS + 1) // 2
    for _i in range(_half):
        _zigzag_order.append(_sector_occ[_i][1])
        _j = _N_SECTORS - 1 - _i
        if _j >= _half:
            _zigzag_order.append(_sector_occ[_j][1])
    _new_order = []
    _max_len = max(len(_s) for _s in _sectors)
    for _round in range(_max_len):
        for _sid in _zigzag_order:
            if _round < len(_sectors[_sid]):
                _new_order.append(_sectors[_sid][_round])
    pos_welds = [(w, p) for _, w, p in _new_order]

    # ---- 分组：同位配对（CJP(Above)+FW(Below)跨类型可配对，同类型Above+Below配对） ----
    # Use 1-dec mm grid: integer rounding merged distinct stack welds
    # (e.g. C-C L=204 @196.8 with L=300 @197.2 → fake tip via redistribute).
    POS_TOL = 1.0
    from collections import defaultdict
    _pos_map = defaultdict(list)
    for wp in pos_welds:
        _key = (round(wp[1][0], 1), round(wp[1][1], 1))
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

    # 密集视图/局部簇：缩小字号（按组局部密度，整视图取最密作为下界）
    _group_heights, _weld_positions = _group_label_heights(groups)
    _view_h = min(_group_heights) if _group_heights else LABEL_HEIGHT
    _set_active_label_height(_view_h)

    try:
        _annotate_view_place(
            msp, groups, no_pos_welds, lines, text_bboxes, circles,
            cross_view_text_bboxes, wm_text_bboxes, hatch_bboxes,
            other_view_bboxes, other_view_part_bboxes,
            vx0, vy0, vx1, vy1, cx, cy, draw_bbox, bbox,
            _down_bbox, _score_part_bbox, _view_line_grid,
            f_counter, w_counter, sampled_labels, drawn_registry,
            group_heights=_group_heights, weld_positions=_weld_positions)
    finally:
        _set_active_label_height(LABEL_HEIGHT)


def _fix_codirectional_neighbors(placements, placed_bboxes, placed_text_bboxes,
                                   lines, text_bboxes, circles,
                                   vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                                   other_view_bboxes, other_view_part_bboxes,
                                   wm_text_bboxes, cx, cy, part_bbox, down_bbox,
                                   line_grid, cross_view_text_bboxes, prefer_by_gi):
    """Near welds with nearly same leader angle → same-half up/down diverge by weld Y.

    Higher weld → upper prefer (Q1/Q2); lower weld → lower prefer (Q4/Q3).
    Moves the worse mismatch first; does not key off label names.
    """
    n = len(placements)
    if n < 2:
        return

    def _near_angles(idx):
        pt = placements[idx][3]
        out = []
        for k in range(n):
            if k == idx:
                continue
            pk, ak = placements[k][3], placements[k][6]
            if math.hypot(pk[0] - pt[0], pk[1] - pt[1]) <= CLUSTER_RADIUS:
                out.append((pk, ak))
        return out

    def _gap_ok(ang, peer_ang, idx, require_all):
        if _angle_delta_deg(ang, peer_ang) < DIVERGE_ANGLE_MIN:
            return False
        if not require_all:
            return True
        for _pk, ak in _near_angles(idx):
            if _angle_delta_deg(ang, ak) < DIVERGE_ANGLE_MIN:
                return False
        return True

    def _band_ok(ang, prefer):
        """Assigned upper prefer expects up-band; lower prefer expects dn-band."""
        want = 'up' if _angle_delta_deg(prefer, 55) < 50 or _angle_delta_deg(prefer, 135) < 50 else 'dn'
        got = _leader_half_band(ang)
        return got == want or got == 'other'

    _dense = n > 20
    for _round in range(2 if not _dense else 1):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                pi, agi = placements[i][3], placements[i][6]
                pj, agj = placements[j][3], placements[j][6]
                if math.hypot(pi[0] - pj[0], pi[1] - pj[1]) > CLUSTER_RADIUS:
                    continue
                # 仅角差过小时分向；已充分分叉的近邻不动（防误伤重叠）
                if _angle_delta_deg(agi, agj) >= DIVERGE_ANGLE_MIN:
                    continue
                pref_i, pref_j = _diverge_prefer_angs(pi, pj, cx, cy)

                # already match Y-assigned bands with enough gap
                if (_band_ok(agi, pref_i) and _band_ok(agj, pref_j)
                        and _leader_half_band(agi) != _leader_half_band(agj)
                        and _leader_half_band(agi) != 'other'
                        and _angle_delta_deg(agi, agj) >= DIVERGE_ANGLE_MIN):
                    continue

                # move worse mismatch (higher weld should be up; lower → down)
                mism_i = _angle_delta_deg(agi, pref_i)
                mism_j = _angle_delta_deg(agj, pref_j)
                if mism_j > mism_i + 1:
                    target, other, prefer_primary = j, i, pref_j
                elif mism_i > mism_j + 1:
                    target, other, prefer_primary = i, j, pref_i
                else:
                    # tie: move the geometrically lower weld (should take dn)
                    if pi[1] <= pj[1]:
                        target, other, prefer_primary = i, j, pref_i
                    else:
                        target, other, prefer_primary = j, i, pref_j

                gt, itt, lbt, pt, ltt, dst, agt = placements[target][:7]
                go, _, _, po, _, _, ago = placements[other][:7]
                hq = _weld_home_quadrant(pt[0], pt[1], cx, cy)
                nbrs = _near_angles(target)
                nbr_angs = [a for _p, a in nbrs]
                prefer_try = [
                    prefer_primary,
                    _halfplane_complement(ago),
                    _maximin_corner_ang(nbr_angs, prefer=prefer_primary, home_q=hq),
                ]
                _half_seeds = DIVERGE_PREF_LEFT if hq in (2, 3) else DIVERGE_PREF_RIGHT
                # put assigned half's seed first among (up, dn)
                _up_s, _dn_s = _half_seeds
                if _angle_delta_deg(prefer_primary, _up_s) <= _angle_delta_deg(prefer_primary, _dn_s):
                    for a in (_up_s, _dn_s):
                        if a not in prefer_try:
                            prefer_try.append(a)
                else:
                    for a in (_dn_s, _up_s):
                        if a not in prefer_try:
                            prefer_try.append(a)
                for da in (40, -40, 55, -55):
                    prefer_try.append((prefer_primary + da) % 360)
                if _dense:
                    prefer_try = prefer_try[:6]
                is_pair = (gt == 'pair')
                others_bb = [placed_bboxes[k] for k in range(n) if k != target]
                others_tb = ([placed_text_bboxes[k] for k in range(n) if k != target]
                             + list(cross_view_text_bboxes or []))
                _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
                applied = False
                for require_all in (True, False):
                    for prefer in prefer_try:
                        _, nd, na = _search_placement(
                            pt, lines, text_bboxes, circles, others_bb, others_tb,
                            vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                            hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                            home_q=hq, quad_cx=cx, quad_cy=cy,
                            other_view_part_bboxes=other_view_part_bboxes,
                            label_text=ltt, wm_text_bboxes=wm_text_bboxes,
                            part_bbox=part_bbox,
                            prefer_down=False,
                            line_grid=line_grid, allow_adjacent=True,
                            prefer_ang=prefer,
                            neighbor_angles=nbrs,
                            max_dist=_max_len, cross_ok=False)
                        if not _gap_ok(na, ago, target, require_all):
                            found = False
                            for dist in range(PREFERRED_DIAG_MIN, min(_max_len, 36) + 1, 4):
                                for da in (0, 12, -12, 25, -25, 40, -40):
                                    cand_a = (prefer + da) % 360
                                    if not _gap_ok(cand_a, ago, target, require_all):
                                        continue
                                    if not any(_angle_in_quadrant(cand_a, q)
                                               for q in _allowed_quadrants(hq, allow_adjacent=True)):
                                        continue
                                    if not _leader_axis_ok(cand_a):
                                        continue
                                    cand_t = _text_bbox(pt, dist, cand_a, ltt, is_pair=is_pair)
                                    if draw_bbox is not None and not _text_in_inner_frame(cand_t, draw_bbox):
                                        continue
                                    if any(_text_overlaps(cand_t, otb, OVERLAP_MARGIN)
                                           for otb in others_tb):
                                        continue
                                    nd, na = dist, cand_a
                                    found = True
                                    break
                                if found:
                                    break
                            if not found:
                                continue
                        # accept only if toward assigned half when peer already there
                        if (_leader_half_band(ago) != 'other'
                                and _leader_half_band(na) == _leader_half_band(ago)
                                and _angle_delta_deg(na, ago) < DIVERGE_ANGLE_MIN + 5):
                            continue
                        if not _band_ok(na, prefer_primary) and require_all:
                            continue
                        ttbb = _text_bbox(pt, nd, na, ltt, is_pair=is_pair)
                        if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                            continue
                        if any(_text_overlaps(ttbb, otb, OVERLAP_MARGIN) for otb in others_tb):
                            continue
                        if not _text_clears_obstacles(ttbb, others_tb, wm_text_bboxes, hatch_bboxes):
                            continue
                        if not _gap_ok(na, ago, target, require_all):
                            continue
                        tnbb = (_paired_bbox(pt, nd, na, ltt) if is_pair
                                else _single_bbox(pt, nd, na, ltt))
                        placements[target] = (gt, itt, lbt, pt, ltt, nd, na, tnbb)
                        placed_bboxes[target] = (
                            min(tnbb[0], pt[0]) - 1, max(tnbb[1], pt[0]) + 1,
                            min(tnbb[2], pt[1]) - 1, max(tnbb[3], pt[1]) + 1)
                        placed_text_bboxes[target] = ttbb
                        applied = True
                        moved = True
                        break
                    if applied:
                        break
                if not applied:
                    # brute assigned prefer band
                    for nd in range(PREFERRED_DIAG_MIN, _max_len + 1, 2):
                        for da in (0, 8, -8, 15, -15, 25, -25, 35, -35):
                            na = (prefer_primary + da) % 360
                            if not any(_angle_in_quadrant(na, q) for q in
                                       _allowed_quadrants(hq, allow_adjacent=True)):
                                continue
                            if not _leader_axis_ok(na):
                                continue
                            if not _gap_ok(na, ago, target, False):
                                continue
                            if not _band_ok(na, prefer_primary):
                                continue
                            ttbb = _text_bbox(pt, nd, na, ltt, is_pair=is_pair)
                            if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                                continue
                            if any(_text_overlaps(ttbb, otb, OVERLAP_MARGIN) for otb in others_tb):
                                continue
                            if not _text_clears_obstacles(ttbb, others_tb, wm_text_bboxes, hatch_bboxes):
                                continue
                            tnbb = (_paired_bbox(pt, nd, na, ltt) if is_pair
                                    else _single_bbox(pt, nd, na, ltt))
                            placements[target] = (gt, itt, lbt, pt, ltt, nd, na, tnbb)
                            placed_bboxes[target] = (
                                min(tnbb[0], pt[0]) - 1, max(tnbb[1], pt[0]) + 1,
                                min(tnbb[2], pt[1]) - 1, max(tnbb[3], pt[1]) + 1)
                            placed_text_bboxes[target] = ttbb
                            applied = True
                            moved = True
                            break
                        if applied:
                            break
        if not moved:
            break


def _fix_inverted_label_order(placements, placed_bboxes, placed_text_bboxes,
                                lines, text_bboxes, circles,
                                vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                                other_view_bboxes, other_view_part_bboxes,
                                wm_text_bboxes, cx, cy, part_bbox, down_bbox,
                                line_grid, cross_view_text_bboxes):
    """焊点上下相对、标签 Y 颠倒时，把后放者重搜回归属象限偏好角。"""
    n = len(placements)
    for i in range(n):
        for j in range(i + 1, n):
            pi, pj = placements[i][3], placements[j][3]
            if math.hypot(pi[0] - pj[0], pi[1] - pj[1]) > CLUSTER_RADIUS * 1.8:
                continue
            dy_w = pi[1] - pj[1]
            if abs(dy_w) < 4:
                continue
            ci = (placed_text_bboxes[i][2] + placed_text_bboxes[i][3]) / 2
            cj = (placed_text_bboxes[j][2] + placed_text_bboxes[j][3]) / 2
            if (dy_w > 0 and ci >= cj - 1) or (dy_w < 0 and ci <= cj + 1):
                continue  # 顺序正常
            # 颠倒：重搜 j（后放）到其 home 偏好角
            target = j
            gt, itt, lbt, pt, ltt, dst, agt = placements[target][:7]
            is_pair = (gt == 'pair')
            hq = _weld_home_quadrant(pt[0], pt[1], cx, cy)
            prefer = {1: 55, 2: 135, 3: 225, 4: 315}.get(hq, agt)
            others_bb = [placed_bboxes[k] for k in range(n) if k != target]
            others_tb = ([placed_text_bboxes[k] for k in range(n) if k != target]
                         + list(cross_view_text_bboxes or []))
            _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
            _, nd, na = _search_placement(
                pt, lines, text_bboxes, circles, others_bb, others_tb,
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=ltt, wm_text_bboxes=wm_text_bboxes,
                part_bbox=part_bbox,
                prefer_down=_prefer_downward_weld(pt[0], pt[1], down_bbox) if down_bbox else False,
                line_grid=line_grid, allow_adjacent=True,
                prefer_ang=prefer, max_dist=_max_len, cross_ok=False)
            ttbb = _text_bbox(pt, nd, na, ltt, is_pair=is_pair)
            cy_new = (ttbb[2] + ttbb[3]) / 2
            # 接受能纠正相对顺序的位姿
            if (dy_w > 0 and cy_new < cj) or (dy_w < 0 and cy_new > cj):
                nbb = (_paired_bbox(pt, nd, na, ltt) if is_pair
                       else _single_bbox(pt, nd, na, ltt))
                placements[target] = (gt, itt, lbt, pt, ltt, nd, na, nbb)
                placed_bboxes[target] = nbb
                placed_text_bboxes[target] = ttbb


def _separate_close_text_labels(placements, placed_bboxes, placed_text_bboxes,
                                lines, text_bboxes, circles,
                                vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                                other_view_bboxes, other_view_part_bboxes,
                                wm_text_bboxes, cx, cy, part_bbox, down_bbox,
                                line_grid, cross_view_text_bboxes):
    """强制拉开文字中心过近或 AABB 重叠的标签（含 W1/W5）。"""
    n = len(placements)
    min_cy = max(_lh() * 1.5, 2.8)
    for _round in range(3):
        moved = False
        for i in range(n):
            for j in range(i + 1, n):
                tba, tbb = placed_text_bboxes[i], placed_text_bboxes[j]
                ox = max(0, min(tba[1], tbb[1]) - max(tba[0], tbb[0]))
                oy = max(0, min(tba[3], tbb[3]) - max(tba[2], tbb[2]))
                cxi = (tba[0] + tba[1]) / 2
                cyi = (tba[2] + tba[3]) / 2
                cxj = (tbb[0] + tbb[1]) / 2
                cyj = (tbb[2] + tbb[3]) / 2
                pi, pj = placements[i][3], placements[j][3]
                weld_near = math.hypot(pi[0] - pj[0], pi[1] - pj[1]) < 15.0
                x_close = abs(cxi - cxj) < _lh() * 4.0
                y_close = abs(cyi - cyj) < min_cy
                overlap = ox > 0 and oy > 0
                li = str(placements[i][4])
                lj = str(placements[j][4])
                both_w = li.startswith('W') and lj.startswith('W')
                if not (overlap or (x_close and y_close) or
                        (both_w and x_close and abs(cyi - cyj) < min_cy * 1.4) or
                        (weld_near and x_close and y_close)):
                    continue
                # 后放者重搜；近焊点 / W 对强制上下半区分向
                target = j
                gt, itt, lbt, pt, ltt, dst, agt = placements[target][:7]
                is_pair = (gt == 'pair')
                hq = _weld_home_quadrant(pt[0], pt[1], cx, cy)
                others_bb = [placed_bboxes[k] for k in range(n) if k != target]
                others_tb = ([placed_text_bboxes[k] for k in range(n) if k != target]
                             + list(cross_view_text_bboxes or []))
                _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
                # 同半区上下角：按焊点 Y（高→上角，低→下角），不用文字中心
                if hq in (1, 4):
                    _up, _dn = DIVERGE_PREF_RIGHT
                else:
                    _up, _dn = DIVERGE_PREF_LEFT
                peer_weld = placements[i][3] if target == j else placements[j][3]
                if pt[1] >= peer_weld[1] - 1e-6:
                    prefer_list = [_up, _up + 15, _up - 15, _dn - 20, agt + 40, agt - 40]
                else:
                    prefer_list = [_dn, _dn + 15, _dn - 15, _up + 20, agt + 40, agt - 40]
                if both_w or weld_near:
                    prefer_list = [prefer_list[0]] + prefer_list
                applied = False
                for prefer in prefer_list:
                    _, nd, na = _search_placement(
                        pt, lines, text_bboxes, circles, others_bb, others_tb,
                        vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                        hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                        home_q=hq, quad_cx=cx, quad_cy=cy,
                        other_view_part_bboxes=other_view_part_bboxes,
                        label_text=ltt, wm_text_bboxes=wm_text_bboxes,
                        part_bbox=part_bbox,
                        prefer_down=True,
                        line_grid=line_grid, allow_adjacent=True,
                        prefer_ang=prefer % 360, max_dist=_max_len, cross_ok=False)
                    ttbb = _text_bbox(pt, nd, na, ltt, is_pair=is_pair)
                    if _text_overlaps(ttbb, tba, OVERLAP_MARGIN):
                        continue
                    if not _text_clears_obstacles(
                            ttbb,
                            [placed_text_bboxes[k] for k in range(n)
                             if k not in (i, target)] + list(cross_view_text_bboxes or []),
                            wm_text_bboxes, hatch_bboxes):
                        continue
                    cy_new = (ttbb[2] + ttbb[3]) / 2
                    cx_new = (ttbb[0] + ttbb[1]) / 2
                    if abs(cy_new - cyi) < min_cy and abs(cx_new - cxi) < _lh() * 3.5:
                        continue
                    nbb = (_paired_bbox(pt, nd, na, ltt) if is_pair
                           else _single_bbox(pt, nd, na, ltt))
                    placements[target] = (gt, itt, lbt, pt, ltt, nd, na, nbb)
                    placed_bboxes[target] = nbb
                    placed_text_bboxes[target] = ttbb
                    applied = True
                    moved = True
                    break
                if applied:
                    continue
                # 硬拉开：按焊点 Y 取上/下角
                _force_ang = _up if pt[1] >= peer_weld[1] - 1e-6 else _dn
                for nd in range(PREFERRED_DIAG_MIN, _max_len + 1, 2):
                    for da in (0, 8, -8, 15, -15, 25, -25):
                        na = _force_ang + da
                        if not any(_angle_in_quadrant(na, q) for q in
                                   _allowed_quadrants(hq, allow_adjacent=True)):
                            continue
                        r = math.radians(na % 360)
                        if abs(math.sin(r)) < math.sin(math.radians(ANGLE_MIN)):
                            continue
                        if abs(math.cos(r)) < math.cos(math.radians(ANGLE_MAX)):
                            continue
                        ttbb = _text_bbox(pt, nd, na, ltt, is_pair=is_pair)
                        if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                            continue
                        if _text_overlaps(ttbb, tba, OVERLAP_MARGIN):
                            continue
                        if not _text_clears_obstacles(
                                ttbb,
                                [placed_text_bboxes[k] for k in range(n)
                                 if k not in (i, target)] + list(cross_view_text_bboxes or []),
                                wm_text_bboxes, hatch_bboxes):
                            continue
                        cy_new = (ttbb[2] + ttbb[3]) / 2
                        if abs(cy_new - cyi) < min_cy:
                            continue
                        nbb = (_paired_bbox(pt, nd, na, ltt) if is_pair
                               else _single_bbox(pt, nd, na, ltt))
                        placements[target] = (gt, itt, lbt, pt, ltt, nd, na, nbb)
                        placed_bboxes[target] = nbb
                        placed_text_bboxes[target] = ttbb
                        moved = True
                        applied = True
                        break
                    if applied:
                        break
        if not moved:
            break


def _relocate_text_on_geometry(placements, placed_bboxes, placed_text_bboxes,
                               lines, text_bboxes, circles,
                               vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                               other_view_bboxes, other_view_part_bboxes,
                               wm_text_bboxes, cx, cy, part_bbox, down_bbox,
                               line_grid, cross_view_text_bboxes):
    """Re-place labels whose text still sits on part geometry; prefer longer leaders."""
    n = len(placements)
    for i in range(n):
        gi, iti, lbi, pi, lti, dsi, agi = placements[i][:7]
        is_pair = (gi == 'pair')
        tbb = placed_text_bboxes[i]
        if not _text_near_lines(tbb, lines):
            continue
        hq = _weld_home_quadrant(pi[0], pi[1], cx, cy)
        others_bb = [placed_bboxes[k] for k in range(n) if k != i]
        others_tb = ([placed_text_bboxes[k] for k in range(n) if k != i]
                     + list(cross_view_text_bboxes or []))
        neighbor_angles = [(placements[k][3], placements[k][6])
                           for k in range(n) if k != i]
        _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
        # keep divergence from near neighbors: only accept poses that stay away
        def _keeps_diverge(ang):
            for npos, nang in neighbor_angles:
                if math.hypot(npos[0] - pi[0], npos[1] - pi[1]) > CLUSTER_RADIUS:
                    continue
                if _angle_delta_deg(ang, nang) < DIVERGE_ANGLE_MIN:
                    return False
            return True

        _, nd, na = _search_placement(
            pi, lines, text_bboxes, circles, others_bb, others_tb,
            vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
            hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
            home_q=hq, quad_cx=cx, quad_cy=cy,
            other_view_part_bboxes=other_view_part_bboxes,
            label_text=lti, wm_text_bboxes=wm_text_bboxes,
            part_bbox=part_bbox,
            prefer_down=False,
            line_grid=line_grid, allow_adjacent=True,
            prefer_ang=agi, neighbor_angles=neighbor_angles,
            max_dist=_max_len, cross_ok=False)
        ttbb = _text_bbox(pi, nd, na, lti, is_pair=is_pair)
        if _text_near_lines(ttbb, lines) or not _keeps_diverge(na):
            # brute: lengthen first, keep diverge from neighbors
            fixed = False
            for dist in range(max(PREFERRED_DIAG_SOFT, int(dsi) + 4), _max_len + 1, 2):
                for da in (0, 8, -8, 15, -15, 25, -25, 35, -35, 45, -45):
                    ang = (agi + da) % 360
                    r = math.radians(ang)
                    if abs(math.sin(r)) < math.sin(math.radians(ANGLE_MIN)):
                        continue
                    if abs(math.cos(r)) < math.cos(math.radians(ANGLE_MAX)):
                        continue
                    if not any(_angle_in_quadrant(ang, q)
                               for q in _allowed_quadrants(hq, allow_adjacent=True)):
                        continue
                    if not _keeps_diverge(ang):
                        continue
                    cand = _text_bbox(pi, dist, float(ang), lti, is_pair=is_pair)
                    if _text_near_lines(cand, lines):
                        continue
                    if draw_bbox is not None and not _text_in_inner_frame(cand, draw_bbox):
                        continue
                    _ov = False
                    for otb in others_tb:
                        if _text_overlaps(cand, otb, OVERLAP_MARGIN):
                            _ov = True
                            break
                    if _ov:
                        continue
                    if hatch_bboxes:
                        for htb in hatch_bboxes:
                            if _text_overlaps(cand, htb, OVERLAP_MARGIN):
                                _ov = True
                                break
                    if _ov:
                        continue
                    nd, na, ttbb = dist, float(ang), cand
                    fixed = True
                    break
                if fixed:
                    break
            if not fixed:
                continue
        elif draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
            continue
        if not _keeps_diverge(na):
            continue
        tnbb = (_paired_bbox(pi, nd, na, lti) if is_pair
                else _single_bbox(pi, nd, na, lti))
        placements[i] = (gi, iti, lbi, pi, lti, nd, na, tnbb)
        placed_bboxes[i] = (min(tnbb[0], pi[0]) - 1, max(tnbb[1], pi[0]) + 1,
                            min(tnbb[2], pi[1]) - 1, max(tnbb[3], pi[1]) + 1)
        placed_text_bboxes[i] = ttbb


def _annotate_view_place(msp, groups, no_pos_welds, lines, text_bboxes, circles,
                         cross_view_text_bboxes, wm_text_bboxes, hatch_bboxes,
                         other_view_bboxes, other_view_part_bboxes,
                         vx0, vy0, vx1, vy1, cx, cy, draw_bbox, bbox,
                         _down_bbox, _score_part_bbox, _view_line_grid,
                         f_counter, w_counter, sampled_labels, drawn_registry,
                         group_heights=None, weld_positions=None):
    """Placement + draw body for one view (runs under active label height)."""
    placed_bboxes = []          # 引线+文字整体包围盒
    placed_text_bboxes = []     # 纯文字包围盒（用于文字重叠检测）
    _placements = []
    _quadrant_used_angles = {1: [], 2: [], 3: [], 4: []}
    _placed_angles = []         # [(weld_pos, angle), ...] for diverge scoring
    _placed_leaders = []        # [(pos, dist, angle, h_land), ...] 蓝×蓝交叉检测
    if group_heights is None:
        group_heights = [_lh()] * len(groups)
    if weld_positions is None:
        weld_positions = [(items[0][1][0], items[0][1][1]) for _, items in groups]
    _prefer_by_gi, _partner_by_gi = _pair_near_groups(groups, cx, cy)

    def _text_obstacles():
        return cross_view_text_bboxes + placed_text_bboxes

    def _next_hint_for_quadrant(q, used_angles):
        a0, a1 = QUAD_ANGLE_RANGES[q]
        n = len(used_angles)
        if n == 0:
            return a1
        return a1 - (a1 - a0) * n / (n + 1)

    for _gi, (gtype, items) in enumerate(groups):
        _set_active_label_height(group_heights[_gi] if _gi < len(group_heights) else _lh())
        _prefer_ang = _prefer_by_gi.get(_gi)
        _partner = _partner_by_gi.get(_gi)
        if (_partner is not None and _partner < len(_placed_angles)
                and _gi in _prefer_by_gi):
            # partner already placed → same-half opposite
            _prefer_ang = _halfplane_complement(_placed_angles[_partner][1])
        if _prefer_ang is None and _quadrant_used_angles:
            pass  # filled below after home_q known
        _diverge = _gi in _prefer_by_gi
        if gtype == 'pair':
            ww_a, wp_a = items[0]
            ww_b, wp_b = items[1]
            labels = [_next_label(ww_a, f_counter, w_counter),
                      _next_label(ww_b, f_counter, w_counter)]
            _label_txt = _placement_label_text('pair', labels)
            # prefer 与近邻已放角度过近 → 同半面 maximin 扇出
            _home_q = _weld_home_quadrant(wp_a[0], wp_a[1], cx, cy)
            if _prefer_ang is not None and _placed_angles:
                _nangs = []
                _confl = False
                for npos, nang in _placed_angles:
                    nd = math.hypot(npos[0] - wp_a[0], npos[1] - wp_a[1])
                    if nd <= CLUSTER_RADIUS and nd > 0.5:
                        _nangs.append(nang)
                        if _angle_delta_deg(_prefer_ang, nang) < DIVERGE_ANGLE_MIN:
                            _confl = True
                if _confl:
                    _prefer_ang = _maximin_corner_ang(
                        _nangs, prefer=_halfplane_complement(_nangs[0]),
                        home_q=_home_q)
                    _diverge = True
            _prefer_down = _prefer_downward_weld(wp_a[0], wp_a[1], _down_bbox)
            _dense_q = len(_quadrant_used_angles.get(_home_q, [])) >= 2
            if _prefer_ang is None and _quadrant_used_angles.get(_home_q):
                _prefer_ang = _next_hint_for_quadrant(_home_q, _quadrant_used_angles[_home_q])
            _allow_adj = _dense_q or _diverge
            # 仅近距成对时才 prefer_down；孤立底点不强制朝下（如 W5 应朝上）
            _has_near = any(
                0.5 < math.hypot(npos[0] - wp_a[0], npos[1] - wp_a[1]) <= CLUSTER_RADIUS
                for npos, _na in _placed_angles)
            _pd_use = _prefer_down and not _diverge and _has_near
            _, diag_len, angle = _search_placement(
                wp_a, lines, text_bboxes, circles, placed_bboxes,
                _text_obstacles(), vx0, vy0, vx1, vy1, draw_bbox, is_pair=True,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=_home_q, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=_label_txt, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_score_part_bbox, prefer_down=_pd_use,
                line_grid=_view_line_grid, allow_adjacent=_allow_adj,
                prefer_ang=_prefer_ang, neighbor_angles=_placed_angles,
                cross_ok=False, placed_leaders=_placed_leaders)
            if _pd_use:
                diag_len, angle = _maybe_retry_downward_placement(
                    wp_a, diag_len, angle, _label_txt, True,
                    lines, text_bboxes, circles, placed_bboxes, _text_obstacles(),
                    vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
                    _home_q, cx, cy, other_view_part_bboxes, wm_text_bboxes,
                    _score_part_bbox, True, line_grid=_view_line_grid)
            bx0, bx1, by0, by1 = _paired_bbox(wp_a, diag_len, angle, _label_txt)
            bbox = (min(bx0, wp_a[0])-1, max(bx1, wp_a[0])+1,
                    min(by0, wp_a[1])-1, max(by1, wp_a[1])+1)
            _placements.append((gtype, items, labels, wp_a, _label_txt, diag_len, angle, bbox))
            placed_bboxes.append(bbox)
            placed_text_bboxes.append(_text_bbox(wp_a, diag_len, angle, _label_txt, is_pair=True))
            _placed_angles.append((wp_a, angle))
            _placed_leaders.append(
                _leader_entry(wp_a, diag_len, angle, _label_txt, True))
        else:
            ww, wp = items[0]
            label = _next_label(ww, f_counter, w_counter)
            _label_txt = label
            _home_q = _weld_home_quadrant(wp[0], wp[1], cx, cy)
            if _prefer_ang is not None and _placed_angles:
                _nangs = []
                _confl = False
                for npos, nang in _placed_angles:
                    nd = math.hypot(npos[0] - wp[0], npos[1] - wp[1])
                    if nd <= CLUSTER_RADIUS and nd > 0.5:
                        _nangs.append(nang)
                        if _angle_delta_deg(_prefer_ang, nang) < DIVERGE_ANGLE_MIN:
                            _confl = True
                if _confl:
                    _prefer_ang = _maximin_corner_ang(
                        _nangs, prefer=_halfplane_complement(_nangs[0]),
                        home_q=_home_q)
                    _diverge = True
            _prefer_down = _prefer_downward_weld(wp[0], wp[1], _down_bbox)
            _dense_q = len(_quadrant_used_angles.get(_home_q, [])) >= 2
            if _prefer_ang is None and _quadrant_used_angles.get(_home_q):
                _prefer_ang = _next_hint_for_quadrant(_home_q, _quadrant_used_angles[_home_q])
            _allow_adj = _dense_q or _diverge
            _has_near = any(
                0.5 < math.hypot(npos[0] - wp[0], npos[1] - wp[1]) <= CLUSTER_RADIUS
                for npos, _na in _placed_angles)
            _pd_use = _prefer_down and not _diverge and _has_near
            _, diag_len, angle = _search_placement(
                wp, lines, text_bboxes, circles, placed_bboxes,
                _text_obstacles(), vx0, vy0, vx1, vy1, draw_bbox,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=_home_q, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=_label_txt, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_score_part_bbox, prefer_down=_pd_use,
                line_grid=_view_line_grid, allow_adjacent=_allow_adj,
                prefer_ang=_prefer_ang, neighbor_angles=_placed_angles,
                cross_ok=False, placed_leaders=_placed_leaders)
            if _pd_use:
                diag_len, angle = _maybe_retry_downward_placement(
                    wp, diag_len, angle, _label_txt, False,
                    lines, text_bboxes, circles, placed_bboxes, _text_obstacles(),
                    vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
                    _home_q, cx, cy, other_view_part_bboxes, wm_text_bboxes,
                    _score_part_bbox, True, line_grid=_view_line_grid)
            bx0, bx1, by0, by1 = _single_bbox(wp, diag_len, angle, _label_txt)
            bbox = (min(bx0, wp[0])-1, max(bx1, wp[0])+1,
                    min(by0, wp[1])-1, max(by1, wp[1])+1)
            _placements.append((gtype, items, [label], wp, _label_txt, diag_len, angle, bbox))
            placed_bboxes.append(bbox)
            placed_text_bboxes.append(_text_bbox(wp, diag_len, angle, _label_txt, is_pair=False))
            _placed_angles.append((wp, angle))
            _placed_leaders.append(
                _leader_entry(wp, diag_len, angle, _label_txt, False))

        _quadrant_used_angles.setdefault(_home_q, []).append(angle)

    # 同向近邻：按焊点高低同半区上下分向（高→Q1/Q2，低→Q4/Q3）
    _fix_codirectional_neighbors(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes, _prefer_by_gi)

    # 上下焊点标签 Y 颠倒 → 重搜回归属象限
    _fix_inverted_label_order(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes)

    # 近距文字强制分槽
    _separate_close_text_labels(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes)

    # 拥挤/过长：重搜进两视图间走廊空白（图示 C-C↔邻视图缝）
    _relocate_into_corridor(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _view_line_grid, cross_view_text_bboxes)

    # 跨半区超长浅线：同侧改角重搜
    _fix_overlong_crossing_leaders(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _view_line_grid, cross_view_text_bboxes)

    # 孤立底点标注：优先翻到朝上（W5 类）
    _flip_isolated_bottom_labels_up(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _view_line_grid, cross_view_text_bboxes)

    # 过长斜线同角缩短（文字向焊点靠拢；保 WM/hatch）
    _shorten_long_same_angle(
        _placements, placed_bboxes, placed_text_bboxes,
        draw_bbox, cross_view_text_bboxes,
        wm_text_bboxes=wm_text_bboxes, hatch_bboxes=hatch_bboxes)

    # 冲突解决按较大字号估框（更保守）；绘制时再按组恢复真实字号
    _set_active_label_height(max(group_heights) if group_heights else LABEL_HEIGHT)
    for _ri, pd in enumerate(_placements):
        gk, _, _, pk, ltk, dsk, agk = pd[:7]
        _set_active_label_height(group_heights[_ri] if _ri < len(group_heights) else _lh())
        placed_text_bboxes[_ri] = _text_bbox(
            pk, dsk, agk, ltk, is_pair=(gk == 'pair'))
    # 用最大字号做后续避让，避免小号字导致 W1/W5 漏检
    _set_active_label_height(max(group_heights) if group_heights else LABEL_HEIGHT)
    for _ri, pd in enumerate(_placements):
        gk, _, _, pk, ltk, dsk, agk = pd[:7]
        placed_text_bboxes[_ri] = _text_bbox(
            pk, dsk, agk, ltk, is_pair=(gk == 'pair'))

    _assign_y_slots(_placements, placed_text_bboxes, placed_bboxes,
                    lines, text_bboxes, wm_text_bboxes, circles,
                    vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                    other_view_bboxes, cx, cy, other_view_part_bboxes,
                    cross_view_text_bboxes, _score_part_bbox, _down_bbox)

    # y 分槽后再拉开一次近距文字
    _separate_close_text_labels(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes)

    # ---- 全局后处理：冲突解决 ----
    _n_fix1 = _resolve_label_conflicts(msp, lines, text_bboxes, circles,
                             vx0, vy0, vx1, vy1, draw_bbox, _placements, placed_text_bboxes, 5,
                             hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                             quad_cx=cx, quad_cy=cy,
                             other_view_part_bboxes=other_view_part_bboxes,
                             cross_view_text_bboxes=cross_view_text_bboxes,
                             wm_text_bboxes=wm_text_bboxes, part_bbox=_score_part_bbox)

    # ---- 最终清理：检测并微调残留重叠（含中心过近）----
    _cln_did = False
    for _cln_iter in range(3):
        _any_cln = False
        for ki in range(len(_placements)):
            for kj in range(ki + 1, len(_placements)):
                tba, tbb = placed_text_bboxes[ki], placed_text_bboxes[kj]
                ox = max(0, min(tba[1], tbb[1]) - max(tba[0], tbb[0]))
                oy = max(0, min(tba[3], tbb[3]) - max(tba[2], tbb[2]))
                cxi = (tba[0] + tba[1]) / 2
                cyi = (tba[2] + tba[3]) / 2
                cxj = (tbb[0] + tbb[1]) / 2
                cyj = (tbb[2] + tbb[3]) / 2
                _near = (abs(cxi - cxj) < _lh() * 3.5 and
                         abs(cyi - cyj) < _lh() * 1.35)
                _li = str(_placements[ki][4])
                _lj = str(_placements[kj][4])
                _w_near = (_li.startswith('W') and _lj.startswith('W') and
                           abs(cxi - cxj) < _lh() * 4 and abs(cyi - cyj) < _lh() * 2.2)
                if ox <= 0 or oy <= 0:
                    if not (_near or _w_near):
                        continue
                for target in [kj, ki]:
                    gk, itk, lbk, pk, ltk, dsk, agk = _placements[target][:7]
                    hqk = _weld_home_quadrant(pk[0], pk[1], cx, cy)
                    _pd_k = _prefer_downward_weld(pk[0], pk[1], _down_bbox)
                    _allowed_k = _allowed_quadrants(hqk, allow_adjacent=True)
                    _max_k = MAX_DIAG_LEN_PAIR if gk == 'pair' else MAX_DIAG_LEN
                    for d_dist in [4, 8, 12, 16, 20, 24, 28, 32, 36, -4, -8, -12, -16]:
                        nd = dsk + d_dist
                        if nd < 6 or nd > _max_k: continue
                        for d_a in [-25, -20, -15, -10, -5, 0, 5, 10, 15, 20, 25, 35, -35]:
                            na = agk + d_a
                            # 角度有效性检查：拒绝过水平/过垂直及越界象限
                            r5 = math.radians(na % 360)
                            if abs(math.sin(r5)) < math.sin(math.radians(ANGLE_MIN)): continue
                            if abs(math.cos(r5)) < math.cos(math.radians(ANGLE_MAX)): continue
                            if not any(_angle_in_quadrant(na, q) for q in _allowed_k): continue
                            tnbb = (_paired_bbox(pk, nd, na, ltk) if gk == 'pair'
                                    else _single_bbox(pk, nd, na, ltk))
                            ttbb = _text_bbox(pk, nd, na, ltk, is_pair=(gk == 'pair'))
                            _ck = True
                            for kk in range(len(_placements)):
                                if kk == target: continue
                                otb = placed_text_bboxes[kk]
                                if not (ttbb[1] < otb[0] - OVERLAP_MARGIN or ttbb[0] > otb[1] + OVERLAP_MARGIN or
                                        ttbb[3] < otb[2] - OVERLAP_MARGIN or ttbb[2] > otb[3] + OVERLAP_MARGIN):
                                    _ck = False; break
                                # 中心距也要拉开
                                _ocx = (otb[0] + otb[1]) / 2
                                _ocy = (otb[2] + otb[3]) / 2
                                _ncx = (ttbb[0] + ttbb[1]) / 2
                                _ncy = (ttbb[2] + ttbb[3]) / 2
                                if (abs(_ncx - _ocx) < _lh() * 3.0 and
                                        abs(_ncy - _ocy) < _lh() * 1.2):
                                    _ck = False; break
                            if not _ck: continue
                            for otb in cross_view_text_bboxes:
                                if not (ttbb[1] < otb[0] - OVERLAP_MARGIN or ttbb[0] > otb[1] + OVERLAP_MARGIN or
                                        ttbb[3] < otb[2] - OVERLAP_MARGIN or ttbb[2] > otb[3] + OVERLAP_MARGIN):
                                    _ck = False; break
                            if not _ck: continue
                            for (tx0, tx1, ty0, ty1) in text_bboxes:
                                if not (ttbb[1] < tx0 - OVERLAP_MARGIN or ttbb[0] > tx1 + OVERLAP_MARGIN or
                                        ttbb[3] < ty0 - OVERLAP_MARGIN or ttbb[2] > ty1 + OVERLAP_MARGIN):
                                    _ck = False; break
                            if not _ck: continue
                            for (tx0, tx1, ty0, ty1) in (wm_text_bboxes or []):
                                if _text_overlaps(ttbb, (tx0, tx1, ty0, ty1), WM_TEXT_MARGIN):
                                    _ck = False; break
                            if not _ck: continue
                            if hatch_bboxes:
                                for (hx0, hx1, hy0, hy1) in hatch_bboxes:
                                    if _text_overlaps(ttbb, (hx0, hx1, hy0, hy1), OVERLAP_MARGIN):
                                        _ck = False; break
                            if not _ck: continue
                            if _text_near_lines(ttbb, lines):
                                _ck = False; continue
                            if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                                _ck = False; continue
                            _placements[target] = (gk, itk, lbk, pk, ltk, nd, na, tnbb)
                            placed_text_bboxes[target] = ttbb
                            _any_cln = True; break
                        if _any_cln: break
                    if _any_cln: break
                if _any_cln: break
            if _any_cln: break
        if _any_cln:
            _cln_did = True
        else:
            break

    # ---- 二次冲突解决：首次零修复且清理无改动则跳过 ----
    if _n_fix1 > 0 or _cln_did:
        _resolve_label_conflicts(msp, lines, text_bboxes, circles,
                                 vx0, vy0, vx1, vy1, draw_bbox, _placements, placed_text_bboxes, 4,
                                 hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                                 quad_cx=cx, quad_cy=cy,
                                 other_view_part_bboxes=other_view_part_bboxes,
                                 cross_view_text_bboxes=cross_view_text_bboxes,
                                 wm_text_bboxes=wm_text_bboxes, part_bbox=_score_part_bbox)

    # ---- 绘制前：压构件文字优先加长引线重放 ----
    _relocate_text_on_geometry(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes)

    # 绘制前最后一次拉开近距/W 重叠
    _set_active_label_height(max(group_heights) if group_heights else LABEL_HEIGHT)
    for _ri, pd in enumerate(_placements):
        gk, _, _, pk, ltk, dsk, agk = pd[:7]
        placed_text_bboxes[_ri] = _text_bbox(
            pk, dsk, agk, ltk, is_pair=(gk == 'pair'))
        placed_bboxes[_ri] = (_paired_bbox(pk, dsk, agk, ltk) if gk == 'pair'
                              else _single_bbox(pk, dsk, agk, ltk))
    _separate_close_text_labels(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes)

    # 绘制前：再做一次同向分向 + 同角缩短（resolve/分槽后回锁）
    _fix_codirectional_neighbors(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes, _prefer_by_gi)
    _relocate_into_corridor(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _view_line_grid, cross_view_text_bboxes)
    _fix_overlong_crossing_leaders(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _view_line_grid, cross_view_text_bboxes)
    _flip_isolated_bottom_labels_up(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _view_line_grid, cross_view_text_bboxes)
    _shorten_long_same_angle(
        _placements, placed_bboxes, placed_text_bboxes,
        draw_bbox, cross_view_text_bboxes,
        wm_text_bboxes=wm_text_bboxes, hatch_bboxes=hatch_bboxes)

    # 绘制前最终蓝×蓝浅角交叉修复（后处理可能重新引入 ≤45° 交叉）
    _fix_shallow_blue_leader_crosses(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        cross_view_text_bboxes)
    # 绘制前：硬禁区（剖面标题 D-D/1:10、WM 文字）上的标签强制挪开
    _push_labels_off_hard_zones(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        cross_view_text_bboxes)

    # ---- 绘制所有标注（绘制前内框闸门；禁止仅保框而压线）----
    for _pi, pd in enumerate(_placements):
        gtype, items, labels, pos, dname, diag_len, angle = pd[:7]
        _hq_draw = _weld_home_quadrant(pos[0], pos[1], cx, cy)
        # 同半区邻象限（Q1↔Q4 / Q2↔Q3）合法：只钳轴带，勿按单 home 吸附
        if _leader_axis_ok(angle) and any(
                _angle_in_quadrant(angle, q)
                for q in _allowed_quadrants(_hq_draw, allow_adjacent=True)):
            angle = angle % 360
        else:
            angle = _snap_leader_angle(angle, _hq_draw)
        _placements[_pi] = (gtype, items, labels, pos, dname, diag_len, angle, pd[7] if len(pd) > 7 else None)
        _set_active_label_height(group_heights[_pi] if _pi < len(group_heights) else _lh())
        _is_pair = (gtype == 'pair')
        _tbb_pre = _text_bbox(pos, diag_len, angle, dname, is_pair=_is_pair)
        _frame_bad = (draw_bbox is not None and not _text_in_inner_frame(_tbb_pre, draw_bbox))
        _near_geo = _text_near_lines(_tbb_pre, lines)
        _need_fix = _frame_bad or _near_geo
        if _need_fix:
            _hq = _weld_home_quadrant(pos[0], pos[1], cx, cy)
            _old_ang, _old_diag = angle, diag_len
            _nbr = [(_placements[k][3], _placements[k][6])
                    for k in range(len(_placements)) if k != _pi]
            _, diag_len, angle = _search_placement(
                pos, lines, text_bboxes, circles,
                [p[7] for k, p in enumerate(_placements) if k != _pi],
                [otb for k, otb in enumerate(placed_text_bboxes) if k != _pi],
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=_is_pair,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=_hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=dname, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_score_part_bbox, prefer_down=False,
                max_dist=(MAX_DIAG_LEN_PAIR if _is_pair else MAX_DIAG_LEN),
                allow_adjacent=True, prefer_ang=_old_ang, neighbor_angles=_nbr,
                cross_ok=False)
            # 已同角缩短：勿因 near_lines 被搜成更长同角
            if (not _frame_bad and _old_diag <= PREFERRED_DIAG_SOFT
                    and _angle_delta_deg(angle, _old_ang) <= 8
                    and diag_len > _old_diag + 2):
                diag_len, angle = _old_diag, _old_ang
            # 勿把近邻分向结果合并成同向
            _collapsed = any(
                math.hypot(npos[0] - pos[0], npos[1] - pos[1]) <= CLUSTER_RADIUS
                and _angle_delta_deg(angle, nang) < DIVERGE_ANGLE_MIN
                for npos, nang in _nbr)
            # 也勿破坏已按焊点 Y 拆开的同半区上下带
            if not _collapsed:
                for npos, nang in _nbr:
                    if math.hypot(npos[0] - pos[0], npos[1] - pos[1]) > CLUSTER_RADIUS:
                        continue
                    pref_self, pref_nbr = _diverge_prefer_angs(pos, npos, cx, cy)
                    if (_leader_half_band(_old_ang) != _leader_half_band(nang)
                            and _leader_half_band(_old_ang) != 'other'
                            and _leader_half_band(angle) == _leader_half_band(nang)):
                        diag_len, angle = _old_diag, _old_ang
                        _collapsed = True
                        break
            if _collapsed:
                diag_len, angle = _old_diag, _old_ang
                # try lengthen in place to clear geometry without changing direction
                _max_k = MAX_DIAG_LEN_PAIR if _is_pair else MAX_DIAG_LEN
                for _nd in range(int(_old_diag) + 4, _max_k + 1, 4):
                    _tb = _text_bbox(pos, _nd, _old_ang, dname, is_pair=_is_pair)
                    if _text_near_lines(_tb, lines):
                        continue
                    if draw_bbox is not None and not _text_in_inner_frame(_tb, draw_bbox):
                        continue
                    diag_len = _nd
                    break
            _tbb_pre = _text_bbox(pos, diag_len, angle, dname, is_pair=_is_pair)
            # 仅内框回退：候选也不压线、且不破坏分向时才采用
            if draw_bbox is not None and not _text_in_inner_frame(_tbb_pre, draw_bbox):
                _fp = _shortest_in_frame_pose(
                    pos, dname, draw_bbox, is_pair=_is_pair, home_q=_hq, prefer_ang=angle)
                if _fp is not None:
                    _fd, _fa = _fp
                    _tbb_fp = _text_bbox(pos, _fd, _fa, dname, is_pair=_is_pair)
                    _fp_collapse = any(
                        math.hypot(npos[0] - pos[0], npos[1] - pos[1]) <= CLUSTER_RADIUS
                        and _angle_delta_deg(_fa, nang) < DIVERGE_ANGLE_MIN
                        for npos, nang in _nbr)
                    if not _text_near_lines(_tbb_fp, lines) and not _fp_collapse:
                        diag_len, angle = _fd, _fa
            if _is_pair:
                nbb = _paired_bbox(pos, diag_len, angle, dname)
            else:
                nbb = _single_bbox(pos, diag_len, angle, dname)
            _placements[_pi] = (gtype, items, labels, pos, dname, diag_len, angle, nbb)
            placed_text_bboxes[_pi] = _text_bbox(pos, diag_len, angle, dname, is_pair=_is_pair)
        # 绘制前最终钳角：仅保证轴带；同半区邻象限（Q1/Q4）保留
        _hq_final = _weld_home_quadrant(pos[0], pos[1], cx, cy)
        if _leader_axis_ok(angle) and any(
                _angle_in_quadrant(angle, q)
                for q in _allowed_quadrants(_hq_final, allow_adjacent=True)):
            angle = angle % 360
        else:
            angle = _snap_leader_angle(angle, _hq_final)
        _smp = items[0][0].get('_sampled', False)
        _short_tips = None
        for _it in items:
            _cand = _it[0].get('_eu_u_short_tips')
            if _cand and len(_cand) >= 2:
                _short_tips = _cand
                break
        if gtype == 'pair':
            _pd = _prefer_downward_weld(pos[0], pos[1], _down_bbox)
            if _short_tips:
                meta = _draw_branched_paired_weld_label(
                    msp, labels, _short_tips, dname, diag_len, angle, sampled=_smp)
            else:
                meta = _draw_paired_weld_label(
                    msp, labels, pos, dname, diag_len, angle, sampled=_smp)
        else:
            _pd = _prefer_downward_weld(pos[0], pos[1], _down_bbox)
            meta = _draw_weld_label(msp, labels[0], pos, dname, diag_len, angle, sampled=_smp)
        if meta:
            meta['prefer_down'] = _pd
        if drawn_registry is not None and meta:
            drawn_registry.append(meta)
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


def _part_bottom_band_weld(wx, wy, part_bbox, band_ratio=0.72):
    """焊点是否位于当前 Part 视图靠下区域（主视图底边围焊，排除 h<25 的小视图）。"""
    if not part_bbox:
        return False
    px0, py0, px1, py1 = part_bbox
    h = py1 - py0
    if h < 25:
        return False
    if py0 < 80:
        return False
    if not (px0 - 10 <= wx <= px1 + 10):
        return False
    return wy <= py0 + h * band_ratio


def _relocate_into_corridor(placements, placed_bboxes, placed_text_bboxes,
                            lines, text_bboxes, circles,
                            vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                            other_view_bboxes, other_view_part_bboxes,
                            wm_text_bboxes, cx, cy, part_bbox,
                            line_grid, cross_view_text_bboxes):
    """Crowded / overlong / not-in-gap labels: re-search into inter-view gap_box."""
    if not part_bbox:
        return
    n = len(placements)
    _ov = other_view_part_bboxes if other_view_part_bboxes else other_view_bboxes

    def _in_gap_text(tbb, gbox):
        if not gbox:
            return False
        tcx = (tbb[0] + tbb[1]) / 2
        tcy = (tbb[2] + tbb[3]) / 2
        return gbox[0] <= tcx <= gbox[2] and gbox[1] <= tcy <= gbox[3]

    for i in range(n):
        gi, iti, lbi, pi, lti, dsi, agi = placements[i][:7]
        _, gap_ang, gap_box = _corridor_info(pi[0], pi[1], part_bbox, _ov or [], home_q=None)
        if gap_box is None or gap_ang is None:
            continue
        tbb = placed_text_bboxes[i]
        already = _in_gap_text(tbb, gap_box)
        tcx = (tbb[0] + tbb[1]) / 2
        gap_right = gap_box[0] >= pi[0] - 2
        gap_left = gap_box[2] <= pi[0] + 2
        wrong_side = (gap_right and tcx < pi[0] - 2) or (gap_left and tcx > pi[0] + 2)
        crowded = False
        for k in range(n):
            if k == i:
                continue
            if _text_overlaps(tbb, placed_text_bboxes[k], OVERLAP_MARGIN):
                crowded = True
                break
        longish = dsi >= PREFERRED_DIAG_HARD
        if already and not crowded and not longish:
            continue
        if already and not crowded and not wrong_side:
            continue
        if not (crowded or longish or wrong_side or not already):
            continue
        is_pair = (gi == 'pair')
        hq = _weld_home_quadrant(pi[0], pi[1], cx, cy)
        others_bb = [placed_bboxes[k] for k in range(n) if k != i]
        others_tb = ([placed_text_bboxes[k] for k in range(n) if k != i]
                     + list(cross_view_text_bboxes or []))
        nbrs = [(placements[k][3], placements[k][6]) for k in range(n) if k != i]
        _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
        _, nd, na = _search_placement(
            pi, lines, text_bboxes, circles, others_bb, others_tb,
            vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
            hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
            home_q=hq, quad_cx=cx, quad_cy=cy,
            other_view_part_bboxes=other_view_part_bboxes,
            label_text=lti, wm_text_bboxes=wm_text_bboxes,
            part_bbox=part_bbox, prefer_down=False,
            line_grid=line_grid, allow_adjacent=True,
            prefer_ang=gap_ang, neighbor_angles=nbrs,
            max_dist=_max_len, cross_ok=False)
        ttbb = _text_bbox(pi, nd, na, lti, is_pair=is_pair)
        if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
            continue
        if not _text_clears_obstacles(ttbb, others_tb, wm_text_bboxes, hatch_bboxes):
            continue
        new_in = _in_gap_text(ttbb, gap_box)
        ntcx = (ttbb[0] + ttbb[1]) / 2
        new_side_ok = (gap_right and ntcx >= pi[0] - 1) or (gap_left and ntcx <= pi[0] + 1) or new_in
        if not new_in and not crowded and not (wrong_side and new_side_ok):
            continue
        if already and not new_in and not (wrong_side and new_side_ok and nd <= dsi + 4):
            continue
        better = ((new_in and not already)
                  or (wrong_side and new_side_ok)
                  or (crowded and nd <= dsi)
                  or (longish and nd < dsi - 2))
        if not better:
            continue
        nbb = (_paired_bbox(pi, nd, na, lti) if is_pair
               else _single_bbox(pi, nd, na, lti))
        placements[i] = (gi, iti, lbi, pi, lti, nd, na, nbb)
        placed_bboxes[i] = nbb
        placed_text_bboxes[i] = ttbb


def _prefer_downward_weld(wx, wy, part_bbox):
    """大视图 Part 靠下围焊：仅作近距成对时的轻推信号；放置侧另受近邻门控。"""
    return _part_bottom_band_weld(wx, wy, part_bbox)


def _flip_isolated_bottom_labels_up(placements, placed_bboxes, placed_text_bboxes,
                                    lines, text_bboxes, circles,
                                    vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                                    other_view_bboxes, other_view_part_bboxes,
                                    wm_text_bboxes, cx, cy, part_bbox,
                                    line_grid, cross_view_text_bboxes):
    """Isolated (or only peer-below) bottom-half labels pointing down → try up.

    Avoids stacking everything into Q3/Q4 for flange welds like W5.
    """
    n = len(placements)
    for i in range(n):
        gi, iti, lbi, pi, lti, dsi, agi = placements[i][:7]
        if pi[1] >= cy - 2:
            continue
        if _leader_half_band(agi) != 'dn':
            continue
        is_pair = (gi == 'pair')
        hq = _weld_home_quadrant(pi[0], pi[1], cx, cy)
        others_bb = [placed_bboxes[k] for k in range(n) if k != i]
        others_tb = ([placed_text_bboxes[k] for k in range(n) if k != i]
                     + list(cross_view_text_bboxes or []))
        nbrs = [(placements[k][3], placements[k][6]) for k in range(n) if k != i]
        _max_len = min(MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN,
                       int(PREFERRED_DIAG_HARD))
        prefers = ([55.0, 45.0, 65.0, 35.0] if hq in (1, 4)
                   else [135.0, 145.0, 125.0, 155.0])
        for prefer in prefers:
            _, nd, na = _search_placement(
                pi, lines, text_bboxes, circles, others_bb, others_tb,
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=lti, wm_text_bboxes=wm_text_bboxes,
                part_bbox=part_bbox, prefer_down=False,
                line_grid=line_grid, allow_adjacent=True,
                prefer_ang=prefer, neighbor_angles=nbrs,
                max_dist=_max_len, cross_ok=False)
            if _leader_half_band(na) != 'up':
                continue
            ttbb = _text_bbox(pi, nd, na, lti, is_pair=is_pair)
            if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                continue
            if not _text_clears_obstacles(ttbb, others_tb, wm_text_bboxes, hatch_bboxes):
                continue
            if any(math.hypot(npos[0] - pi[0], npos[1] - pi[1]) <= CLUSTER_RADIUS
                   and _angle_delta_deg(na, nang) < DIVERGE_ANGLE_MIN
                   for npos, nang in nbrs):
                continue
            nbb = (_paired_bbox(pi, nd, na, lti) if is_pair
                   else _single_bbox(pi, nd, na, lti))
            placements[i] = (gi, iti, lbi, pi, lti, nd, na, nbb)
            placed_bboxes[i] = nbb
            placed_text_bboxes[i] = ttbb
            break


def _downward_quad_same_half(home_q):
    """同半区内的向下象限：右半→Q4，左半→Q3。"""
    return 4 if home_q in (1, 4) else 3


def _label_below_weld(pos, dist, angle, label_text, is_pair=False):
    tbb = _text_bbox(pos, dist, angle, label_text, is_pair=is_pair)
    cy = (tbb[2] + tbb[3]) / 2
    return cy < pos[1] - 0.5


def _maybe_retry_downward_placement(weld_pos, diag_len, angle, label_text, is_pair,
                                    lines, text_bboxes, circles, placed_bboxes,
                                    placed_text_obstacles, vx0, vy0, vx1, vy1, draw_bbox,
                                    hatch_bboxes, other_view_bboxes, home_q, cx, cy,
                                    other_view_part_bboxes, wm_text_bboxes, part_bbox,
                                    prefer_down, line_grid=None):
    """Part 下半区围焊：若文字仍在焊点上方，在同半区向下象限重搜一次。"""
    if not prefer_down or _label_below_weld(weld_pos, diag_len, angle, label_text, is_pair):
        return diag_len, angle
    _dq = _downward_quad_same_half(home_q)
    _, nd, na = _search_placement(
        weld_pos, lines, text_bboxes, circles, placed_bboxes, placed_text_obstacles,
        vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
        hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
        home_q=_dq, quad_cx=cx, quad_cy=cy,
        other_view_part_bboxes=other_view_part_bboxes,
        label_text=label_text, wm_text_bboxes=wm_text_bboxes,
        part_bbox=part_bbox, prefer_down=True, line_grid=line_grid)
    if _label_below_weld(weld_pos, nd, na, label_text, is_pair):
        return nd, na
    return diag_len, angle


def _pos_home_quadrant(px, py, vcx, vcy):
    return _weld_home_quadrant(px, py, vcx, vcy)


def _allowed_quadrants(home_q, allow_adjacent=False, cross_ok=False):
    if cross_ok:
        return {1, 2, 3, 4}
    if allow_adjacent:
        _half = {1: {1, 4}, 2: {2, 3}, 3: {2, 3}, 4: {1, 4}}
        _adj = {1: {1, 2, 4}, 2: {2, 1, 3}, 3: {3, 2, 4}, 4: {4, 1, 3}}
        return _adj.get(home_q, {home_q}) & _half.get(home_q, {home_q})
    return {home_q}


def _corridor_info(wx, wy, part_bbox, other_view_bboxes, home_q=None):
    """Inter-view empty corridor: prefer angle into gap.

    Returns (side_hint_quads, prefer_ang, gap_box). Prefer ang aims into the
    gap strip (may leave home_q) so tip/text can land in blank between views.
    """
    extras = set()
    best_ang, best_gap, best_box = None, 0.0, None
    if not part_bbox or not other_view_bboxes:
        return extras, None, None
    px0, py0, px1, py1 = part_bbox

    def _cands_for(right_side):
        # Prefer angles whose tip can enter the vertical corridor strip
        if right_side:
            return [35, 45, 55, 25, 65, 315, 325, 305, 15, 350]
        return [145, 135, 155, 125, 225, 215, 235, 165, 115]

    def _pick_ang(gx_lo, gx_hi, y_lo, y_hi, right_side):
        cands = _cands_for(right_side)
        dists = [18.0, 22.0, 26.0, 30.0, 34.0, 38.0, 42.0, 46.0]
        if home_q in (1, 2):
            gy_prefs = (min(y_hi - 4, wy + 18), wy + 10, 0.65 * y_hi + 0.35 * y_lo, wy)
        elif home_q in (3, 4):
            gy_prefs = (max(y_lo + 4, wy - 18), wy - 10, 0.35 * y_hi + 0.65 * y_lo, wy)
        else:
            gy_prefs = (wy, 0.5 * (y_lo + y_hi), wy + 12, wy - 12)
        for pang in cands:
            rad = math.radians(pang)
            if abs(math.sin(rad)) < math.sin(math.radians(ANGLE_MIN)):
                continue
            if abs(math.cos(rad)) < math.cos(math.radians(ANGLE_MAX)):
                continue
            for dist in dists:
                tx = wx + dist * math.cos(rad)
                ty = wy + dist * math.sin(rad)
                if gx_lo <= tx <= gx_hi and (y_lo - 28) <= ty <= (y_hi + 28):
                    return pang
        gx = 0.5 * (gx_lo + gx_hi)
        for gy in gy_prefs:
            gy = max(y_lo + 2, min(y_hi - 2, gy))
            ang = math.degrees(math.atan2(gy - wy, gx - wx)) % 360
            if _leader_axis_ok(ang):
                return ang
        return 45.0 if right_side else 135.0

    for ovb in other_view_bboxes:
        if not ovb or len(ovb) < 4:
            continue
        ox0, oy0, ox1, oy1 = ovb[0], ovb[1], ovb[2], ovb[3]
        if ox0 - 2 <= wx <= ox1 + 2 and oy0 - 2 <= wy <= oy1 + 2:
            continue
        y_lo, y_hi = max(py0, oy0), min(py1, oy1)
        if y_hi - y_lo < 8:
            y_lo, y_hi = min(py0, oy0), max(py1, oy1)
        gap_r = ox0 - px1
        if 12 < gap_r < 220:
            extras |= {1, 4}
            gbox = (px1 + 3, y_lo - 10, ox0 - 3, y_hi + 10)
            ang = _pick_ang(gbox[0], gbox[2], y_lo, y_hi, True)
            # Prefer nearer corridor to weld (not just widest): right blank next to C-C
            gmid = 0.5 * (gbox[0] + gbox[2])
            score = gap_r * 1.2 - abs(wx - gmid) * 0.85
            if wy > y_lo + 0.55 * (y_hi - y_lo):
                score += 8  # upper welds mildly prefer available gap
            if score > best_gap:
                best_gap, best_ang, best_box = score, ang, gbox
        gap_l = px0 - ox1
        if 12 < gap_l < 220:
            extras |= {2, 3}
            gbox = (ox1 + 3, y_lo - 10, px0 - 3, y_hi + 10)
            ang = _pick_ang(gbox[0], gbox[2], y_lo, y_hi, False)
            gmid = 0.5 * (gbox[0] + gbox[2])
            score = gap_l * 1.2 - abs(wx - gmid) * 0.85
            if wy > y_lo + 0.55 * (y_hi - y_lo):
                score += 8
            if score > best_gap:
                best_gap, best_ang, best_box = score, ang, gbox
    return extras, best_ang, best_box


def _point_in_bbox_xyxy(px, py, bb, mrg=0.0):
    if not bb or len(bb) < 4:
        return False
    return (bb[0] - mrg <= px <= bb[2] + mrg and bb[1] - mrg <= py <= bb[3] + mrg)


def _angle_in_quadrant(angle_deg, quad, tol=QUADRANT_ANGLE_TOL):
    a0, a1 = QUAD_ANGLE_RANGES[quad]
    a = angle_deg % 360
    return (a0 - tol) <= a <= (a1 + tol)


def _leader_axis_ok(angle_deg):
    """True iff angle is outside ±ANGLE_MIN of horizontal and ±(90-ANGLE_MAX) of vertical."""
    rad = math.radians(angle_deg % 360)
    if abs(math.sin(rad)) < math.sin(math.radians(ANGLE_MIN)):
        return False
    if abs(math.cos(rad)) < math.cos(math.radians(ANGLE_MAX)):
        return False
    return True


def _snap_leader_angle(ang, home_q=None):
    """Clamp leader angle into the legal 10°–80° band (and home quadrant if given)."""
    a = ang % 360
    if _leader_axis_ok(a) and (home_q is None or _angle_in_quadrant(a, home_q)):
        return a
    # Prefer home quadrant mid / edges
    if home_q in QUAD_ANGLE_RANGES:
        a0, a1 = QUAD_ANGLE_RANGES[home_q]
        cands = list(range(int(a0), int(a1) + 1))
    else:
        cands = (list(range(ANGLE_MIN, ANGLE_MAX + 1)) +
                 list(range(180 - ANGLE_MAX, 180 - ANGLE_MIN + 1)) +
                 list(range(180 + ANGLE_MIN, 180 + ANGLE_MAX + 1)) +
                 list(range(360 - ANGLE_MAX, 360 - ANGLE_MIN + 1)))
    best, best_d = None, 1e9
    for c in cands:
        if not _leader_axis_ok(c):
            continue
        d = abs(((c - a + 180) % 360) - 180)
        if d < best_d:
            best, best_d = c, d
    return best if best is not None else {1: 45, 2: 135, 3: 225, 4: 315}.get(home_q, 45)


def _quadrant_angle_offsets(home_q, ideal_ang, step=3):
    """归属象限内的角度 offset 列表（相对 ideal_ang）。"""
    a0, a1 = QUAD_ANGLE_RANGES[home_q]
    return [a - ideal_ang for a in range(int(a0), int(a1) + 1, step)]


def _label_text_width(labels, is_pair=False):
    h = _lh()
    if is_pair:
        if isinstance(labels, (list, tuple)):
            text = f"{labels[0]},{labels[1]}"
        else:
            text = str(labels)
    else:
        text = labels if isinstance(labels, str) else str(labels)
    return max(h, len(text) * h * 0.6)


def _placement_label_text(gtype, labels):
    if gtype == 'pair':
        return f"{labels[0]},{labels[1]}"
    return labels[0]


def _horiz_land(label_text, is_pair=False):
    h = _lh()
    base = h * 5.0 if is_pair else h * 2.2
    min_land = h * 3.0
    return max(base, _label_text_width(label_text, is_pair), min_land)


def _text_in_inner_frame(tbb, inner_bbox, margin=None):
    """True if text bbox (x0,x1,y0,y1) is inside inner frame with margin."""
    if inner_bbox is None:
        return True
    m = BOUNDARY_MARGIN if margin is None else margin
    bx0, bx1, by0, by1 = tbb
    ix0, iy0, ix1, iy1 = inner_bbox
    return (bx0 >= ix0 + m and bx1 <= ix1 - m and
            by0 >= iy0 + m and by1 <= iy1 - m)


def _shortest_in_frame_pose(weld_pos, label_text, draw_bbox, is_pair=False,
                            home_q=None, prefer_ang=None, max_dist=None):
    """Return (dist, angle) with shortest leader whose text stays in inner frame.
    Angle-first at each distance; never returns an out-of-frame pose when one exists."""
    if draw_bbox is None:
        return None
    _max = max_dist if max_dist is not None else (
        MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN)
    wx, wy = weld_pos
    _cx = (draw_bbox[0] + draw_bbox[2]) / 2
    _cy = (draw_bbox[1] + draw_bbox[3]) / 2
    if home_q is None:
        home_q = _weld_home_quadrant(wx, wy, _cx, _cy)
    _pref = prefer_ang if prefer_ang is not None else {1: 45, 2: 135, 3: 225, 4: 315}.get(home_q, 45)
    _angles = [_pref]
    for q in (home_q, ((home_q - 2) % 4) + 1, (home_q % 4) + 1):
        if q in QUAD_ANGLE_RANGES:
            a0, a1 = QUAD_ANGLE_RANGES[q]
            _angles.extend(range(int(a0), int(a1) + 1, 5))
    _angles = list(dict.fromkeys(a % 360 for a in _angles))
    for nd in range(MIN_DIAG_LEN, int(_max) + 1, 2):
        for na in _angles:
            r = math.radians(na)
            if abs(math.sin(r)) < math.sin(math.radians(ANGLE_MIN)):
                continue
            if abs(math.cos(r)) < math.cos(math.radians(ANGLE_MAX)):
                continue
            tbb = _text_bbox(weld_pos, nd, na, label_text, is_pair=is_pair)
            if _text_in_inner_frame(tbb, draw_bbox):
                return nd, na
    # last resort: full 360° / short steps（不超 _max）
    for nd in range(MIN_DIAG_LEN, int(_max) + 1, 2):
        for na in range(0, 360, 5):
            r = math.radians(na)
            if abs(math.sin(r)) < math.sin(math.radians(ANGLE_MIN)):
                continue
            if abs(math.cos(r)) < math.cos(math.radians(ANGLE_MAX)):
                continue
            tbb = _text_bbox(weld_pos, nd, float(na), label_text, is_pair=is_pair)
            if _text_in_inner_frame(tbb, draw_bbox):
                return nd, float(na)
    return None


def _text_overlaps(tbb, otb, margin):
    bx0, bx1, by0, by1 = tbb
    return not (bx1 < otb[0] - margin or bx0 > otb[1] + margin or
                by1 < otb[2] - margin or by0 > otb[3] + margin)


def _single_bbox(weld_pos, diag_len, angle_deg, label_text=''):
    """计算单标注文字 + 引线完整包围盒。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    ex = wx + diag_len * cos_a
    ey = wy + diag_len * math.sin(rad)
    lx, ly = _label_corner(weld_pos, diag_len, angle_deg, label_text=label_text, is_pair=False)
    lw = _label_text_width(label_text, False)
    if cos_a >= -0.05:
        bx0, bx1 = lx, lx + lw
    else:
        bx0, bx1 = lx - lw, lx
    # Include leader path
    all_xs = [wx, ex, bx0, bx1]
    all_ys = [wy, ey, ly, ly + _lh()]
    return min(all_xs), max(all_xs), min(all_ys), max(all_ys)


def _paired_bbox(weld_pos, diag_len, angle_deg, label_text=''):
    """计算配对标注"F1,F2"文字 + 引线完整包围盒。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    ex = wx + diag_len * cos_a
    ey = wy + diag_len * math.sin(rad)
    lx, ly = _label_corner(weld_pos, diag_len, angle_deg, label_text=label_text, is_pair=True)
    pair_w = _label_text_width(label_text, True)
    if cos_a >= -0.05:
        bx0, bx1 = lx, lx + pair_w
    else:
        bx0, bx1 = lx - pair_w, lx
    all_xs = [wx, ex, bx0, bx1]
    all_ys = [wy, ey, ly, ly + _lh()]
    return min(all_xs), max(all_xs), min(all_ys), max(all_ys)


def _next_label(w, f_counter, w_counter):
    if w.get('annotation') == 'CJP' or w.get('weld_type') == 'CJP':
        w_counter[0] += 1
        return f'W{w_counter[0]}'
    else:
        f_counter[0] += 1
        return f'F{f_counter[0]}'


def _label_corner(weld_pos, diag_len, angle_deg, label_text='', is_pair=False):
    """返回标注文字的实际位置：水平接地线末端的 (x, y)。"""
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a = math.cos(rad)
    ex = wx + diag_len * cos_a
    ey = wy + diag_len * math.sin(rad)
    h_len = _horiz_land(label_text, is_pair)
    if cos_a >= -0.05:
        hx = ex + h_len
    else:
        hx = ex - h_len
    return (hx, ey)


def _text_bbox(weld_pos, diag_len, angle_deg, label_text='', is_pair=False):
    """计算纯文字包围盒 (x0,x1,y0,y1)，用于重叠检测。"""
    _rad = math.radians(angle_deg)
    _tbx, _tby = _label_corner(weld_pos, diag_len, angle_deg, label_text=label_text, is_pair=is_pair)
    _width = _label_text_width(label_text, is_pair)
    if math.cos(_rad) >= -0.05:    # BR: text extends LEFT from hx
        _tbx0 = _tbx - _width
        _tbx1 = _tbx
    else:                            # BL: text extends RIGHT from hx
        _tbx0 = _tbx
        _tbx1 = _tbx + _width
    return (_tbx0, _tbx1, _tby, _tby + _lh())


def _text_near_lines(tbb, lines, margin=LINE_CLEARANCE):
    """True if label text bbox is too close to or crosses geometry lines."""
    bx0, bx1, by0, by1 = tbb
    _txt_pts = _txt_sample_points(bx0, bx1, by0, by1)
    _cx_txt = (bx0 + bx1) / 2
    _cy_txt = (by0 + by1) / 2
    _mrg = margin + 2
    for (sx, sy), (ex2, ey2) in lines:
        if bx1 < min(sx, ex2) - _mrg or bx0 > max(sx, ex2) + _mrg:
            continue
        if by1 < min(sy, ey2) - _mrg or by0 > max(sy, ey2) + _mrg:
            continue
        for (cx, cy) in _txt_pts:
            if _dist_pt_to_seg((cx, cy), (sx, sy), (ex2, ey2))[0] < margin:
                return True
        if _dist_pt_to_seg((_cx_txt, _cy_txt), (sx, sy), (ex2, ey2))[0] < margin * 0.75:
            return True
        # vertical / horizontal line through interior of bbox
        if _seg_cross_rect((sx, sy), (ex2, ey2), bx0, bx1, by0, by1):
            return True
        _txt_edges = [((bx0, by0), (bx1, by0)), ((bx1, by0), (bx1, by1)),
                      ((bx1, by1), (bx0, by1)), ((bx0, by1), (bx0, by0))]
        for (_s, _e) in _txt_edges:
            if _segments_cross_(_s, _e, (sx, sy), (ex2, ey2)):
                return True
    return False


def _mtext_label_bbox(mtext_entity):
    ins = mtext_entity.dxf.insert
    txt = mtext_entity.text.strip() if hasattr(mtext_entity, 'text') else ''
    w = _label_text_width(txt, ',' in txt)
    att = getattr(mtext_entity.dxf, 'attachment_point', MT_BOTTOM_RIGHT)
    if att in (MT_BOTTOM_RIGHT, MT_TOP_RIGHT, MT_MIDDLE_RIGHT):
        return (ins.x - w, ins.x, ins.y, ins.y + _lh())
    return (ins.x, ins.x + w, ins.y, ins.y + _lh())


def _collect_part_lines(doc):
    lines = []
    for blk in doc.blocks:
        if not blk.name.startswith('Part'):
            continue
        for e in blk:
            if e.dxftype() == 'LINE':
                lines.append(((e.dxf.start.x, e.dxf.start.y),
                              (e.dxf.end.x, e.dxf.end.y)))
    return lines


def _erase_label_entities(msp, entities):
    for ent in entities:
        try:
            msp.delete_entity(ent)
        except Exception:
            pass


def _redraw_label_meta(msp, meta):
    """Erase and redraw a label from stored placement metadata."""
    _erase_label_entities(msp, meta['entities'])
    _prev_h = _lh()
    _set_active_label_height(meta.get('label_height', LABEL_HEIGHT))
    try:
        _tips = meta.get('branch_tips')
        if meta.get('is_pair') and _tips and len(_tips) >= 2:
            new = _draw_branched_paired_weld_label(
                msp, meta['labels'], _tips, meta['label_text'],
                meta['diag_len'], meta['angle'], sampled=meta.get('sampled', False))
        elif meta.get('is_pair'):
            new = _draw_paired_weld_label(
                msp, meta['labels'], meta['weld_pos'], meta['label_text'],
                meta['diag_len'], meta['angle'], sampled=meta.get('sampled', False))
        else:
            new = _draw_weld_label(
                msp, meta['label_text'], meta['weld_pos'], meta['label_text'],
                meta['diag_len'], meta['angle'], sampled=meta.get('sampled', False))
        # Preserve branch tips across redraw so forks are not lost
        if _tips:
            new['branch_tips'] = _tips
        meta.update(new)
    finally:
        _set_active_label_height(_prev_h)
    return meta


def _with_meta_height(meta, fn):
    """Run fn() under meta's label_height."""
    _prev = _lh()
    _set_active_label_height(meta.get('label_height', LABEL_HEIGHT))
    try:
        return fn()
    finally:
        _set_active_label_height(_prev)


def _label_placement_ok(meta, part_lines, draw_bbox, wm_text_bboxes, other_metas,
                        hatch_bboxes=None):
    def _check():
        if not _leader_axis_ok(meta['angle']):
            return False
        tbb = _text_bbox(meta['weld_pos'], meta['diag_len'], meta['angle'],
                         meta['label_text'], is_pair=meta.get('is_pair', False))
        if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
            return False
        if _text_near_lines(tbb, part_lines):
            return False
        for om in other_metas:
            if om is meta:
                continue
            def _otb():
                return _text_bbox(om['weld_pos'], om['diag_len'], om['angle'],
                                  om['label_text'], is_pair=om.get('is_pair', False))
            otb = _with_meta_height(om, _otb)
            if _text_overlaps(tbb, otb, OVERLAP_MARGIN):
                return False
        for wtb in wm_text_bboxes or []:
            if _text_overlaps(tbb, wtb, WM_TEXT_MARGIN):
                return False
        if hatch_bboxes:
            for htb in hatch_bboxes:
                if _text_overlaps(tbb, htb, OVERLAP_MARGIN):
                    return False
        wx, wy = meta['weld_pos']
        rad = math.radians(meta['angle'])
        ex = wx + meta['diag_len'] * math.cos(rad)
        ey = wy + meta['diag_len'] * math.sin(rad)
        for wtb in wm_text_bboxes or []:
            if _seg_cross_rect((wx, wy), (ex, ey), wtb[0], wtb[1], wtb[2], wtb[3]):
                return False
        if meta['diag_len'] < MIN_DIAG_LEN:
            return False
        h_len = abs(_horiz_land(meta['label_text'], meta.get('is_pair', False)))
        if h_len < _lh() * 3.0:
            return False
        return True
    return _with_meta_height(meta, _check)


def _reposition_drawn_label(msp, meta, diag_len, angle_deg, part_lines, draw_bbox,
                            wm_text_bboxes, other_metas, hatch_bboxes=None):
    old = (meta['diag_len'], meta['angle'])
    wx, wy = meta['weld_pos']
    if draw_bbox:
        _cx = (draw_bbox[0] + draw_bbox[2]) / 2
        _cy = (draw_bbox[1] + draw_bbox[3]) / 2
    else:
        _cx, _cy = wx, wy
    hq = _weld_home_quadrant(wx, wy, _cx, _cy)
    if _leader_axis_ok(angle_deg) and any(
            _angle_in_quadrant(angle_deg, q)
            for q in _allowed_quadrants(hq, allow_adjacent=True)):
        angle_deg = angle_deg % 360
    else:
        angle_deg = _snap_leader_angle(angle_deg, hq)
    meta['diag_len'] = diag_len
    meta['angle'] = angle_deg
    if not _label_placement_ok(meta, part_lines, draw_bbox, wm_text_bboxes, other_metas,
                               hatch_bboxes=hatch_bboxes):
        meta['diag_len'], meta['angle'] = old
        return False
    _redraw_label_meta(msp, meta)
    return True


def _nudge_drawn_label(msp, meta, dx, dy, part_lines, draw_bbox=None,
                       wm_text_bboxes=None, other_metas=None, hatch_bboxes=None):
    """Reposition label by adjusting leader geometry and redrawing."""
    if other_metas is None:
        other_metas = []
    ds, ag = meta['diag_len'], meta['angle']
    # Prefer small angle / moderate length nudges before large extensions
    shifts = [
        (ds + 4, ag + 5), (ds + 4, ag - 5), (ds + 4, ag + 8), (ds + 4, ag - 8),
        (ds + 8, ag + 8), (ds + 8, ag - 8), (ds + 8, ag + 12), (ds + 8, ag - 12),
        (ds + 12, ag + 12), (ds + 12, ag - 12), (ds + 12, ag + 15), (ds + 12, ag - 15),
        (ds + 16, ag), (ds + 20, ag + 10), (ds + 20, ag - 10),
        (ds - 4, ag + 10), (ds - 4, ag - 10),
        (ds + 6, ag + 20), (ds + 6, ag - 20), (ds + 10, ag + 25), (ds + 10, ag - 25),
    ]
    if abs(dx) > abs(dy):
        shifts = [(ds + int(abs(dx) / 2) + 4, ag + (8 if dy >= 0 else -8))] + shifts
    elif abs(dy) > 0:
        shifts = [(ds + int(abs(dy) / 2) + 4, ag + (12 if dy > 0 else -12))] + shifts
    _max_len = MAX_DIAG_LEN_PAIR if meta.get('is_pair') else MAX_DIAG_LEN
    for nd, na in shifts:
        if nd < MIN_DIAG_LEN or nd > _max_len:
            continue
        if _reposition_drawn_label(msp, meta, nd, na % 360, part_lines, draw_bbox,
                                   wm_text_bboxes or [], other_metas,
                                   hatch_bboxes=hatch_bboxes):
            return True
    return False


def _fix_global_label_overlaps(msp, part_lines, draw_bbox=None, wm_text_bboxes=None,
                               drawn_registry=None, hatch_bboxes=None):
    """Final pass: nudge remaining label/WM/hatch overlaps; leave clean labels alone."""
    if not drawn_registry:
        drawn_registry = []
        for e in msp:
            if e.dxftype() == 'MTEXT' and e.dxf.layer == LAYER_NAME:
                drawn_registry.append({'mtext': e, 'entities': [e]})
    wm_text_bboxes = wm_text_bboxes or []
    hatch_bboxes = hatch_bboxes or []

    def _meta_tbb(meta):
        def _calc():
            return _text_bbox(meta['weld_pos'], meta['diag_len'], meta['angle'],
                              meta['label_text'], is_pair=meta.get('is_pair', False))
        return _with_meta_height(meta, _calc)

    def _shrink_overlapping():
        """Shrink font on residual text-text overlaps and their local weld neighbors."""
        if not drawn_registry:
            return False
        entries = [(m, _meta_tbb(m)) for m in drawn_registry if m.get('weld_pos')]
        targets = set()
        for i in range(len(entries)):
            mi, bi = entries[i]
            for j in range(i + 1, len(entries)):
                mj, bj = entries[j]
                if _text_overlaps(bi, bj, OVERLAP_MARGIN):
                    targets.add(id(mi))
                    targets.add(id(mj))
        if not targets:
            return False
        # expand to nearby welds in the same dense pocket
        seed_pos = [m['weld_pos'] for m in drawn_registry if id(m) in targets]
        for meta in drawn_registry:
            if not meta.get('weld_pos'):
                continue
            wx, wy = meta['weld_pos']
            if any(math.hypot(wx - sx, wy - sy) <= CLUSTER_RADIUS for sx, sy in seed_pos):
                targets.add(id(meta))
        shrunk = False
        for meta in drawn_registry:
            if id(meta) not in targets:
                continue
            cur = meta.get('label_height', LABEL_HEIGHT)
            if cur <= LABEL_HEIGHT_VERY_DENSE + 0.05:
                continue
            meta['label_height'] = LABEL_HEIGHT_VERY_DENSE
            _redraw_label_meta(msp, meta)
            shrunk = True
        return shrunk

    def _shrink_dense_clusters():
        """Proactively shrink labels in dense weld pockets or crowded text areas."""
        metas = [m for m in drawn_registry if m.get('weld_pos')]
        if len(metas) < DENSE_CLUSTER_N:
            return False
        positions = [(m['weld_pos'][0], m['weld_pos'][1]) for m in metas]
        tbbs = [_meta_tbb(m) for m in metas]
        centers = [((b[0] + b[1]) / 2.0, (b[2] + b[3]) / 2.0) for b in tbbs]
        text_r = max(18.0, CLUSTER_RADIUS * 0.65)
        shrunk = False
        for i, meta in enumerate(metas):
            n_weld = _local_cluster_size_at(meta['weld_pos'], positions)
            n_text = _local_cluster_size_at(centers[i], centers, radius=text_r)
            tgt = _height_from_cluster_n(max(n_weld, n_text))
            # also shrink if this text box nearly touches another
            bi = tbbs[i]
            near_hit = False
            for j, bj in enumerate(tbbs):
                if j == i:
                    continue
                if _text_overlaps(bi, bj, OVERLAP_MARGIN * 2.5):
                    near_hit = True
                    break
            if near_hit:
                tgt = min(tgt, LABEL_HEIGHT_VERY_DENSE)
            cur = meta.get('label_height', LABEL_HEIGHT)
            if cur > tgt + 0.05:
                meta['label_height'] = tgt
                _redraw_label_meta(msp, meta)
                shrunk = True
        return shrunk

    def _hits_obstacle(tbb):
        if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
            return True
        for wtb in wm_text_bboxes:
            if _text_overlaps(tbb, wtb, WM_TEXT_MARGIN):
                return True
        for htb in hatch_bboxes:
            if _text_overlaps(tbb, htb, OVERLAP_MARGIN):
                return True
        if _text_near_lines(tbb, part_lines):
            return True
        return False

    def _search_reposition(meta):
        others = [m for m in drawn_registry if m is not meta]
        other_text = [_meta_tbb(m) for m in others]
        wx, wy = meta['weld_pos']
        if draw_bbox:
            _cx = (draw_bbox[0] + draw_bbox[2]) / 2
            _cy = (draw_bbox[1] + draw_bbox[3]) / 2
        else:
            _cx, _cy = wx, wy
        hq = _weld_home_quadrant(wx, wy, _cx, _cy)
        if draw_bbox:
            vx0, vy0, vx1, vy1 = draw_bbox
        else:
            vx0, vy0, vx1, vy1 = wx - 200, wy - 200, wx + 200, wy + 200
        _max_len = MAX_DIAG_LEN_PAIR if meta.get('is_pair') else MAX_DIAG_LEN
        _, nd, na = _search_placement(
            meta['weld_pos'], part_lines, wm_text_bboxes, [], [],
            other_text, vx0, vy0, vx1, vy1, draw_bbox,
            is_pair=meta.get('is_pair', False), home_q=hq,
            quad_cx=_cx, quad_cy=_cy,
            label_text=meta['label_text'], wm_text_bboxes=wm_text_bboxes,
            hatch_bboxes=hatch_bboxes or None,
            max_dist=_max_len, allow_adjacent=True)
        return _reposition_drawn_label(msp, meta, nd, na, part_lines, draw_bbox,
                                       wm_text_bboxes, others, hatch_bboxes=hatch_bboxes)

    def _brute_reposition(meta):
        others = [m for m in drawn_registry if m is not meta]
        _max_len = MAX_DIAG_LEN_PAIR if meta.get('is_pair') else MAX_DIAG_LEN
        # 短距优先：固定 dist 扫角度
        for dist in range(MIN_DIAG_LEN, _max_len + 1, 2):
            for ang in range(0, 360, 8):
                r = math.radians(ang)
                if abs(math.sin(r)) < math.sin(math.radians(ANGLE_MIN)):
                    continue
                if abs(math.cos(r)) < math.cos(math.radians(ANGLE_MAX)):
                    continue
                if _reposition_drawn_label(msp, meta, dist, float(ang), part_lines,
                                           draw_bbox, wm_text_bboxes, others,
                                           hatch_bboxes=hatch_bboxes):
                    return True
        return False

    _shrink_dense_clusters()

    for _ in range(4):
        if not drawn_registry:
            return
        entries = [(meta, _meta_tbb(meta)) for meta in drawn_registry]
        fixed = False
        for i in range(len(entries)):
            mi, bi = entries[i]
            needs_fix = _hits_obstacle(bi)
            conflict_partners = []
            for j in range(len(entries)):
                if j == i:
                    continue
                ej, bj = entries[j]
                if _text_overlaps(bi, bj, OVERLAP_MARGIN):
                    needs_fix = True
                    conflict_partners.append(ej)
            if not needs_fix:
                continue
            _out_frame = (draw_bbox is not None and
                          not _text_in_inner_frame(bi, draw_bbox))
            targets = conflict_partners + [mi] if conflict_partners else [mi]
            for target_meta in targets:
                shifts = [
                    (-_lh() * 4, 0), (_lh() * 4, 0),
                    (0, _lh() * 2.5), (0, -_lh() * 2.5),
                    (-_lh() * 6, _lh() * 2.5),
                    (_lh() * 6, _lh() * 2.5),
                    (0, _lh() * 5.0), (0, -_lh() * 5.0),
                    (_lh() * 8, 0), (-_lh() * 8, 0),
                ]
                others = [m for m in drawn_registry if m is not target_meta]
                for dx, dy in shifts:
                    if _nudge_drawn_label(msp, target_meta, dx, dy, part_lines,
                                          draw_bbox, wm_text_bboxes, others,
                                          hatch_bboxes=hatch_bboxes):
                        fixed = True
                        break
                if not fixed and _search_reposition(target_meta):
                    fixed = True
                if not fixed and _out_frame and _brute_reposition(target_meta):
                    fixed = True
                if fixed:
                    break
            if fixed:
                break
        if not fixed:
            # 残留字重叠：整簇缩到最小字号后再试挪位
            if _shrink_overlapping():
                continue
            break


def _enforce_inner_frame_labels(msp, draw_bbox, part_lines, wm_text_bboxes, drawn_registry,
                                hatch_bboxes=None):
    """最终强制：所有已绘制标注文字不得超出内框。"""
    if not draw_bbox or not drawn_registry:
        return
    _cx = (draw_bbox[0] + draw_bbox[2]) / 2
    _cy = (draw_bbox[1] + draw_bbox[3]) / 2
    vx0, vy0, vx1, vy1 = draw_bbox
    for meta in drawn_registry:
        def _tbb_cur():
            return _text_bbox(meta['weld_pos'], meta['diag_len'], meta['angle'],
                              meta['label_text'], is_pair=meta.get('is_pair', False))
        tbb = _with_meta_height(meta, _tbb_cur)
        if _text_in_inner_frame(tbb, draw_bbox):
            continue
        wx, wy = meta['weld_pos']
        _prefer_down = meta.get('prefer_down', False)
        hq = _weld_home_quadrant(wx, wy, _cx, _cy)
        others = [m for m in drawn_registry if m is not meta]
        other_text = []
        for m in others:
            def _otb(mm=m):
                return _text_bbox(mm['weld_pos'], mm['diag_len'], mm['angle'],
                                  mm['label_text'], is_pair=mm.get('is_pair', False))
            other_text.append(_with_meta_height(m, _otb))
        _max_len = MAX_DIAG_LEN_PAIR if meta.get('is_pair') else MAX_DIAG_LEN
        fixed = False
        # 1) 框内短距搜索
        def _do_search():
            return _search_placement(
                meta['weld_pos'], part_lines, wm_text_bboxes or [], [], [],
                other_text, vx0, vy0, vx1, vy1, draw_bbox,
                is_pair=meta.get('is_pair', False), home_q=hq,
                quad_cx=_cx, quad_cy=_cy, label_text=meta['label_text'],
                wm_text_bboxes=wm_text_bboxes, prefer_down=_prefer_down,
                hatch_bboxes=hatch_bboxes or None,
                max_dist=_max_len, allow_adjacent=True)
        _, nd, na = _with_meta_height(meta, _do_search)
        if _reposition_drawn_label(msp, meta, nd, na, part_lines, draw_bbox,
                                   wm_text_bboxes, others, hatch_bboxes=hatch_bboxes):
            fixed = True
        # 2) 短距×角度穷举（完整 _label_placement_ok）
        if not fixed:
            for dist in range(MIN_DIAG_LEN, _max_len + 1, 2):
                for ang in range(0, 360, 8):
                    r = math.radians(ang)
                    if abs(math.sin(r)) < math.sin(math.radians(ANGLE_MIN)):
                        continue
                    if abs(math.cos(r)) < math.cos(math.radians(ANGLE_MAX)):
                        continue
                    if _reposition_drawn_label(msp, meta, dist, float(ang), part_lines,
                                               draw_bbox, wm_text_bboxes, others,
                                               hatch_bboxes=hatch_bboxes):
                        fixed = True
                        break
                if fixed:
                    break
        # 3) 放宽软冲突，仅保证内框：强制改写绘制
        if not fixed:
            _fp = _shortest_in_frame_pose(
                meta['weld_pos'], meta['label_text'], draw_bbox,
                is_pair=meta.get('is_pair', False), home_q=hq,
                prefer_ang=meta['angle'], max_dist=_max_len)
            if _fp is not None:
                meta['diag_len'], meta['angle'] = _fp
                _redraw_label_meta(msp, meta)
                fixed = True
        if not fixed:
            print(f"    [warn] inner-frame violation unresolved at ({wx:.1f},{wy:.1f}) "
                  f"label={meta['label_text']!r}")


def _draw_arrow_head(msp, tip, angle_deg, arm_len=2.0):
    """在焊缝起点 tip 处绘制 V 形箭头，指向焊缝位置。"""
    rad = math.radians(angle_deg)
    lx = tip[0] + arm_len * math.cos(rad + 0.35)
    ly = tip[1] + arm_len * math.sin(rad + 0.35)
    rx = tip[0] + arm_len * math.cos(rad - 0.35)
    ry = tip[1] + arm_len * math.sin(rad - 0.35)
    entities = []
    for p in ((lx, ly), (rx, ry)):
        entities.append(msp.add_line(start=p, end=tip,
                                     dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))
    return entities


def _draw_weld_label(msp, label, weld_pos, dname, diag_len, angle_deg, sampled=False):
    """绘制标注：箭头 → 斜线 → 水平接地短横线 → 文字紧贴横线末端。"""
    entities = []
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    ex = wx + diag_len * cos_a
    ey = wy + diag_len * sin_a

    if cos_a >= -0.05:
        h_land = _horiz_land(label, False)
    else:
        h_land = -_horiz_land(label, False)
    hx = ex + h_land
    hy = ey

    entities.extend(_draw_arrow_head(msp, (wx, wy), angle_deg))
    entities.append(msp.add_line(start=(wx, wy), end=(ex, ey),
                                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))
    entities.append(msp.add_line(start=(ex, ey), end=(hx, hy),
                                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))

    if h_land >= 0:
        ap = MT_BOTTOM_RIGHT
        lx = hx
    else:
        ap = MT_BOTTOM_LEFT
        lx = hx
    mt = msp.add_mtext(label, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': _lh(),
        'insert': (lx, hy),
        'attachment_point': ap,
        'style': 'Arial Narrow',
        'lineweight': 30,
    })
    entities.append(mt)

    if sampled:
        _h = _lh()
        _tw = len(label) * _h * 0.6
        if h_land >= 0:
            _cx = lx - _tw / 2
        else:
            _cx = lx + _tw / 2
        _cy = hy + _h / 2
        _rx = _tw / 2 + 1.3
        _ry = _h / 2 + 1.3
        entities.append(msp.add_ellipse(center=(_cx, _cy), major_axis=(_rx, 0),
                                        ratio=_ry / max(_rx, 0.01),
                                        dxfattribs={'layer': LAYER_NAME, 'color': 1}))

    return {
        'entities': entities, 'mtext': mt, 'weld_pos': weld_pos,
        'diag_len': diag_len, 'angle': angle_deg, 'label_text': label,
        'is_pair': False, 'sampled': sampled, 'label_height': _lh(),
    }


def _draw_branched_paired_weld_label(msp, labels, branch_tips, dname, diag_len,
                                     angle_deg, sampled=False):
    """Paired label with V-fork whose opening faces the short-edge tips."""
    entities = []
    t1, t2 = branch_tips[0], branch_tips[1]
    mx = 0.5 * (t1[0] + t2[0])
    my = 0.5 * (t1[1] + t2[1])
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    # Junction sits along the leader, so the V opens back toward the tips
    # (toward the short-edge geometry), not away from it.
    span = math.hypot(t2[0] - t1[0], t2[1] - t1[1])
    fork_len = max(2.5, min(6.0, 0.35 * max(span, 1.0), 0.45 * max(diag_len, 1.0)))
    jx = mx + fork_len * cos_a
    jy = my + fork_len * sin_a

    remain = max(diag_len - fork_len, 2.0)
    ex = jx + remain * cos_a
    ey = jy + remain * sin_a
    paired_text = f"{labels[0]},{labels[1]}"
    h_len = _horiz_land(paired_text, True)
    h_land = h_len if cos_a >= -0.05 else -h_len
    hx = ex + h_land
    hy = ey

    # Arms of the V: junction → each tip; arrow openings face the junction
    # so the V opens toward the short-edge line segment.
    for tip in (t1, t2):
        entities.append(msp.add_line(
            start=(jx, jy), end=(tip[0], tip[1]),
            dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))
        back_ang = math.degrees(math.atan2(jy - tip[1], jx - tip[0]))
        entities.extend(_draw_arrow_head(msp, tip, back_ang))

    entities.append(msp.add_line(start=(jx, jy), end=(ex, ey),
                                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))
    entities.append(msp.add_line(start=(ex, ey), end=(hx, hy),
                                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))
    if h_land >= 0:
        ap, lx = MT_BOTTOM_RIGHT, hx
    else:
        ap, lx = MT_BOTTOM_LEFT, hx
    mt = msp.add_mtext(paired_text, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': _lh(),
        'insert': (lx, hy),
        'attachment_point': ap,
        'style': 'Arial Narrow',
        'lineweight': 30,
    })
    entities.append(mt)
    if sampled:
        _h = _lh()
        _tw = len(paired_text) * _h * 0.6
        _cx = lx - _tw / 2 if h_land >= 0 else lx + _tw / 2
        _cy = hy + _h / 2
        _rx = _tw / 2 + 1.3
        _ry = _h / 2 + 1.3
        entities.append(msp.add_ellipse(
            center=(_cx, _cy), major_axis=(_rx, 0),
            ratio=_ry / max(_rx, 0.01),
            dxfattribs={'layer': LAYER_NAME, 'color': 1}))
    return {
        'entities': entities, 'mtext': mt, 'weld_pos': (mx, my),
        'diag_len': diag_len, 'angle': angle_deg, 'label_text': paired_text,
        'is_pair': True, 'sampled': sampled, 'labels': list(labels),
        'label_height': _lh(),
        'branch_tips': [tuple(t1), tuple(t2)],
    }


def _draw_paired_weld_label(msp, labels, weld_pos, dname, diag_len, angle_deg, sampled=False):
    """绘制配对标注：共享引线 + 较长水平横线 + \"F1,F2\" 一个 MTEXT。"""
    entities = []
    wx, wy = weld_pos
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    ex = wx + diag_len * cos_a
    ey = wy + diag_len * sin_a

    paired_text = f"{labels[0]},{labels[1]}"
    h_len = _horiz_land(paired_text, True)
    h_land = h_len if cos_a >= -0.05 else -h_len
    hx = ex + h_land
    hy = ey

    entities.extend(_draw_arrow_head(msp, (wx, wy), angle_deg))
    entities.append(msp.add_line(start=(wx, wy), end=(ex, ey),
                                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))
    entities.append(msp.add_line(start=(ex, ey), end=(hx, hy),
                                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR}))

    if h_land >= 0:
        ap = MT_BOTTOM_RIGHT
        lx = hx
    else:
        ap = MT_BOTTOM_LEFT
        lx = hx
    mt = msp.add_mtext(paired_text, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': _lh(),
        'insert': (lx, hy),
        'attachment_point': ap,
        'style': 'Arial Narrow',
        'lineweight': 30,
    })
    entities.append(mt)

    if sampled:
        _h = _lh()
        _tw = len(paired_text) * _h * 0.6
        if h_land >= 0:
            _cx = lx - _tw / 2
        else:
            _cx = lx + _tw / 2
        _cy = hy + _h / 2
        _rx = _tw / 2 + 1.3
        _ry = _h / 2 + 1.3
        entities.append(msp.add_ellipse(center=(_cx, _cy), major_axis=(_rx, 0),
                                        ratio=_ry / max(_rx, 0.01),
                                        dxfattribs={'layer': LAYER_NAME, 'color': 1}))

    return {
        'entities': entities, 'mtext': mt, 'weld_pos': weld_pos,
        'diag_len': diag_len, 'angle': angle_deg, 'label_text': paired_text,
        'is_pair': True, 'sampled': sampled, 'labels': list(labels),
        'label_height': _lh(),
    }



def _search_placement(weld_pos, lines, text_bboxes, circles, placed_bboxes,
                      placed_text_bboxes, vx0, vy0, vx1, vy1, draw_bbox=None, is_pair=False,
                      hatch_bboxes=None, other_view_bboxes=None,
                      home_q=None, quad_cx=None, quad_cy=None,
                      other_view_part_bboxes=None, max_dist=None, label_text='',
                      wm_text_bboxes=None, part_bbox=None, prefer_down=False,
                      line_grid=None, allow_adjacent=False, prefer_ang=None,
                      neighbor_angles=None, cross_ok=False, placed_leaders=None):
    """在360°连续角度中搜索最佳标注位置。"""
    wx, wy = weld_pos

    _GRID = 50
    if line_grid is None:
        _line_grid = _build_line_grid(lines, _GRID)
    else:
        _line_grid = line_grid

    # 预计算理想引线方向（象限用）；prefer_ang 优先用于近距分向
    _vcx = quad_cx if quad_cx is not None else (vx0 + vx1) / 2
    _vcy = quad_cy if quad_cy is not None else (vy0 + vy1) / 2
    _radial_ang = math.degrees(math.atan2(wy - _vcy, wx - _vcx)) % 360
    if home_q is None:
        home_q = _weld_home_quadrant(wx, wy, _vcx, _vcy)
    if wm_text_bboxes is None:
        wm_text_bboxes = []
    if neighbor_angles is None:
        neighbor_angles = []
    if placed_leaders is None:
        placed_leaders = []
    _part_bbox = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
    _ov_corr = other_view_part_bboxes if other_view_part_bboxes else other_view_bboxes
    _corr_quads, _gap_ang, _gap_box = _corridor_info(
        wx, wy, _part_bbox, _ov_corr, home_q=home_q)
    # 走廊：有缝即启用（不再要求焊点贴朝缝侧），以便左/顶拥挤标签进两视图间空白
    _px0, _py0, _px1, _py1 = _part_bbox
    _use_corr = bool(_gap_ang is not None and _gap_box is not None and _corr_quads)
    if not _use_corr:
        _corr_quads, _gap_ang, _gap_box = set(), None, None
    _allowed_quads = _allowed_quadrants(
        home_q, allow_adjacent=prefer_down or allow_adjacent, cross_ok=cross_ok)
    # 朝缝落点时允许走廊面向象限（同竖直半侧优先：右缝→Q1/Q4）
    if _use_corr and _corr_quads:
        _allowed_quads = set(_allowed_quads) | set(_corr_quads)
    # 走廊角作 ideal；否则 prefer / 径向
    if _gap_ang is not None:
        _ideal_ang = _gap_ang
    elif prefer_ang is not None:
        _ideal_ang = prefer_ang % 360
    else:
        _ideal_ang = _radial_ang

    def _near_lines_for(wx0, wy0, wx1, wy1, mrg=15):
        _gx0 = int((min(wx0, wx1) - mrg) / _GRID); _gx1 = int((max(wx0, wx1) + mrg) / _GRID)
        _gy0 = int((min(wy0, wy1) - mrg) / _GRID); _gy1 = int((max(wy0, wy1) + mrg) / _GRID)
        _near = []
        for _gx in range(_gx0, _gx1 + 1):
            for _gy in range(_gy0, _gy1 + 1):
                _near.extend(_line_grid.get((_gx, _gy), []))
        return _near

    def _has_conflict(angle_deg, dist, _db):
        if dist < MIN_DIAG_LEN:
            return True
        rad = math.radians(angle_deg)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        # 引线必须有倾角：拒水平和垂直
        if abs(sin_a) < math.sin(math.radians(ANGLE_MIN)):
            return True
        if abs(cos_a) < math.cos(math.radians(ANGLE_MAX)):
            return True
        # 象限：默认须在允许集；tip+文字中心落在走廊盒时放行朝缝角
        ex = wx + dist * cos_a
        ey = wy + dist * sin_a
        _tip_in_gap = False
        if _gap_box is not None:
            gx0, gy0, gx1, gy1 = _gap_box
            _tip_in_gap = (gx0 <= ex <= gx1 and gy0 <= ey <= gy1)
        if not any(_angle_in_quadrant(angle_deg, q) for q in _allowed_quads):
            if not (_tip_in_gap and _use_corr):
                return True
        h_len = _horiz_land(label_text, is_pair)
        if h_len < _lh() * 3.0:
            return True
        h_land = h_len if cos_a >= -0.05 else -h_len
        hx = ex + h_land; hy = ey
        # 蓝×蓝：浅角交叉硬拒绝（不相交或夹角>45°才放行）
        if _blue_leader_shallow_cross(
                (wx, wy), dist, angle_deg, h_land, placed_leaders):
            return True
        # 文字盒与绘制一致
        bx0, bx1, by0, by1 = _text_bbox(
            (wx, wy), dist, angle_deg, label_text, is_pair=is_pair)
        nbb = (min(wx, ex, bx0, hx), max(wx, ex, bx1, hx),
               min(wy, ey, by0, hy), max(wy, ey, by1, hy))
        if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, _db):
            return True
        # 跨视图：跳过本视图；硬拦文字压入邻视图（允许引线穿过中间空白）
        _ov_check = other_view_part_bboxes if other_view_part_bboxes is not None else other_view_bboxes
        if _ov_check:
            for _ovb in _ov_check:
                ox0, oy0, ox1, oy1 = _ovb[0], _ovb[1], _ovb[2], _ovb[3]
                if ox0 - 2 <= wx <= ox1 + 2 and oy0 - 2 <= wy <= oy1 + 2:
                    continue
                _M = 6 if _use_corr else 10
                if (bx1 > ox0 - _M and bx0 < ox1 + _M
                        and by1 > oy0 - _M and by0 < oy1 + _M):
                    return True
        for otb in placed_text_bboxes:
            if not (bx1 < otb[0] - OVERLAP_MARGIN or bx0 > otb[1] + OVERLAP_MARGIN or
                    by1 < otb[2] - OVERLAP_MARGIN or by0 > otb[3] + OVERLAP_MARGIN):
                return True
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if not (bx1 < tx0 - OVERLAP_MARGIN or bx0 > tx1 + OVERLAP_MARGIN or
                    by1 < ty0 - OVERLAP_MARGIN or by0 > ty1 + OVERLAP_MARGIN):
                return True
        for (tx0, tx1, ty0, ty1) in wm_text_bboxes:
            if not (bx1 < tx0 - WM_TEXT_MARGIN or bx0 > tx1 + WM_TEXT_MARGIN or
                    by1 < ty0 - WM_TEXT_MARGIN or by0 > ty1 + WM_TEXT_MARGIN):
                return True
            if _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
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
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if _seg_cross_rect((ex, ey), (hx, hy), tx0, tx1, ty0, ty1):
                return True
        for (pbx0, pbx1, pby0, pby1) in placed_bboxes:
            if _seg_cross_rect((wx, wy), (ex, ey), pbx0, pbx1, pby0, pby1):
                return True
            if _seg_cross_rect((ex, ey), (hx, hy), pbx0, pbx1, pby0, pby1):
                return True
        _min_x = min(wx, ex, hx, bx0); _max_x = max(wx, ex, hx, bx1)
        _min_y = min(wy, ey, hy, by0); _max_y = max(wy, ey, hy, by1)
        _mrg = 15
        _near = _near_lines_for(_min_x, _min_y, _max_x, _max_y, _mrg)
        for (sx, sy), (ex2, ey2) in _near:
            if max(sx, ex2) < _min_x - _mrg or min(sx, ex2) > _max_x + _mrg or \
                    max(sy, ey2) < _min_y - _mrg or min(sy, ey2) > _max_y + _mrg:
                continue
            if _segments_cross_((ex, ey), (hx, hy), (sx, sy), (ex2, ey2)):
                return True
            # 对角引线 vs 构件线（tip 已进走廊时允许擦过构件，否则进不了缝）
            if not _tip_in_gap and _segments_cross_((wx, wy), (ex, ey), (sx, sy), (ex2, ey2)):
                return True
        # 文字与几何线过近（加密采样）
        _line_mrg = LINE_CLEARANCE
        _txt_pts = _txt_sample_points(bx0, bx1, by0, by1)
        for (sx, sy), (ex2, ey2) in _near:
            for (tcx, tcy) in _txt_pts:
                if _dist_pt_to_seg((tcx, tcy), (sx, sy), (ex2, ey2))[0] < _line_mrg:
                    return True
            if _seg_cross_rect((sx, sy), (ex2, ey2), bx0, bx1, by0, by1):
                return True
        _cx_txt = (bx0 + bx1) / 2
        _cy_txt = (by0 + by1) / 2
        for (sx, sy), (ex2, ey2) in _near:
            if _dist_pt_to_seg((_cx_txt, _cy_txt), (sx, sy), (ex2, ey2))[0] < _line_mrg * 0.75:
                return True
        # 射线法：仅用网格近邻线（速度）；空白环由评分引导
        _odd = 0
        for _rx, _ry in [(_cx_txt + 99999, _cy_txt), (_cx_txt - 99999, _cy_txt)]:
            _cnt = sum(1 for (sx, sy), (ex2, ey2) in _near
                       if (sy <= _cy_txt <= ey2 or ey2 <= _cy_txt <= sy)
                       and _segments_cross_((_cx_txt, _cy_txt), (_rx, _ry), (sx, sy), (ex2, ey2)))
            if _cnt % 2 == 1:
                _odd += 1
        for _rx, _ry in [(_cx_txt, _cy_txt + 99999), (_cx_txt, _cy_txt - 99999)]:
            _cnt = sum(1 for (sx, sy), (ex2, ey2) in _near
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
            # 对角引线穿过 WM 圆
            if _dist_pt_to_seg((ccx, ccy), (wx, wy), (ex, ey))[0] < cr:
                return True
        if hatch_bboxes:
            for (hx0, hx1, hy0, hy1) in hatch_bboxes:
                if not (bx1 < hx0 - OVERLAP_MARGIN or bx0 > hx1 + OVERLAP_MARGIN or
                        by1 < hy0 - OVERLAP_MARGIN or by0 > hy1 + OVERLAP_MARGIN):
                    return True
                if not _tip_in_gap and _seg_cross_rect((wx, wy), (ex, ey), hx0, hx1, hy0, hy1):
                    return True
        # 纯文字内框硬约束
        _tbb_chk = (bx0, bx1, by0, by1)
        if _db is not None and not _text_in_inner_frame(_tbb_chk, _db):
            return True
        return False

    def _fine_tune(dist, angle, _db):
        """Local tune only — never return a conflicting pose."""
        for da in range(-15, 16, 5):
            na2 = angle + da
            r2 = math.radians(na2 % 360)
            if abs(math.sin(r2)) < math.sin(math.radians(ANGLE_MIN)): continue
            if abs(math.cos(r2)) < math.cos(math.radians(ANGLE_MAX)): continue
            if not _has_conflict(na2, dist, _db):
                return na2, dist, 0
        for nd in (dist + 4, dist + 8, dist + 12, dist - 4):
            if nd < MIN_DIAG_LEN or nd > _max_len:
                continue
            for da in (0, 8, -8, 15, -15):
                na = angle + da
                r3 = math.radians(na % 360)
                if abs(math.sin(r3)) < math.sin(math.radians(ANGLE_MIN)): continue
                if abs(math.cos(r3)) < math.cos(math.radians(ANGLE_MAX)): continue
                if not _has_conflict(na, nd, _db):
                    return na, nd, 0
        return None

    def _search_pass(_db, allow_adj=False, max_dist_local=None):
        """分阶段搜索，只返回无冲突位姿。"""
        nonlocal _allowed_quads
        _allowed_quads = _allowed_quadrants(
            home_q, allow_adjacent=allow_adj or prefer_down, cross_ok=cross_ok)
        # 走廊不再并入象限：只作 prefer / 短距快路径提示
        _eff_max = max_dist_local if max_dist_local is not None else (
            max_dist if max_dist is not None else _max_len)
        # Coarser length step: fewer conflict checks, still finds free poses
        distances = list(range(PREFERRED_DIAG_MIN, _eff_max + 1, 3))
        if is_pair:
            distances = [d + 4 for d in distances]
        _full_ao = [0, 8, -8, 15, -15, 25, -25, 35, -35, 45, -45, 60, -60,
                     80, -80, 100, -100, 120, -120, 150, -150, 180]

        def _try_place(dist, ao_list, base_ang=None):
            _base = _ideal_ang if base_ang is None else base_ang
            for offset in ao_list:
                angle = (_base + offset) % 360
                rad = math.radians(angle)
                if abs(math.sin(rad)) < math.sin(math.radians(ANGLE_MIN)): continue
                if abs(math.cos(rad)) < math.cos(math.radians(ANGLE_MAX)): continue
                if not _has_conflict(angle, dist, _db):
                    # Skip fine_tune on every hit — final snap happens once at end
                    return 0, (angle, dist, 0)
            return None

        def _try_place_bases(dist, ao_list):
            # corridor first, then prefer / radial (dedupe bases)
            bases = [_ideal_ang]
            if prefer_ang is not None and _angle_delta_deg(_ideal_ang, prefer_ang) > 3:
                bases.append(prefer_ang % 360)
            if _gap_ang is not None and _angle_delta_deg(_ideal_ang, _gap_ang) > 3:
                bases.append(_gap_ang)
            if _angle_delta_deg(_ideal_ang, _radial_ang) > 3:
                bases.append(_radial_ang)
            for _b in bases:
                result = _try_place(dist, ao_list, base_ang=_b)
                if result:
                    return result
            return None

        # 走廊短引线快路径：尽量把 tip / 文字中心落入空白条带
        if _gap_ang is not None:
            _gap_cap = min(int(_eff_max), MAX_DIAG_LEN)
            _targets = []
            if _gap_box is not None:
                gx0, gy0, gx1, gy1 = _gap_box
                gxm = 0.5 * (gx0 + gx1)
                for gy in (wy, 0.5 * (gy0 + gy1), wy + 14, wy - 14,
                           gy0 + 8, gy1 - 8, wy + 28, wy - 28):
                    gy = max(gy0 + 2, min(gy1 - 2, gy))
                    _targets.append((gxm, gy))
                    _targets.append((0.35 * gx0 + 0.65 * gx1, gy))
                    _targets.append((0.65 * gx0 + 0.35 * gx1, gy))
                    _targets.append((gx0 + 4, gy))
                    _targets.append((gx1 - 4, gy))
            # Prefer target points that put TEXT bbox into the gap (not just tip)
            _best_hit = None
            for (tx, ty) in _targets:
                dx, dy = tx - wx, ty - wy
                nd = math.hypot(dx, dy)
                if nd < PREFERRED_DIAG_MIN or nd > _gap_cap:
                    continue
                na = math.degrees(math.atan2(dy, dx)) % 360
                if not _leader_axis_ok(na):
                    continue
                if not _has_conflict(na, nd, _db):
                    tbb = _text_bbox((wx, wy), nd, na, label_text, is_pair=is_pair)
                    tcx = (tbb[0] + tbb[1]) / 2
                    if _gap_box is not None:
                        gx0, gy0, gx1, gy1 = _gap_box
                        if gx0 <= tcx <= gx1 and gy0 <= (tbb[2] + tbb[3]) / 2 <= gy1:
                            return 0, (na, nd, 0)
                    if _best_hit is None:
                        _best_hit = (na, nd, 0)
            if _best_hit is not None:
                return 0, _best_hit
            for dist in range(PREFERRED_DIAG_MIN, _gap_cap + 1, 3):
                for da in (0, 8, -8, 16, -16, 25, -25, 35, -35, 45, -45, 55, -55):
                    _ga = (_gap_ang + da) % 360
                    if not _has_conflict(_ga, dist, _db):
                        if _gap_box is not None:
                            rad = math.radians(_ga)
                            tipx = wx + dist * math.cos(rad)
                            tipy = wy + dist * math.sin(rad)
                            gx0, gy0, gx1, gy1 = _gap_box
                            if gx0 <= tipx <= gx1 and gy0 <= tipy <= gy1:
                                return 0, (_ga, dist, 0)
                        else:
                            return 0, (_ga, dist, 0)
            for dist in range(PREFERRED_DIAG_MIN, _gap_cap + 1, 3):
                for da in (0, 10, -10, 20, -20, 30, -30):
                    _ga = (_gap_ang + da) % 360
                    if not _has_conflict(_ga, dist, _db):
                        return 0, (_ga, dist, 0)

        _wide_ao = [0, 8, -8, 15, -15, 25, -25, 35, -35, 45, -45, 55, -55]
        _mid_ao = [0, 10, -10, 20, -20, 30, -30, 40, -40]
        # 1) 短甜区 20–28 优先
        for dist in distances:
            if dist < 20 or dist > 28:
                continue
            result = _try_place_bases(dist, _mid_ao)
            if result:
                return result
        # 2) 更短带 18–20
        for dist in distances:
            if dist < PREFERRED_DIAG_MIN or dist > 20:
                continue
            result = _try_place_bases(dist, _wide_ao)
            if result:
                return result
        # 3) 略长但仍在适中带 30–38
        for dist in distances:
            if dist < 30 or dist > 38:
                continue
            result = _try_place_bases(dist, _wide_ao)
            if result:
                return result
        # 4) ≥40 仅兜底
        for dist in distances:
            if dist < 40:
                continue
            result = _try_place_bases(dist, _full_ao)
            if result:
                return result

        # 评分兜底：稀疏扫描（仅当前阶段均失败时）
        _best_score = -999999999
        _best_result = None
        if _gap_ang is not None:
            _score_dists = [d for d in distances if d <= 44][::2] or distances[::3]
            _score_offs = list(range(0, 90, 20)) + list(range(0, -90, -20))
            _bases_sc = [_gap_ang, _ideal_ang]
            if prefer_ang is not None:
                _bases_sc.append(prefer_ang % 360)
        else:
            _score_dists = [d for d in distances if d in (
                PREFERRED_DIAG_MIN, 24, 30, 38, 46) or (distances and d == distances[-1])]
            if not _score_dists:
                _score_dists = distances[::4]
            _score_offs = list(range(0, 360, 45))
            _bases_sc = [_ideal_ang]
        for _base in _bases_sc:
            for dist in _score_dists:
                for _off in _score_offs:
                    angle = (_base + _off) % 360
                    rad = math.radians(angle)
                    if abs(math.sin(rad)) < math.sin(math.radians(ANGLE_MIN)): continue
                    if abs(math.cos(rad)) < math.cos(math.radians(ANGLE_MAX)): continue
                    if _has_conflict(angle, dist, _db): continue
                    score = _score_placement(wx, wy, angle, dist, lines, text_bboxes,
                                             circles, placed_bboxes, placed_text_bboxes,
                                             vx0, vy0, vx1, vy1,
                                             _db, is_pair=is_pair, min_score=_best_score,
                                             line_grid=_line_grid,
                                             hatch_bboxes=hatch_bboxes,
                                             other_view_bboxes=other_view_bboxes,
                                             home_q=home_q, quad_cx=_vcx, quad_cy=_vcy,
                                             label_text=label_text,
                                             wm_text_bboxes=wm_text_bboxes,
                                             part_bbox=_part_bbox, prefer_down=prefer_down,
                                             neighbor_angles=neighbor_angles,
                                             weld_pos=(wx, wy),
                                             allow_adjacent=allow_adj or prefer_down or allow_adjacent,
                                             corridor_quads=_corr_quads,
                                             gap_prefer_ang=_gap_ang,
                                             placed_leaders=placed_leaders)
                    if score > _best_score:
                        _best_score = score
                        _best_result = (angle, dist, 0)
        if _best_result is not None:
            _bd, _bdst, _ = _best_result
            ft = _fine_tune(_bdst, _bd, _db)
            if ft is not None:
                _fa, _fd, _fs = ft
                if not _has_conflict(_fa, _fd, _db):
                    return _fs, (_fa, _fd, 0)
            if not _has_conflict(_bd, _bdst, _db):
                return 0, (_bd, _bdst, 0)
        return None

    _eff_cap = max_dist if max_dist is not None else _max_len
    _eff_cap = min(max(int(_eff_cap), MIN_DIAG_LEN), _max_len)

    result = _search_pass(draw_bbox, allow_adj=allow_adjacent or prefer_down,
                          max_dist_local=_eff_cap)
    if result is None or _has_conflict(result[1][0], result[1][1], draw_bbox):
        # 中距优先：从 PREFERRED_DIAG_MIN 起扫角度，再逐步加长
        _seed_ang = result[1][0] if result else _ideal_ang
        _found = None
        _ang_offs = (0, 8, -8, 15, -15, 25, -25, 35, -35, 45, -45, 60, -60)
        for nd in range(PREFERRED_DIAG_MIN, _eff_cap + 1, 2):
            for da in _ang_offs:
                if not _has_conflict(_seed_ang + da, nd, draw_bbox):
                    _found = (0, (_seed_ang + da, nd, 0))
                    break
            if _found:
                break
            if home_q in QUAD_ANGLE_RANGES:
                a0, a1 = QUAD_ANGLE_RANGES[home_q]
                for na in range(int(a0), int(a1) + 1, 5):
                    if not _has_conflict(na, nd, draw_bbox):
                        _found = (0, (na, nd, 0))
                        break
            if _found:
                break
        if _found:
            result = _found
        else:
            if not hasattr(_search_placement, '_fb_seen'):
                _search_placement._fb_seen = set()
            _fk = (round(wx, 0), round(wy, 0), home_q)
            if _fk not in _search_placement._fb_seen and len(_search_placement._fb_seen) < 8:
                _search_placement._fb_seen.add(_fk)
                print(f"    [warn] quadrant-fallback at ({wx:.1f},{wy:.1f}) home_q={home_q}")
            result = _search_pass(draw_bbox, allow_adj=True, max_dist_local=_eff_cap)
            if result is None or _has_conflict(result[1][0], result[1][1], draw_bbox):
                # 仅接受框内最短无冲突位姿；绝不返回出框默认点
                _frame_pose = _shortest_in_frame_pose(
                    (wx, wy), label_text, draw_bbox, is_pair=is_pair,
                    home_q=home_q, prefer_ang=_seed_ang, max_dist=_eff_cap)
                if _frame_pose is not None:
                    _fd0, _fa0 = _frame_pose
                    # 在框内位姿上优先找无冲突的近邻
                    _picked = None
                    for nd in range(PREFERRED_DIAG_MIN, _eff_cap + 1, 2):
                        for da in (0, 5, -5, 10, -10, 15, -15, 20, -20, 30, -30):
                            na = _fa0 + da
                            if not _has_conflict(na, nd, draw_bbox):
                                _picked = (0, (na, nd, 0))
                                break
                        if _picked:
                            break
                    if _picked:
                        result = _picked
                    elif not _has_conflict(_fa0, max(_fd0, PREFERRED_DIAG_MIN), draw_bbox):
                        result = (0, (_fa0, max(_fd0, PREFERRED_DIAG_MIN), 0))
                    else:
                        # Never park on WM/title hard zones via frame fallback
                        _pref = {1: 45, 2: 135, 3: 225, 4: 315}
                        _fb_ang = _pref.get(home_q, 45)
                        _fb_ok = None
                        for nd in range(PREFERRED_DIAG_MIN, _eff_cap + 1, 2):
                            for da in (0, 20, -20, 40, -40, 60, -60, 90, -90, 120, -120):
                                na = (_fb_ang + da) % 360
                                if not _has_conflict(na, nd, draw_bbox):
                                    _fb_ok = (0, (na, nd, 0))
                                    break
                            if _fb_ok:
                                break
                        if _fb_ok:
                            result = _fb_ok
                        else:
                            result = (0, (_fb_ang, min(PREFERRED_DIAG_SOFT, _eff_cap), 0))
                            print(f"    [warn] unresolved placement at ({wx:.1f},{wy:.1f}) "
                                  f"label={label_text!r}")
                else:
                    _pref = {1: 45, 2: 135, 3: 225, 4: 315}
                    result = (0, (_pref.get(home_q, 45), min(PREFERRED_DIAG_SOFT, _eff_cap), 0))
                    print(f"    [warn] unresolved placement at ({wx:.1f},{wy:.1f}) "
                          f"label={label_text!r}")

    # 硬约束：钳制过水平/过垂直并落入合法象限带
    _fa, _fd = result[1][0], result[1][1]
    ft = _fine_tune(_fd, _fa, draw_bbox)
    if ft is not None:
        _fa2, _fd2, _ = ft
        if not _has_conflict(_fa2, _fd2, draw_bbox):
            _fa, _fd = _fa2, _fd2
    _fa = _snap_leader_angle(_fa, home_q)
    # 仅当结果跑出允许象限时才钳回
    if home_q is not None and not any(
            _angle_in_quadrant(_fa, q) for q in _allowed_quads):
        _pref_angles = {1: 45, 2: 135, 3: 225, 4: 315}
        _candidates = [_pref_angles.get(home_q, 45)]
        a0, a1 = QUAD_ANGLE_RANGES[home_q]
        _candidates.extend(range(int(a0), int(a1) + 1, 5))
        for _ca in _candidates:
            if _leader_axis_ok(_ca) and not _has_conflict(_ca, _fd, draw_bbox):
                _fa = _ca
                break
        _fa = _snap_leader_angle(_fa, home_q)
    # 最终硬门闩：先同距换角，再从甜区起加长
    if _has_conflict(_fa, _fd, draw_bbox):
        _cleared = False
        for nd in range(PREFERRED_DIAG_MIN, _eff_cap + 1, 2):
            for da in (0, 8, -8, 15, -15, 25, -25, 35, -35):
                if not _has_conflict(_fa + da, nd, draw_bbox):
                    _fa, _fd = _fa + da, nd
                    _cleared = True
                    break
            if _cleared:
                break
    # 内框硬不变量：文字必须在框内
    _tbb_final = _text_bbox((wx, wy), _fd, _fa, label_text, is_pair=is_pair)
    if draw_bbox is not None and not _text_in_inner_frame(_tbb_final, draw_bbox):
        _fp = _shortest_in_frame_pose(
            (wx, wy), label_text, draw_bbox, is_pair=is_pair,
            home_q=home_q, prefer_ang=_fa, max_dist=_eff_cap)
        if _fp is not None:
            _fd, _fa = _fp
            for nd in range(PREFERRED_DIAG_MIN, max(int(_fd), PREFERRED_DIAG_MIN) + 1, 2):
                if not _has_conflict(_fa, nd, draw_bbox):
                    t2 = _text_bbox((wx, wy), nd, _fa, label_text, is_pair=is_pair)
                    if _text_in_inner_frame(t2, draw_bbox):
                        _fd = nd
                        break
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
                     home_q=None, quad_cx=None, quad_cy=None, label_text='',
                     wm_text_bboxes=None, part_bbox=None, prefer_down=False,
                     neighbor_angles=None, weld_pos=None, allow_adjacent=False,
                     corridor_quads=None, gap_prefer_ang=None, placed_leaders=None):
    """对 (角度, 距离) 位置评分。分值越高越推荐，正分表示无冲突，负分表示冲突严重。"""
    if wm_text_bboxes is None:
        wm_text_bboxes = []
    if neighbor_angles is None:
        neighbor_angles = []
    if placed_leaders is None:
        placed_leaders = []
    _part_bbox = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    score = 0
    rad = math.radians(angle_deg)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    px0, py0, px1, py1 = _part_bbox

    ex = wx + dist * cos_a
    ey = wy + dist * sin_a

    h_len = _horiz_land(label_text, is_pair)
    h_land = h_len if cos_a >= -0.05 else -h_len
    hx = ex + h_land
    hy = ey

    # 蓝×蓝：优先不相交；浅角交叉大罚；>45° 交叉小罚
    for ppos, pdist, pang, phland in placed_leaders:
        crosses, _ = _leader_crosses_leader(
            (wx, wy), dist, angle_deg, h_land, ppos, pdist, pang, phland)
        if not crosses:
            continue
        acute = _leader_cross_acute_deg(angle_deg, pang)
        if acute <= LEADER_CROSS_MIN_DEG:
            score -= 100000
        else:
            score -= 900

    lw = _label_text_width(label_text, is_pair)
    lh = _lh()
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
    _corr = set()  # 不再用走廊扩展允许象限
    if other_view_bboxes and gap_prefer_ang is None:
        _, _auto_gap, _ = _corridor_info(
            wx, wy, _part_bbox, other_view_bboxes, home_q=home_q)
        gap_prefer_ang = _auto_gap
    if home_q is not None:
        _allowed = set(_allowed_quadrants(
            home_q, allow_adjacent=prefer_down or allow_adjacent))
        if not any(_angle_in_quadrant(angle_deg, q) for q in _allowed):
            score -= 50000
        _label_q = _pos_home_quadrant(_cx_cand, _cy_cand, _vcx_s, _vcy_s)
        if _label_q not in _allowed:
            score -= 50000
        elif _label_q == home_q:
            score += 80
        elif prefer_down:
            score += 25

    # Part 下半区围焊：在归属象限内轻推文字到焊点下方（不改 home_q）
    if prefer_down:
        if _cy_cand < wy - 1.0:
            score += 90
        elif _cy_cand > wy + _lh() * 0.5:
            score -= 120
        if home_q in (3, 4):
            if sin_a < -0.15:
                score += 70
            if sin_a > 0.1:
                score -= 80
        elif home_q in (1, 2) and sin_a > 0.55:
            score -= 40

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
            score -= _extra_g * 100000
        # 渐进边界近距惩罚：离内框越近扣分越多
        _prox_x = max(0, dbx1 - bx1, bx0 - dbx0)
        _prox_y = max(0, dby1 - by1, by0 - dby0)
        _prox = min(_prox_x, _prox_y)
        if 0 < _prox < BOUNDARY_MARGIN * 2:
            score -= (BOUNDARY_MARGIN * 2 - _prox) * 800

    # Part-only 空白环偏好（part_bbox 必须是 part_only，不是整视图框）
    _inside_part = (px0 <= _cx_cand <= px1 and py0 <= _cy_cand <= py1)
    _inside_inner = True
    if draw_bbox is not None:
        dbx0, dby0, dbx1, dby1 = draw_bbox
        _inside_inner = (dbx0 <= _cx_cand <= dbx1 and dby0 <= _cy_cand <= dby1)
        if _inside_inner and not _inside_part:
            score += 300
    if _inside_part:
        score -= 1800

    # 跨视图：硬罚真正压入邻视图；走廊空白（两视图之间）反而加分，勿用近距大罚把标签赶走
    if other_view_bboxes:
        nbb = (min(wx, ex, bx0), max(wx, ex, bx1),
               min(wy, ey, by0), max(wy, ey, by1))
        _in_other_txt = False
        for _ovb in other_view_bboxes:
            if _ovb == (vx0, vy0, vx1, vy1):
                continue
            if _point_in_bbox_xyxy(_cx_cand, _cy_cand, _ovb, mrg=4):
                _in_other_txt = True
                score -= 100000
            # leader/text 包围盒真正切入邻视图
            if (nbb[1] > _ovb[0] and nbb[0] < _ovb[2]
                    and nbb[3] > _ovb[1] and nbb[2] < _ovb[3]):
                score -= 80000
        if _inside_inner and not _inside_part and not _in_other_txt:
            # 视图间走廊：空白区优先
            score += 1200
            if gap_prefer_ang is not None:
                _gdev = _angle_delta_deg(angle_deg, gap_prefer_ang)
                if _gdev < 50:
                    score += (50 - _gdev) * 18
            if dist <= 40:
                score += (40 - dist) * 8
            if home_q in (1, 2) and _cy_cand >= wy - 2:
                score += 80
            if home_q in (3, 4) and _cy_cand <= wy + 2:
                score += 80

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
            score -= 200

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
            score -= 15000

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
            score -= 15000

    # 文字与已放置标注文字重叠：扣2000（不可接受）
    for (pbx0, pbx1, pby0, pby1) in placed_text_bboxes:
        if bx1 > pbx0 - _OVERLAP_MARGIN and bx0 < pbx1 + _OVERLAP_MARGIN and by1 > pby0 - _OVERLAP_MARGIN and by0 < pby1 + _OVERLAP_MARGIN:
            score -= 15000
 
    # 文字与几何线过近：扣30（检测4个角点+4条边中点）
    _LINE_MARGIN = LINE_CLEARANCE
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
                score -= 500
                break
 
    # 文字边穿越几何线：扣500
    _txt_edges = [((bx0, by0), (bx1, by0)), ((bx1, by0), (bx1, by1)),
                  ((bx1, by1), (bx0, by1)), ((bx0, by1), (bx0, by0))]
    for (sx, sy), (ex2, ey2) in _near_lines:
        for (_s, _e) in _txt_edges:
            if _segments_cross_(_s, _e, (sx, sy), (ex2, ey2)):
                score -= 1500
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
        score -= 15000

    _min_center_dist = 999
    for (sx, sy), (ex2, ey2) in _near_lines:
        d, _ = _dist_pt_to_seg((cx_txt, cy_txt), (sx, sy), (ex2, ey2))
        if d < _min_center_dist:
            _min_center_dist = d
    if _min_center_dist < 1.5:
        score -= 200

    # 奖励远离构件几何边
    score += min(_min_center_dist, 20) * 8

    # 适中引线：过短与过长都扣分（偏短胜出）
    if dist < PREFERRED_DIAG_MIN:
        score -= (PREFERRED_DIAG_MIN - dist) * 20
    if dist > PREFERRED_DIAG_SOFT:
        score -= (dist - PREFERRED_DIAG_SOFT) * 12
    if dist > PREFERRED_DIAG_HARD:
        score -= (dist - PREFERRED_DIAG_HARD) * 25
    if dist > 42:
        score -= (dist - 42) * 30

    # 文字与圆/弧重叠：扣30
    for (ccx, ccy, cr) in circles:
        if bx1 > ccx - cr and bx0 < ccx + cr and by1 > ccy - cr and by0 < ccy + cr:
            score -= 30

    # 文字与 HATCH/SOLID 填充区重叠：扣2000
    if hatch_bboxes:
        _h_mrg = OVERLAP_MARGIN
        for (hx0, hx1, hy0, hy1) in hatch_bboxes:
            if bx1 > hx0 - _h_mrg and bx0 < hx1 + _h_mrg and by1 > hy0 - _h_mrg and by0 < hy1 + _h_mrg:
                score -= 5000

    # 文字与已放置标注重叠：扣20000（硬性惩罚，防止标签叠在一起）
    _OV_MARGIN = 8.0
    for (pbx0, pbx1, pby0, pby1) in placed_text_bboxes:
        if bx1 > pbx0 - _OV_MARGIN and bx0 < pbx1 + _OV_MARGIN and by1 > pby0 - _OV_MARGIN and by0 < pby1 + _OV_MARGIN:
            score -= 20000

    # 同高度横向拥挤惩罚
    for otb in placed_text_bboxes:
        ocy = (otb[2] + otb[3]) / 2
        if abs(_cy_cand - ocy) < _lh() * 2:
            if not (bx1 < otb[0] - _lh() * 4 or bx0 > otb[1] + _lh() * 4):
                score -= 800

    # WM / 3 SIDES TYP 重叠惩罚
    for (tx0, tx1, ty0, ty1) in wm_text_bboxes:
        if _text_overlaps((bx0, bx1, by0, by1), (tx0, tx1, ty0, ty1), WM_TEXT_MARGIN):
            score -= 15000

    # 角度偏好：惩罚过水平/过垂直，偏好 45° 方向
    _ang = angle_deg % 180
    if _ang > 90: _ang = 180 - _ang
    # 理想角度 45°，偏离越大惩罚越大
    _ang_dev = abs(_ang - 45)
    if _ang_dev > 20:
        score -= (_ang_dev - 20) * 5

    # 近邻分向：同簇夹角过小惩罚，约 60–120° 加分
    _wp = weld_pos if weld_pos is not None else (wx, wy)
    for npos, nang in neighbor_angles:
        if math.hypot(npos[0] - _wp[0], npos[1] - _wp[1]) > CLUSTER_RADIUS:
            continue
        gap = _angle_delta_deg(angle_deg, nang)
        if gap < DIVERGE_SCORE_CLOSE:
            score -= (DIVERGE_SCORE_CLOSE - gap) * 40
        elif 55 <= gap <= 125:
            score += 120
        elif gap > 150:
            score += 40

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


def _assign_y_slots(placements, placed_text_bboxes, placed_bboxes,
                    lines, text_bboxes, wm_text_bboxes, circles,
                    vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                    other_view_bboxes, quad_cx, quad_cy, other_view_part_bboxes,
                    cross_view_text_bboxes, part_bbox, part_down_bbox=None):
    """Stagger labels at similar Y that crowd horizontally."""
    _down_ref = part_down_bbox if part_down_bbox is not None else part_bbox
    y_tol = _lh() * 0.8
    x_gap = _lh() * 4
    slot_h = _y_slot_height()

    def _crowded(i, j):
        tbb_i, tbb_j = placed_text_bboxes[i], placed_text_bboxes[j]
        cy_i = (tbb_i[2] + tbb_i[3]) / 2
        cy_j = (tbb_j[2] + tbb_j[3]) / 2
        li = str(placements[i][4])
        lj = str(placements[j][4])
        _y_lim = y_tol * (2.2 if (li.startswith('W') and lj.startswith('W')) else 1.0)
        if abs(cy_i - cy_j) > _y_lim:
            return False
        if tbb_i[1] < tbb_j[0] - x_gap or tbb_i[0] > tbb_j[1] + x_gap:
            return False
        return (_text_overlaps(tbb_i, tbb_j, OVERLAP_MARGIN) or
                min(abs(tbb_i[0] - tbb_j[1]), abs(tbb_j[0] - tbb_i[1])) < x_gap or
                (li.startswith('W') and lj.startswith('W') and abs(cy_i - cy_j) < slot_h))

    for i in range(len(placements)):
        if not any(_crowded(i, j) for j in range(i)):
            continue
        gi, it_i, lb_i, pos_i, ltk_i, ds_i, ag_i, _ = placements[i]
        _other_bboxes = [p[7] for k, p in enumerate(placements) if k != i]
        _other_text = [otb for k, otb in enumerate(placed_text_bboxes) if k != i]
        hq = _weld_home_quadrant(pos_i[0], pos_i[1], quad_cx, quad_cy)
        _pd_i = _prefer_downward_weld(pos_i[0], pos_i[1], _down_ref) if _down_ref else False
        _allowed_i = _allowed_quadrants(hq, allow_adjacent=_pd_i)
        _max_len = MAX_DIAG_LEN_PAIR if gi == 'pair' else MAX_DIAG_LEN
        best = None
        best_score = -999999999

        def _try(nd, na):
            nonlocal best, best_score
            if not any(_angle_in_quadrant(na, q) for q in _allowed_i):
                return
            tbb = _text_bbox(pos_i, nd, na, ltk_i, is_pair=(gi == 'pair'))
            if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
                return
            if _text_near_lines(tbb, lines):
                return
            for otb in _other_text:
                if _text_overlaps(tbb, otb, OVERLAP_MARGIN):
                    return
            for wtb in wm_text_bboxes or []:
                if _text_overlaps(tbb, wtb, WM_TEXT_MARGIN):
                    return
            cy_new = (tbb[2] + tbb[3]) / 2
            score = 0
            for j in range(i):
                if not _crowded(i, j):
                    continue
                cy_j = (placed_text_bboxes[j][2] + placed_text_bboxes[j][3]) / 2
                score += min(abs(cy_new - cy_j), slot_h) * 50
            if score > best_score:
                if gi == 'pair':
                    nbb = _paired_bbox(pos_i, nd, na, ltk_i)
                else:
                    nbb = _single_bbox(pos_i, nd, na, ltk_i)
                best_score = score
                best = (nd, na, nbb, tbb)

        # 先同距换角，再少量加长
        for slot_mult in (1, -1, 2, -2):
            for deg_step in (5, 8, 10, 15, 20):
                _try(ds_i, ag_i + slot_mult * deg_step)
        for slot_mult in (1, -1, 2, -2):
            for deg_step in (5, 8, 10, 20):
                for extra in (2, 4, 8):
                    nd = min(ds_i + extra, _max_len)
                    na = ag_i + slot_mult * deg_step
                    _try(nd, na)
        _, nd_s, na_s = _search_placement(
            pos_i, lines, text_bboxes, circles, _other_bboxes,
            (cross_view_text_bboxes or []) + _other_text,
            vx0, vy0, vx1, vy1, draw_bbox, is_pair=(gi == 'pair'),
            hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
            home_q=hq, quad_cx=quad_cx, quad_cy=quad_cy,
            other_view_part_bboxes=other_view_part_bboxes,
            label_text=ltk_i, wm_text_bboxes=wm_text_bboxes,
            part_bbox=part_bbox, max_dist=_max_len, allow_adjacent=True)
        _try(nd_s, na_s)

        if best:
            nd, na, nbb, tbb = best
            placements[i] = (gi, it_i, lb_i, pos_i, ltk_i, nd, na, nbb)
            placed_text_bboxes[i] = tbb
            placed_bboxes[i] = nbb


def _redistribute_groups(groups, centroids, view_bbox=None):
    """Offset duplicate groups to avoid overlap — keep base at true dxf tip."""
    if not centroids:
        return
    _vy_center = (view_bbox[1] + view_bbox[3]) / 2.0 if view_bbox else None
    uniq_c = list(set(centroids))
    pos_map = {}
    for gi, (gtype, items) in enumerate(groups):
        pos = items[0][1]
        key = (round(pos[0], 1), round(pos[1], 1))
        pos_map.setdefault(key, []).append((gi, gtype, pos))
    for pos_key, entries in pos_map.items():
        if len(entries) <= 1:
            continue
        # Cap offset: never invent a tip farther than half a typical plate
        # depth from the true mid — prevents C-C style −173 → −186 jumps.
        _y_step = min(_lh() * 3.0, 4.0)
        for i_idx, (gi, gtype, (wx, wy)) in enumerate(entries):
            if i_idx == 0:
                continue
            if _vy_center is not None and wy > _vy_center:
                _offset_y = wy - _y_step * i_idx
            else:
                _offset_y = wy + _y_step * i_idx
            _, items = groups[gi]
            # Keep true x; only nudge y slightly for label separation
            groups[gi] = (gtype, [(it[0], (it[1][0], _offset_y)) for it in items])


def _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
    """检查 bbox (x0,x1,y0,y1) 在图纸内框范围内（含 BOUNDARY_MARGIN）。"""
    if draw_bbox is not None:
        dx0, dy0, dx1, dy1 = draw_bbox
        if not (dx0 + BOUNDARY_MARGIN <= nbb[0] and nbb[1] <= dx1 - BOUNDARY_MARGIN and
                dy0 + BOUNDARY_MARGIN <= nbb[2] and nbb[3] <= dy1 - BOUNDARY_MARGIN):
            return False
        return True
    if not (vx0 - 80 <= nbb[0] and nbb[1] <= vx1 + 80 and
            vy0 - 80 <= nbb[2] and nbb[3] <= vy1 + 80):
        return False
    return True


def _push_labels_off_hard_zones(
        placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, wm_text_bboxes, quad_cx, quad_cy, part_bbox,
        cross_view_text_bboxes=None):
    """Force-relocate labels whose text still overlaps WM/title/hole hard zones."""
    if not placements:
        return 0
    wm_text_bboxes = wm_text_bboxes or []
    _vcx = quad_cx if quad_cx is not None else (vx0 + vx1) / 2.0
    _vcy = quad_cy if quad_cy is not None else (vy0 + vy1) / 2.0
    _part = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _cross = cross_view_text_bboxes or []
    moved = 0
    _hatch = hatch_bboxes or []
    for i, pd in enumerate(placements):
        g, it, lb, pos, ltk, ds, ag = pd[:7]
        tbb = placed_text_bboxes[i]
        # Zones already include margin; use a small extra pad only.
        _circs = circles or []
        _on_hard = any(_text_overlaps(tbb, z, 1.0) for z in wm_text_bboxes)
        if not _on_hard and _hatch:
            _on_hard = any(_text_overlaps(tbb, z, 1.0) for z in _hatch)
        if not _on_hard and _circs:
            for ccx, ccy, cr in _circs:
                if not (tbb[1] < ccx - cr or tbb[0] > ccx + cr
                        or tbb[3] < ccy - cr or tbb[2] > ccy + cr):
                    _on_hard = True
                    break
        if not _on_hard:
            continue
        hq = _weld_home_quadrant(pos[0], pos[1], _vcx, _vcy)
        is_pair = (g == 'pair')
        _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
        others_tb = [otb for k, otb in enumerate(placed_text_bboxes) if k != i] + _cross
        others_bb = [obb for k, obb in enumerate(placed_bboxes) if k != i]
        _nbr = [(placements[k][3], placements[k][6])
                for k in range(len(placements)) if k != i]
        _leaders = [
            _leader_entry(placements[k][3], placements[k][5], placements[k][6],
                          placements[k][4], placements[k][0] == 'pair')
            for k in range(len(placements)) if k != i]
        best = None
        # Prefer flipping away from downward title band first
        for na in (ag + 180, ag + 140, ag - 140, ag + 90, ag - 90,
                   55, 125, 235, 305, ag + 45, ag - 45, ag + 25, ag - 25):
            na = na % 360
            if not _leader_axis_ok(na):
                continue
            if not any(_angle_in_quadrant(na, q)
                       for q in _allowed_quadrants(hq, allow_adjacent=True)):
                continue
            for nd in range(PREFERRED_DIAG_MIN, _max_len + 1, 2):
                ttbb = _text_bbox(pos, nd, na, ltk, is_pair=is_pair)
                if any(_text_overlaps(ttbb, z, 1.0) for z in wm_text_bboxes):
                    continue
                if any(_text_overlaps(ttbb, otb, OVERLAP_MARGIN) for otb in others_tb):
                    continue
                if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                    continue
                if _text_near_lines(ttbb, lines):
                    continue
                if hatch_bboxes and any(
                        _text_overlaps(ttbb, hb, OVERLAP_MARGIN)
                        for hb in hatch_bboxes):
                    continue
                if any(not (ttbb[1] < ccx - cr or ttbb[0] > ccx + cr
                            or ttbb[3] < ccy - cr or ttbb[2] > ccy + cr)
                       for ccx, ccy, cr in _circs):
                    continue
                h_land = _leader_entry(pos, nd, na, ltk, is_pair)[3]
                if _blue_leader_shallow_cross(pos, nd, na, h_land, _leaders):
                    continue
                nbb = (_paired_bbox(pos, nd, na, ltk) if is_pair
                       else _single_bbox(pos, nd, na, ltk))
                if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
                    continue
                best = (nd, na, nbb, ttbb)
                break
            if best:
                break
        if best is None:
            # Last resort: _search_placement with adjacent quadrants
            _sc, _fd, _fa = _search_placement(
                pos, lines, text_bboxes, circles, others_bb, others_tb,
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=hq, quad_cx=_vcx, quad_cy=_vcy,
                label_text=ltk, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_part, prefer_down=False, max_dist=_max_len,
                allow_adjacent=True, prefer_ang=(ag + 180) % 360,
                neighbor_angles=_nbr, cross_ok=False, placed_leaders=_leaders)
            ttbb = _text_bbox(pos, _fd, _fa, ltk, is_pair=is_pair)
            _ok_hard = not any(_text_overlaps(ttbb, z, 1.0) for z in wm_text_bboxes)
            if _ok_hard and _hatch:
                _ok_hard = not any(_text_overlaps(ttbb, z, 1.0) for z in _hatch)
            if _ok_hard:
                nbb = (_paired_bbox(pos, _fd, _fa, ltk) if is_pair
                       else _single_bbox(pos, _fd, _fa, ltk))
                best = (_fd, _fa, nbb, ttbb)
        if best is None:
            continue
        nd, na, nbb, ttbb = best
        placements[i] = (g, it, lb, pos, ltk, nd, na, nbb)
        placed_bboxes[i] = nbb
        placed_text_bboxes[i] = ttbb
        moved += 1
    return moved


def _fix_shallow_blue_leader_crosses(
        placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, wm_text_bboxes, quad_cx, quad_cy, part_bbox,
        cross_view_text_bboxes=None, max_iter=6):
    """Final pass: remove blue×blue crosses (prefer none; else acute >45°)."""
    if not placements:
        return 0
    _vcx = quad_cx if quad_cx is not None else (vx0 + vx1) / 2.0
    _vcy = quad_cy if quad_cy is not None else (vy0 + vy1) / 2.0
    _part = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _wm = wm_text_bboxes or []
    _cross_txt = cross_view_text_bboxes or []
    circles = circles or []
    fixed = 0

    def _entry(i):
        g, _it, _lb, pos, ltk, ds, ag = placements[i][:7]
        return _leader_entry(pos, ds, ag, ltk, g == 'pair')

    def _ok_pose(idx, pos, ltk, dist, angle, gtype, prefer_no_cross=True):
        rad = math.radians(angle)
        if abs(math.sin(rad)) < math.sin(math.radians(ANGLE_MIN)):
            return None
        if abs(math.cos(rad)) < math.cos(math.radians(ANGLE_MAX)):
            return None
        hq = _weld_home_quadrant(pos[0], pos[1], _vcx, _vcy)
        if not any(_angle_in_quadrant(angle, q)
                   for q in _allowed_quadrants(hq, allow_adjacent=True)):
            return None
        tbb = _text_bbox(pos, dist, angle, ltk, is_pair=(gtype == 'pair'))
        nbb = (_paired_bbox(pos, dist, angle, ltk) if gtype == 'pair'
               else _single_bbox(pos, dist, angle, ltk))
        if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
            return None
        if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
            return None
        for otb in list(placed_text_bboxes[:idx]) + list(placed_text_bboxes[idx + 1:]) + _cross_txt:
            if _text_overlaps(tbb, otb, OVERLAP_MARGIN):
                return None
        for bb in text_bboxes:
            if _text_overlaps(tbb, bb, OVERLAP_MARGIN):
                return None
        for bb in _wm:
            if _text_overlaps(tbb, bb, WM_TEXT_MARGIN):
                return None
        if hatch_bboxes:
            for bb in hatch_bboxes:
                if _text_overlaps(tbb, bb, OVERLAP_MARGIN):
                    return None
        if circles:
            for ccx, ccy, cr in circles:
                if not (tbb[1] < ccx - cr or tbb[0] > ccx + cr
                        or tbb[3] < ccy - cr or tbb[2] > ccy + cr):
                    return None
        if _text_near_lines(tbb, lines):
            return None
        h_land = _leader_entry(pos, dist, angle, ltk, gtype == 'pair')[3]
        for k in range(len(placements)):
            if k == idx:
                continue
            ppos, pdist, pang, ph = _entry(k)
            crosses, _ = _leader_crosses_leader(
                pos, dist, angle, h_land, ppos, pdist, pang, ph)
            if not crosses:
                continue
            # Prefer no blue×blue cross; shallow cross always illegal
            if prefer_no_cross or (
                    _leader_cross_acute_deg(angle, pang) <= LEADER_CROSS_MIN_DEG):
                return None
        return nbb, tbb

    for _ in range(max_iter):
        any_fix = False
        n = len(placements)
        for i in range(n):
            for j in range(i + 1, n):
                gi, iti, lbi, posi, ltki, dsi, agi = placements[i][:7]
                gj, itj, lbj, posj, ltkj, dsj, agj = placements[j][:7]
                hi = _horiz_land(ltki, gi == 'pair')
                hj = _horiz_land(ltkj, gj == 'pair')
                # sign of land from angle
                hi = hi if math.cos(math.radians(agi)) >= -0.05 else -hi
                hj = hj if math.cos(math.radians(agj)) >= -0.05 else -hj
                crosses, _ = _leader_crosses_leader(
                    posi, dsi, agi, hi, posj, dsj, agj, hj)
                if not crosses:
                    continue
                acute = _leader_cross_acute_deg(agi, agj)
                # Always try to remove the cross; shallow ones are mandatory
                prefer_no = True
                moved = False
                for target, g, it, lb, pos, ltk, ds, ag, peer in (
                    (j, gj, itj, lbj, posj, ltkj, dsj, agj, agi),
                    (i, gi, iti, lbi, posi, ltki, dsi, agi, agj),
                ):
                    hq = _weld_home_quadrant(pos[0], pos[1], _vcx, _vcy)
                    comp = _halfplane_complement(peer)
                    half = (DIVERGE_PREF_LEFT if hq in (2, 3)
                            else DIVERGE_PREF_RIGHT)
                    try_angs = [comp, half[0], half[1],
                                ag + 30, ag - 30, ag + 45, ag - 45,
                                ag + 55, ag - 55, ag + 20, ag - 20,
                                ag + 70, ag - 70, ag + 100, ag - 100]
                    _max_len = MAX_DIAG_LEN_PAIR if g == 'pair' else MAX_DIAG_LEN
                    for prefer_no_cross in (True, False):
                        # First: no cross at all; second: allow acute >45°
                        if (not prefer_no_cross
                                and acute > LEADER_CROSS_MIN_DEG):
                            # already steep; only no-cross pass was needed
                            break
                        for na in try_angs:
                            na = na % 360
                            for nd in (ds, min(ds + 8, _max_len),
                                       min(ds + 16, _max_len),
                                       min(ds + 24, _max_len),
                                       max(MIN_DIAG_LEN, ds - 6)):
                                if nd < MIN_DIAG_LEN or nd > _max_len:
                                    continue
                                res = _ok_pose(
                                    target, pos, ltk, nd, na, g,
                                    prefer_no_cross=prefer_no_cross)
                                if not res:
                                    continue
                                nbb, tbb = res
                                # Confirm vs peer
                                _oi = i if target == j else j
                                _po = placements[_oi]
                                _hl_o = _leader_entry(
                                    _po[3], _po[5], _po[6], _po[4],
                                    _po[0] == 'pair')[3]
                                _hl_n = _leader_entry(
                                    pos, nd, na, ltk, g == 'pair')[3]
                                still, _ = _leader_crosses_leader(
                                    pos, nd, na, _hl_n,
                                    _po[3], _po[5], _po[6], _hl_o)
                                if still and prefer_no_cross:
                                    continue
                                if still and (
                                        _leader_cross_acute_deg(na, _po[6])
                                        <= LEADER_CROSS_MIN_DEG):
                                    continue
                                placements[target] = (
                                    g, it, lb, pos, ltk, nd, na, nbb)
                                placed_bboxes[target] = nbb
                                placed_text_bboxes[target] = tbb
                                moved = True
                                fixed += 1
                                break
                            if moved:
                                break
                        if moved:
                            break
                    if moved:
                        break
                if moved:
                    any_fix = True
                    break
            if any_fix:
                break
        if not any_fix:
            break
    return fixed


def _resolve_label_conflicts(msp, lines, text_bboxes, circles,
                              vx0, vy0, vx1, vy1, draw_bbox, placements, placed_text_bboxes, max_iter=8,
                              hatch_bboxes=None, other_view_bboxes=None,
                              quad_cx=None, quad_cy=None,
                              other_view_part_bboxes=None, cross_view_text_bboxes=None,
                              wm_text_bboxes=None, part_bbox=None):
    """全局后处理：检测标注间的文字重叠并进行综合微调（距离/角度/方向翻转/双向调整）。"""
    _OVERLAP_MARGIN = OVERLAP_MARGIN
    _vcx = quad_cx if quad_cx is not None else (vx0 + vx1) / 2.0
    _vcy = quad_cy if quad_cy is not None else (vy0 + vy1) / 2.0
    _cross_text = cross_view_text_bboxes or []
    if wm_text_bboxes is None:
        wm_text_bboxes = []
    _part_bbox = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _total_fixes = 0

    def _foreign_text_bboxes(skip_idx):
        return _cross_text + [otb for k, otb in enumerate(placed_text_bboxes) if k != skip_idx]

    def _leader_crosses_text(pos, dist, angle, gtype, tb, label_text=''):
        """检查斜引线或水平接地线是否穿过文字框 tb。"""
        rad = math.radians(angle)
        ex = pos[0] + dist * math.cos(rad)
        ey = pos[1] + dist * math.sin(rad)
        h_len = _horiz_land(label_text, gtype == 'pair')
        h_land = h_len if math.cos(rad) >= -0.05 else -h_len
        hx = ex + h_land
        hy = ey
        if _seg_cross_rect(pos, (ex, ey), tb[0], tb[1], tb[2], tb[3]):
            return True
        if _seg_cross_rect((ex, ey), (hx, hy), tb[0], tb[1], tb[2], tb[3]):
            return True
        return False

    def _adjust_safe(idx, pos, label_text, dist, angle, gtype, skip_idx):
        """检查调整后的位置是否安全（文字无重叠、引线不穿几何线/文字、文字远离构件边）。"""
        # 角度有效性检查：拒绝过水平/过垂直及越界象限的角度
        rad = math.radians(angle)
        sin_a, cos_a = math.sin(rad), math.cos(rad)
        if abs(sin_a) < math.sin(math.radians(ANGLE_MIN)):
            return None
        if abs(cos_a) < math.cos(math.radians(ANGLE_MAX)):
            return None
        # 象限检查
        wx, wy = pos
        hq = _weld_home_quadrant(wx, wy, _vcx, _vcy)
        _pd = _prefer_downward_weld(wx, wy, _part_bbox)
        _allowed_hq = _allowed_quadrants(hq, allow_adjacent=_pd)
        if not any(_angle_in_quadrant(angle, q) for q in _allowed_hq):
            return None

        if gtype == 'pair':
            nbb = _paired_bbox(pos, dist, angle, label_text)
        else:
            nbb = _single_bbox(pos, dist, angle, label_text)
        if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
            return None

        _tbb = _text_bbox(pos, dist, angle, label_text, is_pair=(gtype == 'pair'))
        if draw_bbox is not None and not _text_in_inner_frame(_tbb, draw_bbox):
            return None
        # 纯文字 vs 已放置/跨视图文字框（跳过自身）
        for otb in _foreign_text_bboxes(skip_idx):
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

        for (tx0, tx1, ty0, ty1) in wm_text_bboxes:
            if not (_tbb[1] < tx0 - WM_TEXT_MARGIN or
                    _tbb[0] > tx1 + WM_TEXT_MARGIN or
                    _tbb[3] < ty0 - WM_TEXT_MARGIN or
                    _tbb[2] > ty1 + WM_TEXT_MARGIN):
                return None

        wx, wy = pos
        rad = math.radians(angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        ex = wx + dist * cos_a
        ey = wy + dist * sin_a
        h_len = _horiz_land(label_text, gtype == 'pair')
        h_land = h_len if cos_a >= -0.05 else -h_len
        hx = ex + h_land
        hy = ey

        # 水平接地线 / 对角引线 与几何线交叉
        _cx0, _cx1 = min(nbb[0], hx, ex, wx), max(nbb[1], hx, ex, wx)
        _cy0, _cy1 = min(nbb[2], hy, ey, wy), max(nbb[3], hy, ey, wy)
        for (sx, sy), (ex2, ey2) in lines:
            if max(sx, ex2) < _cx0 - 5 or min(sx, ex2) > _cx1 + 5: continue
            if max(sy, ey2) < _cy0 - 5 or min(sy, ey2) > _cy1 + 5: continue
            if _segments_cross_((ex, ey), (hx, hy), (sx, sy), (ex2, ey2)):
                return None
            if _segments_cross_((wx, wy), (ex, ey), (sx, sy), (ex2, ey2)):
                return None

        # 斜引线与图纸文字框 / WM 文字交叉
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
                return None
        for (tx0, tx1, ty0, ty1) in wm_text_bboxes:
            if _seg_cross_rect((wx, wy), (ex, ey), tx0, tx1, ty0, ty1):
                return None

        # 斜引线与已放置/跨视图标注文字框交叉
        for otb in _foreign_text_bboxes(skip_idx):
            if _seg_cross_rect((wx, wy), (ex, ey), otb[0], otb[1], otb[2], otb[3]):
                return None

        # 水平接地线与已放置/跨视图标注文字框交叉
        for otb in _foreign_text_bboxes(skip_idx):
            if _seg_cross_rect((ex, ey), (hx, hy), otb[0], otb[1], otb[2], otb[3]):
                return None

        # 水平接地线与图纸原始文字框交叉
        for (tx0, tx1, ty0, ty1) in text_bboxes:
            if _seg_cross_rect((ex, ey), (hx, hy), tx0, tx1, ty0, ty1):
                return None

        # 文字与圆/弧重叠；对角引线不得穿 WM 圆
        for (ccx, ccy, cr) in circles:
            if not (_tbb[1] < ccx - cr or _tbb[0] > ccx + cr or
                    _tbb[3] < ccy - cr or _tbb[2] > ccy + cr):
                return None
            if _dist_pt_to_seg((ccx, ccy), (wx, wy), (ex, ey))[0] < cr:
                return None

        # 文字与 HATCH 填充区重叠；对角引线不得穿 hatch
        if hatch_bboxes:
            for (hx0, hx1, hy0, hy1) in hatch_bboxes:
                if not (_tbb[1] < hx0 - _OVERLAP_MARGIN or _tbb[0] > hx1 + _OVERLAP_MARGIN or
                        _tbb[3] < hy0 - _OVERLAP_MARGIN or _tbb[2] > hy1 + _OVERLAP_MARGIN):
                    return None
                if _seg_cross_rect((wx, wy), (ex, ey), hx0, hx1, hy0, hy1):
                    return None

        if _text_near_lines(_tbb, lines):
            return None

        return nbb, _tbb

    for _iter in range(max_iter):
        _any_fix = False
        n = len(placements)
        for i in range(n):
            gi, it_i, lb_i, pos_i, ltk_i, ds_i, ag_i, _ = placements[i]
            tbb_i = placed_text_bboxes[i]
            for j in range(i + 1, n):
                gj, it_j, lb_j, pos_j, ltk_j, ds_j, ag_j, _ = placements[j]
                tbb_j = placed_text_bboxes[j]
                if (tbb_i[1] < tbb_j[0] - _OVERLAP_MARGIN or
                    tbb_i[0] > tbb_j[1] + _OVERLAP_MARGIN or
                    tbb_i[3] < tbb_j[2] - _OVERLAP_MARGIN or
                    tbb_i[2] > tbb_j[3] + _OVERLAP_MARGIN):
                    # 文字不重叠，检查引线是否穿过对方文字
                    if not (_leader_crosses_text(pos_i, ds_i, ag_i, gi, tbb_j, ltk_i) or
                            _leader_crosses_text(pos_j, ds_j, ag_j, gj, tbb_i, ltk_j)):
                        # 蓝×蓝：优先不相交；无法避免则夹角须 >45°
                        _h_i = _horiz_land(ltk_i, gi == 'pair')
                        _h_j = _horiz_land(ltk_j, gj == 'pair')
                        _crosses, _ = _leader_crosses_leader(
                            pos_i, ds_i, ag_i, _h_i, pos_j, ds_j, ag_j, _h_j)
                        if not _crosses:
                            continue
                        _acute = _leader_cross_acute_deg(ag_i, ag_j)
                        # >45° 交叉可保留；≤45° 必须改
                        if _acute > LEADER_CROSS_MIN_DEG:
                            continue
                        _cross_fixed = False
                        for target, g, it, lb, pos, ltk, ds, ag, peer_ag in [
                            (j, gj, it_j, lb_j, pos_j, ltk_j, ds_j, ag_j, ag_i),
                            (i, gi, it_i, lb_i, pos_i, ltk_i, ds_i, ag_i, ag_j),
                        ]:
                            _hq_t = _weld_home_quadrant(pos[0], pos[1], _vcx, _vcy)
                            _comp = _halfplane_complement(peer_ag)
                            _pref_t = {1: 55, 2: 135, 3: 225, 4: 315}.get(_hq_t, ag)
                            _half = (DIVERGE_PREF_LEFT if _hq_t in (2, 3)
                                     else DIVERGE_PREF_RIGHT)
                            # 优先互补同半区角，避免两引线都斜向同一缝
                            _try_angs = [_comp, _half[0], _half[1], _pref_t,
                                         ag + 25, ag - 25, ag + 40, ag - 40,
                                         ag + 55, ag - 55, ag + 15, ag - 15]
                            for na in _try_angs:
                                na = na % 360
                                ra = math.radians(na)
                                if abs(math.sin(ra)) < math.sin(math.radians(ANGLE_MIN)): continue
                                if abs(math.cos(ra)) < math.cos(math.radians(ANGLE_MAX)): continue
                                if not any(_angle_in_quadrant(na, q) for q in
                                           _allowed_quadrants(_hq_t, allow_adjacent=True)):
                                    continue
                                for nd in (ds, min(ds + 6, MAX_DIAG_LEN),
                                           min(ds + 12, MAX_DIAG_LEN),
                                           max(PREFERRED_DIAG_MIN, ds - 4)):
                                    if nd < MIN_DIAG_LEN or nd > MAX_DIAG_LEN:
                                        continue
                                    result = _adjust_safe(target, pos, ltk, nd, na, g, target)
                                    if not result:
                                        continue
                                    nbb, tbb = result
                                    # 确认与对端：不相交，或交叉角 >45°
                                    _oi = i if target == j else j
                                    _po = placements[_oi]
                                    _hl_o = _horiz_land(_po[4], _po[0] == 'pair')
                                    _hl_n = _horiz_land(ltk, g == 'pair')
                                    still, _ = _leader_crosses_leader(
                                        pos, nd, na, _hl_n,
                                        _po[3], _po[5], _po[6], _hl_o)
                                    if still and (
                                            _leader_cross_acute_deg(na, _po[6])
                                            <= LEADER_CROSS_MIN_DEG):
                                        continue
                                    placements[target] = (g, it, lb, pos, ltk, nd, na, nbb)
                                    placed_text_bboxes[target] = tbb
                                    _cross_fixed = True
                                    break
                                if _cross_fixed:
                                    break
                            if _cross_fixed:
                                break
                        if _cross_fixed:
                            _fixed = True
                            break
                        continue

                _fixed = False

                # 依次尝试调整 j → 调整 i
                for target, g, it, lb, pos, ltk, ds, ag in [
                    (j, gj, it_j, lb_j, pos_j, ltk_j, ds_j, ag_j),
                    (i, gi, it_i, lb_i, pos_i, ltk_i, ds_i, ag_i),
                ]:
                    _max_len = MAX_DIAG_LEN_PAIR if g == 'pair' else MAX_DIAG_LEN
                    # --- 先同距换角 ---
                    for d_a in [1, -1, 2, -2, 3, -3, 5, -5, 8, -8, 12, -12, 15, -15, 20, -20, 25, -25, 30, -30]:
                        na = ag + d_a
                        r5 = math.radians(na % 360)
                        if abs(math.sin(r5)) < math.sin(math.radians(ANGLE_MIN)): continue
                        if abs(math.cos(r5)) < math.cos(math.radians(ANGLE_MAX)): continue
                        result = _adjust_safe(target, pos, ltk, ds, na, g, target)
                        if result:
                            nbb, tbb = result
                            placements[target] = (g, it, lb, pos, ltk, ds, na, nbb)
                            placed_text_bboxes[target] = tbb
                            _fixed = True
                            break
                    if _fixed: break

                    # --- 方向翻转（同距优先）---
                    _opp_ang = (ag + 180) % 360
                    for _oa in [_opp_ang, (_opp_ang-12)%360, (_opp_ang+12)%360,
                                (_opp_ang-24)%360, (_opp_ang+24)%360]:
                        r6 = math.radians(_oa)
                        if abs(math.sin(r6)) < math.sin(math.radians(ANGLE_MIN)): continue
                        if abs(math.cos(r6)) < math.cos(math.radians(ANGLE_MAX)): continue
                        for od in [ds, ds - 4, ds + 4, ds - 8, ds + 8]:
                            if od < MIN_DIAG_LEN or od > _max_len: continue
                            result = _adjust_safe(target, pos, ltk, od, _oa, g, target)
                            if result:
                                nbb, tbb = result
                                placements[target] = (g, it, lb, pos, ltk, od, _oa, nbb)
                                placed_text_bboxes[target] = tbb
                                _fixed = True
                                break
                        if _fixed: break
                    if _fixed: break

                    # --- 再小步调距（含 Y 槽位）---
                    _dist_tries = [1, -1, 2, -2, 4, -4, 8, -8, 12, -12, 16, -16, 20, -20]
                    _ang_tries = [0, 8, -8, 12, -12, 15, -15, 20, -20]
                    for d_dist in _dist_tries:
                        for y_slot in (0, 1, -1, 2, -2):
                            nd = ds + d_dist + abs(y_slot) * 2
                            if nd < MIN_DIAG_LEN or nd > _max_len:
                                continue
                            for d_a in _ang_tries:
                                na = ag + d_a + y_slot * 12
                                result = _adjust_safe(target, pos, ltk, nd, na, g, target)
                                if result:
                                    nbb, tbb = result
                                    placements[target] = (g, it, lb, pos, ltk, nd, na, nbb)
                                    placed_text_bboxes[target] = tbb
                                    _fixed = True
                                    break
                            if _fixed:
                                break
                        if _fixed:
                            break
                    if _fixed:
                        break

                    # 距离增大 + 角度偏移（上限 MAX_DIAG_LEN）
                    for d_dist in [4, 8, 12, 16, 20, 24]:
                        nd = ds + d_dist
                        if nd < MIN_DIAG_LEN or nd > _max_len: continue
                        for d_a in [-15, -10, -5, 5, 10, 15, 20, 25, 30]:
                            na = ag + d_a
                            r6 = math.radians(na % 360)
                            if abs(math.sin(r6)) < math.sin(math.radians(ANGLE_MIN)): continue
                            if abs(math.cos(r6)) < math.cos(math.radians(ANGLE_MAX)): continue
                            result = _adjust_safe(target, pos, ltk, nd, na, g, target)
                            if result:
                                nbb, tbb = result
                                placements[target] = (g, it, lb, pos, ltk, nd, na, nbb)
                                placed_text_bboxes[target] = tbb
                                _fixed = True
                                break
                        if _fixed: break
                    if _fixed: break

                    # --- 全局重新搜索（兜底，不超 MAX_DIAG_LEN）---
                    _, _ds, _ag = _search_placement(
                        pos, lines, text_bboxes, circles,
                        [p[7] for p in placements], _cross_text + placed_text_bboxes,
                        vx0, vy0, vx1, vy1, draw_bbox,
                        is_pair=(g == 'pair'), hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                        other_view_part_bboxes=other_view_part_bboxes,
                        label_text=ltk, wm_text_bboxes=wm_text_bboxes, part_bbox=_part_bbox,
                        max_dist=_max_len, allow_adjacent=True)
                    _re_result = _adjust_safe(target, pos, ltk, _ds, _ag, g, target)
                    if _re_result:
                        _nbb, _tbb = _re_result
                        placements[target] = (g, it, lb, pos, ltk, _ds, _ag, _nbb)
                        placed_text_bboxes[target] = _tbb
                        _fixed = True

                if _fixed:
                    _any_fix = True
                    _total_fixes += 1

        if not _any_fix:
            break

    # ---- 标注-WM文字重叠专项修复 ----
    if wm_text_bboxes or text_bboxes or hatch_bboxes:
        for _wm_iter in range(2):
            _wm_fixed = False
            n = len(placements)
            for ki in range(n):
                gi, iti, lbi, pi, ltki, dsi, agi = placements[ki][:7]
                tbbi = placed_text_bboxes[ki]
                _wm_over = False
                for (tx0, tx1, ty0, ty1) in wm_text_bboxes:
                    if _text_overlaps(tbbi, (tx0, tx1, ty0, ty1), WM_TEXT_MARGIN):
                        _wm_over = True
                        break
                if not _wm_over:
                    for (tx0, tx1, ty0, ty1) in text_bboxes:
                        if not (tbbi[1] < tx0 - OVERLAP_MARGIN or
                                tbbi[0] > tx1 + OVERLAP_MARGIN or
                                tbbi[3] < ty0 - OVERLAP_MARGIN or
                                tbbi[2] > ty1 + OVERLAP_MARGIN):
                            _wm_over = True
                            break
                if not _wm_over and hatch_bboxes:
                    for (hx0, hx1, hy0, hy1) in hatch_bboxes:
                        if not (tbbi[1] < hx0 - OVERLAP_MARGIN or
                                tbbi[0] > hx1 + OVERLAP_MARGIN or
                                tbbi[3] < hy0 - OVERLAP_MARGIN or
                                tbbi[2] > hy1 + OVERLAP_MARGIN):
                            _wm_over = True
                            break
                if not _wm_over:
                    continue
                _max_len = MAX_DIAG_LEN_PAIR if gi == 'pair' else MAX_DIAG_LEN
                _ext_max = _max_len
                # --- 优先小角度微调（保留原长度）---
                for d_a in [5, -5, 8, -8, 12, -12, 15, -15, 20, -20, 3, -3, 1, -1, 25, -25, 30, -30]:
                    na = agi + d_a
                    r5 = math.radians(na % 360)
                    if abs(math.sin(r5)) < math.sin(math.radians(ANGLE_MIN)): continue
                    if abs(math.cos(r5)) < math.cos(math.radians(ANGLE_MAX)): continue
                    result = _adjust_safe(ki, pi, ltki, dsi, na, gi, ki)
                    if result:
                        nbb, tbb = result
                        placements[ki] = (gi, iti, lbi, pi, ltki, dsi, na, nbb)
                        placed_text_bboxes[ki] = tbb
                        _wm_fixed = True; break
                if _wm_fixed: continue
                # --- 适度拉长 / 缩短（同角度）---
                for d_dist in [4, -4, 8, -8, 12, -12, 16, 20, 24, 28, 32, 36, 40, 44, 48]:
                    nd = dsi + d_dist
                    if nd < 6 or nd > _ext_max: continue
                    result = _adjust_safe(ki, pi, ltki, nd, agi, gi, ki)
                    if result:
                        nbb, tbb = result
                        placements[ki] = (gi, iti, lbi, pi, ltki, nd, agi, nbb)
                        placed_text_bboxes[ki] = tbb
                        _wm_fixed = True; break
                if _wm_fixed: continue
                # --- 距离+角度组合（限 ±40°）---
                for c_dist in [4, 6, 8, 10, 12, 16, 20, 24, 28, 32, 36, 40, 44, 48]:
                    nd = dsi + c_dist
                    if nd < 6 or nd > _ext_max: continue
                    for c_ang in [-15, 15, -10, 10, -20, 20, -25, 25, -30, 30, -40, 40, -5, 5]:
                        na = agi + c_ang
                        r_c = math.radians(na % 360)
                        if abs(math.sin(r_c)) < math.sin(math.radians(ANGLE_MIN)): continue
                        if abs(math.cos(r_c)) < math.cos(math.radians(ANGLE_MAX)): continue
                        result = _adjust_safe(ki, pi, ltki, nd, na, gi, ki)
                        if result:
                            nbb, tbb = result
                            placements[ki] = (gi, iti, lbi, pi, ltki, nd, na, nbb)
                            placed_text_bboxes[ki] = tbb
                            _wm_fixed = True; break
                    if _wm_fixed: break
                if _wm_fixed: continue
                _, _ds, _ag = _search_placement(
                    pi, lines, text_bboxes, circles,
                    [p[7] for p in placements], _cross_text + placed_text_bboxes,
                    vx0, vy0, vx1, vy1, draw_bbox,
                    is_pair=(gi == 'pair'), hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                    home_q=_weld_home_quadrant(pi[0], pi[1], _vcx, _vcy),
                    quad_cx=_vcx, quad_cy=_vcy,
                    other_view_part_bboxes=other_view_part_bboxes,
                    max_dist=_ext_max, label_text=ltki,
                    wm_text_bboxes=wm_text_bboxes, part_bbox=_part_bbox)
                _re_result = _adjust_safe(ki, pi, ltki, _ds, _ag, gi, ki)
                if _re_result:
                    _nbb, _tbb = _re_result
                    placements[ki] = (gi, iti, lbi, pi, ltki, _ds, _ag, _nbb)
                    placed_text_bboxes[ki] = _tbb
                    _wm_fixed = True
            if not _wm_fixed:
                break

    # ---- 文字与构件边重合专项修复 ----
    for _line_iter in range(2):
        _line_fixed = False
        n = len(placements)
        for ki in range(n):
            gi, iti, lbi, pi, ltki, dsi, agi = placements[ki][:7]
            tbbi = placed_text_bboxes[ki]
            if not _text_near_lines(tbbi, lines):
                continue
            _max_len = MAX_DIAG_LEN_PAIR if gi == 'pair' else MAX_DIAG_LEN
            _ext_max = _max_len
            # 先小角度，再适度距离
            for d_a in [5, -5, 10, -10, 15, -15, 20, -20, 25, -25, 30, -30]:
                na = agi + d_a
                r5 = math.radians(na % 360)
                if abs(math.sin(r5)) < math.sin(math.radians(ANGLE_MIN)):
                    continue
                if abs(math.cos(r5)) < math.cos(math.radians(ANGLE_MAX)):
                    continue
                result = _adjust_safe(ki, pi, ltki, dsi, na, gi, ki)
                if result:
                    nbb, tbb = result
                    placements[ki] = (gi, iti, lbi, pi, ltki, dsi, na, nbb)
                    placed_text_bboxes[ki] = tbb
                    _line_fixed = True
                    break
            if _line_fixed:
                continue
            for d_dist in [2, -2, 4, -4, 8, -8, 12, -12, 16, -16, 20, -20, 24, -24, 28, -28, 32, -32, 36, -36, 40]:
                nd = dsi + d_dist
                if nd < 6 or nd > _ext_max:
                    continue
                result = _adjust_safe(ki, pi, ltki, nd, agi, gi, ki)
                if result:
                    nbb, tbb = result
                    placements[ki] = (gi, iti, lbi, pi, ltki, nd, agi, nbb)
                    placed_text_bboxes[ki] = tbb
                    _line_fixed = True
                    break
            if _line_fixed:
                continue
            for c_dist in [6, 10, 14, 18, 22, 26, 30, 34, 38, 42, 46]:
                nd = dsi + c_dist
                if nd < 6 or nd > _ext_max:
                    continue
                for c_ang in [-15, 15, -10, 10, -25, 25, -35, 35]:
                    na = agi + c_ang
                    r_c = math.radians(na % 360)
                    if abs(math.sin(r_c)) < math.sin(math.radians(ANGLE_MIN)):
                        continue
                    if abs(math.cos(r_c)) < math.cos(math.radians(ANGLE_MAX)):
                        continue
                    result = _adjust_safe(ki, pi, ltki, nd, na, gi, ki)
                    if result:
                        nbb, tbb = result
                        placements[ki] = (gi, iti, lbi, pi, ltki, nd, na, nbb)
                        placed_text_bboxes[ki] = tbb
                        _line_fixed = True
                        break
                if _line_fixed:
                    break
            if _line_fixed:
                continue
            # 受限二次搜索：定点失败时再搜一次
            _wx, _wy = pi
            _hq = _weld_home_quadrant(_wx, _wy, _vcx, _vcy)
            _others = [placed_text_bboxes[j] for j in range(n) if j != ki]
            _pb = []
            for j in range(n):
                if j == ki:
                    continue
                _pb.append(placements[j][7])
            try:
                _, nd, na = _search_placement(
                    pi, lines, text_bboxes, circles, _pb, _others,
                    vx0, vy0, vx1, vy1, draw_bbox,
                    is_pair=(gi == 'pair'), home_q=_hq,
                    quad_cx=_vcx, quad_cy=_vcy, label_text=ltki,
                    wm_text_bboxes=wm_text_bboxes, hatch_bboxes=hatch_bboxes,
                    other_view_bboxes=other_view_bboxes,
                    other_view_part_bboxes=other_view_part_bboxes,
                    max_dist=_max_len, allow_adjacent=True)
                result = _adjust_safe(ki, pi, ltki, nd, na, gi, ki)
                if result:
                    nbb, tbb = result
                    placements[ki] = (gi, iti, lbi, pi, ltki, nd, na, nbb)
                    placed_text_bboxes[ki] = tbb
                    _line_fixed = True
            except Exception:
                pass
        if not _line_fixed:
            break

    # ---- 残留冲突：同距换角 → 再短步加长；二次搜索不超 MAX_DIAG_LEN ----
    for _ext_iter in range(4):
        _ext_fixed = False
        n = len(placements)
        for ki in range(n):
            gi, iti, lbi, pi, ltki, dsi, agi = placements[ki][:7]
            tbbi = placed_text_bboxes[ki]
            _need = False
            for kj in range(n):
                if kj == ki:
                    continue
                if _text_overlaps(tbbi, placed_text_bboxes[kj], OVERLAP_MARGIN):
                    _need = True
                    break
            if not _need:
                for wtb in wm_text_bboxes:
                    if _text_overlaps(tbbi, wtb, WM_TEXT_MARGIN):
                        _need = True
                        break
            if not _need and hatch_bboxes:
                for htb in hatch_bboxes:
                    if _text_overlaps(tbbi, htb, OVERLAP_MARGIN):
                        _need = True
                        break
            if not _need and _text_near_lines(tbbi, lines):
                _need = True
            if not _need:
                continue
            _max_len = MAX_DIAG_LEN_PAIR if gi == 'pair' else MAX_DIAG_LEN
            # 同距换角
            for d_a in [5, -5, 10, -10, 15, -15, 20, -20, 25, -25, 30, -30]:
                na = agi + d_a
                r5 = math.radians(na % 360)
                if abs(math.sin(r5)) < math.sin(math.radians(ANGLE_MIN)):
                    continue
                if abs(math.cos(r5)) < math.cos(math.radians(ANGLE_MAX)):
                    continue
                result = _adjust_safe(ki, pi, ltki, dsi, na, gi, ki)
                if result:
                    nbb, tbb = result
                    placements[ki] = (gi, iti, lbi, pi, ltki, dsi, na, nbb)
                    placed_text_bboxes[ki] = tbb
                    _ext_fixed = True
                    break
            if _ext_fixed:
                continue
            # 短步加长 + 换角
            for d_dist in [2, 4, 8, 12, 16, 20, 24]:
                nd = dsi + d_dist
                if nd < MIN_DIAG_LEN or nd > _max_len:
                    continue
                for d_a in [0, 5, -5, 10, -10, 15, -15, 20, -20]:
                    na = agi + d_a
                    r5 = math.radians(na % 360)
                    if abs(math.sin(r5)) < math.sin(math.radians(ANGLE_MIN)):
                        continue
                    if abs(math.cos(r5)) < math.cos(math.radians(ANGLE_MAX)):
                        continue
                    result = _adjust_safe(ki, pi, ltki, nd, na, gi, ki)
                    if result:
                        nbb, tbb = result
                        placements[ki] = (gi, iti, lbi, pi, ltki, nd, na, nbb)
                        placed_text_bboxes[ki] = tbb
                        _ext_fixed = True
                        break
                if _ext_fixed:
                    break
            if _ext_fixed:
                continue
            _wx, _wy = pi
            _hq = _weld_home_quadrant(_wx, _wy, _vcx, _vcy)
            _others = [placed_text_bboxes[j] for j in range(n) if j != ki]
            _pb = [placements[j][7] for j in range(n) if j != ki]
            try:
                _, nd, na = _search_placement(
                    pi, lines, text_bboxes, circles, _pb, _others,
                    vx0, vy0, vx1, vy1, draw_bbox,
                    is_pair=(gi == 'pair'), home_q=_hq,
                    quad_cx=_vcx, quad_cy=_vcy, label_text=ltki,
                    wm_text_bboxes=wm_text_bboxes, hatch_bboxes=hatch_bboxes,
                    other_view_bboxes=other_view_bboxes,
                    other_view_part_bboxes=other_view_part_bboxes,
                    max_dist=_max_len, allow_adjacent=True)
                result = _adjust_safe(ki, pi, ltki, nd, na, gi, ki)
                if result:
                    nbb, tbb = result
                    placements[ki] = (gi, iti, lbi, pi, ltki, nd, na, nbb)
                    placed_text_bboxes[ki] = tbb
                    _ext_fixed = True
            except Exception:
                pass
        if not _ext_fixed:
            break

    # 最终安全兜底：文字不得超出内框（最短框内位姿）
    for k, pd in enumerate(placements):
        gk, it_k, lb_k, pk, ltk, dsk, agk, bbk = pd
        _tb = _text_bbox(pk, dsk, agk, ltk, is_pair=(gk == 'pair'))
        if draw_bbox is not None and _text_in_inner_frame(_tb, draw_bbox):
            continue
        if draw_bbox is None and _bbox_in_boundary(bbk, vx0, vy0, vx1, vy1, draw_bbox):
            continue
        _max_len = MAX_DIAG_LEN_PAIR if gk == 'pair' else MAX_DIAG_LEN
        _hq = _weld_home_quadrant(pk[0], pk[1], _vcx, _vcy)
        _fp = _shortest_in_frame_pose(
            pk, ltk, draw_bbox, is_pair=(gk == 'pair'),
            home_q=_hq, prefer_ang=agk, max_dist=_max_len) if draw_bbox else None
        if _fp is not None:
            nd, na = _fp
            result = _adjust_safe(k, pk, ltk, nd, na, gk, k)
            if result:
                nbb, tbb = result
                placements[k] = (gk, it_k, lb_k, pk, ltk, nd, na, nbb)
                placed_text_bboxes[k] = tbb
            else:
                if gk == 'pair':
                    nbb = _paired_bbox(pk, nd, na, ltk)
                else:
                    nbb = _single_bbox(pk, nd, na, ltk)
                placements[k] = (gk, it_k, lb_k, pk, ltk, nd, na, nbb)
                placed_text_bboxes[k] = _text_bbox(pk, nd, na, ltk, is_pair=(gk == 'pair'))
            continue
        for nd in range(MIN_DIAG_LEN, min(dsk, _max_len) + 1, 2):
            if gk == 'pair':
                nbb = _paired_bbox(pk, nd, agk, ltk)
            else:
                nbb = _single_bbox(pk, nd, agk, ltk)
            _tb2 = _text_bbox(pk, nd, agk, ltk, is_pair=(gk == 'pair'))
            if draw_bbox is not None and not _text_in_inner_frame(_tb2, draw_bbox):
                continue
            if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
                continue
            placements[k] = (gk, it_k, lb_k, pk, ltk, nd, agk, nbb)
            placed_text_bboxes[k] = _tb2
            break

    return _total_fixes


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
