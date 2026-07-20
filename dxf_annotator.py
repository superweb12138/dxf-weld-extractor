"""
DXF Weld Annotator — adds weld labels to DXF drawings based on extracted results.

Rules:
  - CJP → W{n} prefix
  - FW/PP/PJP → F{n} prefix
  - Numbering: clockwise within each view, global sequential across views
  - Left-half welds → leader line points left
  - Right-half welds → leader line points right
  - Above/Below pair at same position → one leader, two labels stacked
  - Output: annotated/gb/ directory, new layer WELD_LABELS in magenta
    (EU annotations go to annotated/eu/ via dxf_annotator_eu)
"""

import os, math, re
from collections import defaultdict

import ezdxf

FOLDER = os.path.dirname(os.path.abspath(__file__))
ANNOTATED_DIR = os.path.join(FOLDER, "annotated", "gb")

SCALE = 10          # 1 CAD unit = 10 mm
# Unified weld-label height (matches section titles A-A / B-B when detected).
# Dense-area shrinking removed — all labels use the same height.
LABEL_HEIGHT = 2.5
LABEL_HEIGHT_DENSE = 2.5      # kept for compat; no longer used to shrink
LABEL_HEIGHT_VERY_DENSE = 2.5
DENSE_VIEW_N = 5
VERY_DENSE_VIEW_N = 10
DENSE_CLUSTER_N = 2
VERY_DENSE_CLUSTER_N = 3
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
DIVERGE_ANGLE_MIN = 40.0     # near neighbors with smaller angle gap → re-search
DIVERGE_SCORE_CLOSE = 45.0   # angle gap below this → score penalty
NEAR_COINCIDENT_DIST = 6.0   # almost-same weld tip → force same-side up/down pair
STACK_PEER_DIST = 16.0       # same-side vertical stack (e.g. F25↔F31) → up/down pair

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
PREFERRED_DIAG_MIN = 18                # 常规甜区下限（局部空白袋可更短）
PREFERRED_DIAG_SOFT = 24               # 甜区中心偏短侧
PREFERRED_DIAG_HARD = 38               # 超过开始明显偏长
LOCAL_POCKET_MAX = 30                  # 焊点附近局部空白袋最大引线长
LADDER_SLOT_PITCH = 2.4                # 竖向梯子格距 = _lh() * 此系数
LADDER_X_STAGGER = 1.35                # 共用走廊 X 微交错 = _lh() * 此系数
LADDER_MAX_DIAG = 50                   # 强制空白硬上限 = MAX_DIAG；优先 ≤ PREFERRED_DIAG_HARD
EXHAUSTIVE_MAX_DIAG = 50               # 终检改角/改长上限 = MAX_DIAG；禁止为救标超长
LADDER_PARALLEL_MIN_DEG = 35.0         # 同簇近平行硬下限
LADDER_TIP_Y_MIN = 0.9                 # tip Y 最小间距 = pitch * 此系数
LADDER_GAP_DEPTH = 0.22                # 走廊 tip 靠本侧缝边（半区导向，勿堆缝心）
WM_TEXT_MARGIN = 8.0                   # WM / 3 SIDES TYP 避让边距
WM_SYMBOL_RADIUS = 18.0              # 焊缝符号禁区半径（紧凑符号区，不含长引线）
WM_SYMBOL_LINE_MAX = 35.0            # 计入符号 AABB 的短线最大长度
WM_SYMBOL_CLUSTER_R = 45.0           # 短线相对 WM 文字中心的聚类半径
SECTION_TITLE_MARGIN = 8.0           # D-D / 1:10 等剖面标题硬禁区边距
# Blue×blue cross: prefer none; angles ≤ this are always illegal (was 45°).
LEADER_CROSS_MIN_DEG = 70.0

# Hard exclusion margins
BOM_MARGIN = 0.5             # table AABB pad: text four edges stay ≥0.5 outside table
BOUNDARY_MARGIN = 0.5        # inner-frame inset: text four edges stay ≥0.5 inside frame
HATCH_CLEAR_MARGIN = 0.0     # hatch hard overlap (tables already padded by BOM_MARGIN)

_SECTION_TITLE_RE = re.compile(
    r'^(?:[A-Z]\s*[-–—]\s*[A-Z]|\d+\s*:\s*\d+)$', re.I)


def _lh():
    """Current unified weld-label text height (section-title matched)."""
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


def _detect_section_title_height(doc, default=None):
    """Median height of A-A / 1:10 style titles in the drawing (model + blocks).

    Section titles are often split ('A','-','A','1:10'), so single uppercase
    letters and scale texts are both considered.
    """
    default = LABEL_HEIGHT if default is None else default
    letter_hs, scale_hs, joined_hs = [], [], []

    def _collect(entity):
        try:
            dt = entity.dxftype()
            if dt in ('TEXT', 'ATTDEF', 'ATTRIB'):
                txt = (entity.dxf.text or '').strip()
                h = float(getattr(entity.dxf, 'height', 0) or 0)
            elif dt == 'MTEXT':
                txt = (entity.text or '').replace('\\n', ' ').strip()
                h = float(getattr(entity.dxf, 'char_height', 0) or 0)
            else:
                return
        except Exception:
            return
        if not txt or h < 2.0:
            return
        cmp_ = txt.replace(' ', '')
        if _SECTION_TITLE_RE.match(txt) or _SECTION_TITLE_RE.match(cmp_):
            if re.search(r'[A-Za-z]', cmp_):
                joined_hs.append(h)
            else:
                scale_hs.append(h)
        elif len(txt) == 1 and txt.isalpha() and txt.isupper():
            letter_hs.append(h)

    try:
        for e in doc.modelspace():
            _collect(e)
        for blk in doc.blocks:
            for e in blk:
                _collect(e)
    except Exception:
        pass
    # Prefer joined 'A-A', then scale '1:10' (paired with titles), then letters.
    # Standalone letters can be other annotations at a different height.
    pool = joined_hs or scale_hs or letter_hs
    if not pool:
        return default
    pool.sort()
    mid = pool[len(pool) // 2]
    return max(2.0, min(4.5, mid))


def _height_from_cluster_n(n):
    """Density no longer shrinks labels — always use active/unified height."""
    return _lh()


def _choose_view_label_height(groups):
    """Unified label height for the whole view (no dense shrink)."""
    return _lh()


def _group_label_heights(groups):
    """Per-group heights: all equal to the active (section-title) height."""
    positions = [(items[0][1][0], items[0][1][1]) for _gtype, items in groups]
    h = _lh()
    return [h] * len(groups), positions


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
            _text_overlaps(ttbb, htb, HATCH_CLEAR_MARGIN) for htb in hatch_bboxes):
        return False
    return True


def _label_hard_clear(tbb, others_tb, lines, draw_bbox,
                      wm_text_bboxes=None, hatch_bboxes=None):
    """Final print gate: no overlap, no near-lines, no out-of-frame."""
    if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
        return False
    if not _text_clears_obstacles(tbb, others_tb, wm_text_bboxes, hatch_bboxes):
        return False
    if _text_near_lines(tbb, lines):
        return False
    return True


def _text_crosses_lines(tbb, lines):
    """True only if a geometry segment intersects the text rectangle."""
    if not tbb or not lines:
        return False
    bx0, bx1, by0, by1 = tbb
    for (sx, sy), (ex2, ey2) in lines:
        if bx1 < min(sx, ex2) or bx0 > max(sx, ex2):
            continue
        if by1 < min(sy, ey2) or by0 > max(sy, ey2):
            continue
        if _seg_cross_rect((sx, sy), (ex2, ey2), bx0, bx1, by0, by1):
            return True
        _txt_edges = [((bx0, by0), (bx1, by0)), ((bx1, by0), (bx1, by1)),
                      ((bx1, by1), (bx0, by1)), ((bx0, by1), (bx0, by0))]
        for (_s, _e) in _txt_edges:
            if _segments_cross_(_s, _e, (sx, sy), (ex2, ey2)):
                return True
    return False


def _label_corridor_soft_clear(tbb, others_tb, lines, draw_bbox,
                               wm_text_bboxes=None, hatch_bboxes=None,
                               in_gap=False):
    """Corridor diverge soft clear: ignore inflated pads, forbid real hits.

    In the gap: hatch pads are ignored (often cover the whole strip), but
    green WM text must not overlap — label must sit fully left/right of it
    (pushes F25 further into the corridor past the weld mark).
    Outside the gap: hard WM/hatch overlap (margin 0).
    Always forbids: blue-label overlap, geometry segment through text.
    """
    if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
        return False
    if any(_text_overlaps(tbb, otb, OVERLAP_MARGIN) for otb in others_tb):
        return False
    if _text_crosses_lines(tbb, lines):
        return False
    if wm_text_bboxes:
        for wtb in wm_text_bboxes:
            if not _text_overlaps(tbb, wtb, 0.0):
                continue
            if in_gap:
                # Fully clear of WM: text entirely on one side with 1.5 pad
                if tbb[1] < wtb[0] - 1.5 or tbb[0] > wtb[1] + 1.5:
                    continue
            return False
    if (not in_gap) and hatch_bboxes and any(
            _text_overlaps(tbb, htb, HATCH_CLEAR_MARGIN)
            for htb in hatch_bboxes):
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


def _apply_pose(idx, placements, placed_bboxes, placed_text_bboxes,
                nd, na, tbb):
    gi, iti, lbi, pos, lti = placements[idx][:5]
    is_pair = (gi == 'pair')
    nbb = (_paired_bbox(pos, nd, na, lti) if is_pair
           else _single_bbox(pos, nd, na, lti))
    placements[idx] = (gi, iti, lbi, pos, lti, nd, na, nbb)
    placed_bboxes[idx] = nbb
    placed_text_bboxes[idx] = tbb
    return True


def _label_has_blue_cross(idx, placements):
    """True if this label's blue leader shallow-crosses any other blue leader."""
    n = len(placements)
    gi, _, _, pos, lti, dsi, agi = placements[idx][:7]
    is_pair = (gi == 'pair')
    h_land = _horiz_land(lti, is_pair)
    cos_a = math.cos(math.radians(agi % 360))
    h_sign = h_land if cos_a >= -0.05 else -h_land
    others = [
        _leader_entry(placements[k][3], placements[k][5], placements[k][6],
                      placements[k][4], placements[k][0] == 'pair')
        for k in range(n) if k != idx
    ]
    return _blue_leader_shallow_cross(pos, dsi, agi, h_sign, others)


def _find_near_coincident_peer(idx, placements, max_dist=None):
    """Nearest other weld tip within max_dist (default NEAR_COINCIDENT_DIST)."""
    cap = NEAR_COINCIDENT_DIST if max_dist is None else max_dist
    pos = placements[idx][3]
    best_j, best_d = None, cap + 1e-9
    for j in range(len(placements)):
        if j == idx:
            continue
        d = math.hypot(placements[j][3][0] - pos[0],
                       placements[j][3][1] - pos[1])
        if d < best_d:
            best_d = d
            best_j = j
    return best_j


def _find_stack_peer(idx, placements, cx, max_dist=None):
    """Nearest same L/R-half tip within STACK_PEER_DIST (vertical stack)."""
    cap = STACK_PEER_DIST if max_dist is None else max_dist
    pos = placements[idx][3]
    hq = 1 if pos[0] >= cx else 2
    best_j, best_d = None, cap + 1e-9
    for j in range(len(placements)):
        if j == idx:
            continue
        pj = placements[j][3]
        # same left/right half of view
        if (pos[0] >= cx) != (pj[0] >= cx):
            continue
        d = math.hypot(pj[0] - pos[0], pj[1] - pos[1])
        if d < best_d:
            best_d = d
            best_j = j
    return best_j


def _pose_ok_gates(pos, nd, na, lti, is_pair, others_tb, lines, draw_bbox,
                   wm_text_bboxes, hatch_bboxes, leaders, hq,
                   part_bbox=None, other_view_bboxes=None, old_tbb=None):
    """Text hard-clear + blue×blue + same L/R half (+ corridor/neighbor)."""
    if not _leader_axis_ok(na):
        return None
    if hq is not None and not any(
            _angle_in_quadrant(na, q)
            for q in _allowed_quadrants(hq, allow_adjacent=True)):
        return None
    tbb = _text_bbox(pos, nd, na, lti, is_pair=is_pair)
    if not _label_hard_clear(
            tbb, others_tb, lines, draw_bbox, wm_text_bboxes, hatch_bboxes):
        return None
    if part_bbox is not None or other_view_bboxes:
        if not _corridor_pose_acceptable(
                pos[0], pos[1], old_tbb, tbb, part_bbox, other_view_bboxes,
                require_keep_gap=(old_tbb is not None)):
            return None
    h_land = _horiz_land(lti, is_pair)
    cos_a = math.cos(math.radians(na % 360))
    h_sign = h_land if cos_a >= -0.05 else -h_land
    if _blue_leader_shallow_cross(pos, nd, na, h_sign, leaders):
        return None
    return tbb


def _force_up_down_pair(i, j, placements, placed_bboxes, placed_text_bboxes,
                        lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                        cross_view_text_bboxes, cx, cy,
                        part_bbox=None, other_view_bboxes=None):
    """Near-coincident pair: higher→up band, lower→dn band (same L/R half only)."""
    pi, pj = placements[i][3], placements[j][3]
    if pi[1] >= pj[1]:
        hi, lo = i, j
    else:
        hi, lo = j, i
    right = ((pi[0] + pj[0]) / 2.0) >= cx
    if right:
        up_cands = [55, 45, 65, 35, 75, 50, 40, 60]
        dn_cands = [305, 315, 295, 325, 285, 300, 310, 320]
    else:
        # Include near-level left angles so corridor tips can diverge inside the gap
        up_cands = [135, 145, 125, 155, 115, 160, 170, 130, 140, 150]
        dn_cands = [200, 190, 210, 180, 220, 225, 215, 235, 205, 245, 230]
    n = len(placements)
    dists = list(range(PREFERRED_DIAG_MIN, MAX_DIAG_LEN + 1, 2))
    _ov = other_view_bboxes

    def _others(exclude):
        return ([placed_text_bboxes[k] for k in range(n) if k not in exclude]
                + list(cross_view_text_bboxes or []))

    def _leaders(exclude):
        return [
            _leader_entry(placements[k][3], placements[k][5], placements[k][6],
                          placements[k][4], placements[k][0] == 'pair')
            for k in range(n) if k not in exclude
        ]

    ghi, _, _, phi, lthi = placements[hi][:5]
    glo, _, _, plo, ltlo = placements[lo][:5]
    hi_pair = (ghi == 'pair')
    lo_pair = (glo == 'pair')
    hq_hi = _weld_home_quadrant(phi[0], phi[1], cx, cy)
    hq_lo = _weld_home_quadrant(plo[0], plo[1], cx, cy)
    best = None
    best_sc = -1e18
    for au in up_cands:
        for ad in dn_cands:
            if _angle_delta_deg(au, ad) < DIVERGE_ANGLE_MIN:
                continue
            for du in dists:
                tbb_u = _pose_ok_gates(
                    phi, du, au, lthi, hi_pair, _others({hi, lo}), lines,
                    draw_bbox, wm_text_bboxes, hatch_bboxes,
                    _leaders({hi, lo}), hq_hi,
                    part_bbox=part_bbox, other_view_bboxes=_ov,
                    old_tbb=placed_text_bboxes[hi])
                if tbb_u is None:
                    continue
                for dd in dists:
                    # include provisional upper leader when testing lower
                    leaders_lo = _leaders({hi, lo}) + [
                        _leader_entry(phi, du, au, lthi, hi_pair)]
                    others_lo = _others({hi, lo}) + [tbb_u]
                    tbb_d = _pose_ok_gates(
                        plo, dd, ad, ltlo, lo_pair, others_lo, lines,
                        draw_bbox, wm_text_bboxes, hatch_bboxes,
                        leaders_lo, hq_lo,
                        part_bbox=part_bbox, other_view_bboxes=_ov,
                        old_tbb=placed_text_bboxes[lo])
                    if tbb_d is None:
                        continue
                    # mutual cross (upper vs lower)
                    h_u = _horiz_land(lthi, hi_pair)
                    h_d = _horiz_land(ltlo, lo_pair)
                    cos_u = math.cos(math.radians(au % 360))
                    cos_d = math.cos(math.radians(ad % 360))
                    hs_u = h_u if cos_u >= -0.05 else -h_u
                    hs_d = h_d if cos_d >= -0.05 else -h_d
                    if _blue_leader_shallow_cross(
                            phi, du, au, hs_u,
                            [_leader_entry(plo, dd, ad, ltlo, lo_pair)]):
                        continue
                    if _blue_leader_shallow_cross(
                            plo, dd, ad, hs_d,
                            [_leader_entry(phi, du, au, lthi, hi_pair)]):
                        continue
                    sc = -(du + dd) * 20
                    if du <= PREFERRED_DIAG_HARD:
                        sc += 40
                    if dd <= PREFERRED_DIAG_HARD:
                        sc += 40
                    if part_bbox and _ov:
                        _, _, gbox = _corridor_info(
                            plo[0], plo[1], part_bbox, _ov, home_q=None)
                        if gbox is not None:
                            if _text_in_gap_box(tbb_u, gbox):
                                sc += 120
                            if _text_in_gap_box(tbb_d, gbox):
                                sc += 120
                    if sc > best_sc:
                        best_sc = sc
                        best = (hi, du, au, tbb_u, lo, dd, ad, tbb_d)
                if best is not None and best[1] <= PREFERRED_DIAG_SOFT:
                    break
            if best is not None and best[1] <= PREFERRED_DIAG_SOFT:
                break
        if best is not None and best[1] <= PREFERRED_DIAG_SOFT:
            break
    if best is None:
        return False
    hi, du, au, tbb_u, lo, dd, ad, tbb_d = best
    _apply_pose(hi, placements, placed_bboxes, placed_text_bboxes, du, au, tbb_u)
    _apply_pose(lo, placements, placed_bboxes, placed_text_bboxes, dd, ad, tbb_d)
    return True


def _relocate_blockers_for_target(target_idx, placements, placed_bboxes,
                                  placed_text_bboxes, lines, draw_bbox,
                                  hatch_bboxes, wm_text_bboxes,
                                  cross_view_text_bboxes, cx, cy, max_n=4,
                                  part_bbox=None, other_view_bboxes=None):
    """Move overlapping / near labels aside (same L/R half, no cross relax)."""
    n = len(placements)
    if n < 2:
        return 0
    tbb = placed_text_bboxes[target_idx]
    tpos = placements[target_idx][3]
    blockers = []
    for k in range(n):
        if k == target_idx:
            continue
        otb = placed_text_bboxes[k]
        ox = max(0, min(tbb[1], otb[1]) - max(tbb[0], otb[0]))
        oy = max(0, min(tbb[3], otb[3]) - max(tbb[2], otb[2]))
        near = (math.hypot(placements[k][3][0] - tpos[0],
                           placements[k][3][1] - tpos[1])
                <= CLUSTER_RADIUS * 1.2)
        if not ((ox > 0 and oy > 0) or near):
            continue
        blockers.append((ox * oy + (40 if near else 0), k))
    if not blockers:
        return 0
    blockers.sort(reverse=True)
    moved = 0
    for _, k in blockers[:max_n]:
        hq = _weld_home_quadrant(placements[k][3][0], placements[k][3][1], cx, cy)
        if _exhaustive_hard_clear_pose(
                k, placements, placed_bboxes, placed_text_bboxes,
                lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                cross_view_text_bboxes, max_diag=MAX_DIAG_LEN,
                relax_parallel=False, home_q=hq, cx=cx, cy=cy,
                part_bbox=part_bbox, other_view_bboxes=other_view_bboxes):
            moved += 1
    return moved


