"""诊断2：Qwen3 thinking 模式处理"""
from openai import OpenAI

API_KEY         = "sk-URBynpfNYkkj3kewnxeA"
CUSTOM_BASE_URL = "http://172.18.162.10:8002/v1"
MODEL_NAME      = "Qwen/Qwen3.5-27B"
client = OpenAI(api_key=API_KEY, base_url=CUSTOM_BASE_URL)

def test(label, **extra):
    print(f"\n=== {label} ===")
    try:
        resp = client.chat.completions.create(
            model=MODEL_NAME,
            messages=[{"role": "user", "content": "请用一句话介绍焊缝"}],
            max_tokens=512,
            **extra,
        )
        c = resp.choices[0]
        msg = c.message
        print(f"  finish_reason     : {c.finish_reason}")
        print(f"  content           : {str(msg.content)[:300]!r}")
        # Qwen3 thinking 字段
        rc = getattr(msg, 'reasoning_content', None)
        print(f"  reasoning_content : {str(rc)[:200]!r}")
        # 原始字段
        raw = msg.model_dump() if hasattr(msg, 'model_dump') else vars(msg)
        for k, v in raw.items():
            if v and k not in ('content', 'role', 'reasoning_content'):
                print(f"  [{k}] = {str(v)[:100]}")
    except Exception as e:
        import traceback; traceback.print_exc()

# 方案1：默认（thinking=True，大 token）
test("默认（max_tokens=512）")

# 方案2：关闭 thinking（vLLM Qwen3 支持）
test("enable_thinking=False",
     extra_body={"enable_thinking": False})

# 方案3：通过 chat_template_kwargs 关闭
test("chat_template_kwargs",
     extra_body={"chat_template_kwargs": {"enable_thinking": False}})

# 方案4：大 token 看能否拿到 content
test("max_tokens=8192",
     max_tokens=8192)
