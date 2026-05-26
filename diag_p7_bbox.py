"""Diagnostic: p7 vs CO009 3D bounding box, contact axis, weld length."""
import ifcopenshell
import ifcopenshell.geom

TOL = 2.0  # mm


def get_prop(entity, name):
    try:
        for rel in entity.IsDefinedBy:
            pset = rel.RelatingPropertyDefinition
            if hasattr(pset, 'HasProperties'):
                for prop in pset.HasProperties or []:
                    if prop.Name == name:
                        v = prop.NominalValue
                        if v is not None:
                            return v.wrappedValue
    except Exception:
        pass
    return None


def get_bbox(entity, settings):
    try:
        shape = ifcopenshell.geom.create_shape(settings, entity)
        if shape:
            verts = shape.geometry.verts
            if verts:
                xs = verts[0::3]
                ys = verts[1::3]
                zs = verts[2::3]
                return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    except Exception:
        pass
    return None


def bbox_overlap_gap(bb1, bb2):
    x1min, y1min, z1min, x1max, y1max, z1max = bb1
    x2min, y2min, z2min, x2max, y2max, z2max = bb2

    overlap_x = max(0, min(x1max, x2max) - max(x1min, x2min))
    gap_x = max(0, max(x1min, x2min) - min(x1max, x2max))

    overlap_y = max(0, min(y1max, y2max) - max(y1min, y2min))
    gap_y = max(0, max(y1min, y2min) - min(y1max, y2max))

    overlap_z = max(0, min(z1max, z2max) - max(z1min, z2min))
    gap_z = max(0, max(z1min, z2min) - min(z1max, z2max))

    return {
        'x': {'overlap': overlap_x, 'gap': gap_x},
        'y': {'overlap': overlap_y, 'gap': gap_y},
        'z': {'overlap': overlap_z, 'gap': gap_z},
    }


def main():
    path = r'C:\Users\15297\Desktop\hanf\ifc格式\CO009.ifc'
    f = ifcopenshell.open(path)

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    # ---- find the two entities ----
    p7_entity = None
    co009_entity = None

    for p in f.by_type('IfcPlate'):
        if get_prop(p, 'Reference') == 'p7':
            p7_entity = p
            break

    for c in f.by_type('IfcColumn'):
        if get_prop(c, 'Reference') == 'CO009':
            co009_entity = c
            break

    if not p7_entity:
        print("ERROR: p7 not found in IfcPlate entities")
        return
    if not co009_entity:
        print("ERROR: CO009 not found in IfcColumn entities")
        return

    # ---- height / width / length ----
    p7_h = get_prop(p7_entity, 'Height')
    p7_w = get_prop(p7_entity, 'Width')
    p7_l = get_prop(p7_entity, 'Length')

    co009_h = get_prop(co009_entity, 'Height')
    co009_w = get_prop(co009_entity, 'Width')
    co009_l = get_prop(co009_entity, 'Length')

    print("=" * 70)
    print("PROPERTIES (Height / Width / Length in mm)")
    print("=" * 70)
    print(f"{'Name':<10} {'Type':<12} {'Height':>10} {'Width':>10} {'Length':>10}")
    print(f"{'p7':<10} {'IfcPlate':<12} {p7_h:>10.1f} {p7_w:>10.1f} {p7_l:>10.1f}")
    print(f"{'CO009':<10} {'IfcColumn':<12} {co009_h:>10.1f} {co009_w:>10.1f} {co009_l:>10.1f}")

    # ---- bounding boxes ----
    bb_p7 = get_bbox(p7_entity, settings)
    bb_co009 = get_bbox(co009_entity, settings)

    if not bb_p7:
        print("ERROR: could not compute bbox for p7")
        return
    if not bb_co009:
        print("ERROR: could not compute bbox for CO009")
        return

    # ifcopenshell.geom returns world coords in metres; convert to mm
    bb_p7_mm = tuple(v * 1000.0 for v in bb_p7)
    bb_co009_mm = tuple(v * 1000.0 for v in bb_co009)

    print(f"\n{'=' * 70}")
    print("3D BOUNDING BOXES (world coordinates, mm)")
    print("=" * 70)

    for name, bb in [('p7', bb_p7_mm), ('CO009', bb_co009_mm)]:
        xmin, ymin, zmin, xmax, ymax, zmax = bb
        dx = xmax - xmin
        dy = ymax - ymin
        dz = zmax - zmin
        print(f"\n  {name}:")
        print(f"    X : [{xmin:>12.2f}, {xmax:>12.2f}]  span = {dx:.2f}")
        print(f"    Y : [{ymin:>12.2f}, {ymax:>12.2f}]  span = {dy:.2f}")
        print(f"    Z : [{zmin:>12.2f}, {zmax:>12.2f}]  span = {dz:.2f}")

    # ---- overlap / gap per axis ----
    result = bbox_overlap_gap(bb_p7_mm, bb_co009_mm)

    print(f"\n{'=' * 70}")
    print("OVERLAP & GAP PER AXIS (mm)")
    print("=" * 70)
    for axis in ['x', 'y', 'z']:
        ol = result[axis]['overlap']
        gap = result[axis]['gap']
        print(f"  {axis.upper()} : overlap = {ol:>10.2f}   gap = {gap:>10.2f}")

    # ---- contact axis ----
    EPS = 1e-6  # floating-point tolerance (same as gap/overlap noise)
    contact_axis = None
    contact_gap = float('inf')
    for axis in ['x', 'y', 'z']:
        ol = result[axis]['overlap']
        gap = result[axis]['gap']
        if ol <= EPS and gap <= TOL:
            if gap < contact_gap:
                contact_gap = gap
                contact_axis = axis

    print(f"\n{'=' * 70}")
    print("CONTACT AXIS  (open gap <= 2mm, overlap == 0)")
    print("=" * 70)

    if contact_axis:
        print(f"  Contact axis : {contact_axis.upper()}")
        print(f"  Contact gap  : {contact_gap:.2f} mm")

        # ---- weld length ----
        non_contact = [ax for ax in ['x', 'y', 'z'] if ax != contact_axis]
        overlays = [result[ax]['overlap'] for ax in non_contact]
        weld_len = max(overlays)

        print(f"\n{'=' * 70}")
        print("WELD LENGTH  (max overlap in the two non-contact axes)")
        print("=" * 70)
        print(f"  Non-contact axes : {non_contact[0].upper()} and {non_contact[1].upper()}")
        print(f"  Overlap {non_contact[0].upper()}  : {overlays[0]:.2f} mm")
        print(f"  Overlap {non_contact[1].upper()}  : {overlays[1]:.2f} mm")
        print(f"  Max overlap      : {weld_len:.2f} mm")
        print(f"  >>> WELD LENGTH  = {weld_len:.2f} mm")
    else:
        print("  No single contact axis found.")
        for axis in ['x', 'y', 'z']:
            ol = result[axis]['overlap']
            gap = result[axis]['gap']
            if ol > EPS:
                print(f"    {axis.upper()} is overlapping       (overlap={ol:.2f})")
            elif gap == 0:
                print(f"    {axis.upper()} is touching exactly   (gap={gap:.2f})")
            elif gap <= TOL:
                print(f"    {axis.upper()} is in contact         (gap={gap:.2f})")
            else:
                print(f"    {axis.upper()} is separated          (gap={gap:.2f})")

    print()


if __name__ == '__main__':
    main()