def _scheme_repair_one(idx, placements, placed_bboxes, placed_text_bboxes,
                       lines, text_bboxes, circles, vx0, vy0, vx1, vy1,
                       draw_bbox, hatch_bboxes, other_view_bboxes,
                       other_view_part_bboxes, wm_text_bboxes, cx, cy,
                       part_bbox, line_grid, cross_view_text_bboxes):
    """终检：仅改角/改长；锁 L/R 半区；短优先；字硬清 + 蓝×蓝不交叉。"""
    gi, iti, lbi, pos, lti, dsi, agi = placements[idx][:7]
    is_pair = (gi == 'pair')
    hq = _weld_home_quadrant(pos[0], pos[1], cx, cy)
    _force_dn = any(w.get('_prefer_leader_down') for w, _p in iti)
    if _force_dn:
        hq = _downward_quad_same_half(hq)
    others_bb = [placed_bboxes[k] for k in range(len(placements)) if k != idx]
    others_tb = ([placed_text_bboxes[k] for k in range(len(placements)) if k != idx]
                 + list(cross_view_text_bboxes or []))
    nbrs = [(placements[k][3], placements[k][6])
            for k in range(len(placements)) if k != idx]
    leaders = [
        _leader_entry(placements[k][3], placements[k][5], placements[k][6],
                      placements[k][4], placements[k][0] == 'pair')
        for k in range(len(placements)) if k != idx
    ]
    # Seed: current angle + same-half up/dn (prefer band opposite nearest peer)
    up_s, dn_s = (DIVERGE_PREF_RIGHT if hq in (1, 4) else DIVERGE_PREF_LEFT)
    peer = _find_near_coincident_peer(idx, placements)
    seed_angs = [agi, up_s, dn_s]
    if peer is not None:
        pref_self, _ = _diverge_prefer_angs(
            pos, placements[peer][3], cx, cy)
        seed_angs = [pref_self, _halfplane_complement(pref_self), agi, up_s, dn_s]
    if _force_dn:
        seed_angs = [dn_s, 305.0 if hq == 4 else 225.0, agi, up_s]
    _ov_corr = (other_view_part_bboxes
                if other_view_part_bboxes else other_view_bboxes)
    _old_tbb = placed_text_bboxes[idx]
    if part_bbox and _ov_corr:
        _, _gap_seed, _ = _corridor_info(
            pos[0], pos[1], part_bbox, _ov_corr, home_q=None)
        if _gap_seed is not None:
            seed_angs = [_gap_seed % 360] + [
                a for a in seed_angs if _angle_delta_deg(a, _gap_seed) > 3]
    # Short-first caps with explicit up/dn seeds
    for prefer in seed_angs:
        for cap in (PREFERRED_DIAG_SOFT, PREFERRED_DIAG_HARD, MAX_DIAG_LEN):
            _, nd, na = _search_placement(
                pos, lines, text_bboxes, circles, others_bb, others_tb,
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=lti, wm_text_bboxes=wm_text_bboxes,
                part_bbox=part_bbox, prefer_down=_force_dn,
                line_grid=line_grid, allow_adjacent=True,
                prefer_ang=prefer % 360, neighbor_angles=nbrs,
                max_dist=int(cap), cross_ok=False, placed_leaders=leaders)
            tbb = _pose_ok_gates(
                pos, nd, na, lti, is_pair, others_tb, lines, draw_bbox,
                wm_text_bboxes, hatch_bboxes, leaders, hq,
                part_bbox=part_bbox, other_view_bboxes=_ov_corr,
                old_tbb=_old_tbb)
            if tbb is None:
                continue
            if _force_dn and _leader_half_band(na) != 'dn':
                continue
            return _apply_pose(idx, placements, placed_bboxes, placed_text_bboxes,
                               nd, na, tbb)
    # Exhaustive within home L/R half
    if _exhaustive_hard_clear_pose(
            idx, placements, placed_bboxes, placed_text_bboxes,
            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
            cross_view_text_bboxes, max_diag=MAX_DIAG_LEN,
            relax_parallel=False, home_q=hq, cx=cx, cy=cy,
            part_bbox=part_bbox, other_view_bboxes=_ov_corr):
        return True
    # Near-coincident / vertical stack: force joint up/down
    for peer_try in (peer, _find_stack_peer(idx, placements, cx)):
        if peer_try is None:
            continue
        if _force_up_down_pair(
                idx, peer_try, placements, placed_bboxes, placed_text_bboxes,
                lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                cross_view_text_bboxes, cx, cy,
                part_bbox=part_bbox, other_view_bboxes=_ov_corr):
            return True
    # Nudge blockers then retry exhaustive + pair
    if _relocate_blockers_for_target(
            idx, placements, placed_bboxes, placed_text_bboxes,
            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
            cross_view_text_bboxes, cx, cy, max_n=8,
            part_bbox=part_bbox, other_view_bboxes=_ov_corr):
        if _exhaustive_hard_clear_pose(
                idx, placements, placed_bboxes, placed_text_bboxes,
                lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                cross_view_text_bboxes, max_diag=MAX_DIAG_LEN,
                relax_parallel=False, home_q=hq, cx=cx, cy=cy,
                part_bbox=part_bbox, other_view_bboxes=_ov_corr):
            return True
        for peer2 in (_find_near_coincident_peer(idx, placements),
                      _find_stack_peer(idx, placements, cx)):
            if peer2 is not None and _force_up_down_pair(
                    idx, peer2, placements, placed_bboxes, placed_text_bboxes,
                    lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                    cross_view_text_bboxes, cx, cy,
                    part_bbox=part_bbox, other_view_bboxes=_ov_corr):
                return True
    # Last resort: no blue×blue relax; drop near-parallel; softer clearances
    if _exhaustive_hard_clear_pose(
            idx, placements, placed_bboxes, placed_text_bboxes,
            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
            cross_view_text_bboxes, max_diag=MAX_DIAG_LEN,
            relax_parallel=True, home_q=hq, cx=cx, cy=cy,
            part_bbox=part_bbox, other_view_bboxes=_ov_corr):
        return True
    if _exhaustive_hard_clear_pose(
            idx, placements, placed_bboxes, placed_text_bboxes,
            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
            cross_view_text_bboxes, max_diag=MAX_DIAG_LEN,
            relax_parallel=True, home_q=hq, cx=cx, cy=cy,
            soft_lines=True, soft_obstacles=True,
            part_bbox=part_bbox, other_view_bboxes=_ov_corr):
        return True
    # Nuclear (dense leftover only): keep text clear, allow obtuse blue cross
    if _exhaustive_hard_clear_pose(
            idx, placements, placed_bboxes, placed_text_bboxes,
            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
            cross_view_text_bboxes, max_diag=MAX_DIAG_LEN,
            relax_parallel=True, home_q=hq, cx=cx, cy=cy,
            soft_lines=True, soft_obstacles=True, relax_cross=True,
            part_bbox=part_bbox, other_view_bboxes=_ov_corr):
        return True
    return False


def _force_all_near_coincident_pairs(placements, placed_bboxes, placed_text_bboxes,
                                     lines, draw_bbox, hatch_bboxes,
                                     wm_text_bboxes, cross_view_text_bboxes,
                                     cx, cy, part_bbox=None,
                                     other_view_bboxes=None):
    """Force every near-coincident tip pair into same-side up/down bands."""
    n = len(placements)
    used = set()
    n_fixed = 0
    pairs = []
    for i in range(n):
        if i in used:
            continue
        for j in range(i + 1, n):
            if j in used:
                continue
            pi, pj = placements[i][3], placements[j][3]
            if math.hypot(pi[0] - pj[0], pi[1] - pj[1]) > NEAR_COINCIDENT_DIST:
                continue
            pairs.append((i, j))
            used.add(i)
            used.add(j)
            break
    for i, j in pairs:
        # skip if already opposite bands with enough angle gap and both clear
        bi = _leader_half_band(placements[i][6])
        bj = _leader_half_band(placements[j][6])
        if (bi in ('up', 'dn') and bj in ('up', 'dn') and bi != bj
                and _angle_delta_deg(placements[i][6], placements[j][6])
                >= DIVERGE_ANGLE_MIN):
            oi = ([placed_text_bboxes[k] for k in range(n) if k != i]
                  + list(cross_view_text_bboxes or []))
            oj = ([placed_text_bboxes[k] for k in range(n) if k != j]
                  + list(cross_view_text_bboxes or []))
            if (_label_hard_clear(placed_text_bboxes[i], oi, lines, draw_bbox,
                                  wm_text_bboxes, hatch_bboxes)
                    and _label_hard_clear(placed_text_bboxes[j], oj, lines,
                                          draw_bbox, wm_text_bboxes, hatch_bboxes)
                    and not _label_has_blue_cross(i, placements)
                    and not _label_has_blue_cross(j, placements)):
                continue
        if _force_up_down_pair(
                i, j, placements, placed_bboxes, placed_text_bboxes,
                lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                cross_view_text_bboxes, cx, cy,
                part_bbox=part_bbox, other_view_bboxes=other_view_bboxes):
            n_fixed += 1
    return n_fixed


def _scheme_final_repair(placements, placed_bboxes, placed_text_bboxes,
                         lines, text_bboxes, circles, vx0, vy0, vx1, vy1,
                         draw_bbox, hatch_bboxes, other_view_bboxes,
                         other_view_part_bboxes, wm_text_bboxes, cx, cy,
                         part_bbox, line_grid, cross_view_text_bboxes):
    """终检：违规（字/交叉）→ 改角改长；近重合强制上下分向。"""
    n = len(placements)
    if n == 0:
        return 0
    fixed = 0
    _ov_corr = (other_view_part_bboxes
                if other_view_part_bboxes else other_view_bboxes)
    _n_pair = _force_all_near_coincident_pairs(
        placements, placed_bboxes, placed_text_bboxes, lines, draw_bbox,
        hatch_bboxes, wm_text_bboxes, cross_view_text_bboxes, cx, cy,
        part_bbox=part_bbox, other_view_bboxes=_ov_corr)
    if _n_pair:
        fixed += _n_pair
        print(f"    [scheme] forced {_n_pair} near-coincident up/down pair(s)")
    for _round in range(3):
        dirty = []
        for i in range(n):
            others_tb = (
                [placed_text_bboxes[k] for k in range(n) if k != i]
                + list(cross_view_text_bboxes or []))
            text_bad = not _label_hard_clear(
                placed_text_bboxes[i], others_tb, lines, draw_bbox,
                wm_text_bboxes, hatch_bboxes)
            cross_bad = _label_has_blue_cross(i, placements)
            if text_bad or cross_bad:
                dirty.append(i)
        if not dirty:
            break
        progressed = False
        # Prefer repairing near-coincident peers jointly first
        seen = set()
        for i in dirty:
            if i in seen:
                continue
            peer = _find_near_coincident_peer(i, placements)
            if peer is not None and peer in dirty:
                if _force_up_down_pair(
                        i, peer, placements, placed_bboxes, placed_text_bboxes,
                        lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                        cross_view_text_bboxes, cx, cy):
                    fixed += 1
                    progressed = True
                    seen.add(i)
                    seen.add(peer)
                    continue
            if _scheme_repair_one(
                    i, placements, placed_bboxes, placed_text_bboxes,
                    lines, text_bboxes, circles, vx0, vy0, vx1, vy1,
                    draw_bbox, hatch_bboxes, other_view_bboxes,
                    other_view_part_bboxes, wm_text_bboxes, cx, cy,
                    part_bbox, line_grid, cross_view_text_bboxes):
                fixed += 1
                progressed = True
                seen.add(i)
        if not progressed:
            break
    return fixed


def _exhaustive_hard_clear_pose(idx, placements, placed_bboxes, placed_text_bboxes,
                                lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                                cross_view_text_bboxes, max_diag=None,
                                relax_parallel=False, home_q=None, cx=None, cy=None,
                                soft_lines=False, soft_obstacles=False,
                                relax_cross=False, part_bbox=None,
                                other_view_bboxes=None):
    """Full angle×distance scan; short-first; blue×blue unless relax_cross."""
    n = len(placements)
    gi, iti, lbi, pos, lti, dsi, agi = placements[idx][:7]
    is_pair = (gi == 'pair')
    cap = int(max_diag if max_diag is not None else MAX_DIAG_LEN)
    cap = min(cap, MAX_DIAG_LEN, EXHAUSTIVE_MAX_DIAG)
    others_tb = (
        [placed_text_bboxes[k] for k in range(n) if k != idx]
        + list(cross_view_text_bboxes or []))
    placed_leaders = [
        _leader_entry(placements[k][3], placements[k][5], placements[k][6],
                      placements[k][4], placements[k][0] == 'pair')
        for k in range(n) if k != idx
    ]
    if home_q is None and cx is not None and cy is not None:
        home_q = _weld_home_quadrant(pos[0], pos[1], cx, cy)
    allowed = (_allowed_quadrants(home_q, allow_adjacent=True)
               if home_q is not None else None)
    _old_tbb = placed_text_bboxes[idx]
    _ov = other_view_bboxes
    # Soft still keeps most of line clearance so text cannot sit on plates
    _line_m = max(4.0, LINE_CLEARANCE * 0.85) if soft_lines else LINE_CLEARANCE
    _ov_m = 1.0 if soft_obstacles else OVERLAP_MARGIN
    _wm_m = 2.0 if soft_obstacles else WM_TEXT_MARGIN
    best = None
    best_sc = -1e18
    dists = list(range(PREFERRED_DIAG_MIN, cap + 1, 2))
    angs = list(range(10, 351, 5))
    for nd in dists:
        for ang in angs:
            if not _leader_axis_ok(ang):
                continue
            if allowed is not None and not any(
                    _angle_in_quadrant(ang, q) for q in allowed):
                continue
            tbb = _text_bbox(pos, nd, ang, lti, is_pair=is_pair)
            if soft_lines or soft_obstacles:
                if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
                    continue
                if any(_text_overlaps(tbb, otb, _ov_m) for otb in others_tb):
                    continue
                if wm_text_bboxes and any(
                        _text_overlaps(tbb, wtb, _wm_m) for wtb in wm_text_bboxes):
                    continue
                if hatch_bboxes and any(
                        _text_overlaps(tbb, htb, HATCH_CLEAR_MARGIN)
                        for htb in hatch_bboxes):
                    continue
                if _text_near_lines(tbb, lines, margin=_line_m):
                    continue
            elif not _label_hard_clear(
                    tbb, others_tb, lines, draw_bbox,
                    wm_text_bboxes, hatch_bboxes):
                continue
            if part_bbox is not None or _ov:
                if not _corridor_pose_acceptable(
                        pos[0], pos[1], _old_tbb, tbb, part_bbox, _ov,
                        require_keep_gap=True):
                    continue
            rad = math.radians(ang)
            cos_a = math.cos(rad)
            h_land = _horiz_land(lti, is_pair)
            h_sign = h_land if cos_a >= -0.05 else -h_land
            if not relax_cross and _blue_leader_shallow_cross(
                    pos, nd, ang, h_sign, placed_leaders, min_deg=40.0):
                continue
            if not relax_parallel:
                parallel_bad = False
                for k in range(n):
                    if k == idx:
                        continue
                    pk, dsk, agk = (placements[k][3], placements[k][5],
                                    placements[k][6])
                    if _leaders_near_parallel(pos, nd, ang, pk, dsk, agk):
                        parallel_bad = True
                        break
                if parallel_bad:
                    continue
            sc = -nd * 35
            if nd <= PREFERRED_DIAG_SOFT:
                sc += 100
            elif nd <= PREFERRED_DIAG_HARD:
                sc += 50
            # Prefer corridor landings when a gap exists
            if part_bbox and _ov:
                _, _, gbox = _corridor_info(
                    pos[0], pos[1], part_bbox, _ov, home_q=None)
                if gbox is not None and _text_in_gap_box(tbb, gbox):
                    sc += 180
            if sc > best_sc:
                best_sc = sc
                best = (nd, ang, tbb)
        if best is not None and best[0] <= PREFERRED_DIAG_HARD:
            break
    if best is None:
        return False
    nd, ang, tbb = best
    return _apply_pose(idx, placements, placed_bboxes, placed_text_bboxes, nd, ang, tbb)


def _resolve_hard_clear_label(idx, placements, placed_bboxes, placed_text_bboxes,
                              lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                              other_view_bboxes, other_view_part_bboxes,
                              part_bbox, cx, cy, cross_view_text_bboxes):
    """Own-side blank → exhaustive; never relax blue×blue cross."""
    hq = _weld_home_quadrant(placements[idx][3][0], placements[idx][3][1], cx, cy)
    if _force_place_into_blank(
            idx, placements, placed_bboxes, placed_text_bboxes,
            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
            other_view_bboxes, other_view_part_bboxes,
            part_bbox, cx, cy, cross_view_text_bboxes,
            max_diag=MAX_DIAG_LEN):
        # Reject if introduced blue cross
        if not _label_has_blue_cross(idx, placements):
            return True
    if _exhaustive_hard_clear_pose(
            idx, placements, placed_bboxes, placed_text_bboxes,
            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
            cross_view_text_bboxes, max_diag=MAX_DIAG_LEN,
            relax_parallel=False, home_q=hq, cx=cx, cy=cy):
        return True
    return False


def _shorten_overlong_labels(placements, placed_bboxes, placed_text_bboxes,
                             lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                             cross_view_text_bboxes, soft_cap=None):
    """After layout: pull diagonals toward soft_cap, allowing angle change.

    Only accepts poses that still pass _label_hard_clear. Returns count shortened.
    """
    n = len(placements)
    if n == 0:
        return 0
    cap = int(PREFERRED_DIAG_HARD if soft_cap is None else soft_cap)
    moved = 0
    for i in range(n):
        gi, iti, lbi, pi, lti, dsi, agi = placements[i][:7]
        if dsi <= cap:
            continue
        is_pair = (gi == 'pair')
        others_tb = (
            [placed_text_bboxes[k] for k in range(n) if k != i]
            + list(cross_view_text_bboxes or []))
        best = None
        best_sc = -1e18
        for nd in range(PREFERRED_DIAG_MIN, min(int(dsi), MAX_DIAG_LEN) + 1, 2):
            for da in (0, 8, -8, 15, -15, 25, -25, 35, -35, 45, -45,
                       55, -55, 65, -65):
                na = (agi + da) % 360
                if not _leader_axis_ok(na):
                    continue
                ttbb = _text_bbox(pi, nd, na, lti, is_pair=is_pair)
                if not _label_hard_clear(
                        ttbb, others_tb, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes):
                    continue
                sc = -nd * 40 - abs(da) * 0.5
                if nd <= PREFERRED_DIAG_SOFT:
                    sc += 80
                elif nd <= cap:
                    sc += 40
                if sc > best_sc:
                    best_sc = sc
                    best = (nd, na, ttbb)
            if best is not None and best[0] <= cap:
                break
        if best is None or best[0] >= dsi - 0.5:
            continue
        nd, na, ttbb = best
        nbb = (_paired_bbox(pi, nd, na, lti) if is_pair
               else _single_bbox(pi, nd, na, lti))
        placements[i] = (gi, iti, lbi, pi, lti, nd, na, nbb)
        placed_bboxes[i] = nbb
        placed_text_bboxes[i] = ttbb
        moved += 1
    return moved


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


def annotate(results, dxf_paths=None, out_dir=None):
    """GB-only annotation entry. For EU drawings use dxf_annotator_eu.annotate_eu.
    Writes to annotated/gb/ by default.
    """
    import time
    from weld_extractor import is_eu_comp

    bad = sorted({r.get('component', '') for r in results if is_eu_comp(r.get('component', ''))})
    if bad:
        raise ValueError(
            f"annotate() is GB-only; refused EU components {bad}. "
            f"Use dxf_annotator_eu.annotate_eu instead.")

    out_dir = out_dir or ANNOTATED_DIR
    os.makedirs(out_dir, exist_ok=True)

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

        # CO010 still too dense for stable annotation under unified font / zero-cross
        if comp == 'CO010':
            print(f"  SKIP {comp_full}: annotation disabled (pending)")
            continue

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
            out_path = os.path.join(out_dir, os.path.basename(dxf_path))
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

    # Match weld labels to section-title height (A-A / B-B); no dense shrink.
    _title_h = _detect_section_title_height(doc, default=LABEL_HEIGHT)
    _set_active_label_height(_title_h)
    print(f"    [label height] unified to section-title h={_title_h:.2f}")

    try:
        return _annotate_one_body(doc, welds, msp, sampled_labels)
    finally:
        _set_active_label_height(LABEL_HEIGHT)


