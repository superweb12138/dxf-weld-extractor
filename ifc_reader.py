"""
IFC Reader for weld statistics
Parses Tekla-exported IFC files to extract plate dimensions and 3D adjacency.

Plate dimensions (Tekla naming convention):
  Height  = plate width (mm) along main axis
  Width   = plate thickness (mm)
  Length  = plate length (mm)

Adjacency: two plates are adjacent if their 3D bounding boxes
  overlap in two axes and are within 2mm in the third axis.
"""
import ifcopenshell
import ifcopenshell.geom
import math
import os
from collections import defaultdict

# Adjacency tolerance: plates within this distance (mm) are considered touching
ADJ_TOL_3D = 2.0

def get_prop(entity, name):
    """Extract a single property value from an IFC entity."""
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


def get_plate_bbox(entity, settings):
    """Get the axis-aligned bounding box of an IFC plate in world coordinates.
    Returns (xmin, ymin, zmin, xmax, ymax, zmax) or None."""
    try:
        shape = ifcopenshell.geom.create_shape(settings, entity)
        if shape:
            verts = shape.geometry.verts
            if verts:
                # verts is a tuple of floats: [x0, y0, z0, x1, y1, z1, ...]
                xs = verts[0::3]
                ys = verts[1::3]
                zs = verts[2::3]
                return (min(xs), min(ys), min(zs), max(xs), max(ys), max(zs))
    except Exception:
        pass
    return None


def bboxes_adjacent(bb1, bb2, tol=ADJ_TOL_3D):
    """Check if two 3D bounding boxes are touching/overlapping.
    Two plates touch if they overlap in 2 axes (gap <= tol) and
    are within tol distance in the third axis."""
    x1min, y1min, z1min, x1max, y1max, z1max = bb1
    x2min, y2min, z2min, x2max, y2max, z2max = bb2

    # Check X-axis overlap
    overlap_x = max(0, min(x1max, x2max) - max(x1min, x2min))
    gap_x = max(0, max(x1min, x2min) - min(x1max, x2max))
    near_x = overlap_x > 0 or gap_x <= tol

    # Check Y-axis overlap
    overlap_y = max(0, min(y1max, y2max) - max(y1min, y2min))
    gap_y = max(0, max(y1min, y2min) - min(y1max, y2max))
    near_y = overlap_y > 0 or gap_y <= tol

    # Check Z-axis overlap
    overlap_z = max(0, min(z1max, z2max) - max(z1min, z2min))
    gap_z = max(0, max(z1min, z2min) - min(z1max, z2max))
    near_z = overlap_z > 0 or gap_z <= tol

    # Adjacent if all three axes are near, with at least one tight contact
    near_axes = [near_x, near_y, near_z]
    tight_contact = (
        (overlap_x > 0 or gap_x <= tol) and
        (overlap_y > 0 or gap_y <= tol) and
        (overlap_z > 0 or gap_z <= tol)
    )

    if tight_contact:
        # Compute shared edge length: the overlap length in the two
        # co-planar axes, and the plate dimension in the contact axis
        contact_axis = None
        contact_gap = float('inf')
        for axis, (gap, ol) in enumerate(
            [(gap_x, overlap_x), (gap_y, overlap_y), (gap_z, overlap_z)]
        ):
            if ol <= 0 and gap <= tol:
                if gap < contact_gap:
                    contact_gap = gap
                    contact_axis = axis

        if contact_axis is not None:
            # Weld length = overlap in the two non-contact axes
            if contact_axis == 0:  # X is contact axis
                weld_len = min(overlap_y + overlap_z, overlap_y * 2)
                if overlap_y == 0 and overlap_z == 0:
                    weld_len = 0
                else:
                    weld_len = math.sqrt(overlap_y**2 + overlap_z**2) if overlap_y > 0 and overlap_z > 0 else max(overlap_y, overlap_z)
            elif contact_axis == 1:  # Y is contact axis
                weld_len = max(overlap_x, overlap_z)
            else:  # Z is contact axis
                weld_len = max(overlap_x, overlap_y)
            return True, round(weld_len, 1)
    return False, 0


