import json
import re
import httpx
from typing import List, Dict, Any, Optional
import config as cfg

ANTHROPIC_VERSION = "2023-06-01"

# 全局共享 httpx 客户端，复用连接池，避免每次调用都重新建立 TCP/TLS 连接
_shared_client: Optional[httpx.AsyncClient] = None


def _get_shared_client() -> httpx.AsyncClient:
    global _shared_client
    if _shared_client is None or _shared_client.is_closed:
        _shared_client = httpx.AsyncClient(
            timeout=180,
            proxy=None,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )
    return _shared_client


async def call_llm(
    messages: List[Dict[str, Any]],
    system: str = "",
    temperature: float = 0.1,
    max_tokens: int = 4096,
    tools: Optional[List[Dict]] = None,
) -> Dict[str, Any]:
    if cfg.LLM_API_FORMAT == "anthropic":
        return await _call_anthropic(messages, system, temperature, max_tokens, tools)
    return await _call_openai(messages, system, temperature, max_tokens, tools)


async def call_llm_text(messages: List[Dict], system: str = "", temperature: float = 0.1, max_tokens: int = 4096) -> str:
    r = await call_llm(messages, system=system, temperature=temperature, max_tokens=max_tokens)
    return r["text"]


# =====================  流式调用（SSE）  =====================

class _ThinkFilter:
    """过滤 <think>...</think> 思维链。

    部分模型（MiniMax 等）会在正文里夹带思维链。流式场景下标签可能被切分到
    多个 delta 中，因此需要保留可能是半个标签的尾巴，等下一段拼上再判断。
    """

    _OPEN = "<think>"
    _CLOSE = "</think>"

    def __init__(self) -> None:
        self._buf = ""
        self._in_think = False

    def feed(self, delta: str) -> str:
        if not delta:
            return ""
        self._buf += delta
        out: List[str] = []
        while True:
            if self._in_think:
                idx = self._buf.find(self._CLOSE)
                if idx < 0:
                    # 思维链异常长且没有闭合标签时，丢弃并复位，避免无限累积
                    if len(self._buf) > 4000:
                        self._buf = ""
                        self._in_think = False
                    break
                self._buf = self._buf[idx + len(self._CLOSE):]
                self._in_think = False
            else:
                idx = self._buf.find(self._OPEN)
                if idx < 0:
                    keep = 0
                    for k in range(1, len(self._OPEN)):
                        if self._buf.endswith(self._OPEN[:k]):
                            keep = k
                    if keep:
                        out.append(self._buf[:-keep])
                        self._buf = self._buf[-keep:]
                    else:
                        out.append(self._buf)
                        self._buf = ""
                    break
                out.append(self._buf[:idx])
                self._buf = self._buf[idx + len(self._OPEN):]
                self._in_think = True
        return "".join(out)

    def flush(self) -> str:
        if self._in_think:
            self._buf = ""
            return ""
        rest, self._buf = self._buf, ""
        return rest


def _iter_sse_payloads(line: str) -> Optional[str]:
    """从一行 SSE 文本中取出 data 字段；非数据行返回 None。"""
    if not line or not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    return payload or None


async def call_llm_stream(
    messages: List[Dict[str, Any]],
    system: str = "",
    temperature: float = 0.1,
    max_tokens: int = 4096,
    tools: Optional[List[Dict]] = None,
):
    """流式调用大模型。

    逐个 yield 事件字典：
      {"type": "text", "text": "增量文本"}
      {"type": "done", "text": "完整文本", "tool_calls": [...], "stop_reason": "..."}

    仅在尚未产出任何文本前允许重试，避免重复输出已推送的内容。
    """
    impl = _stream_anthropic if cfg.LLM_API_FORMAT == "anthropic" else _stream_openai
    emitted = False
    last_err: Optional[Exception] = None

    for attempt in range(3):
        try:
            async for ev in impl(messages, system, temperature, max_tokens, tools):
                if ev["type"] == "text":
                    emitted = True
                yield ev
            return
        except Exception as e:
            last_err = e
            if emitted or attempt == 2:
                raise
            import asyncio
            await asyncio.sleep(2 * (attempt + 1))
    if last_err:
        raise last_err