def _annotate_one_body(doc, welds, msp, sampled_labels):
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

    # Unified height for every group (section-title matched; no dense shrink)
    _group_heights, _weld_positions = _group_label_heights(groups)

    _annotate_view_place(
        msp, groups, no_pos_welds, lines, text_bboxes, circles,
        cross_view_text_bboxes, wm_text_bboxes, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes,
        vx0, vy0, vx1, vy1, cx, cy, draw_bbox, bbox,
        _down_bbox, _score_part_bbox, _view_line_grid,
        f_counter, w_counter, sampled_labels, drawn_registry,
        group_heights=_group_heights, weld_positions=_weld_positions)


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
                _force_t = any(w.get('_prefer_leader_down') for w, _p in itt)
                hq = _weld_home_quadrant(pt[0], pt[1], cx, cy)
                if _force_t:
                    hq = _downward_quad_same_half(hq)
                    prefer_primary = (
                        DIVERGE_PREF_RIGHT[1] if hq in (1, 4)
                        else DIVERGE_PREF_LEFT[1])
                nbrs = _near_angles(target)
                nbr_angs = [a for _p, a in nbrs]
                _ov_corr = (other_view_part_bboxes
                            if other_view_part_bboxes else other_view_bboxes)
                _gap_ang_t = None
                if part_bbox and _ov_corr:
                    _cq_t, _gap_ang_t, _ = _corridor_info(
                        pt[0], pt[1], part_bbox, _ov_corr, home_q=None)
                _old_tbb = placed_text_bboxes[target]
                prefer_try = [
                    prefer_primary,
                    _halfplane_complement(ago),
                    _maximin_corner_ang(nbr_angs, prefer=prefer_primary, home_q=hq),
                ]
                # Diverge inside the inter-view strip when one is available
                if _gap_ang_t is not None:
                    prefer_try.insert(0, _gap_ang_t % 360)
                    for _da in (12, -12, 22, -22, 35, -35):
                        prefer_try.append((_gap_ang_t + _da) % 360)
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
                    prefer_try = prefer_try[:8]
                is_pair = (gt == 'pair')
                others_bb = [placed_bboxes[k] for k in range(n) if k != target]
                others_tb = ([placed_text_bboxes[k] for k in range(n) if k != target]
                             + list(cross_view_text_bboxes or []))
                _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN

                def _accept_codir_pose(nd, na):
                    """Hard gates for diverge moves (incl. corridor preserve)."""
                    if _force_t and _leader_half_band(na) != 'dn':
                        return None
                    ttbb = _text_bbox(pt, nd, na, ltt, is_pair=is_pair)
                    if draw_bbox is not None and not _text_in_inner_frame(ttbb, draw_bbox):
                        return None
                    if any(_text_overlaps(ttbb, otb, OVERLAP_MARGIN) for otb in others_tb):
                        return None
                    if not _label_hard_clear(
                            ttbb, others_tb, lines, draw_bbox,
                            wm_text_bboxes, hatch_bboxes):
                        return None
                    if not _corridor_pose_acceptable(
                            pt[0], pt[1], _old_tbb, ttbb, part_bbox, _ov_corr):
                        return None
                    return ttbb

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
                            prefer_down=_force_t,
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
                                    if _accept_codir_pose(dist, cand_a) is None:
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
                        ttbb = _accept_codir_pose(nd, na)
                        if ttbb is None:
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
                            ttbb = _accept_codir_pose(nd, na)
                            if ttbb is None:
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
                            if _text_overlaps(cand, htb, HATCH_CLEAR_MARGIN):
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
    _gi_placed_idx = {}  # group index → index in _placed_angles

    def _text_obstacles():
        return cross_view_text_bboxes + placed_text_bboxes

    def _next_hint_for_quadrant(q, used_angles):
        a0, a1 = QUAD_ANGLE_RANGES[q]
        n = len(used_angles)
        if n == 0:
            return a1
        return a1 - (a1 - a0) * n / (n + 1)

    def _group_force_down(items):
        return any(w.get('_prefer_leader_down') for w, _p in items)

    for _gi, (gtype, items) in enumerate(groups):
        _set_active_label_height(group_heights[_gi] if _gi < len(group_heights) else _lh())
        _prefer_ang = _prefer_by_gi.get(_gi)
        _partner = _partner_by_gi.get(_gi)
        _force_dn = _group_force_down(items)
        if (not _force_dn and _partner is not None
                and _partner in _gi_placed_idx and _gi in _prefer_by_gi):
            # partner already placed → same-half opposite (by group id, not place order)
            _prefer_ang = _halfplane_complement(
                _placed_angles[_gi_placed_idx[_partner]][1])
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
            if _force_dn:
                _home_q = _downward_quad_same_half(_home_q)
                _prefer_ang = 305.0 if _home_q == 4 else 225.0
            if _prefer_ang is not None and _placed_angles and not _force_dn:
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
            _prefer_down = _force_dn or _prefer_downward_weld(
                wp_a[0], wp_a[1], _down_bbox)
            _dense_q = len(_quadrant_used_angles.get(_home_q, [])) >= 2
            if _prefer_ang is None and _quadrant_used_angles.get(_home_q):
                _prefer_ang = _next_hint_for_quadrant(_home_q, _quadrant_used_angles[_home_q])
            _allow_adj = _dense_q or _diverge or _force_dn
            # 仅近距成对时才 prefer_down；孤立底点不强制朝下（如 W5 应朝上）
            # _prefer_leader_down（E-E 底翼缘）始终强制朝下
            _has_near = any(
                0.5 < math.hypot(npos[0] - wp_a[0], npos[1] - wp_a[1]) <= CLUSTER_RADIUS
                for npos, _na in _placed_angles)
            _pd_use = _force_dn or (_prefer_down and not _diverge and _has_near)
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
            _gi_placed_idx[_gi] = len(_placed_angles)
            _placed_angles.append((wp_a, angle))
            _placed_leaders.append(
                _leader_entry(wp_a, diag_len, angle, _label_txt, True))
        else:
            ww, wp = items[0]
            label = _next_label(ww, f_counter, w_counter)
            _label_txt = label
            _home_q = _weld_home_quadrant(wp[0], wp[1], cx, cy)
            if _force_dn:
                _home_q = _downward_quad_same_half(_home_q)
                _prefer_ang = 305.0 if _home_q == 4 else 225.0
            if _prefer_ang is not None and _placed_angles and not _force_dn:
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
            _prefer_down = _force_dn or _prefer_downward_weld(wp[0], wp[1], _down_bbox)
            _dense_q = len(_quadrant_used_angles.get(_home_q, [])) >= 2
            if _prefer_ang is None and _quadrant_used_angles.get(_home_q):
                _prefer_ang = _next_hint_for_quadrant(_home_q, _quadrant_used_angles[_home_q])
            _allow_adj = _dense_q or _diverge or _force_dn
            _has_near = any(
                0.5 < math.hypot(npos[0] - wp[0], npos[1] - wp[1]) <= CLUSTER_RADIUS
                for npos, _na in _placed_angles)
            _pd_use = _force_dn or (_prefer_down and not _diverge and _has_near)
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
            _gi_placed_idx[_gi] = len(_placed_angles)
            _placed_angles.append((wp, angle))
            _placed_leaders.append(
                _leader_entry(wp, diag_len, angle, _label_txt, False))

        _quadrant_used_angles.setdefault(_home_q, []).append(angle)

    # ---- 用户方案后处理（一轮收口）：朝向/短引线初放已完成 ----
    # 1) 同向近邻按焊点高低分向（减蓝线交叉）
    _fix_codirectional_neighbors(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes, _prefer_by_gi)
    # 2) 近距字拉开（防叠字）
    _separate_close_text_labels(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _down_bbox, _view_line_grid, cross_view_text_bboxes)
    # 3) 同角压短
    _shorten_long_same_angle(
        _placements, placed_bboxes, placed_text_bboxes,
        draw_bbox, cross_view_text_bboxes,
        wm_text_bboxes=wm_text_bboxes, hatch_bboxes=hatch_bboxes)
    # 4) 离开 WM / 剖面标题硬禁区
    _push_labels_off_hard_zones(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        cross_view_text_bboxes,
        other_view_part_bboxes=other_view_part_bboxes)
    # 5) 蓝×蓝浅角交叉修复（锁 home，不放宽）
    _fix_shallow_blue_leader_crosses(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        cross_view_text_bboxes)
    # 6) 终检一轮：违规 → 仅改角/改长（短优先 + 字硬清 + 不交叉）
    _set_active_label_height(max(group_heights) if group_heights else LABEL_HEIGHT)
    for _ri, pd in enumerate(_placements):
        gk, _, _, pk, ltk, dsk, agk = pd[:7]
        _set_active_label_height(group_heights[_ri] if _ri < len(group_heights) else _lh())
        placed_text_bboxes[_ri] = _text_bbox(
            pk, dsk, agk, ltk, is_pair=(gk == 'pair'))
        placed_bboxes[_ri] = (_paired_bbox(pk, dsk, agk, ltk) if gk == 'pair'
                              else _single_bbox(pk, dsk, agk, ltk))
    _n_scheme = _scheme_final_repair(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes, other_view_bboxes,
        other_view_part_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        _view_line_grid, cross_view_text_bboxes)
    if _n_scheme:
        print(f"    [scheme] repaired {_n_scheme} label(s) by angle/length")
    _n_short = _shorten_overlong_labels(
        _placements, placed_bboxes, placed_text_bboxes, lines, draw_bbox,
        hatch_bboxes, wm_text_bboxes, cross_view_text_bboxes,
        soft_cap=PREFERRED_DIAG_HARD)
    if _n_short:
        print(f"    [shorten] pulled {_n_short} overlong leader(s)")
    # 压短后再修一次交叉（终检）
    _fix_shallow_blue_leader_crosses(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, wm_text_bboxes, cx, cy, _score_part_bbox,
        cross_view_text_bboxes)
    # E-E 底翼缘等：强制朝下（防走廊/空白环把标签拽到焊点上方）
    _n_dn = _enforce_prefer_leader_down(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes, wm_text_bboxes,
        cx, cy, _score_part_bbox, _view_line_grid, cross_view_text_bboxes)
    if _n_dn:
        print(f"    [prefer-down] enforced {_n_dn} underside label(s)")
    # Pull crowded / overshot labels into the inter-view blank strip
    _relocate_into_corridor(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes, wm_text_bboxes,
        cx, cy, _score_part_bbox, _view_line_grid, cross_view_text_bboxes)
    # Near-parallel stacks: hard-only before gate (soft corridor poses park peers)
    _force_diverge_parallel_leaders(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes, wm_text_bboxes,
        cx, cy, _score_part_bbox, cross_view_text_bboxes,
        allow_soft_wm=False)

    # ---- 绘制前闸门：失败则暂存占位并让出空间，避免连锁 skip ----
    _n_place = len(_placements)
    _parked = set()
    _PARK_TBB = (1e6, 1e6 + 1.0, 1e6, 1e6 + 1.0)

    def _park_label(ii):
        g0, it0, lb0, p0, lt0, ds0, ag0 = _placements[ii][:7]
        _placements[ii] = (g0, it0, lb0, p0, lt0, 0.01, ag0, _PARK_TBB)
        placed_bboxes[ii] = _PARK_TBB
        placed_text_bboxes[ii] = _PARK_TBB
        _parked.add(ii)

    def _gate_one(ii, do_repair=True):
        gtype, items, labels, pos, dname, diag_len, angle = _placements[ii][:7]
        _hq_draw = _weld_home_quadrant(pos[0], pos[1], cx, cy)
        if _leader_axis_ok(angle) and any(
                _angle_in_quadrant(angle, q)
                for q in _allowed_quadrants(_hq_draw, allow_adjacent=True)):
            angle = angle % 360
        else:
            _snap = _snap_leader_angle(angle, _hq_draw)
            angle = _snap if _leader_axis_ok(_snap) else angle % 360
        _placements[ii] = (gtype, items, labels, pos, dname, diag_len, angle,
                           _placements[ii][7] if len(_placements[ii]) > 7 else None)
        _set_active_label_height(
            group_heights[ii] if ii < len(group_heights) else _lh())
        _is_pair = (gtype == 'pair')
        _tbb_pre = _text_bbox(pos, diag_len, angle, dname, is_pair=_is_pair)
        placed_text_bboxes[ii] = _tbb_pre
        placed_bboxes[ii] = (_paired_bbox(pos, diag_len, angle, dname) if _is_pair
                             else _single_bbox(pos, diag_len, angle, dname))
        _others_tb_draw = (
            [otb for k, otb in enumerate(placed_text_bboxes)
             if k != ii and k not in _parked]
            + list(cross_view_text_bboxes or []))
        _clear_ok = _label_hard_clear(
            _tbb_pre, _others_tb_draw, lines, draw_bbox,
            wm_text_bboxes, hatch_bboxes)
        # Cross vs non-parked only
        _cross_bad = False
        if _clear_ok:
            h_land = _horiz_land(dname, _is_pair)
            cos_a = math.cos(math.radians(angle % 360))
            h_sign = h_land if cos_a >= -0.05 else -h_land
            _leaders = [
                _leader_entry(_placements[k][3], _placements[k][5],
                              _placements[k][6], _placements[k][4],
                              _placements[k][0] == 'pair')
                for k in range(_n_place) if k != ii and k not in _parked
            ]
            _cross_bad = _blue_leader_shallow_cross(
                pos, diag_len, angle, h_sign, _leaders)
        if _clear_ok and not _cross_bad:
            _parked.discard(ii)
            return True
        if not do_repair:
            return False
        if _scheme_repair_one(
                ii, _placements, placed_bboxes, placed_text_bboxes,
                lines, text_bboxes, circles, vx0, vy0, vx1, vy1,
                draw_bbox, hatch_bboxes, other_view_bboxes,
                other_view_part_bboxes, wm_text_bboxes, cx, cy,
                _score_part_bbox, _view_line_grid, cross_view_text_bboxes):
            gtype, items, labels, pos, dname, diag_len, angle = _placements[ii][:7]
            _tbb_pre = placed_text_bboxes[ii]
            _others_tb_draw = (
                [otb for k, otb in enumerate(placed_text_bboxes)
                 if k != ii and k not in _parked]
                + list(cross_view_text_bboxes or []))
            _is_pair = (gtype == 'pair')
            h_land = _horiz_land(dname, _is_pair)
            cos_a = math.cos(math.radians(angle % 360))
            h_sign = h_land if cos_a >= -0.05 else -h_land
            _leaders = [
                _leader_entry(_placements[k][3], _placements[k][5],
                              _placements[k][6], _placements[k][4],
                              _placements[k][0] == 'pair')
                for k in range(_n_place) if k != ii and k not in _parked
            ]
            # Soft rescue still must clear part lines (full LINE_CLEARANCE)
            _ok_txt = (
                (draw_bbox is None or _text_in_inner_frame(_tbb_pre, draw_bbox))
                and not any(_text_overlaps(_tbb_pre, otb, 1.0)
                            for otb in _others_tb_draw)
                and not (wm_text_bboxes and any(
                    _text_overlaps(_tbb_pre, wtb, 2.0)
                    for wtb in wm_text_bboxes))
                and not (hatch_bboxes and any(
                    _text_overlaps(_tbb_pre, htb, HATCH_CLEAR_MARGIN)
                    for htb in hatch_bboxes))
                and not _text_near_lines(_tbb_pre, lines, margin=LINE_CLEARANCE))
            # After repair: prefer no cross; still accept pose if only cross remains
            if _ok_txt:
                _parked.discard(ii)
                return True
        return False

    # Pass 1: gate; park failures so they stop blocking peers
    for _pi in range(_n_place):
        if not _gate_one(_pi, do_repair=True):
            _park_label(_pi)

    # Pass 2: retry parked with vacated space (Y-high first, then pairs)
    _retry = sorted(
        _parked, key=lambda i: (-_placements[i][3][1], _placements[i][3][0]))
    for _pi in list(_retry):
        if _pi not in _parked:
            continue
        peer = _find_near_coincident_peer(_pi, _placements)
        if peer is None:
            peer = _find_stack_peer(_pi, _placements, cx)
        if peer is not None and peer in _parked:
            if _force_up_down_pair(
                    _pi, peer, _placements, placed_bboxes, placed_text_bboxes,
                    lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                    cross_view_text_bboxes, cx, cy,
                    part_bbox=_score_part_bbox,
                    other_view_bboxes=other_view_part_bboxes or other_view_bboxes):
                # validate both vs non-parked others
                for _jj in (_pi, peer):
                    _parked.discard(_jj)
                    if not _gate_one(_jj, do_repair=False):
                        _park_label(_jj)
                continue
        if _gate_one(_pi, do_repair=True):
            continue
        _park_label(_pi)

    # Pass 3: blank force + repair once more with vacated space
    for _pi in sorted(
            list(_parked),
            key=lambda i: (-_placements[i][3][1], _placements[i][3][0])):
        if _pi not in _parked:
            continue
        peer = (_find_near_coincident_peer(_pi, _placements)
                or _find_stack_peer(_pi, _placements, cx))
        # Joint only if peer also still parked (don't break a good label)
        if peer is not None and peer in _parked:
            if _force_up_down_pair(
                    _pi, peer, _placements, placed_bboxes, placed_text_bboxes,
                    lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                    cross_view_text_bboxes, cx, cy,
                    part_bbox=_score_part_bbox,
                    other_view_bboxes=other_view_part_bboxes or other_view_bboxes):
                for _jj in (_pi, peer):
                    _parked.discard(_jj)
                    if not _gate_one(_jj, do_repair=False):
                        _park_label(_jj)
                if _pi not in _parked:
                    continue
        if _force_place_into_blank(
                _pi, _placements, placed_bboxes, placed_text_bboxes,
                lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                other_view_bboxes, other_view_part_bboxes,
                _score_part_bbox, cx, cy, cross_view_text_bboxes,
                max_diag=MAX_DIAG_LEN):
            _parked.discard(_pi)
            if _gate_one(_pi, do_repair=False):
                continue
        if _gate_one(_pi, do_repair=True):
            continue
        _park_label(_pi)

    # Pass 4: vacate nearby cluster temporarily, place leftover, then restore
    for _pi in list(_parked):
        if _pi not in _parked:
            continue
        _pos_i = _placements[_pi][3]
        _near = []
        for k in range(_n_place):
            if k == _pi or k in _parked:
                continue
            pk = _placements[k][3]
            if math.hypot(pk[0] - _pos_i[0], pk[1] - _pos_i[1]) <= CLUSTER_RADIUS:
                _near.append(k)
        _bak = {}
        for k in _near:
            _bak[k] = (_placements[k], placed_bboxes[k], placed_text_bboxes[k])
            _park_label(k)
        _parked.discard(_pi)
        _placed_ok = _gate_one(_pi, do_repair=True)
        # restore neighbors
        for k, (pl, bb, tb) in _bak.items():
            _placements[k] = pl
            placed_bboxes[k] = bb
            placed_text_bboxes[k] = tb
            _parked.discard(k)
            if not _gate_one(k, do_repair=True):
                _park_label(k)
        if not _placed_ok:
            _park_label(_pi)
        elif _pi in _parked:
            pass
        else:
            # re-validate target against restored neighbors
            if not _gate_one(_pi, do_repair=True):
                _park_label(_pi)

    # ---- 绘制：仅绘制未 park 的标 ----
    # Gate / blank-rescue 之后再强制一次朝下（空白救援可能又拽上去）
    _n_dn2 = _enforce_prefer_leader_down(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes, wm_text_bboxes,
        cx, cy, _score_part_bbox, _view_line_grid, cross_view_text_bboxes)
    if _n_dn2:
        print(f"    [prefer-down] re-enforced {_n_dn2} after gate/blank")
        for _pi in list(_parked):
            _it = _placements[_pi][1]
            if not any(w.get('_prefer_leader_down') for w, _p in _it):
                continue
            if _leader_half_band(_placements[_pi][6]) == 'dn':
                _parked.discard(_pi)
    # Blank rescue can re-parallelize; corridor first, then diverge last
    # so soft up-into-gap poses are not yanked back down.
    _relocate_into_corridor(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes, wm_text_bboxes,
        cx, cy, _score_part_bbox, _view_line_grid, cross_view_text_bboxes)
    _force_diverge_parallel_leaders(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes, wm_text_bboxes,
        cx, cy, _score_part_bbox, cross_view_text_bboxes,
        allow_soft_wm=True)
    # Restore parked labels: hard clear, corridor soft, or short search
    _part_g0 = _score_part_bbox if _score_part_bbox else (vx0, vy0, vx1, vy1)
    for _pi in list(_parked):
        gtype, items, labels, pos, dname, diag_len, angle = _placements[_pi][:7]
        _is_pair = (gtype == 'pair')
        _others = (
            [placed_text_bboxes[k] for k in range(_n_place)
             if k != _pi and k not in _parked]
            + list(cross_view_text_bboxes or []))
        _hq = _weld_home_quadrant(pos[0], pos[1], cx, cy)
        _candidates = []
        if diag_len >= 1.0:
            _candidates.append((diag_len, angle))
        _, nd0, na0 = _search_placement(
            pos, lines, text_bboxes, circles,
            [placed_bboxes[k] for k in range(_n_place)
             if k != _pi and k not in _parked],
            _others, vx0, vy0, vx1, vy1, draw_bbox, is_pair=_is_pair,
            hatch_bboxes=hatch_bboxes,
            other_view_bboxes=other_view_bboxes,
            home_q=_hq, quad_cx=cx, quad_cy=cy,
            other_view_part_bboxes=other_view_part_bboxes,
            label_text=dname, wm_text_bboxes=wm_text_bboxes,
            part_bbox=_part_g0, allow_adjacent=True,
            max_dist=int(LADDER_MAX_DIAG), cross_ok=False)
        _candidates.append((nd0, na0))
        _restored = False
        _, _, _gbox0 = _corridor_info(
            pos[0], pos[1], _part_g0, other_view_part_bboxes or other_view_bboxes or [],
            home_q=None)
        for nd, na in _candidates:
            ttbb = _text_bbox(pos, nd, na, dname, is_pair=_is_pair)
            _ig0 = _text_in_gap_box(ttbb, _gbox0) if _gbox0 else False
            if not (_label_hard_clear(
                    ttbb, _others, lines, draw_bbox,
                    wm_text_bboxes, hatch_bboxes)
                    or _label_corridor_soft_clear(
                        ttbb, _others, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes, in_gap=_ig0)):
                continue
            nbb = (_paired_bbox(pos, nd, na, dname) if _is_pair
                   else _single_bbox(pos, nd, na, dname))
            _placements[_pi] = (
                gtype, items, labels, pos, dname, nd, na, nbb)
            placed_bboxes[_pi] = nbb
            placed_text_bboxes[_pi] = ttbb
            _parked.discard(_pi)
            _restored = True
            break
        if not _restored:
            # Draw with search pose rather than skip the label entirely
            ttbb = _text_bbox(pos, nd0, na0, dname, is_pair=_is_pair)
            nbb = (_paired_bbox(pos, nd0, na0, dname) if _is_pair
                   else _single_bbox(pos, nd0, na0, dname))
            _placements[_pi] = (
                gtype, items, labels, pos, dname, nd0, na0, nbb)
            placed_bboxes[_pi] = nbb
            placed_text_bboxes[_pi] = ttbb
            _parked.discard(_pi)

    # Post-diverge hard-ish gate: soft poses must still clear lines / real WM /
    # other blue labels before draw (prevents F25-on-flange, F21-on-3SIDES).
    _part_g = _score_part_bbox if _score_part_bbox else (vx0, vy0, vx1, vy1)
    _ov_g = other_view_part_bboxes or other_view_bboxes
    for _pi in range(_n_place):
        if _pi in _parked:
            continue
        gtype, items, labels, pos, dname, diag_len, angle = _placements[_pi][:7]
        if diag_len < 1.0:
            continue
        _is_pair = (gtype == 'pair')
        _tbb = _text_bbox(pos, diag_len, angle, dname, is_pair=_is_pair)
        _others = (
            [placed_text_bboxes[k] for k in range(_n_place)
             if k != _pi and k not in _parked]
            + list(cross_view_text_bboxes or []))
        _ok = _label_hard_clear(
            _tbb, _others, lines, draw_bbox, wm_text_bboxes, hatch_bboxes)
        if not _ok:
            # Corridor strip: allow reduced WM pad but not geometry / real WM
            _, _, _gbox = _corridor_info(
                pos[0], pos[1], _part_g, _ov_g or [], home_q=None)
            _in_gap = _text_in_gap_box(_tbb, _gbox) if _gbox else False
            _ok = (_in_gap and _label_corridor_soft_clear(
                _tbb, _others, lines, draw_bbox, wm_text_bboxes, hatch_bboxes,
                in_gap=True)
                and _corridor_pose_acceptable(
                    pos[0], pos[1], _tbb, _tbb, _part_g, _ov_g,
                    require_keep_gap=False))
        if _ok:
            placed_text_bboxes[_pi] = _tbb
            continue
        # Repair: local fan with corridor soft clear
        _hq = _weld_home_quadrant(pos[0], pos[1], cx, cy)
        _right = pos[0] >= cx
        _want = _leader_half_band(angle)
        if _want == 'up':
            _fix_angs = ([155, 160, 150, 145, 140, 135, 125, 115]
                          if not _right else
                          [55, 45, 65, 35, 75, 50, 40])
        else:
            _fix_angs = ([215, 225, 235, 205, 200, 245]
                          if not _right else
                          [305, 315, 295, 325, 285])
        _fixed = False
        _, _, _gbox_r = _corridor_info(
            pos[0], pos[1], _part_g, _ov_g or [], home_q=None)
        for nd in range(MIN_DIAG_LEN, PREFERRED_DIAG_HARD + 1, 2):
            for na in _fix_angs:
                if not _leader_axis_ok(na):
                    continue
                if _leader_half_band(na) != _want:
                    continue
                ttbb = _text_bbox(pos, nd, na, dname, is_pair=_is_pair)
                _ig = _text_in_gap_box(ttbb, _gbox_r) if _gbox_r else False
                if not _label_corridor_soft_clear(
                        ttbb, _others, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes, in_gap=_ig):
                    continue
                if not _corridor_pose_acceptable(
                        pos[0], pos[1], _tbb, ttbb, _part_g, _ov_g,
                        require_keep_gap=False):
                    continue
                nbb = (_paired_bbox(pos, nd, na, dname) if _is_pair
                       else _single_bbox(pos, nd, na, dname))
                _placements[_pi] = (
                    gtype, items, labels, pos, dname, nd, na, nbb)
                placed_bboxes[_pi] = nbb
                placed_text_bboxes[_pi] = ttbb
                _fixed = True
                break
            if _fixed:
                break
        if not _fixed:
            # Last resort: full hard search, then corridor-soft accept;
            # never park here (skipping labels is worse than a soft pose).
            _, nd, na = _search_placement(
                pos, lines, text_bboxes, circles,
                [placed_bboxes[k] for k in range(_n_place)
                 if k != _pi and k not in _parked],
                _others, vx0, vy0, vx1, vy1, draw_bbox, is_pair=_is_pair,
                hatch_bboxes=hatch_bboxes,
                other_view_bboxes=other_view_bboxes,
                home_q=_hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=dname, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_part_g, allow_adjacent=True,
                max_dist=int(LADDER_MAX_DIAG), cross_ok=False)
            ttbb = _text_bbox(pos, nd, na, dname, is_pair=_is_pair)
            _ig = _text_in_gap_box(ttbb, _gbox_r) if _gbox_r else False
            if (_label_hard_clear(
                    ttbb, _others, lines, draw_bbox,
                    wm_text_bboxes, hatch_bboxes)
                    or _label_corridor_soft_clear(
                        ttbb, _others, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes, in_gap=_ig)):
                nbb = (_paired_bbox(pos, nd, na, dname) if _is_pair
                       else _single_bbox(pos, nd, na, dname))
                _placements[_pi] = (
                    gtype, items, labels, pos, dname, nd, na, nbb)
                placed_bboxes[_pi] = nbb
                placed_text_bboxes[_pi] = ttbb
            else:
                placed_text_bboxes[_pi] = _tbb

    # Final blue×blue overlap sweep (e.g. W1/W5 same land)
    for _pass in range(3):
        _moved = False
        for _pi in range(_n_place):
            if _pi in _parked:
                continue
            gtype, items, labels, pos, dname, diag_len, angle = (
                _placements[_pi][:7])
            _is_pair = (gtype == 'pair')
            _tbb = placed_text_bboxes[_pi]
            _hit = None
            for _k in range(_n_place):
                if _k == _pi or _k in _parked:
                    continue
                if _text_overlaps(_tbb, placed_text_bboxes[_k], OVERLAP_MARGIN):
                    _hit = _k
                    break
            if _hit is None:
                continue
            _hq = _weld_home_quadrant(pos[0], pos[1], cx, cy)
            _others = (
                [placed_text_bboxes[k] for k in range(_n_place)
                 if k != _pi and k not in _parked]
                + list(cross_view_text_bboxes or []))
            _, nd, na = _search_placement(
                pos, lines, text_bboxes, circles,
                [placed_bboxes[k] for k in range(_n_place)
                 if k != _pi and k not in _parked],
                _others, vx0, vy0, vx1, vy1, draw_bbox, is_pair=_is_pair,
                hatch_bboxes=hatch_bboxes,
                other_view_bboxes=other_view_bboxes,
                home_q=_hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=dname, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_part_g, allow_adjacent=True,
                prefer_ang=(angle + 25) % 360,
                max_dist=int(LADDER_MAX_DIAG), cross_ok=False)
            ttbb = _text_bbox(pos, nd, na, dname, is_pair=_is_pair)
            if not _label_hard_clear(
                    ttbb, _others, lines, draw_bbox,
                    wm_text_bboxes, hatch_bboxes):
                continue
            if (nd, na) == (diag_len, angle):
                continue
            nbb = (_paired_bbox(pos, nd, na, dname) if _is_pair
                   else _single_bbox(pos, nd, na, dname))
            _placements[_pi] = (
                gtype, items, labels, pos, dname, nd, na, nbb)
            placed_bboxes[_pi] = nbb
            placed_text_bboxes[_pi] = ttbb
            _moved = True
        if not _moved:
            break

    # Right-half W* stack: tip-Y order → text-Y order; high tip upper-right
    _realign_right_w_stack_by_tip_y(
        _placements, placed_bboxes, placed_text_bboxes, lines, text_bboxes,
        circles, vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes, wm_text_bboxes,
        cx, cy, _score_part_bbox, cross_view_text_bboxes)

    # Corridor labels overlapping green WM → push further into the gap
    for _pi in range(_n_place):
        if _pi in _parked:
            continue
        gtype, items, labels, pos, dname, diag_len, angle = _placements[_pi][:7]
        _is_pair = (gtype == 'pair')
        _tbb = placed_text_bboxes[_pi]
        if not wm_text_bboxes or not any(
                _text_overlaps(_tbb, wtb, 0.0) for wtb in wm_text_bboxes):
            continue
        _, _, _gbox = _corridor_info(
            pos[0], pos[1], _part_g, _ov_g or [], home_q=None)
        if _gbox is None or not _text_in_gap_box(_tbb, _gbox):
            continue
        _others = (
            [placed_text_bboxes[k] for k in range(_n_place)
             if k != _pi and k not in _parked]
            + list(cross_view_text_bboxes or []))
        _right = pos[0] >= cx
        _angs = ([170, 165, 175, 160, 155, 180, 150, 145]
                 if not _right else
                 [10, 20, 30, 350, 40])
        _fixed = False
        for nd in range(max(MIN_DIAG_LEN, int(diag_len)), LADDER_MAX_DIAG + 1, 2):
            for na in _angs:
                if not _leader_axis_ok(na):
                    continue
                ttbb = _text_bbox(pos, nd, na, dname, is_pair=_is_pair)
                if not _text_in_gap_box(ttbb, _gbox):
                    continue
                if not _label_corridor_soft_clear(
                        ttbb, _others, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes, in_gap=True):
                    continue
                if not _corridor_pose_acceptable(
                        pos[0], pos[1], _tbb, ttbb, _part_g, _ov_g,
                        require_keep_gap=False):
                    continue
                nbb = (_paired_bbox(pos, nd, na, dname) if _is_pair
                       else _single_bbox(pos, nd, na, dname))
                _placements[_pi] = (
                    gtype, items, labels, pos, dname, nd, na, nbb)
                placed_bboxes[_pi] = nbb
                placed_text_bboxes[_pi] = ttbb
                _fixed = True
                break
            if _fixed:
                break

    for _pi, pd in enumerate(_placements):
        if _pi in _parked:
            gtype, items, labels, pos, dname, diag_len, angle = pd[:7]
            print(f"    [error] scheme gate failed, skip draw {dname!r} "
                  f"at ({pos[0]:.1f},{pos[1]:.1f})")
            continue
        gtype, items, labels, pos, dname, diag_len, angle = pd[:7]
        _set_active_label_height(
            group_heights[_pi] if _pi < len(group_heights) else _lh())
        _smp = items[0][0].get('_sampled', False)
        _short_tips = None
        for _it in items:
            _cand = _it[0].get('_eu_u_short_tips')
            if _cand and len(_cand) >= 2:
                _short_tips = _cand
                break
        _force_dn_draw = any(w.get('_prefer_leader_down') for w, _p in items)
        if gtype == 'pair':
            _pd = _force_dn_draw or _prefer_downward_weld(
                pos[0], pos[1], _down_bbox)
            if _short_tips:
                meta = _draw_branched_paired_weld_label(
                    msp, labels, _short_tips, dname, diag_len, angle, sampled=_smp)
            else:
                meta = _draw_paired_weld_label(
                    msp, labels, pos, dname, diag_len, angle, sampled=_smp)
        else:
            _pd = _force_dn_draw or _prefer_downward_weld(
                pos[0], pos[1], _down_bbox)
            meta = _draw_weld_label(msp, labels[0], pos, dname, diag_len, angle, sampled=_smp)
        if meta:
            meta['prefer_down'] = _pd
            meta['_prefer_leader_down'] = _force_dn_draw
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
        others_tb0 = ([placed_text_bboxes[k] for k in range(n) if k != i]
                      + list(cross_view_text_bboxes or []))
        dirty = not _label_hard_clear(
            tbb, others_tb0, lines, draw_bbox, wm_text_bboxes, hatch_bboxes)
        if already and not crowded and not longish and not dirty and not wrong_side:
            continue
        if not (crowded or longish or wrong_side or not already or dirty):
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
                  or (longish and nd < dsi - 2)
                  or (dirty and new_in)
                  or (dirty and _label_hard_clear(
                      ttbb, others_tb, lines, draw_bbox, wm_text_bboxes, hatch_bboxes)))
        if not better:
            continue
        # Refuse corridor moves that fail hard clear
        if not _label_hard_clear(
                ttbb, others_tb, lines, draw_bbox, wm_text_bboxes, hatch_bboxes):
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


