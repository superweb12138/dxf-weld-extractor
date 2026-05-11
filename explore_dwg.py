"""
Exploratory script: open BE018 DWG via AutoCAD COM and dump WeldMark block info.
"""
import win32com.client
import pythoncom
import os, sys, math

DWG = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.dwg"

pythoncom.CoInitialize()

print("Connecting to AutoCAD...")
try:
    acad = win32com.client.GetActiveObject("AutoCAD.Application")
    print("  (reused running instance)")
except:
    acad = win32com.client.Dispatch("AutoCAD.Application")
    print("  (started new instance)")

acad.Visible = True

print(f"Opening {os.path.basename(DWG)} ...")
doc = acad.Documents.Open(DWG)
mspace = doc.ModelSpace

print(f"Total entities in ModelSpace: {mspace.Count}")

# Collect stats
block_names = {}
layers = set()
text_samples = []
insert_attrs = []

for i in range(mspace.Count):
    try:
        ent = mspace.Item(i)
        etype = ent.EntityName
        layer = ent.Layer
        layers.add(layer)

        if etype == "AcDbBlockReference":
            bname = ent.Name
            block_names[bname] = block_names.get(bname, 0) + 1
            if bname.startswith("WeldMark") or "weld" in bname.lower() or "焊" in bname:
                # Get insertion point
                ip = ent.InsertionPoint
                # Get attributes if any
                attrs = {}
                try:
                    if ent.HasAttributes:
                        attr_refs = ent.GetAttributes()
                        for a in attr_refs:
                            attrs[a.TagString] = a.TextString
                except:
                    pass
                insert_attrs.append({
                    "block": bname,
                    "layer": layer,
                    "insert": (round(ip[0],2), round(ip[1],2)),
                    "attrs": attrs
                })
        elif etype in ("AcDbText", "AcDbMText"):
            try:
                txt = ent.TextString if etype == "AcDbText" else ent.Contents
                if txt.strip():
                    text_samples.append((layer, txt.strip()[:60]))
            except:
                pass
    except Exception as e:
        pass

print("\n=== Block names (top 30 by count) ===")
for k, v in sorted(block_names.items(), key=lambda x: -x[1])[:30]:
    print(f"  {k!r:40s} x{v}")

print(f"\n=== Layers ({len(layers)}) ===")
for l in sorted(layers)[:40]:
    print(f"  {l!r}")

print(f"\n=== WeldMark inserts ({len(insert_attrs)}) ===")
for w in insert_attrs[:30]:
    print(f"  {w}")

print(f"\n=== Text samples (first 30) ===")
for l, t in text_samples[:30]:
    print(f"  layer={l!r:25s}  text={t!r}")

doc.Close(False)
print("\nDone.")
