"""诊断：测试 API 连通性 + 模型能力"""
from openai import OpenAI
import json

API_KEY         = "sk-URBynpfNYkkj3kewnxeA"
CUSTOM_BASE_URL = "http://172.18.162.10:8002/v1"
MODEL_NAME      = "Qwen/Qwen3.5-27B"

client = OpenAI(api_key=API_KEY, base_url=CUSTOM_BASE_URL)

# 1. 列出可用模型
print("=== 可用模型 ===")
try:
    models = client.models.list()
    for m in models.data:
        print(f"  {m.id}")
except Exception as e:
    print(f"  list_models 失败: {e}")

# 2. 纯文本测试
print("\n=== 纯文本测试 ===")
try:
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{"role": "user", "content": "请用一句话回复：你好"}],
        max_tokens=64,
        temperature=0.1,
    )
    choice = resp.choices[0]
    print(f"  finish_reason : {choice.finish_reason}")
    print(f"  content       : {choice.message.content!r}")
    if hasattr(choice.message, 'reasoning_content'):
        print(f"  reasoning     : {str(choice.message.reasoning_content)[:200]}")
except Exception as e:
    import traceback; traceback.print_exc()

# 3. 视觉能力测试（发一个 1x1 白色像素）
print("\n=== 视觉能力测试 ===")
try:
    import base64
    # 最小 JPEG（1x1 白色）
    TINY_JPEG = (
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8U"
        "HRofHh0aHBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgN"
        "DRgyIRwhMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIy"
        "MjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QAFAABAAAAAAAAAAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/xAAUAQEAAAAAAAAAAAAAAAAAAAAA/8QAFBEBAAAAAAAAAAAAAAAAAAAAAP/aAAwDAQACEQMRAD8AJQAB/9k="
    )
    resp = client.chat.completions.create(
        model=MODEL_NAME,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{TINY_JPEG}"}},
                {"type": "text", "text": "这张图片里有什么？"}
            ]
        }],
        max_tokens=64,
        temperature=0.1,
    )
    choice = resp.choices[0]
    print(f"  finish_reason : {choice.finish_reason}")
    print(f"  content       : {choice.message.content!r}")
except Exception as e:
    import traceback; traceback.print_exc()