def _enforce_prefer_leader_down(placements, placed_bboxes, placed_text_bboxes,
                                lines, text_bboxes, circles,
                                vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                                other_view_bboxes, other_view_part_bboxes,
                                wm_text_bboxes, cx, cy, part_bbox,
                                line_grid, cross_view_text_bboxes):
    """Force labels flagged `_prefer_leader_down` into down-band if still up.

    Soft last resort: ignore part-line press so underside tips can sit in the
    H-pocket below the flange (E-E F35/F36).
    """
    n = len(placements)
    moved = 0
    for i in range(n):
        gi, iti, lbi, pos, lti, dsi, agi = placements[i][:7]
        if not any(w.get('_prefer_leader_down') for w, _p in iti):
            continue
        is_pair = (gi == 'pair')
        if (_leader_half_band(agi) == 'dn'
                and _label_below_weld(pos, dsi, agi, lti, is_pair)):
            continue
        hq = _downward_quad_same_half(_weld_home_quadrant(pos[0], pos[1], cx, cy))
        prefer = 305.0 if hq == 4 else 225.0
        others_tb = ([placed_text_bboxes[k] for k in range(n) if k != i]
                     + list(cross_view_text_bboxes or []))
        leaders = [
            _leader_entry(placements[k][3], placements[k][5], placements[k][6],
                          placements[k][4], placements[k][0] == 'pair')
            for k in range(n) if k != i
        ]
        _max_len = MAX_DIAG_LEN_PAIR if is_pair else MAX_DIAG_LEN
        best = None
        # Brute down-band poses; soft clear (no part-line gate) as last resort
        _angs = [prefer, prefer + 15, prefer - 15, prefer + 30, prefer - 30,
                 prefer + 45, prefer - 45]
        if hq == 4:
            _angs += [305, 315, 295, 325, 285, 340, 280]
        else:
            _angs += [225, 215, 235, 205, 245, 200, 250]
        for na in _angs:
            na = na % 360
            if not _leader_axis_ok(na) or _leader_half_band(na) != 'dn':
                continue
            for nd in range(MIN_DIAG_LEN, int(_max_len) + 1, 2):
                if not _label_below_weld(pos, nd, na, lti, is_pair):
                    continue
                tbb = _text_bbox(pos, nd, na, lti, is_pair=is_pair)
                if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
                    continue
                if not _text_clears_obstacles(
                        tbb, others_tb, wm_text_bboxes, hatch_bboxes):
                    continue
                h_land = _horiz_land(lti, is_pair)
                cos_a = math.cos(math.radians(na % 360))
                h_sign = h_land if cos_a >= -0.05 else -h_land
                if _blue_leader_shallow_cross(pos, nd, na, h_sign, leaders):
                    continue
                # Prefer short + clearer of peers
                sc = -nd * 10
                if nd <= PREFERRED_DIAG_SOFT:
                    sc += 50
                if best is None or sc > best[0]:
                    best = (sc, nd, na, tbb)
            if best is not None and best[1] <= PREFERRED_DIAG_SOFT:
                break
        if best is None:
            print(f"    [prefer-down] FAILED at ({pos[0]:.1f},{pos[1]:.1f}) "
                  f"label={lti!r} was_ang={agi:.0f}")
            continue
        _, nd, na, tbb = best
        _apply_pose(i, placements, placed_bboxes, placed_text_bboxes, nd, na, tbb)
        moved += 1
    return moved


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
        # Side corridor: prefer near-level into the strip (avoid climbing into
        # WM/title lines at the top of the gap). Up/down extras as fallback.
        if right_side:
            return [25, 15, 35, 340, 350, 45, 55, 325, 315, 305, 65]
        return [160, 170, 155, 190, 200, 150, 145, 180, 135, 125, 215, 225, 235, 115]

    def _pick_ang(gx_lo, gx_hi, y_lo, y_hi, right_side):
        cands = _cands_for(right_side)
        dists = [16.0, 18.0, 22.0, 26.0, 30.0, 34.0, 38.0, 42.0]
        # Same-height into gap first — mid-strip blank beats top WM clutter
        gy_prefs = (wy, wy + 8, wy - 8, wy + 14, wy - 14,
                    0.5 * (y_lo + y_hi), wy + 22, wy - 22)
        gx = 0.5 * (gx_lo + gx_hi)
        for gy in gy_prefs:
            gy = max(y_lo + 2, min(y_hi - 2, gy))
            ang = math.degrees(math.atan2(gy - wy, gx - wx)) % 360
            if _leader_axis_ok(ang):
                # Prefer this level-into-gap angle when tip lands in strip
                for dist in dists:
                    tx = wx + dist * math.cos(math.radians(ang))
                    ty = wy + dist * math.sin(math.radians(ang))
                    if gx_lo <= tx <= gx_hi and y_lo <= ty <= y_hi:
                        return ang
        for pang in cands:
            rad = math.radians(pang)
            if abs(math.sin(rad)) < math.sin(math.radians(ANGLE_MIN)):
                continue
            if abs(math.cos(rad)) < math.cos(math.radians(ANGLE_MAX)):
                continue
            for dist in dists:
                tx = wx + dist * math.cos(rad)
                ty = wy + dist * math.sin(rad)
                if gx_lo <= tx <= gx_hi and (y_lo - 8) <= ty <= (y_hi + 8):
                    return pang
        for gy in gy_prefs:
            gy = max(y_lo + 2, min(y_hi - 2, gy))
            ang = math.degrees(math.atan2(gy - wy, gx - wx)) % 360
            if _leader_axis_ok(ang):
                return ang
        return 45.0 if right_side else 160.0

    for ovb in other_view_bboxes:
        if not ovb or len(ovb) < 4:
            continue
        ox0, oy0, ox1, oy1 = ovb[0], ovb[1], ovb[2], ovb[3]
        if ox0 - 2 <= wx <= ox1 + 2 and oy0 - 2 <= wy <= oy1 + 2:
            continue
        y_lo, y_hi = max(py0, oy0), min(py1, oy1)
        if y_hi - y_lo < 8:
            y_lo, y_hi = min(py0, oy0), max(py1, oy1)
        def _score_gap(gap_w, gbox, right_side):
            """Prefer the nearest-neighbor corridor (not the widest distant void).

            Left/right gaps share the same near edge (own part side); ranking by
            width alone lets a huge far gap beat the tight C-C↔D-D strip.
            """
            # near = own-part face of the gap; far = neighbor face
            near_edge = gbox[0] if right_side else gbox[2]
            far_edge = gbox[2] if right_side else gbox[0]
            seam_dist = abs(wx - near_edge)
            neighbor_dist = abs(wx - far_edge)
            if seam_dist > 55:
                return -1e9
            # Nearest neighbor wins; label-sized gaps (15–55) get a bonus
            sc = 220.0 - neighbor_dist * 3.0 - seam_dist * 1.2
            if 15.0 <= gap_w <= 55.0:
                sc += 40.0
            elif gap_w > 80.0:
                sc -= (gap_w - 80.0) * 3.5
            if gbox[1] <= wy <= gbox[3]:
                sc += 8.0
            return sc

        gap_r = ox0 - px1
        if 12 < gap_r < 220:
            extras |= {1, 4}
            gbox = (px1 + 3, y_lo - 10, ox0 - 3, y_hi + 10)
            ang = _pick_ang(gbox[0], gbox[2], y_lo, y_hi, True)
            score = _score_gap(gap_r, gbox, True)
            if score > best_gap:
                best_gap, best_ang, best_box = score, ang, gbox
        gap_l = px0 - ox1
        if 12 < gap_l < 220:
            extras |= {2, 3}
            gbox = (ox1 + 3, y_lo - 10, px0 - 3, y_hi + 10)
            ang = _pick_ang(gbox[0], gbox[2], y_lo, y_hi, False)
            score = _score_gap(gap_l, gbox, False)
            if score > best_gap:
                best_gap, best_ang, best_box = score, ang, gbox
    return extras, best_ang, best_box


