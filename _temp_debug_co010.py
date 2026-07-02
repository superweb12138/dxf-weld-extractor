import sys, os, re
sys.path.insert(0, ".")
import importlib, importlib.util as iu
spec = iu.spec_from_file_location("we", "weld_extractor.py")
we = iu.module_from_spec(spec)
spec.loader.exec_module(we)

orig_parse = we.parse_bom
def debug_parse(doc, comp):
    dims, cdims = orig_parse(doc, comp)
    print("BOM dims for %s:" % comp)
    for k, v in sorted(dims.items()):
        print("  %s: %s" % (k, v))
    print("Comp dims: %s" % cdims)
    return dims, cdims
we.parse_bom = debug_parse

results, skipped = we.extract_welds(r"C:\Users\15297\Desktop\hanf\361-RC3210-S-01-CO010_02.dxf")
print("\n=== CO010 entries ===")
for r in results:
    if r["component"]=="CO010":
        print("  %-8s hf=%-5s len=%-8s %-12s %-12s ann=%s" % (r["position"], r["hf"], r["length_mm"], r["part1"], r["part2"], r["annotation"]))
