"""
Deep dive: look for part numbers, XData, and geometry in views.
"""
import win32com.client
import pythoncom
import os, math

DWG = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.dwg"

pythoncom.CoInitialize()
try:
    acad = win32com.client.GetActiveObject("AutoCAD.Application")
except:
    acad = win32com.client.Dispatch("AutoCAD.Application")
acad.Visible = True

doc = acad.Documents.Open(DWG)
blocks = doc.Blocks

# Find ALL text in ALL blocks
print("=== All text values in all blocks ===")
all_texts = []
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                if etype == "AcDbText":
                    txt = ent.TextString.strip()
                    if txt:
                        all_texts.append((bname, txt))
                elif etype == "AcDbMText":
                    txt = ent.Contents.strip()
                    if txt:
                        all_texts.append((bname, txt))
                elif etype == "AcDbAttributeDefinition":
                    all_texts.append((bname, f"ATTRDEF:{ent.TagString}={ent.TextString}"))
            except:
                pass
    except:
        pass

# Print unique texts
seen = set()
for bname, txt in all_texts:
    key = (bname[:50], txt[:50])
    if key not in seen:
        seen.add(key)
        print(f"  {bname[:50]:52s}  {txt!r}")

print("\n=== XData check on WeldMark entities ===")
# Check XData on WeldMark block instances in model space
mspace = doc.ModelSpace
for i in range(mspace.Count):
    try:
        ent = mspace.Item(i)
        if "WeldMark" in ent.EntityName or "WeldMark" in getattr(ent, 'Name', ''):
            print(f"\nEntity [{i}]: {ent.EntityName}  Name={getattr(ent,'Name','?')!r}")
            try:
                # Try to get XData
                xdata = ent.GetXData("", [], [])
                print(f"  XData (all apps): {xdata}")
            except Exception as xe:
                print(f"  XData error: {xe}")
    except Exception as e:
        pass

# Now look at view blocks and find WeldMark inserts inside them
print("\n=== View blocks containing WeldMark inserts ===")
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if bname.startswith("Unknown-") or bname.startswith("Part-") or bname.startswith("Bolt-") or bname.startswith("ReferenceModel-"):
            continue
        if bname.startswith("*") or bname.startswith("WeldMark"):
            continue
        # This is a view block
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                if ent.EntityName == "AcDbBlockReference" and "WeldMark" in ent.Name:
                    print(f"\nView block: {bname!r}")
                    print(f"  Contains WeldMark: {ent.Name!r}")
                    print(f"  Insert: {tuple(round(x,3) for x in ent.InsertionPoint)}")
                    # Get XData on this insert
                    try:
                        xdata = ent.GetXData("", [], [])
                        print(f"  XData: {xdata}")
                    except Exception as xe:
                        print(f"  XData error: {xe}")
                    # Also look at nearby entities (other inserts in same block)
            except:
                pass
    except:
        pass

print("\n=== Part block names and their attributes ===")
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if not bname.startswith("Part-"):
            continue
        # Check for any text that might be a part number
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                if etype == "AcDbText":
                    txt = ent.TextString.strip()
                    if txt and (txt.startswith("p") or txt.startswith("BE") or txt.isalnum()):
                        print(f"  {bname!r}: text={txt!r}")
            except:
                pass
    except:
        pass

doc.Close(False)
print("\nDone.")