def _point_in_bbox_xyxy(px, py, bb, mrg=0.0):
    if not bb or len(bb) < 4:
        return False
    return (bb[0] - mrg <= px <= bb[2] + mrg and bb[1] - mrg <= py <= bb[3] + mrg)


def _text_center_xy(tbb):
    """Text bbox is (x0, x1, y0, y1)."""
    return (tbb[0] + tbb[1]) / 2.0, (tbb[2] + tbb[3]) / 2.0


def _text_in_gap_box(tbb, gap_box, mrg=0.0):
    if not gap_box or not tbb:
        return False
    tcx, tcy = _text_center_xy(tbb)
    return _point_in_bbox_xyxy(tcx, tcy, gap_box, mrg=mrg)


def _text_overshoots_gap(wx, wy, tbb, gap_box):
    """True when text center crosses past the far side of an inter-view gap."""
    if not gap_box or not tbb:
        return False
    tcx, _tcy = _text_center_xy(tbb)
    gx0, _gy0, gx1, _gy1 = gap_box
    # Tip on the right of the strip → overshoot left into neighbor column
    if wx >= gx1 - 1 and tcx < gx0 - 0.5:
        return True
    # Tip on the left of the strip → overshoot right
    if wx <= gx0 + 1 and tcx > gx1 + 0.5:
        return True
    return False


def _text_hits_neighbor_part(wx, wy, tbb, other_view_bboxes, gap_box=None):
    """True if text center sits in another view's Part column (not in gap)."""
    if not other_view_bboxes or not tbb:
        return False
    tcx, tcy = _text_center_xy(tbb)
    _txt_in_gap = _text_in_gap_box(tbb, gap_box) if gap_box else False
    if _txt_in_gap:
        return False
    for ovb in other_view_bboxes:
        if not ovb or len(ovb) < 4:
            continue
        ox0, oy0, ox1, oy1 = ovb[0], ovb[1], ovb[2], ovb[3]
        if ox0 - 2 <= wx <= ox1 + 2 and oy0 - 2 <= wy <= oy1 + 2:
            continue  # tip's own part
        if (ox0 - 2 <= tcx <= ox1 + 2 and oy0 - 24 <= tcy <= oy1 + 24):
            return True
    return False


def _corridor_pose_acceptable(wx, wy, old_tbb, new_tbb, part_bbox, other_view_bboxes,
                              require_keep_gap=True):
    """Post-pass gate: keep gap landings; never accept overshoot into neighbor Part."""
    if not part_bbox or not other_view_bboxes:
        return not _text_hits_neighbor_part(wx, wy, new_tbb, other_view_bboxes)
    _cq, _ga, gap_box = _corridor_info(wx, wy, part_bbox, other_view_bboxes, home_q=None)
    if _text_overshoots_gap(wx, wy, new_tbb, gap_box):
        return False
    if _text_hits_neighbor_part(wx, wy, new_tbb, other_view_bboxes, gap_box):
        return False
    if gap_box is None:
        return True
    if require_keep_gap:
        old_in = _text_in_gap_box(old_tbb, gap_box)
        new_in = _text_in_gap_box(new_tbb, gap_box)
        # Do not yank a label already seated in the corridor out into clutter
        if old_in and not new_in:
            return False
    return True


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
                if _text_overlaps(tbb, htb, HATCH_CLEAR_MARGIN):
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
    _force_dn = bool(meta.get('_prefer_leader_down') or meta.get('prefer_down'))
    if _force_dn:
        # Sheet-center home_q is often Q1/Q2 for E-E; do not snap dn → up.
        hq = _downward_quad_same_half(hq)
    if _leader_axis_ok(angle_deg) and any(
            _angle_in_quadrant(angle_deg, q)
            for q in _allowed_quadrants(hq, allow_adjacent=True)):
        angle_deg = angle_deg % 360
    else:
        angle_deg = _snap_leader_angle(angle_deg, hq)
    if _force_dn and _leader_half_band(angle_deg) != 'dn':
        # Refuse upward snap for underside tips (E-E F35/F36)
        meta['diag_len'], meta['angle'] = old
        return False
    meta['diag_len'] = diag_len
    meta['angle'] = angle_deg
    if not _label_placement_ok(meta, part_lines, draw_bbox, wm_text_bboxes, other_metas,
                               hatch_bboxes=hatch_bboxes):
        meta['diag_len'], meta['angle'] = old
        return False
    _redraw_label_meta(msp, meta)
    # Keep underside flags across meta.update(new) from redraw
    if _force_dn:
        meta['prefer_down'] = True
        meta['_prefer_leader_down'] = True
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
    _force_dn = bool(meta.get('_prefer_leader_down') or meta.get('prefer_down'))
    for nd, na in shifts:
        if nd < MIN_DIAG_LEN or nd > _max_len:
            continue
        if _force_dn and _leader_half_band(na) != 'dn':
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
        """Disabled: font size stays unified with section titles (no dense shrink)."""
        return False

    def _shrink_dense_clusters():
        """Disabled: font size stays unified with section titles (no dense shrink)."""
        return False

    def _hits_obstacle(tbb, meta=None):
        if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
            return True
        for wtb in wm_text_bboxes:
            if _text_overlaps(tbb, wtb, WM_TEXT_MARGIN):
                return True
        for htb in hatch_bboxes:
            if _text_overlaps(tbb, htb, HATCH_CLEAR_MARGIN):
                return True
        # Underside tips (E-E F35): allow sitting near part lines in H-pocket;
        # otherwise global pass yanks them into upper blank.
        if meta and meta.get('_prefer_leader_down'):
            return False
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
        _force_dn = bool(meta.get('_prefer_leader_down') or meta.get('prefer_down'))
        if _force_dn:
            hq = _downward_quad_same_half(hq)
        if draw_bbox:
            vx0, vy0, vx1, vy1 = draw_bbox
        else:
            vx0, vy0, vx1, vy1 = wx - 200, wy - 200, wx + 200, wy + 200
        _max_len = MAX_DIAG_LEN_PAIR if meta.get('is_pair') else MAX_DIAG_LEN
        _pang = (305.0 if hq == 4 else 225.0) if _force_dn else None
        _, nd, na = _search_placement(
            meta['weld_pos'], part_lines, wm_text_bboxes, [], [],
            other_text, vx0, vy0, vx1, vy1, draw_bbox,
            is_pair=meta.get('is_pair', False), home_q=hq,
            quad_cx=_cx, quad_cy=_cy,
            label_text=meta['label_text'], wm_text_bboxes=wm_text_bboxes,
            hatch_bboxes=hatch_bboxes or None,
            prefer_down=_force_dn, prefer_ang=_pang,
            max_dist=_max_len, allow_adjacent=True)
        if _force_dn and _leader_half_band(na) != 'dn':
            return False
        return _reposition_drawn_label(msp, meta, nd, na, part_lines, draw_bbox,
                                       wm_text_bboxes, others, hatch_bboxes=hatch_bboxes)

    def _brute_reposition(meta):
        others = [m for m in drawn_registry if m is not meta]
        _max_len = MAX_DIAG_LEN_PAIR if meta.get('is_pair') else MAX_DIAG_LEN
        _force_dn = bool(meta.get('_prefer_leader_down') or meta.get('prefer_down'))
        # 短距优先：固定 dist 扫角度
        for dist in range(MIN_DIAG_LEN, _max_len + 1, 2):
            for ang in range(0, 360, 8):
                r = math.radians(ang)
                if abs(math.sin(r)) < math.sin(math.radians(ANGLE_MIN)):
                    continue
                if abs(math.cos(r)) < math.cos(math.radians(ANGLE_MAX)):
                    continue
                if _force_dn and _leader_half_band(ang) != 'dn':
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
            needs_fix = _hits_obstacle(bi, mi)
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
                _tdn = bool(target_meta.get('_prefer_leader_down'))
                shifts = [
                    (-_lh() * 4, 0), (_lh() * 4, 0),
                    (0, _lh() * 2.5), (0, -_lh() * 2.5),
                    (-_lh() * 6, _lh() * 2.5),
                    (_lh() * 6, _lh() * 2.5),
                    (0, _lh() * 5.0), (0, -_lh() * 5.0),
                    (_lh() * 8, 0), (-_lh() * 8, 0),
                ]
                if _tdn:
                    # Prefer downward nudges for underside tips
                    shifts = [
                        (0, -_lh() * 2.5), (0, -_lh() * 5.0),
                        (-_lh() * 4, -_lh() * 2.5), (_lh() * 4, -_lh() * 2.5),
                        (-_lh() * 4, 0), (_lh() * 4, 0),
                    ]
                others = [m for m in drawn_registry if m is not target_meta]
                for dx, dy in shifts:
                    if _tdn and dy > 0:
                        continue
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
        _prefer_down = bool(
            meta.get('_prefer_leader_down') or meta.get('prefer_down', False))
        hq = _weld_home_quadrant(wx, wy, _cx, _cy)
        if _prefer_down:
            hq = _downward_quad_same_half(hq)
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
                prefer_ang=(305.0 if hq == 4 else 225.0) if _prefer_down else None,
                hatch_bboxes=hatch_bboxes or None,
                max_dist=_max_len, allow_adjacent=True)
        _, nd, na = _with_meta_height(meta, _do_search)
        if not (_prefer_down and _leader_half_band(na) != 'dn'):
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
                    if _prefer_down and _leader_half_band(ang) != 'dn':
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
    # prefer_down：禁止向上走廊把标签拽到焊点上方（E-E 底翼缘 F35 等）
    if prefer_down and _gap_ang is not None and _leader_half_band(_gap_ang) == 'up':
        _corr_quads = {q for q in (_corr_quads or set()) if q in (3, 4)}
        _gap_ang = None
        if not _corr_quads:
            _use_corr = False
            _gap_box = None
    _allowed_quads = _allowed_quadrants(
        home_q, allow_adjacent=prefer_down or allow_adjacent, cross_ok=cross_ok)
    # 朝缝落点时允许走廊面向象限（同竖直半侧优先：右缝→Q1/Q4）
    if _use_corr and _corr_quads and not prefer_down:
        _allowed_quads = set(_allowed_quads) | set(_corr_quads)
    elif _use_corr and _corr_quads and prefer_down:
        _allowed_quads = set(_allowed_quads) | ({q for q in _corr_quads if q in (3, 4)})
    # prefer_down 时 prefer_ang（朝下）优先于走廊角；否则走廊角作 ideal
    if (prefer_down and prefer_ang is not None
            and _leader_half_band(prefer_ang) == 'dn'):
        _ideal_ang = prefer_ang % 360
    elif _gap_ang is not None:
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
        # 跨视图：跳过 tip 所在 Part；硬拦文字压入邻 Part / 越过走廊
        _ov_check = other_view_part_bboxes if other_view_part_bboxes is not None else other_view_bboxes
        _tcx, _tcy = (bx0 + bx1) / 2, (by0 + by1) / 2
        _txt_in_gap = False
        if _gap_box is not None:
            gx0, gy0, gx1, gy1 = _gap_box
            _txt_in_gap = (gx0 <= _tcx <= gx1 and gy0 <= _tcy <= gy1)
            # 越过缝：tip 在缝一侧、文字中心跑到缝另一侧更远（压邻视图柱）
            if not _txt_in_gap:
                if wx >= gx1 - 1 and _tcx < gx0 - 0.5:
                    return True
                if wx <= gx0 + 1 and _tcx > gx1 + 0.5:
                    return True
        if _ov_check:
            for _ovb in _ov_check:
                ox0, oy0, ox1, oy1 = _ovb[0], _ovb[1], _ovb[2], _ovb[3]
                if ox0 - 2 <= wx <= ox1 + 2 and oy0 - 2 <= wy <= oy1 + 2:
                    continue
                # 邻 Part 柱：x 落入邻框且 y 在邻框上下扩展带内（含略高出 AABB）
                if (ox0 - 2 <= _tcx <= ox1 + 2
                        and oy0 - 24 <= _tcy <= oy1 + 24
                        and not _txt_in_gap):
                    return True
                _M = 4 if _use_corr else 8
                if (not _txt_in_gap
                        and bx1 > ox0 - _M and bx0 < ox1 + _M
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
                if not (bx1 < hx0 - HATCH_CLEAR_MARGIN or bx0 > hx1 + HATCH_CLEAR_MARGIN or
                        by1 < hy0 - HATCH_CLEAR_MARGIN or by0 > hy1 + HATCH_CLEAR_MARGIN):
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
        # Keep corridor-facing quads so gap landings stay legal across passes
        if _use_corr and _corr_quads and not prefer_down:
            _allowed_quads = set(_allowed_quads) | set(_corr_quads)
        elif _use_corr and _corr_quads and prefer_down:
            _allowed_quads = set(_allowed_quads) | ({q for q in _corr_quads if q in (3, 4)})
        _eff_max = max_dist_local if max_dist_local is not None else (
            max_dist if max_dist is not None else _max_len)
        # Include ultra-short lengths so local blank pockets can win
        distances = list(range(MIN_DIAG_LEN, _eff_max + 1, 2))
        if is_pair:
            distances = [min(d + 2, _eff_max) for d in distances]
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

        # 0) Local blank pocket (lightweight): shortest clear pose first
        _pocket_cap = min(int(_eff_max), LOCAL_POCKET_MAX)
        _pocket_angs = []
        for _base in (_ideal_ang, prefer_ang, _gap_ang, _radial_ang):
            if _base is None:
                continue
            for _da in (0, 10, -10, 20, -20, 30, -30, 40, -40):
                _pocket_angs.append(int((_base + _da) % 360))
        for _q in _allowed_quads:
            _lo, _hi = QUAD_ANGLE_RANGES.get(_q, (10, 80))
            _pocket_angs.append(int(0.5 * (_lo + _hi)) % 360)
        _seen_pa, _pocket_angs_u = set(), []
        for _a in _pocket_angs:
            if _a in _seen_pa:
                continue
            _seen_pa.add(_a)
            _pocket_angs_u.append(_a)
        for _dist in range(MIN_DIAG_LEN, _pocket_cap + 1, 2):
            for _ang in _pocket_angs_u:
                _rad = math.radians(_ang)
                if abs(math.sin(_rad)) < math.sin(math.radians(ANGLE_MIN)):
                    continue
                if abs(math.cos(_rad)) < math.cos(math.radians(ANGLE_MAX)):
                    continue
                if _has_conflict(_ang, _dist, _db):
                    continue
                _tbb = _text_bbox((wx, wy), _dist, _ang, label_text, is_pair=is_pair)
                if _text_near_lines(_tbb, lines, margin=LINE_CLEARANCE):
                    continue
                return 0, (_ang, _dist, 0)

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
                if nd < MIN_DIAG_LEN or nd > _gap_cap:
                    continue
                na = math.degrees(math.atan2(dy, dx)) % 360
                if not _leader_axis_ok(na):
                    continue
                if not _has_conflict(na, nd, _db):
                    tbb = _text_bbox((wx, wy), nd, na, label_text, is_pair=is_pair)
                    tcx = (tbb[0] + tbb[1]) / 2
                    tcy = (tbb[2] + tbb[3]) / 2
                    if _gap_box is not None:
                        gx0, gy0, gx1, gy1 = _gap_box
                        if gx0 <= tcx <= gx1 and gy0 <= tcy <= gy1:
                            return 0, (na, nd, 0)
                    elif _best_hit is None:
                        _best_hit = (na, nd, 0)
            # Only fall back to non-gap hit when no corridor box exists
            if _best_hit is not None and _gap_box is None:
                return 0, _best_hit
            for dist in range(PREFERRED_DIAG_MIN, _gap_cap + 1, 3):
                for da in (0, 8, -8, 16, -16, 25, -25, 35, -35, 45, -45, 55, -55):
                    _ga = (_gap_ang + da) % 360
                    if not _has_conflict(_ga, dist, _db):
                        if _gap_box is not None:
                            tbb = _text_bbox(
                                (wx, wy), dist, _ga, label_text, is_pair=is_pair)
                            tcx = (tbb[0] + tbb[1]) / 2
                            tcy = (tbb[2] + tbb[3]) / 2
                            gx0, gy0, gx1, gy1 = _gap_box
                            if gx0 <= tcx <= gx1 and gy0 <= tcy <= gy1:
                                return 0, (_ga, dist, 0)
                        else:
                            return 0, (_ga, dist, 0)
            # Last corridor try: tip in gap (text may be near edge)
            for dist in range(PREFERRED_DIAG_MIN, _gap_cap + 1, 3):
                for da in (0, 10, -10, 20, -20, 30, -30):
                    _ga = (_gap_ang + da) % 360
                    if _has_conflict(_ga, dist, _db):
                        continue
                    if _gap_box is not None:
                        rad = math.radians(_ga)
                        tipx = wx + dist * math.cos(rad)
                        tipy = wy + dist * math.sin(rad)
                        gx0, gy0, gx1, gy1 = _gap_box
                        if not (gx0 <= tipx <= gx1 and gy0 <= tipy <= gy1):
                            continue
                    return 0, (_ga, dist, 0)

        _wide_ao = [0, 8, -8, 15, -15, 25, -25, 35, -35, 45, -45, 55, -55]
        _mid_ao = [0, 10, -10, 20, -20, 30, -30, 40, -40]
        # 1) 超短局部带 10–18（空白袋）
        for dist in distances:
            if dist < MIN_DIAG_LEN or dist > PREFERRED_DIAG_MIN:
                continue
            result = _try_place_bases(dist, _wide_ao)
            if result:
                return result
        # 2) 短甜区 20–28
        for dist in distances:
            if dist < 20 or dist > 28:
                continue
            result = _try_place_bases(dist, _mid_ao)
            if result:
                return result
        # 3) 过渡带 18–20
        for dist in distances:
            if dist < PREFERRED_DIAG_MIN or dist > 20:
                continue
            result = _try_place_bases(dist, _wide_ao)
            if result:
                return result
        # 4) 略长但仍在适中带 30–38
        for dist in distances:
            if dist < 30 or dist > 38:
                continue
            result = _try_place_bases(dist, _wide_ao)
            if result:
                return result
        # 5) ≥40 仅兜底
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
                                             placed_leaders=placed_leaders,
                                             gap_box=_gap_box)
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
                            # Exhaustive clear search — never prefer a known-conflict short default
                            _fb_ok2 = None
                            _ext = min(max(_eff_cap, MAX_DIAG_LEN), LADDER_MAX_DIAG)
                            for nd in range(PREFERRED_DIAG_MIN, _ext + 1, 2):
                                for ang in range(10, 350, 5):
                                    if not _leader_axis_ok(ang):
                                        continue
                                    if not _has_conflict(ang, nd, draw_bbox):
                                        _fb_ok2 = (0, (float(ang), nd, 0))
                                        break
                                if _fb_ok2:
                                    break
                            if _fb_ok2:
                                result = _fb_ok2
                            else:
                                result = (0, (_fb_ang, min(PREFERRED_DIAG_SOFT, _eff_cap), 0))
                                print(f"    [warn] unresolved placement at ({wx:.1f},{wy:.1f}) "
                                      f"label={label_text!r} (enforce-blank will retry)")
                else:
                    _pref = {1: 45, 2: 135, 3: 225, 4: 315}
                    _fb_ang = _pref.get(home_q, 45)
                    _fb_ok2 = None
                    _ext = min(max(_eff_cap, MAX_DIAG_LEN), LADDER_MAX_DIAG)
                    for nd in range(PREFERRED_DIAG_MIN, _ext + 1, 2):
                        for ang in range(10, 350, 5):
                            if not _leader_axis_ok(ang):
                                continue
                            if not _has_conflict(ang, nd, draw_bbox):
                                _fb_ok2 = (0, (float(ang), nd, 0))
                                break
                        if _fb_ok2:
                            break
                    if _fb_ok2:
                        result = _fb_ok2
                    else:
                        result = (0, (_fb_ang, min(PREFERRED_DIAG_SOFT, _eff_cap), 0))
                        print(f"    [warn] unresolved placement at ({wx:.1f},{wy:.1f}) "
                              f"label={label_text!r} (enforce-blank will retry)")

    # 硬约束：钳制过水平/过垂直；象限以 _allowed_quads 为准（含走廊邻象限）
    # result may be (score, (ang, dist, _)) or (score, dist, ang)
    if isinstance(result[1], (tuple, list)):
        _fa, _fd = result[1][0], result[1][1]
    else:
        _fd, _fa = result[1], result[2]
    ft = _fine_tune(_fd, _fa, draw_bbox)
    if ft is not None:
        _fa2, _fd2, _ = ft
        if not _has_conflict(_fa2, _fd2, draw_bbox):
            _fa, _fd = _fa2, _fd2
    # Snap into any allowed quadrant (corridor may add Q3 next to home Q2)
    if _leader_axis_ok(_fa) and any(
            _angle_in_quadrant(_fa, q) for q in _allowed_quads):
        _fa = _fa % 360
    else:
        _snap_q = home_q
        for q in _allowed_quads:
            if _angle_in_quadrant(_fa, q) or _leader_half_band(_fa) == (
                    'up' if q in (1, 2) else 'dn'):
                _snap_q = q
                break
            if _angle_delta_deg(_fa, {1: 45, 2: 135, 3: 225, 4: 315}[q]) < 50:
                _snap_q = q
                break
        _fa = _snap_leader_angle(_fa, _snap_q)
    # 仅当结果跑出允许象限时才钳回（遍历全部允许象限，勿锁死 home_q）
    if home_q is not None and not any(
            _angle_in_quadrant(_fa, q) for q in _allowed_quads):
        _pref_angles = {1: 45, 2: 135, 3: 225, 4: 315}
        _candidates = []
        for q in _allowed_quads:
            _candidates.append(_pref_angles.get(q, 45))
            a0, a1 = QUAD_ANGLE_RANGES[q]
            _candidates.extend(range(int(a0), int(a1) + 1, 5))
        for _ca in _candidates:
            if _leader_axis_ok(_ca) and not _has_conflict(_ca, _fd, draw_bbox):
                _fa = _ca
                break
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
                     corridor_quads=None, gap_prefer_ang=None, placed_leaders=None,
                     gap_box=None):
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
    _gap_box = gap_box
    if other_view_bboxes and (gap_prefer_ang is None or _gap_box is None):
        _cq, _auto_gap, _auto_box = _corridor_info(
            wx, wy, _part_bbox, other_view_bboxes, home_q=home_q)
        if gap_prefer_ang is None:
            gap_prefer_ang = _auto_gap
        if _gap_box is None:
            _gap_box = _auto_box
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
    # 惩罚须压过「Part 外空白环 +300」，否则 E-E 底翼缘会被拽到视图上方
    if prefer_down:
        if _cy_cand < wy - 1.0:
            score += 220
        elif _cy_cand > wy + _lh() * 0.5:
            score -= 800
        if home_q in (3, 4):
            if sin_a < -0.15:
                score += 120
            if sin_a > 0.1:
                score -= 400
        elif home_q in (1, 2) and sin_a > 0.55:
            score -= 200
        if _leader_half_band(angle_deg) == 'up':
            score -= 600

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
    # 局部空白袋常落在 Part AABB 内：仅轻罚，把重罚留给真正压线。
    _inside_part = (px0 <= _cx_cand <= px1 and py0 <= _cy_cand <= py1)
    _inside_inner = True
    if draw_bbox is not None:
        dbx0, dby0, dbx1, dby1 = draw_bbox
        _inside_inner = (dbx0 <= _cx_cand <= dbx1 and dby0 <= _cy_cand <= dby1)
        if _inside_inner and not _inside_part:
            # prefer_down 时禁止用「上方空白环」盖过朝下偏好
            if prefer_down and _cy_cand > wy:
                score -= 200
            else:
                score += 300
    if _inside_part:
        score -= 250

    # 跨视图 / 走廊：只奖励真正落在 gap_box 的文字；越过缝压入邻 Part 重罚
    _in_gap_txt = False
    if _gap_box is not None and len(_gap_box) >= 4:
        gx0, gy0, gx1, gy1 = _gap_box[0], _gap_box[1], _gap_box[2], _gap_box[3]
        _in_gap_txt = (gx0 <= _cx_cand <= gx1 and gy0 <= _cy_cand <= gy1)
        if _in_gap_txt:
            score += 1600
            if gap_prefer_ang is not None:
                _gdev = _angle_delta_deg(angle_deg, gap_prefer_ang)
                if _gdev < 50:
                    score += (50 - _gdev) * 20
            if dist <= 40:
                score += (40 - dist) * 12
            # 同竖直半侧：上焊略偏上、下焊略偏下
            if home_q in (1, 2) and _cy_cand >= wy - 4:
                score += 60
            if home_q in (3, 4) and _cy_cand <= wy + 4:
                score += 60
        else:
            # 越过走廊进入邻侧（tip 在缝右、字在缝左更远，或反向）
            if wx >= gx1 - 1 and _cx_cand < gx0 - 1:
                score -= 2500
            elif wx <= gx0 + 1 and _cx_cand > gx1 + 1:
                score -= 2500
    if other_view_bboxes:
        nbb = (min(wx, ex, bx0), max(wx, ex, bx1),
               min(wy, ey, by0), max(wy, ey, by1))
        for _ovb in other_view_bboxes:
            if not _ovb or len(_ovb) < 4:
                continue
            # tip 所在视图的 Part 框：跳过
            if _point_in_bbox_xyxy(wx, wy, _ovb, mrg=2):
                continue
            if _point_in_bbox_xyxy(_cx_cand, _cy_cand, _ovb, mrg=2):
                score -= 100000
            # leader/text 包围盒真正切入邻视图 Part
            if (nbb[1] > _ovb[0] and nbb[0] < _ovb[2]
                    and nbb[3] > _ovb[1] and nbb[2] < _ovb[3]
                    and not _in_gap_txt):
                score -= 80000
        # 无 gap 时保留弱空白环（有 gap 则勿把「任意 Part 外」当走廊）
        if (_gap_box is None and _inside_inner and not _inside_part
                and gap_prefer_ang is not None):
            score += 400
            _gdev = _angle_delta_deg(angle_deg, gap_prefer_ang)
            if _gdev < 50:
                score += (50 - _gdev) * 10

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
 
    # 文字与几何线过近：重罚（压竖板/翼缘不可接受）
    _LINE_MARGIN = LINE_CLEARANCE
    _txt_sample_pts = [(bx0, by0), (bx1, by0), (bx0, by1), (bx1, by1),
                       ((bx0+bx1)/2, by0), ((bx0+bx1)/2, by1),
                       (bx0, (by0+by1)/2), (bx1, (by0+by1)/2),
                       ((bx0+bx1)/2, (by0+by1)/2)]
    for (sx, sy), (ex2, ey2) in _near_lines:
        if bx1 < min(sx, ex2) - _LINE_MARGIN: continue
        if bx0 > max(sx, ex2) + _LINE_MARGIN: continue
        if by1 < min(sy, ey2) - _LINE_MARGIN: continue
        if by0 > max(sy, ey2) + _LINE_MARGIN: continue
        for (cx, cy) in _txt_sample_pts:
            d, _ = _dist_pt_to_seg((cx, cy), (sx, sy), (ex2, ey2))
            if d < _LINE_MARGIN:
                score -= 2500
                break
 
    # 文字边穿越几何线：硬罚
    _txt_edges = [((bx0, by0), (bx1, by0)), ((bx1, by0), (bx1, by1)),
                  ((bx1, by1), (bx0, by1)), ((bx0, by1), (bx0, by0))]
    for (sx, sy), (ex2, ey2) in _near_lines:
        for (_s, _e) in _txt_edges:
            if _segments_cross_(_s, _e, (sx, sy), (ex2, ey2)):
                score -= 5000
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

    # 引线长度：局部短清晰空白袋优先；过长重罚
    if dist < PREFERRED_DIAG_MIN:
        # 超短允许（局部口袋）；仅轻罚，清晰时下面再加回
        score -= (PREFERRED_DIAG_MIN - dist) * 4
    if dist <= LOCAL_POCKET_MAX and _min_center_dist >= LINE_CLEARANCE * 0.85:
        score += (LOCAL_POCKET_MAX - dist) * 28
        if _inside_inner:
            score += 180
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


