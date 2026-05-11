"""单文件测试：只处理 BE018，打印 LLM 完整响应"""
import importlib, pdf_weld_extractor as pw
importlib.reload(pw)

rows = pw.extract_welds_from_pdf(
    r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.pdf"
)
print("\n=== 最终结果 ===")
for i, r in enumerate(rows, 1):
    print("%2d. %-5s hf=%-4s len=%-7s %-6s/%-6s  %s" % (
        i, r.get("position",""), r.get("hf",""), r.get("length_mm",""),
        r.get("part1",""), r.get("part2",""), r.get("annotation","")))
