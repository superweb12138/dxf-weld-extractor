"""
Deep exploration: look into block definitions for WeldMark data.
"""
import win32com.client
import pythoncom
import os

DWG = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.dwg"

pythoncom.CoInitialize()

try:
    acad = win32com.client.GetActiveObject("AutoCAD.Application")
except:
    acad = win32com.client.Dispatch("AutoCAD.Application")
acad.Visible = True

doc = acad.Documents.Open(DWG)
blocks = doc.Blocks

print(f"Total block definitions: {blocks.Count}")

weld_blocks = []
part_blocks = []
all_block_names = []

for i in range(blocks.Count):
    try:
        blk = blocks.Item(i)
        bname = blk.Name
        all_block_names.append(bname)
        
        if bname.startswith("WeldMark"):
            weld_blocks.append(blk)
        elif bname.startswith("Part-"):
            part_blocks.append(blk)
    except Exception as e:
        print(f"  Error at block {i}: {e}")

print(f"\nWeldMark block definitions: {len(weld_blocks)}")
print(f"Part block definitions: {len(part_blocks)}")

# Inspect each WeldMark block definition
print("\n=== WeldMark Block Contents ===")
for blk in weld_blocks:
    print(f"\nBlock: {blk.Name!r}  (count={blk.Count})")
    for j in range(blk.Count):
        try:
            ent = blk.Item(j)
            etype = ent.EntityName
            print(f"  [{j}] {etype}  layer={ent.Layer!r}", end="")
            
            if etype == "AcDbText":
                print(f"  text={ent.TextString!r}  pos={tuple(round(x,2) for x in ent.InsertionPoint)}", end="")
            elif etype == "AcDbMText":
                print(f"  text={ent.Contents!r}", end="")
            elif etype == "AcDbLine":
                sp = tuple(round(x,2) for x in ent.StartPoint)
                ep = tuple(round(x,2) for x in ent.EndPoint)
                print(f"  start={sp}  end={ep}  len={ent.Length:.2f}", end="")
            elif etype == "AcDbPolyline" or etype == "AcDb2dPolyline":
                try:
                    print(f"  length={ent.Length:.2f}", end="")
                except:
                    pass
            elif etype == "AcDbBlockReference":
                print(f"  refblock={ent.Name!r}  insert={tuple(round(x,2) for x in ent.InsertionPoint)}", end="")
                # Check for attributes
                try:
                    if ent.HasAttributes:
                        attrs = ent.GetAttributes()
                        for a in attrs:
                            print(f"\n    ATTR tag={a.TagString!r}  val={a.TextString!r}", end="")
                except:
                    pass
            elif etype == "AcDbAttributeDefinition":
                print(f"  tag={ent.TagString!r}  prompt={ent.PromptString!r}  default={ent.TextString!r}", end="")
            print()
        except Exception as e:
            print(f"  [{j}] ERROR: {e}")

# Look at a Part block briefly
print("\n=== Sample Part Block Contents ===")
if part_blocks:
    blk = part_blocks[0]
    print(f"Block: {blk.Name!r}  (count={blk.Count})")
    for j in range(min(blk.Count, 30)):
        try:
            ent = blk.Item(j)
            etype = ent.EntityName
            print(f"  [{j}] {etype}  layer={ent.Layer!r}", end="")
            if etype == "AcDbText":
                print(f"  text={ent.TextString!r}", end="")
            elif etype == "AcDbBlockReference":
                print(f"  refblock={ent.Name!r}", end="")
                try:
                    if ent.HasAttributes:
                        attrs = ent.GetAttributes()
                        for a in attrs:
                            print(f"\n    ATTR tag={a.TagString!r}  val={a.TextString!r}", end="")
                except:
                    pass
            print()
        except Exception as e:
            print(f"  [{j}] ERROR: {e}")

doc.Close(False)
print("\nDone.")