def _leader_diag_endpoints(pos, dist, angle):
    rad = math.radians(angle % 360)
    return pos, (pos[0] + dist * math.cos(rad), pos[1] + dist * math.sin(rad))


def _leaders_near_parallel(pos_a, dist_a, ang_a, pos_b, dist_b, ang_b,
                           min_deg=None, min_sep=None):
    """True if diagonals are near-parallel and spatially close (stack/overlap)."""
    thr = LADDER_PARALLEL_MIN_DEG if min_deg is None else min_deg
    sep = (_lh() * 2.8) if min_sep is None else min_sep
    if _angle_delta_deg(ang_a, ang_b) >= thr:
        return False
    _, ta = _leader_diag_endpoints(pos_a, dist_a, ang_a)
    _, tb = _leader_diag_endpoints(pos_b, dist_b, ang_b)
    mid_a = (0.5 * (pos_a[0] + ta[0]), 0.5 * (pos_a[1] + ta[1]))
    d, _ = _dist_pt_to_seg(mid_a, pos_b, tb)
    if d < sep:
        return True
    mid_b = (0.5 * (pos_b[0] + tb[0]), 0.5 * (pos_b[1] + tb[1]))
    d2, _ = _dist_pt_to_seg(mid_b, pos_a, ta)
    return d2 < sep