async def _stream_anthropic(messages, system, temperature, max_tokens, tools):
    url = f"{cfg.LLM_API_BASE.rstrip('/')}/v1/messages"
    headers = {"Content-Type": "application/json", "x-api-key": cfg.LLM_API_KEY, "anthropic-version": ANTHROPIC_VERSION}
    clean = _fix_messages(messages)
    body: Dict[str, Any] = {"model": cfg.LLM_MODEL, "max_tokens": max_tokens, "messages": clean, "stream": True}
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
    if cfg.LLM_DISABLE_THINKING:
        body["thinking"] = {"type": "disabled"}

    think = _ThinkFilter()
    text_parts: List[str] = []
    blocks: Dict[int, Dict[str, Any]] = {}
    stop_reason = ""

    c = _get_shared_client()
    async with c.stream("POST", url, json=body, headers=headers) as resp:
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode("utf-8", "ignore")[:500]
            raise RuntimeError(f"LLM 流式请求失败 {resp.status_code}: {detail}")
        async for line in resp.aiter_lines():
            payload = _iter_sse_payloads(line)
            if not payload or payload == "[DONE]":
                continue
            try:
                ev = json.loads(payload)
            except Exception:
                continue
            et = ev.get("type")
            if et == "content_block_start":
                idx = ev.get("index", 0)
                blk = ev.get("content_block") or {}
                if blk.get("type") == "tool_use":
                    blocks[idx] = {"id": blk.get("id", ""), "name": blk.get("name", ""), "json": ""}
            elif et == "content_block_delta":
                idx = ev.get("index", 0)
                delta = ev.get("delta") or {}
                dt = delta.get("type")
                if dt == "text_delta":
                    out = think.feed(delta.get("text", ""))
                    if out:
                        text_parts.append(out)
                        yield {"type": "text", "text": out}
                elif dt == "input_json_delta" and idx in blocks:
                    blocks[idx]["json"] += delta.get("partial_json", "")
            elif et == "message_delta":
                stop_reason = (ev.get("delta") or {}).get("stop_reason") or stop_reason
            elif et == "error":
                raise RuntimeError(f"LLM 流式返回错误: {json.dumps(ev.get('error', ev), ensure_ascii=False)[:300]}")
            elif et == "message_stop":
                break

    tail = think.flush()
    if tail:
        text_parts.append(tail)
        yield {"type": "text", "text": tail}

    tool_calls = []
    for idx in sorted(blocks):
        b = blocks[idx]
        try:
            inp = json.loads(b["json"]) if b["json"].strip() else {}
        except Exception:
            inp = {}
        tool_calls.append({"id": b["id"], "name": b["name"], "input": inp})

    yield {
        "type": "done",
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "stop_reason": "tool_use" if tool_calls else (stop_reason or "end_turn"),
    }


