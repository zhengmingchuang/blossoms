"""LLM 通道健康检查 —— 换模型 / 换供应商后跑一遍，确认所有业务路径可用。

覆盖本项目实际用到的 6 条调用路径：
  1. 非流式纯文本
  2. 非流式 + tools（chat.py 非流式 agent 循环）
  3. 非流式多轮 tool_use -> tool_result（思考模式未关时此处会 400）
  4. 流式纯文本 + 流式 tools（chat.py SSE 入口）
  5. 流式多轮 tool_result
  6. JSON 结构化抽取（table_fill / extract_fields 核心形态）
  7. 图片理解（chat 上传图片路径，需要模型支持视觉）

配置解析严格复刻 main.py::load_saved_config —— 数据库 system_config 会覆盖
.env，这是最容易踩的坑：只改 .env 而数据库存着旧值，运行时仍然走旧模型。

用法:
    python scripts/verify_llm.py            # 全部检查
    python scripts/verify_llm.py --no-image # 跳过图片检查（模型不支持视觉时）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

import config as cfg  # noqa: E402
from services import llm_service  # noqa: E402

TOOLS = [{
    "name": "get_weather",
    "description": "查询指定城市的天气",
    "input_schema": {
        "type": "object",
        "properties": {"city": {"type": "string", "description": "城市名"}},
        "required": ["city"],
    },
}]
SYS_TOOL = "你可以使用工具回答问题。"


def apply_saved_config() -> dict:
    """与 main.py::load_saved_config 一致的 DB -> cfg 覆盖逻辑。"""
    mapping = {
        "llm_api_base": "LLM_API_BASE",
        "llm_api_key": "LLM_API_KEY",
        "llm_model": "LLM_MODEL",
        "llm_api_format": "LLM_API_FORMAT",
    }
    applied = {}
    try:
        c = sqlite3.connect(BACKEND / "app.db")
        for db_key, attr in mapping.items():
            row = c.execute(
                "select config_value from system_config where config_key=?", (db_key,)
            ).fetchone()
            if row and row[0]:
                setattr(cfg, attr, row[0])
                applied[attr] = row[0]
        c.close()
    except Exception as e:
        print(f"  [警告] 读取数据库配置失败，仅使用 .env: {e}")
    return applied


def mask(v: str) -> str:
    if not v:
        return "(空)"
    return f"{v[:7]}...{v[-4:]}" if len(v) > 12 else "***"


async def collect_stream(messages, system="", tools=None):
    text, done = "", None
    async for ev in llm_service.call_llm_stream(
        messages, system=system, temperature=0.3, max_tokens=512, tools=tools
    ):
        if ev["type"] == "text":
            text += ev["text"]
        elif ev["type"] == "done":
            done = ev
    return text, done or {}


async def check_text() -> tuple[bool, str]:
    r = await llm_service.call_llm(
        messages=[{"role": "user", "content": "只回复两个字：正常"}],
        temperature=0.0, max_tokens=64,
    )
    t = (r.get("text") or "").strip()
    return bool(t), f"返回 {t[:40]!r}"


async def check_tools() -> tuple[bool, str, dict | None]:
    r = await llm_service.call_llm(
        messages=[{"role": "user", "content": "帮我查一下杭州的天气"}],
        system=SYS_TOOL, temperature=0.0, max_tokens=256, tools=TOOLS,
    )
    tcs = r.get("tool_calls") or []
    ok = bool(tcs) and bool((tcs[0].get("input") or {}).get("city"))
    detail = f"{tcs[0]['name']}({tcs[0]['input']})" if tcs else "未产生 tool_use"
    return ok, detail, (tcs[0] if tcs else None)


async def check_multi_round(tc: dict) -> tuple[bool, str]:
    history = [
        {"role": "user", "content": "帮我查一下杭州的天气"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc["id"], "content": "杭州：晴，26℃"}
        ]},
    ]
    r = await llm_service.call_llm(
        messages=history, system=SYS_TOOL, temperature=0.0,
        max_tokens=256, tools=TOOLS,
    )
    t = (r.get("text") or "").strip()
    return bool(t), f"第二轮返回 {t[:50]!r}"


async def check_stream_text() -> tuple[bool, str]:
    t, done = await collect_stream([{"role": "user", "content": "用一句话说明什么是GDP。"}])
    return bool(t.strip()), f"长度 {len(t)}，stop={done.get('stop_reason')}"


async def check_stream_tools() -> tuple[bool, str, dict | None]:
    t, done = await collect_stream(
        [{"role": "user", "content": "帮我查一下杭州的天气"}],
        system=SYS_TOOL, tools=TOOLS,
    )
    tcs = done.get("tool_calls") or []
    ok = bool(tcs) and bool((tcs[0].get("input") or {}).get("city"))
    detail = f"{tcs[0]['name']}({tcs[0]['input']})" if tcs else "未产生 tool_use"
    return ok, detail, (tcs[0] if tcs else None)


async def check_stream_multi(tc: dict) -> tuple[bool, str]:
    history = [
        {"role": "user", "content": "帮我查一下杭州的天气"},
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": tc["id"], "name": tc["name"], "input": tc["input"]}
        ]},
        {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": tc["id"], "content": "杭州：晴，26℃"}
        ]},
    ]
    t, done = await collect_stream(history, system=SYS_TOOL, tools=TOOLS)
    return bool(t.strip()), f"第二轮返回 {t[:50]!r}"


async def check_json() -> tuple[bool, str]:
    src = (
        "2025 年，杭州市 GDP 总量达到 21860.32 亿元，同比增长 5.2%，常住人口 1262.4 万人。"
        "同期宁波市 GDP 为 16452.80 亿元，同比增长 4.8%，常住人口 969.7 万人。"
    )
    cols = ["城市", "GDP(亿元)", "增长率(%)", "常住人口(万人)"]
    sys_prompt = (
        "你是数据抽取助手。从用户提供的文本中抽取数据，严格输出 JSON 数组，"
        f"每个对象的键必须且只能是这些列名（逐字一致）：{json.dumps(cols, ensure_ascii=False)}。"
        "只输出 JSON，不要任何解释、不要 markdown 代码块。"
    )
    r = await llm_service.call_llm(
        messages=[{"role": "user", "content": src}],
        system=sys_prompt, temperature=0.0, max_tokens=1024,
    )
    raw = r.get("text", "")
    try:
        cleaned = re.sub(r"^```(?:json)?|```$", "", raw.strip(), flags=re.M).strip()
        data = json.loads(cleaned)
    except Exception:
        return False, f"JSON 解析失败: {raw[:80]!r}"
    if not isinstance(data, list) or len(data) != 2:
        return False, f"行数异常: {len(data) if isinstance(data, list) else type(data)}"
    keys_ok = all(list(o.keys()) == cols for o in data)
    vals_ok = ("21860" in str(data[0].get(cols[1], "")).replace(",", "")
               and "16452" in str(data[1].get(cols[1], "")).replace(",", ""))
    return (keys_ok and vals_ok), f"{len(data)} 行，键名一致={keys_ok}，数值正确={vals_ok}"


async def check_image() -> tuple[bool, str]:
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return False, "缺少 Pillow，跳过（pip install pillow）"
    img_path = ROOT / ".workbuddy-ai" / "_llm_check.png"
    img = Image.new("RGB", (760, 320), "white")
    d = ImageDraw.Draw(img)
    font = None
    for cand in ("C:/Windows/Fonts/msyh.ttc", "C:/Windows/Fonts/simhei.ttf"):
        if Path(cand).exists():
            try:
                font = ImageFont.truetype(cand, 34)
                break
            except Exception:
                pass
    font = font or ImageFont.load_default()
    for i, ln in enumerate(["增值税专用发票", "购买方：杭州云图科技有限公司",
                            "价税合计：￥12,345.67", "开票日期：2026年03月15日"]):
        d.text((40, 30 + i * 62), ln, fill="black", font=font)
    img.save(img_path)
    try:
        blocks = llm_service.build_image_content_blocks(
            [str(img_path)], "读出图中「价税合计」的金额和「开票日期」，用一行简短中文回答。"
        )
        r = await llm_service.call_llm(
            messages=[{"role": "user", "content": blocks}],
            temperature=0.0, max_tokens=256,
        )
        ans = (r.get("text") or "").strip()
        ok = "12345" in ans.replace(",", "").replace("￥", "")
        return ok, f"回答 {ans[:60]!r}"
    finally:
        if img_path.exists():
            img_path.unlink()


async def main() -> int:
    ap = argparse.ArgumentParser(description="LLM 通道健康检查")
    ap.add_argument("--no-image", action="store_true", help="跳过图片理解检查")
    args = ap.parse_args()

    applied = apply_saved_config()
    print("=" * 62)
    print("生效配置（数据库覆盖 .env 之后）")
    print("=" * 62)
    if applied:
        for k, v in applied.items():
            print(f"  {k:16} = {mask(v) if 'KEY' in k else v}")
    print(f"  {'LLM_DISABLE_THINKING':16} = {cfg.LLM_DISABLE_THINKING}")
    fmt = cfg.LLM_API_FORMAT
    endpoint = (f"{cfg.LLM_API_BASE.rstrip('/')}/v1/messages" if fmt == "anthropic"
                else f"{cfg.LLM_API_BASE.rstrip('/')}/chat/completions")
    print(f"  {'请求地址':16} = {endpoint}")
    if not cfg.LLM_API_KEY:
        print("\n[终止] API Key 为空，请填写 backend/.env 的 LLM_API_KEY")
        return 1

    results: list[tuple[str, bool, str]] = []

    async def run(name, coro):
        print(f"\n── {name} " + "─" * max(0, 50 - len(name)))
        try:
            out = await coro
            ok, detail = out[0], out[1]
            extra = out[2] if len(out) > 2 else None
            print(f"  {'✓' if ok else '✗'} {detail}")
            results.append((name, ok, detail))
            return extra
        except Exception as e:
            msg = f"{type(e).__name__}: {str(e)[:200]}"
            print(f"  ✗ {msg}")
            results.append((name, False, msg))
            return None

    await run("1. 非流式纯文本", check_text())
    tc1 = await run("2. 非流式 tools", check_tools())
    if tc1:
        await run("3. 非流式多轮 tool_result", check_multi_round(tc1))
    else:
        results.append(("3. 非流式多轮 tool_result", False, "跳过（上游未产生 tool_use）"))

    await run("4. 流式纯文本", check_stream_text())
    tc2 = await run("5. 流式 tools", check_stream_tools())
    if tc2:
        await run("6. 流式多轮 tool_result", check_stream_multi(tc2))
    else:
        results.append(("6. 流式多轮 tool_result", False, "跳过（上游未产生 tool_use）"))

    await run("7. JSON 结构化抽取", check_json())
    if not args.no_image:
        await run("8. 图片理解", check_image())

    print("\n" + "=" * 62)
    passed = sum(1 for _, ok, _ in results if ok)
    for name, ok, _ in results:
        print(f"  {'✓' if ok else '✗'} {name}")
    print("=" * 62)
    print(f"通过 {passed}/{len(results)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
