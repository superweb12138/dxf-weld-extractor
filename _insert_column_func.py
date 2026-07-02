with open('dxf_annotator.py', 'r', encoding='utf-8') as f:
    content = f.read()

marker = 'def _draw_paired_weld_label(msp, labels, weld_pos, dname, diag_len, angle_deg, sampled=False):'

new_func = '''def _draw_column_label(msp, labels, weld_pos, column_x, label_y, side,
                       gtype='single', sampled=False):
    """Draw side-stacked column label: weld -> horizontal -> vertical -> text.
    L-shaped leaders avoid crossings because horizontal segments are at distinct y-levels.
    """
    wx, wy = weld_pos
    msp.add_line(start=(wx, wy), end=(column_x, wy),
                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})
    msp.add_line(start=(column_x, wy), end=(column_x, label_y),
                 dxfattribs={'layer': LAYER_NAME, 'color': LABEL_COLOR})

    _arrow_ang = 0.0 if side == 'right' else 180.0
    _draw_arrow_head(msp, (wx, wy), _arrow_ang)

    label = f"{labels[0]},{labels[1]}" if gtype == 'pair' else labels[0]
    if side == 'right':
        ap = MT_BOTTOM_LEFT
        lx = column_x
    else:
        ap = MT_BOTTOM_RIGHT
        lx = column_x
    msp.add_mtext(label, dxfattribs={
        'layer': LAYER_NAME, 'color': LABEL_COLOR,
        'char_height': LABEL_HEIGHT,
        'insert': (lx, label_y),
        'attachment_point': ap,
        'style': 'Arial Narrow',
        'lineweight': 30,
    })

    if sampled:
        _tw = len(label) * LABEL_HEIGHT * 0.6
        if side == 'right':
            _cx = lx + _tw / 2
        else:
            _cx = lx - _tw / 2
        _cy = label_y + LABEL_HEIGHT / 2
        _rx = _tw / 2 + 1.3
        _ry = LABEL_HEIGHT / 2 + 1.3
        msp.add_ellipse(center=(_cx, _cy), major_axis=(_rx, 0),
                        ratio=_ry / max(_rx, 0.01),
                        dxfattribs={'layer': LAYER_NAME, 'color': 1})


'''

idx = content.find(marker)
if idx < 0:
    print('marker not found')
else:
    content = content[:idx] + new_func + content[idx:]
    with open('dxf_annotator.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print('inserted')