def read_ifc(filepath):
    """
    Parse an IFC file and return:
      plate_dims:  {label: {'thick': t, 'width': bw, 'bom_len': bl, 'ifc_profile': spec}}
      adjacency:   [(label_a, label_b, contact_length_mm), ...]

    The main column/beam component is identified by its IFC entity type
    (IfcColumn or IfcBeam) and labeled with the component name.
    """
    if not os.path.exists(filepath):
        print(f"  [IFC] file not found: {filepath}")
        return {}, []

    comp = os.path.basename(filepath).replace('.ifc', '')
    f = ifcopenshell.open(filepath)

    settings = ifcopenshell.geom.settings()
    settings.set(settings.USE_WORLD_COORDS, True)

    # Collect all plates with dimensions and bboxes
    plates = f.by_type('IfcPlate')
    plate_data = {}   # label -> {'thick', 'width', 'bom_len', 'ifc_profile', 'bbox'}
    plate_instances = []  # (label, bbox) for each instance (multi-instance plates)

    for p in plates:
        ref = get_prop(p, 'Reference')
        if not ref:
            continue
        height = get_prop(p, 'Height')  # plate width (Tekla naming)
        width = get_prop(p, 'Width')    # plate thickness
        length = get_prop(p, 'Length')  # plate length
        profile = get_prop(p, 'Profile') or ''

        # Trim profile string (e.g., "PL12x100" or "GUSSET_PL")
        bbox = get_plate_bbox(p, settings)

        if bbox:
            plate_instances.append((ref, bbox))

        if ref not in plate_data:
            # IFC naming: Height/Width/Length are in local coords.
            # Width dimension: use min(Height,Length) — the narrow edge for welds.
            # Length dimension: use max(Height,Length) — the long side.
            _h = height if height else 0
            _l = length if length else 0
            plate_data[ref] = {
                'thick': round(width, 1) if width else None,
                'width': round(min(_h, _l), 1) if (_h or _l) else None,
                'bom_len': round(max(_h, _l), 1) if (_h or _l) else None,
                'ifc_profile': str(profile),
                'ifc_name': str(p.Name) if hasattr(p, 'Name') else '',
            }

    # Also check for the main column/beam
    columns = f.by_type('IfcColumn')
    beams = f.by_type('IfcBeam')
    for member in columns + beams:
        ref = get_prop(member, 'Reference')
        if ref and ref == comp:
            height = get_prop(member, 'Height')
            width = get_prop(member, 'Width')
            length = get_prop(member, 'Length')
            profile = get_prop(member, 'Profile') or ''
            plate_data[ref] = {
                'thick': round(width, 1) if width else None,
                'width': round(height, 1) if height else None,
                'bom_len': round(length, 1) if length else None,
                'ifc_profile': str(profile),
                'ifc_name': str(member.Name) if hasattr(member, 'Name') else '',
            }
            bbox = get_plate_bbox(member, settings)
            if bbox:
                plate_instances.append((ref, bbox))
            break

    # Build adjacency by comparing all instance bboxes
    adjacency = []
    seen_pairs = set()
    n = len(plate_instances)
    for i in range(n):
        li, bbi = plate_instances[i]
        for j in range(i + 1, n):
            lj, bbj = plate_instances[j]
            if li == lj:
                continue  # same label = same plate type, different instances
            is_adj, wl = bboxes_adjacent(bbi, bbj)
            if is_adj and wl > 0:
                pair = tuple(sorted((li, lj)))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    adjacency.append((pair[0], pair[1], wl))

    return plate_data, adjacency


# For quick test
if __name__ == '__main__':
    import glob
    folder = r'C:\Users\15297\Desktop\hanf\ifc格式'
    for fpath in sorted(glob.glob(os.path.join(folder, '*.ifc'))):
        comp = os.path.basename(fpath).replace('.ifc', '')
        dims, adj = read_ifc(fpath)
        print(f"\n{comp}: {len(dims)} plates, {len(adj)} adjacency pairs")
        for lbl, d in dims.items():
            print(f"  {lbl:<8} H={d['width']} W={d['thick']} L={d['bom_len']}")
        for a, b, wl in adj:
            print(f"  ADJ: {a}/{b} contact={wl}mm")
