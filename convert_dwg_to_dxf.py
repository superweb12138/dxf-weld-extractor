"""
Batch convert all DWG files in the folder to DXF using AutoCAD COM.
Run once before running the weld extractor.
"""
import win32com.client
import pythoncom
import os
import glob

FOLDER = r"c:\Users\hp\OneDrive\Desktop\dxf\hanf"
DWG_FILES = glob.glob(os.path.join(FOLDER, "*.dwg"))

pythoncom.CoInitialize()
try:
    acad = win32com.client.GetActiveObject("AutoCAD.Application")
except:
    acad = win32com.client.Dispatch("AutoCAD.Application")
acad.Visible = False

for dwg_path in DWG_FILES:
    dxf_path = dwg_path.replace(".dwg", ".dxf")
    if os.path.exists(dxf_path):
        print(f"SKIP (exists): {os.path.basename(dxf_path)}")
        continue
    print(f"Converting: {os.path.basename(dwg_path)} ...", end=" ", flush=True)
    try:
        doc = acad.Documents.Open(dwg_path)
        # SaveAs with FileType=1 => DXF R12/LT2  (61 = R2010 DXF, 1 = R12)
        # Use 61 for modern DXF (preserves block structure best)
        doc.SaveAs(dxf_path, 61)
        doc.Close(False)
        print("OK")
    except Exception as e:
        print(f"ERROR: {e}")

print("\nAll done.")
