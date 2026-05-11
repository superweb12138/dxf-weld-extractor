"""查看 PDF 文本提取质量"""
import fitz, json

doc = fitz.open(r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.pdf")
page = doc[0]

# 方法1：直接 get_text
print("=== 原始文字（get_text） ===")
print(page.get_text()[:2000])

# 方法2：带坐标的 blocks
print("\n=== 文字块（带坐标）===")
blocks = page.get_text("dict")["blocks"]
texts = []
for b in blocks:
    for line in b.get("lines", []):
        for span in line.get("spans", []):
            t = span["text"].strip()
            if t:
                x0, y0, x1, y1 = span["bbox"]
                texts.append({"text": t, "x": round(x0,1), "y": round(y0,1), "size": round(span["size"],1)})

# 按 y 坐标排序
texts.sort(key=lambda s: (round(s["y"]/5)*5, s["x"]))
for t in texts:
    print(f"  [{t['x']:7.1f}, {t['y']:7.1f}] size={t['size']:4.1f}  {t['text']!r}")