async def _stream_openai(messages, system, temperature, max_tokens, tools=None):
    url = f"{cfg.LLM_API_BASE.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {cfg.LLM_API_KEY}"

    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content", "")
        role = m.get("role", "user")
        if isinstance(content, list):
            if role == "assistant":
                text_parts_ = [b["text"] for b in content if b.get("type") == "text"]
                oai_tool_calls = [
                    {
                        "id": b["id"], "type": "function",
                        "function": {"name": b["name"], "arguments": json.dumps(b.get("input", {}), ensure_ascii=False)},
                    }
                    for b in content if b.get("type") == "tool_use"
                ]
                msg = {"role": "assistant", "content": "\n".join(text_parts_) or None}
                if oai_tool_calls:
                    msg["tool_calls"] = oai_tool_calls
                msgs.append(msg)
            else:
                oai_parts, has_tool_result = [], False
                for b in content:
                    if b.get("type") == "tool_result":
                        has_tool_result = True
                        msgs.append({"role": "tool", "tool_call_id": b["tool_use_id"], "content": b.get("content", "")})
                    elif b.get("type") == "image":
                        src = b.get("source", {})
                        oai_parts.append({"type": "image_url", "image_url": {"url": f"data:{src.get('media_type', 'image/png')};base64,{src.get('data', '')}"}})
                    elif b.get("type") == "text":
                        oai_parts.append({"type": "text", "text": b["text"]})
                if oai_parts:
                    msgs.append({"role": "user", "content": oai_parts})
        else:
            msgs.append({"role": role, "content": content})

    body: Dict[str, Any] = {
        "model": cfg.LLM_MODEL, "messages": msgs, "temperature": temperature,
        "max_tokens": max_tokens, "stream": True,
    }
    if tools:
        body["tools"] = [
            {"type": "function", "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})}}
            for t in tools
        ]
    if cfg.LLM_DISABLE_THINKING:
        body["thinking"] = {"type": "disabled"}

    think = _ThinkFilter()
    text_parts: List[str] = []
    tool_buf: Dict[int, Dict[str, str]] = {}
    stop_reason = ""

    c = _get_shared_client()
    async with c.stream("POST", url, json=body, headers=headers) as resp:
        if resp.status_code >= 400:
            detail = (await resp.aread()).decode("utf-8", "ignore")[:500]
            raise RuntimeError(f"LLM 流式请求失败 {resp.status_code}: {detail}")
        async for line in resp.aiter_lines():
            payload = _iter_sse_payloads(line)
            if not payload or payload == "[DONE]":
                continue
            try:
                obj = json.loads(payload)
            except Exception:
                continue
            choices = obj.get("choices") or []
            if not choices:
                continue
            choice = choices[0]
            delta = choice.get("delta") or {}

            content = delta.get("content")
            if content:
                out = think.feed(content)
                if out:
                    text_parts.append(out)
                    yield {"type": "text", "text": out}

            for tc in (delta.get("tool_calls") or []):
                idx = tc.get("index", 0)
                slot = tool_buf.setdefault(idx, {"id": "", "name": "", "args": ""})
                if tc.get("id"):
                    slot["id"] = tc["id"]
                fn = tc.get("function") or {}
                if fn.get("name") and not slot["name"]:
                    slot["name"] = fn["name"]
                if fn.get("arguments"):
                    slot["args"] += fn["arguments"]

            fr = choice.get("finish_reason")
            if fr:
                stop_reason = "tool_use" if fr == "tool_calls" else fr

    tail = think.flush()
    if tail:
        text_parts.append(tail)
        yield {"type": "text", "text": tail}

    tool_calls = []
    for idx in sorted(tool_buf):
        slot = tool_buf[idx]
        try:
            inp = json.loads(slot["args"]) if slot["args"].strip() else {}
        except Exception:
            inp = {}
        tool_calls.append({"id": slot["id"], "name": slot["name"], "input": inp})

    yield {
        "type": "done",
        "text": "".join(text_parts),
        "tool_calls": tool_calls,
        "stop_reason": "tool_use" if tool_calls else (stop_reason or "end_turn"),
    }



async def _call_anthropic(messages, system, temperature, max_tokens, tools):
    import logging
    _logger = logging.getLogger("llm_service")
    url = f"{cfg.LLM_API_BASE.rstrip('/')}/v1/messages"
    headers = {"Content-Type": "application/json", "x-api-key": cfg.LLM_API_KEY, "anthropic-version": ANTHROPIC_VERSION}
    clean = _fix_messages(messages)
    has_image = any(
        isinstance(m.get("content"), list) and any(b.get("type") == "image" for b in m["content"])
        for m in clean
    )
    if has_image:
        _logger.info("Sending multimodal request with image(s) to LLM")
    body: Dict[str, Any] = {"model": cfg.LLM_MODEL, "max_tokens": max_tokens, "messages": clean}
    if system:
        body["system"] = system
    if temperature is not None:
        body["temperature"] = temperature
    if tools:
        body["tools"] = tools
    if cfg.LLM_DISABLE_THINKING:
        # DeepSeek Anthropic 兼容层支持 thinking.type=disabled；
        # 关闭后 temperature 恢复生效，且 tool 多轮无需回传 reasoning_content。
        body["thinking"] = {"type": "disabled"}
    
    last_err = None
    for attempt in range(3):
        try:
            c = _get_shared_client()
            resp = await c.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            texts, tool_calls = [], []
            for bl in data.get("content", []):
                if bl.get("type") == "text":
                    texts.append(bl["text"])
                elif bl.get("type") == "tool_use":
                    tool_calls.append({"id": bl["id"], "name": bl["name"], "input": bl["input"]})
            return {"text": "\n".join(texts), "tool_calls": tool_calls, "stop_reason": data.get("stop_reason", ""), "raw": data}
        except httpx.HTTPStatusError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else 0
            if code in (502, 503, 504) and attempt < 2:
                import asyncio
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_err = e
            if attempt < 2:
                import asyncio
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise
        except Exception:
            raise
    raise last_err


async def _call_openai(messages, system, temperature, max_tokens, tools=None):
    url = f"{cfg.LLM_API_BASE.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if cfg.LLM_API_KEY:
        headers["Authorization"] = f"Bearer {cfg.LLM_API_KEY}"
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    for m in messages:
        content = m.get("content", "")
        role = m.get("role", "user")
        if isinstance(content, list):
            if role == "assistant":
                text_parts = []
                oai_tool_calls = []
                for bl in content:
                    if bl.get("type") == "text":
                        text_parts.append(bl["text"])
                    elif bl.get("type") == "tool_use":
                        oai_tool_calls.append({
                            "id": bl["id"], "type": "function",
                            "function": {"name": bl["name"], "arguments": json.dumps(bl["input"], ensure_ascii=False)},
                        })
                msg = {"role": "assistant", "content": "\n".join(text_parts) or None}
                if oai_tool_calls:
                    msg["tool_calls"] = oai_tool_calls
                msgs.append(msg)
            elif role == "user":
                oai_parts = []
                has_tool_result = False
                for bl in content:
                    if bl.get("type") == "tool_result":
                        has_tool_result = True
                        msgs.append({
                            "role": "tool", "tool_call_id": bl["tool_use_id"],
                            "content": bl.get("content", ""),
                        })
                    elif bl.get("type") == "image":
                        src = bl.get("source", {})
                        b64 = src.get("data", "")
                        media = src.get("media_type", "image/png")
                        oai_parts.append({"type": "image_url", "image_url": {"url": f"data:{media};base64,{b64}"}})
                    elif bl.get("type") == "text":
                        oai_parts.append({"type": "text", "text": bl["text"]})
                if oai_parts and not has_tool_result:
                    msgs.append({"role": "user", "content": oai_parts})
                elif oai_parts and has_tool_result:
                    msgs.append({"role": "user", "content": oai_parts})
            else:
                msgs.append({"role": role, "content": str(content)})
        else:
            msgs.append({"role": role, "content": content})
    body: Dict[str, Any] = {"model": cfg.LLM_MODEL, "messages": msgs, "temperature": temperature, "max_tokens": max_tokens}
    if cfg.LLM_DISABLE_THINKING:
        # OpenAI 兼容格式下 thinking 是顶层 body 字段（SDK 中需放 extra_body）
        body["thinking"] = {"type": "disabled"}
    if tools:
        oai_tools = []
        for t in tools:
            oai_tools.append({
                "type": "function",
                "function": {"name": t["name"], "description": t.get("description", ""), "parameters": t.get("input_schema", {})},
            })
        body["tools"] = oai_tools
    
    last_err = None
    for attempt in range(3):
        try:
            c = _get_shared_client()
            resp = await c.post(url, json=body, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            choice = data["choices"][0]
            msg = choice.get("message", {})
            fr = (choice.get("finish_reason") or "").strip()
            text = msg.get("content") or ""
            # 过滤 MiniMax 等模型的 <think>...</think> 思维链
            text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
            tool_calls = []
            for tc in (msg.get("tool_calls") or []):
                fn = tc.get("function", {})
                try:
                    inp = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    inp = {}
                tool_calls.append({"id": tc["id"], "name": fn.get("name", ""), "input": inp})
            stop_reason = "tool_use" if fr == "tool_calls" else (fr or "end_turn")
            return {
                "text": text,
                "tool_calls": tool_calls,
                "stop_reason": stop_reason,
                "raw": data,
            }
        except httpx.HTTPStatusError as e:
            last_err = e
            code = e.response.status_code if e.response is not None else 0
            if code in (502, 503, 504) and attempt < 2:
                import asyncio
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise
        except (httpx.TimeoutException, httpx.ConnectError) as e:
            last_err = e
            if attempt < 2:
                import asyncio
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise
        except Exception:
            raise
    raise last_err


def _fix_messages(messages):
    cleaned = []
    for m in messages:
        if m.get("role") == "system":
            continue
        cleaned.append({"role": m["role"], "content": m["content"]})
    if not cleaned:
        return [{"role": "user", "content": "你好"}]
    if cleaned[0]["role"] != "user":
        cleaned.insert(0, {"role": "user", "content": "."})
    merged = [cleaned[0]]
    for m in cleaned[1:]:
        if m["role"] == merged[-1]["role"]:
            prev = merged[-1]["content"]
            cur = m["content"]
            if isinstance(prev, str) and isinstance(cur, str):
                merged[-1]["content"] = prev + "\n\n" + cur
            elif isinstance(cur, list):
                merged[-1]["content"] = cur
            elif isinstance(prev, list):
                pass
            else:
                merged.append(m)
        else:
            merged.append(m)
    return merged


def build_image_content_blocks(image_paths: list, text: str) -> list:
    """构建包含图片的多模态 content blocks（Anthropic 格式，也用于 OpenAI 转换）。"""
    import base64
    _EXT_MEDIA = {
        "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
        "gif": "image/gif", "webp": "image/webp", "bmp": "image/bmp",
    }
    blocks = []
    for p in image_paths:
        ext = p.rsplit(".", 1)[-1].lower()
        media = _EXT_MEDIA.get(ext, "image/png")
        try:
            with open(p, "rb") as f:
                b64 = base64.b64encode(f.read()).decode()
            blocks.append({
                "type": "image",
                "source": {"type": "base64", "media_type": media, "data": b64},
            })
        except Exception:
            pass
    blocks.append({"type": "text", "text": text})
    return blocks


def fix_messages_for_tools(messages):
    """支持 tool-use 的消息清洗：保留 list 类型 content（tool_use / tool_result 块）。"""
    cleaned = []
    for m in messages:
        if m.get("role") == "system":
            continue
        cleaned.append({"role": m["role"], "content": m["content"]})
    if not cleaned:
        return [{"role": "user", "content": "你好"}]
    if cleaned[0]["role"] != "user":
        cleaned.insert(0, {"role": "user", "content": "."})
    merged = [cleaned[0]]
    for m in cleaned[1:]:
        if m["role"] == merged[-1]["role"]:
            prev = merged[-1]["content"]
            cur = m["content"]
            if isinstance(prev, str) and isinstance(cur, str):
                merged[-1]["content"] = prev + "\n\n" + cur
            else:
                merged.append(m)
        else:
            merged.append(m)
    return merged


def _parse_json(text: str):
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    start = t.find("{")
    end = t.rfind("}")
    if start >= 0 and end > start:
        t = t[start:end + 1]
    return json.loads(t)


async def extract_fields(text: str, fields: Optional[List[str]] = None) -> Dict:
    truncated = text[:8000]
    if fields:
        prompt = f"从以下文本提取这些字段：{'、'.join(fields)}\n\n文本：\n{truncated}"
    else:
        prompt = f"从以下文本提取所有关键信息。包括但不限于：标题、主题、统计年份、地区、主要指标数值及单位、关键占比、同比增长率、涉及的城市/区域、监测站点、数据类型等。\n\n如果文本包含表格数据，提取列名和关键统计值。\n\n文本：\n{truncated}"
    sys = '信息提取助手。只返回JSON：{"字段名":"值"}。找不到则不包含。不要编造数据。'
    r = await call_llm_text([{"role": "user", "content": prompt}], system=sys, temperature=0.0, max_tokens=3000)
    try:
        return _parse_json(r)
    except Exception:
        return {"_raw": r}


async def match_fields(template_fields: List[str], data: Dict, context: str = "", requirement: str = "") -> Dict:
    ctx_truncated = context[:5000] if context else ""
    # 只传有意义的数据，过滤掉碎片
    clean_data = {}
    for k, v in data.items():
        vs = str(v).strip()
        if vs and len(vs) >= 2 and len(vs) < 500 and vs != k:
            clean_data[k] = vs
    data_str = json.dumps(clean_data, ensure_ascii=False)
    if len(data_str) > 4000:
        data_str = data_str[:4000]
    
    sys = """你是表格填写助手。根据模板字段名，从提供的数据和文档片段中找到对应的值来填写。

规则：
1. 如果字段名是一个指标名（如"GDP"、"常住人口"），找到对应的数值
2. 如果字段名是一个文档/政策/标准/书籍标题，从文档中找到关于它的描述、进展、状态等信息作为值（如"已发布"、"已制定实施"、"编制完成"等）
3. 如果字段名是一个类别名，找到对应的分类数据
4. 如果文档中确实提到了该字段但没有详细内容，填写"已提及"或相关简要描述
5. 如果文档中完全没有提到该字段，设为空字符串
6. 不要编造数据，不要把字段名本身作为值返回
7. 如果用户要求或字段关系涉及加减乘除，允许基于已提取数据做四则运算得到字段值
8. 四则运算只能使用已给出的数据，除零或缺失数据时返回空字符串

只返回JSON：{"字段名":"值"}"""
    
    prompt = f"需要填写的模板字段：\n{json.dumps(template_fields, ensure_ascii=False)}\n\n用户要求：\n{requirement or ''}\n\n已提取的数据：\n{data_str}\n\n相关文档片段：\n{ctx_truncated}"
    r = await call_llm_text([{"role": "user", "content": prompt}], system=sys, temperature=0.0, max_tokens=3000)
    try:
        result = _parse_json(r)
        # 过滤：值不能等于字段名
        filtered = {}
        for k, v in result.items():
            vs = str(v).strip()
            if vs and vs != k and vs != k.strip():
                filtered[k] = vs
        return filtered
    except Exception:
        return {}
