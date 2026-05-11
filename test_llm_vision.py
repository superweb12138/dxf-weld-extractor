"""测试 Qwen3.5-27B + chat_template_kwargs 能否处理图像"""
import base64, fitz
from openai import OpenAI

API_KEY         = "sk-URBynpfNYkkj3kewnxeA"
CUSTOM_BASE_URL = "http://172.18.162.10:8002/v1"
MODEL_NAME      = "Qwen/Qwen3.5-27B"
client = OpenAI(api_key=API_KEY, base_url=CUSTOM_BASE_URL)

EXTRA = {"chat_template_kwargs": {"enable_thinking": False}}

# 渲染 BE018 第一页
doc = fitz.open(r"c:\Users\hp\OneDrive\Desktop\dxf\hanf\361-RC3210-S-01-BE018_00.pdf")
mat = fitz.Matrix(150/72, 150/72)
pix = doc[0].get_pixmap(matrix=mat, colorspace=fitz.csRGB)
b64 = base64.b64encode(pix.tobytes("jpeg")).decode()
print(f"图像大小: {pix.width}x{pix.height}，base64长度: {len(b64)}")

# 先发纯文本确认工作
print("\n--- 纯文本测试 ---")
r = client.chat.completions.create(
    model=MODEL_NAME,
    messages=[{"role":"user","content":"请回复：OK"}],
    max_tokens=16,
    extra_body=EXTRA,
)
print(f"content={r.choices[0].message.content!r}")

# 再发图像
print("\n--- 图像+文本测试 ---")
r2 = client.chat.completions.create(
    model=MODEL_NAME,
    messages=[{
        "role": "user",
        "content": [
            {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}","detail":"low"}},
            {"type":"text","text":"这张图里有哪些文字标注（列出前10个）？"}
        ]
    }],
    max_tokens=512,
    extra_body=EXTRA,
)
c2 = r2.choices[0]
print(f"finish_reason : {c2.finish_reason}")
print(f"content       : {c2.message.content!r}")