def _apply_ladder_layout(placements, placed_bboxes, placed_text_bboxes,
                         lines, text_bboxes, circles,
                         vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                         other_view_bboxes, other_view_part_bboxes,
                         wm_text_bboxes, cx, cy, part_bbox,
                         cross_view_text_bboxes):
    """
    Hard ladder: tip pinned to a vertical column (Y by weld height), X claim
    on own side of a shared corridor. Zigzag tip X + Q1/Q4 alternate to avoid
    parallel stacks. Accept only poses that clear overlap, near-lines, and
    inner frame. Returns set of successfully laddered indices.
    """
    n = len(placements)
    locked = set()
    if n < 2:
        return locked
    _ov = other_view_part_bboxes if other_view_part_bboxes else other_view_bboxes
    _part = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    px0, py0, px1, py1 = _part
    pitch = max(_lh() * LADDER_SLOT_PITCH, _lh() * 2.0)
    x_stag = _lh() * LADDER_X_STAGGER
    tip_y_min = pitch * LADDER_TIP_Y_MIN
    max_diag = max(LADDER_MAX_DIAG, MAX_DIAG_LEN)

    left_i, right_i = [], []
    for i in range(n):
        if placements[i][3][0] < cx:
            left_i.append(i)
        else:
            right_i.append(i)

    def _hard_ok(tbb, others_tb):
        if draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox):
            return False
        if not _text_clears_obstacles(
                tbb, others_tb, wm_text_bboxes, hatch_bboxes):
            return False
        if _text_near_lines(tbb, lines):
            return False
        return True

    def _wm_blocks_height(wx, wy):
        """WM near this weld height → prefer tip below (Q3/Q4 empty)."""
        for wtb in (wm_text_bboxes or []):
            wcy = 0.5 * (wtb[2] + wtb[3])
            wcx = 0.5 * (wtb[0] + wtb[1])
            if abs(wcy - wy) <= _lh() * 4 and abs(wcx - wx) <= 80:
                return True
        return False

    def _cluster_gap(idxs):
        best = None  # (score, gap_box, gap_right)
        for i in idxs:
            wx, wy = placements[i][3]
            _, _, gbox = _corridor_info(wx, wy, _part, _ov or [], home_q=None)
            if not gbox:
                continue
            gap_right = gbox[0] >= wx - 2
            gmid = 0.5 * (gbox[0] + gbox[2])
            sc = (gbox[2] - gbox[0]) - abs(wx - gmid) * 0.5
            if best is None or sc > best[0]:
                best = (sc, gbox, gap_right)
        return best

    def _slot_column_x(gap_info, side_right):
        if gap_info is not None:
            _, gbox, gap_right = gap_info
            gw = max(4.0, gbox[2] - gbox[0])
            # Push tip into the corridor blank (red-box), not just the seam edge
            depth = max(x_stag, gw * LADDER_GAP_DEPTH)
            if gap_right:
                return min(gbox[0] + depth, gbox[2] - x_stag)
            return max(gbox[2] - depth, gbox[0] + x_stag)
        if side_right:
            return px1 + max(8.0, _lh() * 3.5)
        return px0 - max(8.0, _lh() * 3.5)

    def _pose_from_tip(wx, wy, tip_x, tip_y, label_text, is_pair, prefer_right):
        dx, dy = tip_x - wx, tip_y - wy
        dist = math.hypot(dx, dy)
        if dist < MIN_DIAG_LEN or dist > max_diag:
            return None
        ang = math.degrees(math.atan2(dy, dx)) % 360
        if not _leader_axis_ok(ang):
            return None
        cos_a = math.cos(math.radians(ang))
        if prefer_right and cos_a < -0.05:
            return None
        if (not prefer_right) and cos_a >= -0.05:
            return None
        tbb = _text_bbox((wx, wy), dist, ang, label_text, is_pair=is_pair)
        return dist, ang, tbb

    def _conflicts_used(wx, wy, dist, ang, tip_y, used):
        for ua, uty, upos, udist in used:
            if abs(tip_y - uty) < tip_y_min:
                return True
            if _angle_delta_deg(ang, ua) < LADDER_PARALLEL_MIN_DEG:
                if _leaders_near_parallel(
                        (wx, wy), dist, ang, upos, udist, ua):
                    return True
                # same-direction near-parallel even if segments a bit apart
                if abs(tip_y - uty) < pitch * 1.35:
                    return True
        return False

    def _pose_for_slot(wx, wy, slot_x, slot_cy, label_text, is_pair,
                      prefer_right, used, others_tb, gap_info, rank,
                      want_down):
        lh = _lh()
        tip_y0 = slot_cy - lh * 0.45
        # Zigzag column X so equal weld/tip spacing does not yield parallel rays
        zig = x_stag * (0.55 if (rank % 2) else 0.0)
        if prefer_right:
            base_xs = [slot_x + zig, slot_x + zig + x_stag * 0.4,
                       slot_x, slot_x + x_stag * 0.8, slot_x - x_stag * 0.25]
        else:
            base_xs = [slot_x - zig, slot_x - zig - x_stag * 0.4,
                       slot_x, slot_x - x_stag * 0.8, slot_x + x_stag * 0.25]
        if gap_info is not None:
            _, gbox, gap_right = gap_info
            lo, hi = gbox[0] + 1.0, gbox[2] - 1.0
            gw = max(4.0, hi - lo)
            # Extra tips deeper into the shared blank (use the red-box width)
            mid = 0.5 * (gbox[0] + gbox[2])
            if gap_right:
                base_xs.extend([
                    gbox[0] + gw * 0.12, gbox[0] + gw * 0.20,
                    gbox[0] + gw * 0.28, gbox[0] + gw * 0.36])
                # Stay on own half of the corridor (left of mid)
                base_xs = [x for x in base_xs if x <= mid - x_stag * 0.25]
            else:
                base_xs.extend([
                    gbox[2] - gw * 0.12, gbox[2] - gw * 0.20,
                    gbox[2] - gw * 0.28, gbox[2] - gw * 0.36])
                base_xs = [x for x in base_xs if x >= mid + x_stag * 0.25]
            base_xs = [min(hi, max(lo, x)) for x in base_xs]
        seen_x, x_ord = set(), []
        for x in base_xs:
            k = round(x, 2)
            if k in seen_x:
                continue
            seen_x.add(k)
            x_ord.append(x)

        # Prefer tip below weld when WM blocks mid height or rank wants Q4
        if want_down or _wm_blocks_height(wx, wy):
            y_offs = (0.0, -0.5 * pitch, -pitch, -1.5 * pitch, -2.0 * pitch,
                      0.5 * pitch, pitch, 1.5 * pitch, 0.25 * pitch)
        else:
            y_offs = (0.0, 0.5 * pitch, pitch, 1.5 * pitch, -0.5 * pitch,
                      -pitch, -1.5 * pitch, 0.25 * pitch, -0.25 * pitch)

        # Try preferred side first, then opposite (F9/F10 → Q4)
        side_order = (True, False) if prefer_right else (False, True)
        best = None
        best_sc = -1e18
        for side_pref in side_order:
            for tip_x in x_ord:
                for y_off in y_offs:
                    tip_y = tip_y0 + y_off
                    if want_down and tip_y > wy + lh * 0.2 and side_pref == prefer_right:
                        continue
                    pose = _pose_from_tip(
                        wx, wy, tip_x, tip_y, label_text, is_pair, side_pref)
                    if pose is None:
                        continue
                    dist, ang, tbb = pose
                    if not _hard_ok(tbb, others_tb):
                        continue
                    if _conflicts_used(wx, wy, dist, ang, tip_y, used):
                        continue
                    sc = -abs(tip_x - slot_x) * 18 - abs(y_off) * 8
                    sc -= dist * 12  # prefer short leaders
                    sc -= max(0.0, dist - PREFERRED_DIAG_SOFT) * 4
                    if side_pref == prefer_right:
                        sc += 100
                    else:
                        sc -= 80  # opposite half only as last resort
                    if want_down and tip_y <= wy:
                        sc += 40
                    if _leader_half_band(ang) == ('dn' if want_down else 'up'):
                        sc += 30
                    min_gap = 180.0
                    for ua, _, _, _ in used:
                        min_gap = min(min_gap, _angle_delta_deg(ang, ua))
                    sc += min(min_gap, 90) * 2.5
                    if sc > best_sc:
                        best_sc = sc
                        best = (dist, ang, tbb, tip_y)
        # Prefer own-side result; only if none, opposite already explored with heavy penalty
        return best

    def _layout_side(idxs, side_right):
        if len(idxs) < 1:
            return
        gap_info = _cluster_gap(idxs)
        # Corridor present → force ladder even for a single label on this side
        if gap_info is None and len(idxs) < 2:
            return
        slot_x = _slot_column_x(gap_info, side_right)
        order = sorted(idxs, key=lambda i: -placements[i][3][1])
        nslot = len(order)
        weld_ys = [placements[i][3][1] for i in order]
        y_hi = max(weld_ys) + _lh() * 0.8
        y_lo = min(weld_ys) - _lh() * 0.8
        if gap_info is not None:
            gbox = gap_info[1]
            y_hi = min(y_hi + pitch, gbox[3] - _lh())
            y_lo = max(y_lo - pitch, gbox[1] + 2)
        need = (nslot - 1) * pitch
        span = y_hi - y_lo
        if span < need:
            mid = 0.5 * (y_hi + y_lo)
            y_hi = mid + need * 0.5
            y_lo = mid - need * 0.5
        used = []  # (ang, tip_y, pos, dist)
        for rank, i in enumerate(order):
            slot_cy = y_hi - rank * pitch
            gi, iti, lbi, pos, ltk, ds, ag = placements[i][:7]
            is_pair = (gi == 'pair')
            # Lower half of the ladder / below view centre → prefer Q4/Q3
            want_down = (pos[1] < cy) or (rank >= (nslot + 1) // 2)
            done = set(order[:rank])
            others_tb = [
                placed_text_bboxes[k] for k in range(n)
                if k != i and (k not in idxs or k in done)
            ] + list(cross_view_text_bboxes or [])
            pose = _pose_for_slot(
                pos[0], pos[1], slot_x, slot_cy, ltk, is_pair,
                prefer_right=side_right, used=used, others_tb=others_tb,
                gap_info=gap_info, rank=rank, want_down=want_down)
            if pose is None:
                continue
            nd, na, tbb, tip_y = pose
            nbb = (_paired_bbox(pos, nd, na, ltk) if is_pair
                   else _single_bbox(pos, nd, na, ltk))
            placements[i] = (gi, iti, lbi, pos, ltk, nd, na, nbb)
            placed_bboxes[i] = nbb
            placed_text_bboxes[i] = tbb
            used.append((na, tip_y, pos, nd))
            locked.add(i)

    _layout_side(left_i, side_right=False)
    _layout_side(right_i, side_right=True)
    return locked


def _realign_right_w_stack_by_tip_y(
        placements, placed_bboxes, placed_text_bboxes,
        lines, text_bboxes, circles,
        vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
        other_view_bboxes, other_view_part_bboxes,
        wm_text_bboxes, cx, cy, part_bbox, cross_view_text_bboxes):
    """Right-half single W* labels: text Y order matches tip Y (high→high).

    Highest tip → upper-right (~55°); lowest → lower-right (~305°). Prevents
    W4/W1/W5 leader crossings when tip order and text stack are inverted.
    """
    n = len(placements)
    cands = []
    for i in range(n):
        gi, _, _, pos, lti, _, _ = placements[i][:7]
        if gi == 'pair':
            continue
        if not str(lti).startswith('W'):
            continue
        if pos[0] < cx - 2:
            continue
        cands.append(i)
    if len(cands) < 2:
        return
    # Connected components by tip proximity
    unused = set(cands)
    clusters = []
    while unused:
        seed = unused.pop()
        stack = [seed]
        comp = [seed]
        while stack:
            a = stack.pop()
            pa = placements[a][3]
            for b in list(unused):
                pb = placements[b][3]
                if math.hypot(pa[0] - pb[0], pa[1] - pb[1]) <= CLUSTER_RADIUS * 1.6:
                    unused.discard(b)
                    stack.append(b)
                    comp.append(b)
        if len(comp) >= 2:
            clusters.append(comp)
    _part = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    # Angle ladder high→low tip: upper-right … lower-right
    _ang_ladder = (55.0, 40.0, 25.0, 15.0, 335.0, 315.0, 305.0)
    for comp in clusters:
        order = sorted(comp, key=lambda i: (-placements[i][3][1], placements[i][3][0]))
        # Skip if text Y already strictly matches tip order and no shallow cross
        tys = [0.5 * (placed_text_bboxes[i][2] + placed_text_bboxes[i][3])
               for i in order]
        order_ok = all(tys[k] >= tys[k + 1] - 0.5 for k in range(len(tys) - 1))
        leaders = [
            _leader_entry(placements[k][3], placements[k][5],
                          placements[k][6], placements[k][4], False)
            for k in range(n)]
        cross_bad = False
        for i in order:
            gi, _, _, pos, lti, dsi, agi = placements[i][:7]
            h_land = _horiz_land(lti, False)
            cos_a = math.cos(math.radians(agi % 360))
            h_sign = h_land if cos_a >= -0.05 else -h_land
            others = [L for k, L in enumerate(leaders) if k != i]
            if _blue_leader_shallow_cross(pos, dsi, agi, h_sign, others):
                cross_bad = True
                break
        # Skip only when tip/text order OK, no cross, AND every non-bottom
        # tip already sits clearly upper-right (not steep-up / not down).
        def _clear_ur(i):
            _pos, _ds, _ag = placements[i][3], placements[i][5], placements[i][6]
            if _leader_half_band(_ag) != 'up':
                return False
            _rad = math.radians(_ag % 360)
            # Need a real rightward component (朝右上), not near-vertical up
            if math.cos(_rad) < math.cos(math.radians(70)):
                return False
            if _ds * math.cos(_rad) < 12.0:
                return False
            return True

        hi = order[0]
        hi_needs_ur = not _clear_ur(hi)
        mid_need_ur = any(
            (rank < len(order) - 1 and not _clear_ur(idx))
            for rank, idx in enumerate(order))
        if order_ok and not cross_bad and not hi_needs_ur and not mid_need_ur:
            continue
        # Place from highest tip downward so peers park below
        for rank, idx in enumerate(order):
            gt, itt, lbt, pt, ltt, dst, agt = placements[idx][:7]
            # Upper half of stack → insist on Q1 upper-right (45–55°)
            if rank < max(1, (len(order) + 1) // 2):
                prefer = 50.0 if rank == 0 else 45.0
                pref_list = (50.0, 45.0, 55.0, 40.0, 60.0, 35.0)
            else:
                prefer = _ang_ladder[min(rank, len(_ang_ladder) - 1)]
                pref_list = (prefer, prefer + 10, prefer - 10, 315.0, 305.0)
            others_tb = (
                [placed_text_bboxes[k] for k in range(n) if k != idx]
                + list(cross_view_text_bboxes or []))
            others_bb = [placed_bboxes[k] for k in range(n) if k != idx]
            hq = _weld_home_quadrant(pt[0], pt[1], cx, cy)
            nbrs = [(placements[k][3], placements[k][6])
                    for k in range(n) if k != idx]
            applied = False

            def _try_pose(nd, na):
                if not _leader_axis_ok(na):
                    return False
                # Upper-half ranks must stay upper-right with enough +X
                if rank < max(1, (len(order) + 1) // 2):
                    if _leader_half_band(na) != 'up':
                        return False
                    _rad = math.radians(na % 360)
                    if math.cos(_rad) < math.cos(math.radians(70)):
                        return False
                    if nd * math.cos(_rad) < 12.0:
                        return False
                ttbb = _text_bbox(pt, nd, na, ltt, is_pair=False)
                if not _label_hard_clear(
                        ttbb, others_tb, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes):
                    return False
                tcy = 0.5 * (ttbb[2] + ttbb[3])
                for prev in order[:rank]:
                    pcy = 0.5 * (placed_text_bboxes[prev][2]
                                 + placed_text_bboxes[prev][3])
                    if tcy > pcy - 0.5:
                        return False
                nbb = _single_bbox(pt, nd, na, ltt)
                placements[idx] = (gt, itt, lbt, pt, ltt, nd, na, nbb)
                placed_bboxes[idx] = nbb
                placed_text_bboxes[idx] = ttbb
                return True

            for pref in pref_list:
                _, nd, na = _search_placement(
                    pt, lines, text_bboxes, circles, others_bb, others_tb,
                    vx0, vy0, vx1, vy1, draw_bbox, is_pair=False,
                    hatch_bboxes=hatch_bboxes,
                    other_view_bboxes=other_view_bboxes,
                    home_q=hq, quad_cx=cx, quad_cy=cy,
                    other_view_part_bboxes=other_view_part_bboxes,
                    label_text=ltt, wm_text_bboxes=wm_text_bboxes,
                    part_bbox=_part, allow_adjacent=True,
                    prefer_ang=pref % 360, neighbor_angles=nbrs,
                    max_dist=int(LADDER_MAX_DIAG), cross_ok=False)
                if _try_pose(nd, na):
                    applied = True
                    break
            if not applied:
                want_up = (rank < max(1, (len(order) + 1) // 2))
                fan = ([50, 45, 55, 40, 60, 35] if want_up else
                       [315, 305, 325, 295, 285])
                for nd in range(MIN_DIAG_LEN, LADDER_MAX_DIAG + 1, 2):
                    for na in fan:
                        if _try_pose(nd, na):
                            applied = True
                            break
                    if applied:
                        break

        # Near-coincident tip pairs on this cluster: higher must be clear UR
        for a in range(len(order)):
            for b in range(a + 1, len(order)):
                ia, ib = order[a], order[b]
                pa, pb = placements[ia][3], placements[ib][3]
                if math.hypot(pa[0] - pb[0], pa[1] - pb[1]) > 3.5:
                    continue
                # ia has higher tip Y (order is high→low)
                if _clear_ur(ia):
                    continue
                gt, itt, lbt, pt, ltt, dst, agt = placements[ia][:7]
                others_tb = (
                    [placed_text_bboxes[k] for k in range(n) if k != ia]
                    + list(cross_view_text_bboxes or []))
                for nd in range(22, LADDER_MAX_DIAG + 1, 2):
                    for na in (50, 45, 55, 40, 60):
                        _rad = math.radians(na)
                        if nd * math.cos(_rad) < 14:
                            continue
                        ttbb = _text_bbox(pt, nd, na, ltt, is_pair=False)
                        if not _label_hard_clear(
                                ttbb, others_tb, lines, draw_bbox,
                                wm_text_bboxes, hatch_bboxes):
                            continue
                        nbb = _single_bbox(pt, nd, na, ltt)
                        placements[ia] = (gt, itt, lbt, pt, ltt, nd, na, nbb)
                        placed_bboxes[ia] = nbb
                        placed_text_bboxes[ia] = ttbb
                        break
                    else:
                        continue
                    break


def _force_diverge_parallel_leaders(placements, placed_bboxes, placed_text_bboxes,
                                   lines, text_bboxes, circles,
                                   vx0, vy0, vx1, vy1, draw_bbox, hatch_bboxes,
                                   other_view_bboxes, other_view_part_bboxes,
                                   wm_text_bboxes, cx, cy, part_bbox,
                                   cross_view_text_bboxes, allow_soft_wm=True):
    """Near-parallel close leaders → higher tip up-band, lower tip down-band.

    Prefer short leaders into the own-side blank (corridor when present).
    Pre-gate calls should pass allow_soft_wm=False so soft corridor poses
    are not handed to the hard gate (which parks peers).
    """
    n = len(placements)
    if n < 2:
        return
    _part = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _ov = other_view_part_bboxes if other_view_part_bboxes else other_view_bboxes
    # Higher tip-pairs first so mid-stack (F25) lifts before a lower tip
    # (F27) can be mistaken for the 'hi' of a deeper pair.
    _pair_idxs = []
    for i in range(n):
        for j in range(i + 1, n):
            pi, pj = placements[i][3], placements[j][3]
            if math.hypot(pi[0] - pj[0], pi[1] - pj[1]) > CLUSTER_RADIUS * 1.4:
                continue
            dsi, agi = placements[i][5], placements[i][6]
            dsj, agj = placements[j][5], placements[j][6]
            if not _leaders_near_parallel(pi, dsi, agi, pj, dsj, agj):
                if _angle_delta_deg(agi, agj) >= LADDER_PARALLEL_MIN_DEG:
                    continue
            _pair_idxs.append((i, j, max(pi[1], pj[1])))
    _pair_idxs.sort(key=lambda t: -t[2])
    _frozen_dn = set()

    def _try_retarget(target, prefer, peer_ang, right, short_first=True):
        gt, itt, lbt, pt, ltt, dst, agt = placements[target][:7]
        is_pair = (gt == 'pair')
        others_tb = (
            [placed_text_bboxes[k] for k in range(n) if k != target]
            + list(cross_view_text_bboxes or []))
        others_bb = [placed_bboxes[k] for k in range(n) if k != target]
        hq = _weld_home_quadrant(pt[0], pt[1], cx, cy)
        _old_tbb = placed_text_bboxes[target]
        want = 'up' if _leader_half_band(prefer) == 'up' else 'dn'
        caps = ((PREFERRED_DIAG_SOFT, PREFERRED_DIAG_HARD, LADDER_MAX_DIAG)
                if short_first else (PREFERRED_DIAG_HARD, LADDER_MAX_DIAG))

        def _accept(nd, na, soft_wm=False):
            if _angle_delta_deg(na, peer_ang) < DIVERGE_ANGLE_MIN - 5:
                return False
            if _leader_half_band(na) != want:
                return False
            ttbb = _text_bbox(pt, nd, na, ltt, is_pair=is_pair)
            if draw_bbox is not None and not _text_in_inner_frame(
                    ttbb, draw_bbox):
                return False
            # Soft corridor: ignore inflated WM pad only; still clear lines /
            # real WM / hatch / other blue labels (see _label_corridor_soft_clear).
            _, _, _gbox = _corridor_info(
                pt[0], pt[1], _part, _ov or [], home_q=None)
            _in_gap = _text_in_gap_box(ttbb, _gbox) if _gbox else False
            _corr_ok = _corridor_pose_acceptable(
                pt[0], pt[1], _old_tbb, ttbb, _part, _ov,
                require_keep_gap=False)
            if soft_wm and (_in_gap or (want == 'dn' and _corr_ok)):
                if not _label_corridor_soft_clear(
                        ttbb, others_tb, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes, in_gap=_in_gap):
                    return False
            elif not _label_hard_clear(
                    ttbb, others_tb, lines, draw_bbox,
                    wm_text_bboxes, hatch_bboxes):
                return False
            if not _corr_ok:
                return False
            nbb = (_paired_bbox(pt, nd, na, ltt) if is_pair
                   else _single_bbox(pt, nd, na, ltt))
            placements[target] = (gt, itt, lbt, pt, ltt, nd, na, nbb)
            placed_bboxes[target] = nbb
            placed_text_bboxes[target] = ttbb
            return True

        for cap in caps:
            _, nd, na = _search_placement(
                pt, lines, text_bboxes, circles, others_bb, others_tb,
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                hatch_bboxes=hatch_bboxes,
                other_view_bboxes=other_view_bboxes,
                home_q=hq, quad_cx=cx, quad_cy=cy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=ltt, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_part,
                prefer_down=(want == 'dn'),
                allow_adjacent=True, prefer_ang=prefer,
                neighbor_angles=[(placements[k][3], placements[k][6])
                                 for k in range(n) if k != target],
                max_dist=int(cap), cross_ok=False)
            if _accept(nd, na):
                return True
        if want == 'up':
            # Deeper left into corridor (165–175) clears WM near the stem
            _angs = ([165, 170, 160, 155, 175, 150, 145, 140, 135, 125]
                     if not right else
                     [55, 45, 65, 35, 75, 50, 40])
        else:
            _angs = ([215, 225, 235, 205, 200, 245, 190]
                     if not right else
                     [305, 315, 295, 325, 285, 300])
        _soft_modes = (False, True) if allow_soft_wm else (False,)
        _dist_hi = (LADDER_MAX_DIAG if allow_soft_wm else PREFERRED_DIAG_HARD)
        for soft in _soft_modes:
            _dists = list(range(MIN_DIAG_LEN, int(_dist_hi) + 1, 2))
            # Soft up-left: prefer longer leaders first so text clears WM
            if soft and want == 'up' and not right:
                _dists = list(reversed(_dists))
            for nd in _dists:
                for na in _angs:
                    if not _leader_axis_ok(na):
                        continue
                    if _accept(nd, na, soft_wm=soft):
                        return True
        return False

    for i, j, _ymax in _pair_idxs:
        pi, pj = placements[i][3], placements[j][3]
        if pi[1] >= pj[1]:
            hi, lo = i, j
        else:
            hi, lo = j, i
        right = ((pi[0] + pj[0]) / 2.0) >= cx
        up_pref, dn_pref = (DIVERGE_PREF_RIGHT if right else DIVERGE_PREF_LEFT)
        bi = _leader_half_band(placements[hi][6])
        bj = _leader_half_band(placements[lo][6])
        adeg = _angle_delta_deg(placements[hi][6], placements[lo][6])
        # Already split into opposite bands with enough angle → freeze lo
        if bi == 'up' and bj == 'dn' and adeg >= DIVERGE_ANGLE_MIN:
            _frozen_dn.add(lo)
            continue

        same_band = (bi == bj)
        parallelish = (adeg < LADDER_PARALLEL_MIN_DEG)
        # Only split downward stacks by lifting hi. Never yank an up label
        # down just because a higher tip is also up (that undoes F25 corridor).
        if same_band and bi == 'dn' and (hi not in _frozen_dn or parallelish):
            if _try_retarget(hi, up_pref, placements[lo][6], right):
                bi = _leader_half_band(placements[hi][6])
                _frozen_dn.discard(hi)
        elif bi != 'up' and hi not in _frozen_dn:
            if _try_retarget(hi, up_pref, placements[lo][6], right):
                bi = _leader_half_band(placements[hi][6])
                _frozen_dn.discard(hi)

        adeg = _angle_delta_deg(placements[hi][6], placements[lo][6])
        bj = _leader_half_band(placements[lo][6])
        # Push lo down only when it is not already up in a valid split,
        # or when both are still dn / parallelish.
        if bj != 'dn' and bi == 'dn':
            if _try_retarget(lo, dn_pref, placements[hi][6], right):
                _frozen_dn.add(lo)
        elif bj == 'dn' and adeg < DIVERGE_ANGLE_MIN and bi == 'up':
            # hi up / lo dn but still too parallel → nudge lo further dn
            if _try_retarget(lo, dn_pref, placements[hi][6], right):
                _frozen_dn.add(lo)
        elif bj == 'dn':
            _frozen_dn.add(lo)

        # Same-band leftover — dn stacks only
        if (_leader_half_band(placements[hi][6]) == 'dn'
                and _leader_half_band(placements[lo][6]) == 'dn'):
            if _try_retarget(hi, up_pref, placements[lo][6], right):
                _frozen_dn.discard(hi)
            if _leader_half_band(placements[lo][6]) != 'dn':
                if _try_retarget(lo, dn_pref, placements[hi][6], right):
                    _frozen_dn.add(lo)

        # Wrong Y-band (higher tip dn, lower tip up) → swap
        if (_leader_half_band(placements[hi][6]) == 'dn'
                and _leader_half_band(placements[lo][6]) == 'up'):
            if hi not in _frozen_dn:
                _try_retarget(hi, up_pref, placements[lo][6], right)
            _try_retarget(lo, dn_pref, placements[hi][6], right)


def _blank_tip_grid(wx, wy, part_bbox, ov, draw_bbox, cx):
    """Tip candidates: short near-weld first, then own-half corridor / outer blank."""
    tips = []
    pitch = max(_lh() * LADDER_SLOT_PITCH, _lh() * 2.0)
    x_stag = _lh() * LADDER_X_STAGGER
    px0, py0, px1, py1 = part_bbox
    _, _, gbox = _corridor_info(wx, wy, part_bbox, ov or [], home_q=None)
    gap_right = None
    if gbox is not None:
        gap_right = gbox[0] >= wx - 2

    # Phase A: tips into corridor mid (text should land in gap, not overshoot)
    prefer_right = (gap_right if gap_right is not None else (wx >= cx))
    if gbox is not None:
        gxm = 0.5 * (gbox[0] + gbox[2])
        for gy in (wy, wy + 10, wy - 10, wy + 18, wy - 18,
                   0.5 * (gbox[1] + gbox[3])):
            gy = max(gbox[1] + 2, min(gbox[3] - 2, gy))
            for gx in (gxm, 0.4 * gbox[0] + 0.6 * gbox[2],
                       0.6 * gbox[0] + 0.4 * gbox[2]):
                tips.append((gx, gy, 'short', math.hypot(gx - wx, gy - wy)))
    short_angs = ([35, 45, 55, 25, 65, 315, 325, 305, 15]
                  if prefer_right else
                  [145, 135, 155, 125, 115, 225, 215, 235, 165])
    for dist in (PREFERRED_DIAG_MIN, PREFERRED_DIAG_SOFT, 28, 32, 36,
                 PREFERRED_DIAG_HARD, 42):
        for ang in short_angs:
            if not _leader_axis_ok(ang):
                continue
            rad = math.radians(ang)
            tips.append((wx + dist * math.cos(rad), wy + dist * math.sin(rad),
                         'short', dist))

    # Phase B: own-half corridor column (stay inside gap_box x-range)
    y_lo = wy - 40.0
    y_hi = wy + 40.0
    xs = []
    if gbox is not None:
        y_lo = min(y_lo, gbox[1] + 2)
        y_hi = max(y_hi, gbox[3] - 2)
        gw = max(4.0, gbox[2] - gbox[0])
        mid = 0.5 * (gbox[0] + gbox[2])
        # Sample across the full gap strip (not only near tip edge)
        fracs = (0.20, 0.35, 0.50, 0.65, 0.80)
        xs = [gbox[0] + f * gw for f in fracs]
    else:
        if prefer_right:
            xs = [px1 + max(8.0, _lh() * 3.5) + k * x_stag for k in (0, 1, 2, 3)]
        else:
            xs = [px0 - max(8.0, _lh() * 3.5) - k * x_stag for k in (0, 1, 2, 3)]
        # Also try above/below part in open frame
        xs += [wx + (18 if prefer_right else -18),
               wx + (28 if prefer_right else -28)]
    if draw_bbox is not None:
        y_lo = max(y_lo, draw_bbox[1] + BOUNDARY_MARGIN + _lh())
        y_hi = min(y_hi, draw_bbox[3] - BOUNDARY_MARGIN - _lh())
        xs = [x for x in xs
              if draw_bbox[0] + BOUNDARY_MARGIN < x < draw_bbox[2] - BOUNDARY_MARGIN]
    nslot = max(3, int((y_hi - y_lo) / pitch) + 1)
    for si in range(nslot):
        ty = y_hi - si * pitch
        for tx in xs:
            tips.append((tx, ty, 'blank', math.hypot(tx - wx, ty - wy)))

    # Short first, then nearer blank tips; drop tips beyond MAX_DIAG
    tips = [t for t in tips if t[3] <= MAX_DIAG_LEN + 1e-6 or t[2] == 'short']
    tips.sort(key=lambda t: (0 if t[2] == 'short' else 1, t[3], abs(t[1] - wy)))
    return tips, gbox, prefer_right


def _force_place_into_blank(idx, placements, placed_bboxes, placed_text_bboxes,
                           lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                           other_view_bboxes, other_view_part_bboxes,
                           part_bbox, cx, cy, cross_view_text_bboxes,
                           max_diag=None):
    """Force one dirty label into blank: short + own-half first; no shallow cross."""
    n = len(placements)
    gi, iti, lbi, pos, lti, dsi, agi = placements[idx][:7]
    is_pair = (gi == 'pair')
    wx, wy = pos
    _force_dn = any(w.get('_prefer_leader_down') for w, _p in iti)
    _ov = other_view_part_bboxes if other_view_part_bboxes else other_view_bboxes
    _part = part_bbox if part_bbox else (wx - 20, wy - 20, wx + 20, wy + 20)
    _max = max_diag if max_diag is not None else max(LADDER_MAX_DIAG, MAX_DIAG_LEN)
    # Cap: prefer not exceeding soft-hard band unless necessary
    _max = min(_max, LADDER_MAX_DIAG)
    others_tb = (
        [placed_text_bboxes[k] for k in range(n) if k != idx]
        + list(cross_view_text_bboxes or []))
    placed_leaders = [
        _leader_entry(placements[k][3], placements[k][5], placements[k][6],
                      placements[k][4], placements[k][0] == 'pair')
        for k in range(n) if k != idx
    ]
    tips, gbox, prefer_right = _blank_tip_grid(wx, wy, _part, _ov, draw_bbox, cx)
    if _force_dn:
        # Prefer text tips below the underside weld (H-pocket / below flange)
        _dn_tips = [(tx, ty, k, p) for tx, ty, k, p in tips if ty <= wy - 2]
        if _dn_tips:
            tips = _dn_tips + [(tx, ty, k, p) for tx, ty, k, p in tips
                               if ty > wy - 2]
        else:
            hq = _downward_quad_same_half(_weld_home_quadrant(wx, wy, cx, cy))
            _seed = 305.0 if hq == 4 else 225.0
            for _d in (12, 18, 24, 30, 36):
                _rad = math.radians(_seed)
                tips = ([(wx + _d * math.cos(_rad),
                          wy + _d * math.sin(_rad), 'short', True)]
                        + list(tips))
    mid = 0.5 * (gbox[0] + gbox[2]) if gbox is not None else None

    def _try_pose(tip_x, tip_y, side_pref, allow_long):
        dx, dy = tip_x - wx, tip_y - wy
        dist = math.hypot(dx, dy)
        # Pass1: ≤ HARD; Pass2+: ≤ MAX_DIAG only (never stretch past LADDER_MAX)
        if not allow_long:
            cap = min(_max, PREFERRED_DIAG_HARD)
        else:
            cap = min(_max, MAX_DIAG_LEN, LADDER_MAX_DIAG)
        if dist < MIN_DIAG_LEN or dist > cap:
            return None
        ang = math.degrees(math.atan2(dy, dx)) % 360
        if not _leader_axis_ok(ang):
            return None
        if _force_dn and _leader_half_band(ang) == 'up':
            return None
        cos_a = math.cos(math.radians(ang))
        if side_pref and cos_a < -0.05:
            return None
        if (not side_pref) and cos_a >= -0.05:
            return None
        # Keep tip on own half of corridor when possible
        if mid is not None and gbox is not None:
            if prefer_right and tip_x > mid + _lh() * 0.5:
                return None
            if (not prefer_right) and tip_x < mid - _lh() * 0.5:
                return None
        tbb = _text_bbox(pos, dist, ang, lti, is_pair=is_pair)
        if not _label_hard_clear(
                tbb, others_tb, lines, draw_bbox, wm_text_bboxes, hatch_bboxes):
            return None
        # Refuse overshoot past corridor into neighbor column
        if gbox is not None:
            tcx = (tbb[0] + tbb[1]) / 2
            if wx >= gbox[2] - 1 and tcx < gbox[0] - 0.5:
                return None
            if wx <= gbox[0] + 1 and tcx > gbox[2] + 0.5:
                return None
        h_land = _horiz_land(lti, is_pair)
        h_sign = h_land if cos_a >= -0.05 else -h_land
        if _blue_leader_shallow_cross(pos, dist, ang, h_sign, placed_leaders):
            return None
        for k in range(n):
            if k == idx:
                continue
            pk, dsk, agk = placements[k][3], placements[k][5], placements[k][6]
            if _leaders_near_parallel(pos, dist, ang, pk, dsk, agk):
                return None
        return dist, ang, tbb

    best = None
    best_sc = -1e18
    # Pass 1: own side only, short-first tips, ≤ HARD
    for tip_x, tip_y, kind, _pre in tips:
        pose = _try_pose(tip_x, tip_y, prefer_right, allow_long=False)
        if pose is None:
            continue
        dist, ang, tbb = pose
        sc = -dist * 40  # strong short preference
        if kind == 'short':
            sc += 100
        if dist <= PREFERRED_DIAG_SOFT:
            sc += 80
        elif dist <= PREFERRED_DIAG_HARD:
            sc += 30
        if gbox is not None:
            tcx = (tbb[0] + tbb[1]) / 2
            tcy = (tbb[2] + tbb[3]) / 2
            if gbox[0] <= tcx <= gbox[2] and gbox[1] <= tcy <= gbox[3]:
                sc += 220  # text in corridor beats overshoot into neighbor Part
            elif gbox[0] <= tip_x <= gbox[2]:
                sc += 40
            else:
                sc -= 120  # tip past gap → neighbor side
        if _force_dn and tip_y < wy:
            sc += 200
        if sc > best_sc:
            best_sc = sc
            best = (dist, ang, tbb)

    # Pass 2: still own side, up to MAX_DIAG_LEN
    if best is None:
        for tip_x, tip_y, kind, _pre in tips:
            pose = _try_pose(tip_x, tip_y, prefer_right, allow_long=True)
            if pose is None:
                continue
            dist, ang, tbb = pose
            sc = -dist * 30
            if kind == 'short':
                sc += 40
            if sc > best_sc:
                best_sc = sc
                best = (dist, ang, tbb)

    # Pass 3: opposite side, still ≤ MAX_DIAG
    if best is None:
        for tip_x, tip_y, kind, _pre in tips:
            pose = _try_pose(tip_x, tip_y, not prefer_right, allow_long=True)
            if pose is None:
                continue
            dist, ang, tbb = pose
            sc = -dist * 25 - 60
            if sc > best_sc:
                best_sc = sc
                best = (dist, ang, tbb)

    # Pass 4: denser tips, still capped at MAX_DIAG — no +28 stretch
    if best is None:
        full_tips = []
        if gbox is not None:
            gw = max(4.0, gbox[2] - gbox[0])
            pitch = max(_lh() * LADDER_SLOT_PITCH, _lh() * 2.0)
            y_lo = max(wy - 36.0, gbox[1] + 2)
            y_hi = min(wy + 36.0, gbox[3] - 2)
            if draw_bbox is not None:
                y_lo = max(y_lo, draw_bbox[1] + BOUNDARY_MARGIN + _lh())
                y_hi = min(y_hi, draw_bbox[3] - BOUNDARY_MARGIN - _lh())
            for f in (0.12, 0.25, 0.38, 0.50, 0.62, 0.75, 0.88):
                tx = gbox[0] + f * gw
                for si in range(max(4, int((y_hi - y_lo) / pitch) + 1)):
                    full_tips.append((tx, y_hi - si * pitch))
        else:
            for tip_x, tip_y, _, _ in tips:
                full_tips.append((tip_x, tip_y))
        hard_cap = min(_max, MAX_DIAG_LEN, LADDER_MAX_DIAG)
        for tip_x, tip_y in full_tips:
            for side_pref in (prefer_right, not prefer_right):
                dx, dy = tip_x - wx, tip_y - wy
                dist = math.hypot(dx, dy)
                if dist < MIN_DIAG_LEN or dist > hard_cap:
                    continue
                ang = math.degrees(math.atan2(dy, dx)) % 360
                if not _leader_axis_ok(ang):
                    continue
                cos_a = math.cos(math.radians(ang))
                if side_pref and cos_a < -0.05:
                    continue
                if (not side_pref) and cos_a >= -0.05:
                    continue
                tbb = _text_bbox(pos, dist, ang, lti, is_pair=is_pair)
                if not _label_hard_clear(
                        tbb, others_tb, lines, draw_bbox,
                        wm_text_bboxes, hatch_bboxes):
                    continue
                h_land = _horiz_land(lti, is_pair)
                h_sign = h_land if cos_a >= -0.05 else -h_land
                if _blue_leader_shallow_cross(
                        pos, dist, ang, h_sign, placed_leaders, min_deg=40.0):
                    continue
                sc = -dist * 20
                if side_pref == prefer_right:
                    sc += 20
                if sc > best_sc:
                    best_sc = sc
                    best = (dist, ang, tbb)

    if best is None:
        if _exhaustive_hard_clear_pose(
                idx, placements, placed_bboxes, placed_text_bboxes,
                lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                cross_view_text_bboxes, max_diag=min(_max, MAX_DIAG_LEN)):
            return True
        return False
    nd, na, tbb = best
    nbb = (_paired_bbox(pos, nd, na, lti) if is_pair
           else _single_bbox(pos, nd, na, lti))
    placements[idx] = (gi, iti, lbi, pos, lti, nd, na, nbb)
    placed_bboxes[idx] = nbb
    placed_text_bboxes[idx] = tbb
    return True


def _enforce_all_hard_clear(placements, placed_bboxes, placed_text_bboxes,
                            lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                            other_view_bboxes, other_view_part_bboxes,
                            part_bbox, cx, cy, cross_view_text_bboxes):
    """Until every label passes hard clear, force tips into blank (short + own-half)."""
    n = len(placements)
    if n == 0:
        return 0
    fixed = 0
    for _round in range(6):
        dirty = []
        for i in range(n):
            others_tb = (
                [placed_text_bboxes[k] for k in range(n) if k != i]
                + list(cross_view_text_bboxes or []))
            if not _label_hard_clear(
                    placed_text_bboxes[i], others_tb, lines, draw_bbox,
                    wm_text_bboxes, hatch_bboxes):
                dirty.append(i)
        if not dirty:
            break

        def _prio(i):
            tbb = placed_text_bboxes[i]
            on_line = 1 if _text_near_lines(tbb, lines) else 0
            oob = 1 if (draw_bbox is not None and not _text_in_inner_frame(tbb, draw_bbox)) else 0
            return (-on_line, -oob, placements[i][3][1])

        dirty.sort(key=_prio)
        progressed = False
        for i in dirty:
            # Round 0–1: ≤ HARD; later: ≤ MAX_DIAG only (no stretch past 50)
            if _round < 2:
                cap = PREFERRED_DIAG_HARD
            else:
                cap = min(LADDER_MAX_DIAG, MAX_DIAG_LEN)
            if _force_place_into_blank(
                    i, placements, placed_bboxes, placed_text_bboxes,
                    lines, draw_bbox, hatch_bboxes, wm_text_bboxes,
                    other_view_bboxes, other_view_part_bboxes,
                    part_bbox, cx, cy, cross_view_text_bboxes,
                    max_diag=cap):
                fixed += 1
                progressed = True
        if not progressed:
            break
    return fixed


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
        cross_view_text_bboxes=None, other_view_part_bboxes=None):
    """Force-relocate labels whose text still overlaps WM/title/hole hard zones."""
    if not placements:
        return 0
    wm_text_bboxes = wm_text_bboxes or []
    _vcx = quad_cx if quad_cx is not None else (vx0 + vx1) / 2.0
    _vcy = quad_cy if quad_cy is not None else (vy0 + vy1) / 2.0
    _part = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _ov = other_view_part_bboxes if other_view_part_bboxes else other_view_bboxes
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
        _old_tbb = tbb
        _, _gap_ang, _gap_box = _corridor_info(
            pos[0], pos[1], _part, _ov or [], home_q=None)
        # Prefer staying in the inter-view strip (slide down from WM), not flipping
        # into the neighbor Part column.
        _ang_try = []
        if _gap_ang is not None:
            for _da in (0, 10, -10, 18, -18, 28, -28, 40, -40):
                _ang_try.append((_gap_ang + _da) % 360)
            _ang_try.extend([160, 170, 180, 150, 190, 200, 145, 155])
        _ang_try.extend([
            ag + 25, ag - 25, ag + 45, ag - 45, ag + 90, ag - 90,
            ag + 140, ag - 140, ag + 180,
            55, 125, 235, 305,
        ])
        _seen_a, _ang_u = set(), []
        for _a in _ang_try:
            _a = int(round(_a % 360))
            if _a in _seen_a:
                continue
            _seen_a.add(_a)
            _ang_u.append(_a)
        best = None
        best_sc = -1e18
        for na in _ang_u:
            if not _leader_axis_ok(na):
                continue
            if not any(_angle_in_quadrant(na, q)
                       for q in _allowed_quadrants(hq, allow_adjacent=True)):
                # corridor tip-in-gap angles still allowed below via accept gate
                if _gap_box is None:
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
                        _text_overlaps(ttbb, hb, HATCH_CLEAR_MARGIN)
                        for hb in hatch_bboxes):
                    continue
                if any(not (ttbb[1] < ccx - cr or ttbb[0] > ccx + cr
                            or ttbb[3] < ccy - cr or ttbb[2] > ccy + cr)
                       for ccx, ccy, cr in _circs):
                    continue
                if not _corridor_pose_acceptable(
                        pos[0], pos[1], _old_tbb, ttbb, _part, _ov,
                        require_keep_gap=False):
                    continue
                # If old was in gap, prefer staying in gap (slide along strip)
                if (_gap_box is not None and _text_in_gap_box(_old_tbb, _gap_box)
                        and not _text_in_gap_box(ttbb, _gap_box)):
                    continue
                h_land = _leader_entry(pos, nd, na, ltk, is_pair)[3]
                if _blue_leader_shallow_cross(pos, nd, na, h_land, _leaders):
                    continue
                nbb = (_paired_bbox(pos, nd, na, ltk) if is_pair
                       else _single_bbox(pos, nd, na, ltk))
                if not _bbox_in_boundary(nbb, vx0, vy0, vx1, vy1, draw_bbox):
                    continue
                sc = -nd * 30
                if _gap_box is not None and _text_in_gap_box(ttbb, _gap_box):
                    sc += 250
                if sc > best_sc:
                    best_sc = sc
                    best = (nd, na, nbb, ttbb)
        if best is None:
            # Last resort: _search_placement with adjacent quadrants
            _pref = _gap_ang if _gap_ang is not None else (ag + 180) % 360
            _sc, _fd, _fa = _search_placement(
                pos, lines, text_bboxes, circles, others_bb, others_tb,
                vx0, vy0, vx1, vy1, draw_bbox, is_pair=is_pair,
                hatch_bboxes=hatch_bboxes, other_view_bboxes=other_view_bboxes,
                home_q=hq, quad_cx=_vcx, quad_cy=_vcy,
                other_view_part_bboxes=other_view_part_bboxes,
                label_text=ltk, wm_text_bboxes=wm_text_bboxes,
                part_bbox=_part, prefer_down=False, max_dist=_max_len,
                allow_adjacent=True, prefer_ang=_pref,
                neighbor_angles=_nbr, cross_ok=False, placed_leaders=_leaders)
            ttbb = _text_bbox(pos, _fd, _fa, ltk, is_pair=is_pair)
            _ok_hard = not any(_text_overlaps(ttbb, z, 1.0) for z in wm_text_bboxes)
            if _ok_hard and _hatch:
                _ok_hard = not any(_text_overlaps(ttbb, z, 1.0) for z in _hatch)
            if _ok_hard and _corridor_pose_acceptable(
                    pos[0], pos[1], _old_tbb, ttbb, _part, _ov,
                    require_keep_gap=(_gap_box is not None
                                     and _text_in_gap_box(_old_tbb, _gap_box))):
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
    """Final pass: remove blue×blue crosses (prefer none; else acute > min_deg)."""
    if not placements:
        return 0
    _vcx = quad_cx if quad_cx is not None else (vx0 + vx1) / 2.0
    _vcy = quad_cy if quad_cy is not None else (vy0 + vy1) / 2.0
    _part = part_bbox if part_bbox else (vx0, vy0, vx1, vy1)
    _wm = wm_text_bboxes or []
    _cross_txt = cross_view_text_bboxes or []
    circles = circles or []
    fixed = 0
    max_iter = max(max_iter, 8)

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
                hi = hi if math.cos(math.radians(agi)) >= -0.05 else -hi
                hj = hj if math.cos(math.radians(agj)) >= -0.05 else -hj
                crosses, _ = _leader_crosses_leader(
                    posi, dsi, agi, hi, posj, dsj, agj, hj)
                if not crosses:
                    continue
                acute = _leader_cross_acute_deg(agi, agj)
                # Shallow/medium crosses must be removed; steep ones still try
                # no-cross first, then may keep if > LEADER_CROSS_MIN_DEG.
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
                        if (not prefer_no_cross
                                and acute > LEADER_CROSS_MIN_DEG):
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
                        if not (tbbi[1] < hx0 - HATCH_CLEAR_MARGIN or
                                tbbi[0] > hx1 + HATCH_CLEAR_MARGIN or
                                tbbi[3] < hy0 - HATCH_CLEAR_MARGIN or
                                tbbi[2] > hy1 + HATCH_CLEAR_MARGIN):
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
                    if _text_overlaps(tbbi, htb, HATCH_CLEAR_MARGIN):
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
