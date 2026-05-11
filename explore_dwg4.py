"""
Explore view block hierarchy and find geometry for weld length calculation.
Also extract weld sizes and part numbers per view/weld.
"""
import win32com.client
import pythoncom
import os, math, re
from collections import defaultdict

DWG = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.dwg"

pythoncom.CoInitialize()
try:
    acad = win32com.client.GetActiveObject("AutoCAD.Application")
except:
    acad = win32com.client.Dispatch("AutoCAD.Application")
acad.Visible = True

doc = acad.Documents.Open(DWG)
blocks = doc.Blocks

# Build a map: block_name -> list of (entity_type, data)
print("=== ModelSpace entity tree ===")
mspace = doc.ModelSpace

# What's directly in ModelSpace?
for i in range(mspace.Count):
    try:
        ent = mspace.Item(i)
        etype = ent.EntityName
        name = getattr(ent, 'Name', '?')
        ip = tuple(round(x,3) for x in ent.InsertionPoint) if hasattr(ent, 'InsertionPoint') else '?'
        print(f"  [{i:3d}] {etype:30s}  Name={name!r:50s}  Insert={ip}")
    except Exception as e:
        print(f"  [{i:3d}] ERROR: {e}")

# Now look inside each "Unknown" view block
print("\n=== View (Unknown) block contents ===")
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if not bname.startswith("Unknown-"):
            continue
        print(f"\nView block: {bname!r}  count={blk.Count}")
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                name = getattr(ent, 'Name', '?')
                if hasattr(ent, 'InsertionPoint'):
                    ip = tuple(round(x,3) for x in ent.InsertionPoint)
                else:
                    ip = None
                print(f"  [{j:3d}] {etype:30s}  Name={name!r:50s}", end="")
                if ip:
                    print(f"  Insert={ip}", end="")
                print()
            except Exception as e:
                print(f"  [{j:3d}] ERROR: {e}")
    except Exception as e:
        pass

# Now examine Part blocks that are inside view 1655 (by checking block names with - 1655)
print("\n=== Part blocks for view 1655 - line lengths ===")
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if not (bname.startswith("Part-") and bname.endswith("- 1655")):
            continue
        print(f"\nPart block: {bname!r}")
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                if etype == "AcDbLine":
                    sp = tuple(round(x,2) for x in ent.StartPoint)
                    ep = tuple(round(x,2) for x in ent.EndPoint)
                    length = ent.Length
                    print(f"  Line: {sp} -> {ep}  len={length:.2f}")
                elif etype in ("AcDbPolyline", "AcDb2dPolyline", "AcDbLWPolyline"):
                    try:
                        length = ent.Length
                        print(f"  Polyline: len={length:.2f}")
                    except:
                        pass
            except:
                pass
    except:
        pass

doc.Close(False)
print("\nDone.")
