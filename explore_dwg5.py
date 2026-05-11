"""
Focus: extract WeldMark arrow tips and Part line geometries for view 1655.
Compare with expected weld lengths to understand the coordinate system.
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

# Get the drawing units
try:
    print("InsUnits:", doc.GetVariable("INSUNITS"))
    print("LUNITS:", doc.GetVariable("LUNITS"))
except:
    pass

def get_block_by_name(name):
    for i in range(blocks.Count):
        try:
            blk = blocks.Item(i)
            if blk.Name == name:
                return blk
        except:
            pass
    return None

# Examine WeldMark blocks completely
print("\n=== ALL WeldMark block details ===")
weld_data = {}  # block_name -> {size, arrow_tip, type}
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if not bname.startswith("WeldMark"):
            continue
        print(f"\nWeldMark: {bname!r}  count={blk.Count}")
        texts = []
        lines = []
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                if etype == "AcDbText":
                    txt = ent.TextString.strip()
                    pos = tuple(round(x,3) for x in ent.InsertionPoint)
                    texts.append((txt, pos))
                    print(f"  Text: {txt!r}  at {pos}")
                elif etype == "AcDbLine":
                    sp = tuple(round(x,3) for x in ent.StartPoint)
                    ep = tuple(round(x,3) for x in ent.EndPoint)
                    ln = ent.Length
                    lines.append((sp, ep, ln))
                    print(f"  Line: {sp} -> {ep}  len={ln:.3f}")
                elif etype == "AcDbHatch":
                    print(f"  Hatch (fill)")
                elif etype == "AcDbWipeout":
                    print(f"  Wipeout (mask)")
            except Exception as e:
                print(f"  [{j}] ERROR: {e}")
        # Store data
        weld_data[bname] = {"texts": texts, "lines": lines}
    except:
        pass

# Now look at Part blocks in view 1655 - get ALL line lengths
print("\n\n=== Part geometries in view 1655 ===")
print("(Lines with length > 10 units)")
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if not (bname.startswith("Part-") and "1655" in bname):
            continue
        long_lines = []
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                if etype == "AcDbLine":
                    sp = tuple(round(x,3) for x in ent.StartPoint)
                    ep = tuple(round(x,3) for x in ent.EndPoint)
                    ln = ent.Length
                    if ln > 10:
                        long_lines.append((sp, ep, ln))
            except:
                pass
        if long_lines:
            print(f"\n{bname!r}")
            for sp, ep, ln in sorted(long_lines, key=lambda x: -x[2])[:10]:
                print(f"  len={ln:8.3f}  {sp} -> {ep}")
    except:
        pass

# Also view 2046
print("\n\n=== Part geometries in view 2046 ===")
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if not (bname.startswith("Part-") and "2046" in bname):
            continue
        long_lines = []
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                if etype == "AcDbLine":
                    sp = tuple(round(x,3) for x in ent.StartPoint)
                    ep = tuple(round(x,3) for x in ent.EndPoint)
                    ln = ent.Length
                    if ln > 10:
                        long_lines.append((sp, ep, ln))
            except:
                pass
        if long_lines:
            print(f"\n{bname!r}")
            for sp, ep, ln in sorted(long_lines, key=lambda x: -x[2])[:10]:
                print(f"  len={ln:8.3f}  {sp} -> {ep}")
    except:
        pass

# Also view 2162
print("\n\n=== Part geometries in view 2162 ===")
for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        if not (bname.startswith("Part-") and "2162" in bname):
            continue
        long_lines = []
        for j in range(blk.Count):
            try:
                ent = blk.Item(j)
                etype = ent.EntityName
                if etype == "AcDbLine":
                    sp = tuple(round(x,3) for x in ent.StartPoint)
                    ep = tuple(round(x,3) for x in ent.EndPoint)
                    ln = ent.Length
                    if ln > 10:
                        long_lines.append((sp, ep, ln))
            except:
                pass
        if long_lines:
            print(f"\n{bname!r}")
            for sp, ep, ln in sorted(long_lines, key=lambda x: -x[2])[:10]:
                print(f"  len={ln:8.3f}  {sp} -> {ep}")
    except:
        pass

doc.Close(False)
print("\nDone.")
