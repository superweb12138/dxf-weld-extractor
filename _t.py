# -*- coding: utf-8 -*-
import weld_extractor as we
r,s = we.extract_welds(r"C:\Users\15297\Desktop\hanf\361-RC3210-S-01-CO007_00.dxf")
for x in r:
    if "p101" in {x.get("part1",""),x.get("part2","")} and x.get("view_id")=="2424":
        print(f'{x["part1"]}/{x["part2"]} l={x["length_mm"]:.0f} ({x["dxf_pos"][0]:.1f},{x["dxf_pos"][1]:.1f})')
print()
for x in r:
    if "p126" in {x.get("part1",""),x.get("part2","")} and x.get("view_id")=="2475":
        print(f'{x["part1"]}/{x["part2"]} l={x["length_mm"]:.0f} ({x["dxf_pos"][0]:.1f},{x["dxf_pos"][1]:.1f})')
