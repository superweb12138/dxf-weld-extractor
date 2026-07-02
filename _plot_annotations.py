import ezdxf, os, glob, sys
import matplotlib.pyplot as plt
import matplotlib.patches as patches

LAYER = 'WELD_LABELS'


def get_bounding_box(msp):
    xs, ys = [], []
    for e in msp.query('*'):
        try:
            b = e.get_bbox()
            xs += [b.extmin.x, b.extmax.x]
            ys += [b.extmin.y, b.extmax.y]
        except Exception:
            pass
    if not xs:
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def line_intersect(a, b, c, d):
    def ccw(A, B, C):
        return (C[1] - A[1]) * (B[0] - A[0]) > (B[1] - A[1]) * (C[0] - A[0])
    return ccw(a, c, d) != ccw(b, c, d) and ccw(a, b, c) != ccw(a, b, d)


def analyze(path, ax):
    doc = ezdxf.readfile(path)
    msp = doc.modelspace()

    # labels
    labels = []
    for e in msp.query('MTEXT'):
        if e.dxf.layer != LAYER:
            continue
        ins = e.dxf.insert
        h = e.dxf.char_height or 2.5
        w = (len(e.text) if e.text else 1) * h * 0.65
        labels.append({'xy': (ins.x, ins.y), 'w': w, 'h': h, 'text': e.text})

    # leader lines on WELD_LABELS
    lines = []
    for e in msp.query('LINE'):
        if e.dxf.layer == LAYER:
            lines.append(((e.dxf.start.x, e.dxf.start.y), (e.dxf.end.x, e.dxf.end.y)))
    for e in msp.query('LWPOLYLINE'):
        if e.dxf.layer != LAYER:
            continue
        pts = list(e.get_points(format='xy'))
        for i in range(len(pts) - 1):
            lines.append((pts[i], pts[i + 1]))

    # crossings only between leader lines
    crosses = []
    for i, (a, b) in enumerate(lines):
        for j, (c, d) in enumerate(lines):
            if j <= i:
                continue
            # ignore if sharing endpoint
            shared = (a == c or a == d or b == c or b == d)
            if shared:
                continue
            if line_intersect(a, b, c, d):
                crosses.append((a, b, c, d))

    # weld points: endpoints not inside any label bbox
    def inside_label(pt):
        for lab in labels:
            x, y = lab['xy']
            if x <= pt[0] <= x + lab['w'] and y - lab['h'] <= pt[1] <= y:
                return True
        return False

    weld_pts = []
    for a, b in lines:
        for pt in [a, b]:
            if not inside_label(pt):
                weld_pts.append(pt)
    # deduplicate
    seen = set()
    uniq = []
    for p in weld_pts:
        key = (round(p[0], 2), round(p[1], 2))
        if key not in seen:
            seen.add(key)
            uniq.append(p)
    weld_pts = uniq

    # draw
    for lab in labels:
        x, y = lab['xy']
        ax.add_patch(patches.Rectangle((x, y - lab['h']), lab['w'], lab['h'],
                                       linewidth=0.6, edgecolor='blue', facecolor='lightblue', alpha=0.5))
        ax.text(x + lab['w'] / 2, y - lab['h'] / 2, lab['text'],
                ha='center', va='center', fontsize=6, color='navy')
    for a, b in lines:
        ax.plot([a[0], b[0]], [a[1], b[1]], 'k-', linewidth=0.6)
    for p in weld_pts:
        ax.plot(p[0], p[1], 'ro', markersize=3)
    for a, b, c, d in crosses:
        mx = (a[0] + b[0]) / 2
        my = (a[1] + b[1]) / 2
        ax.plot(mx, my, 'gx', markersize=5)

    ax.set_aspect('equal')
    ax.set_title(f"{os.path.basename(path)}\nlabels={len(labels)} leader_segs={len(lines)} crossings={len(crosses)} welds={len(weld_pts)}")


if __name__ == '__main__':
    target = sys.argv[1] if len(sys.argv) > 1 else 'CO007'
    files = [f for f in glob.glob('annotated/*.dxf') if target in os.path.basename(f)]
    if not files:
        print('No files found for', target)
        sys.exit(1)
    fig, axes = plt.subplots(1, len(files), figsize=(8 * len(files), 10))
    if len(files) == 1:
        axes = [axes]
    for ax, f in zip(axes, files):
        analyze(f, ax)
    out = f'_plot_{target}.png'
    plt.tight_layout()
    plt.savefig(out, dpi=150)
    print('saved', out)
