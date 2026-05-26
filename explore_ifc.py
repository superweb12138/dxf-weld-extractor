"""Explore IFC structure with ifcopenshell."""
import ifcopenshell
from collections import Counter

def explore(path, comp):
    f = ifcopenshell.open(path)
    print(f"\n{'='*60}\n{comp}  [{path}]")

    # Entity types
    types = Counter(e.is_a() for e in f)
    print("Entity types:")
    for t, n in sorted(types.items()):
        print(f"  {t}: {n}")

    # Plates
    plates = f.by_type('IfcPlate')
    print(f"\nTotal plates: {len(plates)}")
    for p in plates[:5]:
        ref = get_prop(p, 'Reference')
        height = get_prop(p, 'Height')
        width = get_prop(p, 'Width')
        length = get_prop(p, 'Length')
        print(f"  {p.Name:<12} ref={ref:<8} H={height} W={width} L={length}")

        # Check Geometry
        try:
            rep = p.Representation
            if rep:
                for r in rep.Representations:
                    items = r.Items
                    print(f"    Repr: {r.RepresentationIdentifier}, Items: {len(items)}")
                    if items:
                        item0 = items[0]
                        print(f"    Item0 type: {item0.is_a()}")
                        # Try getting bbox
                        if hasattr(item0, 'BoundingBox'):
                            bbox = item0.BoundingBox
                            if bbox:
                                print(f"    BBox: ({bbox[0]:.1f},{bbox[2]:.1f}) -> ({bbox[1]:.1f},{bbox[3]:.1f})")
        except Exception as e:
            print(f"    Geometry error: {e}")

    # Check if any welds exist
    welds = f.by_type('IfcFastener') or []
    print(f"\nWelds/Fasteners: {len(welds)}")

    # Check spatial containment
    contained = f.by_type('IfcRelContainedInSpatialStructure')
    print(f"\nSpatial containers: {len(contained)}")

def get_prop(entity, name):
    try:
        for rel in entity.IsDefinedBy:
            pset = rel.RelatingPropertyDefinition
            if hasattr(pset, 'HasProperties'):
                for prop in pset.HasProperties or []:
                    if prop.Name == name:
                        v = prop.NominalValue
                        if v:
                            return v.wrappedValue
    except:
        pass
    return None

import os
folder = r'C:\Users\15297\Desktop\hanf\ifc格式'
for fname in sorted(os.listdir(folder)):
    if fname.endswith('.ifc'):
        comp = fname.replace('.ifc', '')
        explore(os.path.join(folder, fname), comp)
