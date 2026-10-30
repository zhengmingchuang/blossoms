#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
DeepSeek vs Claude —— 模板填表抽取层 A/B 对比

设计原则（保证结论只反映模型能力差异）：
  1. 输入完全一致：两边都读同一份源文档全文（来自 app.db 的 doc_chunk），不走向量检索，
     避免检索随机性干扰。
  2. Prompt 完全一致：复用 routers/templates.py 中 table_fill 的系统提示词。
  3. 分批策略完全一致：同样的字符阈值切分，同样的并发。
  4. 后处理完全一致：仅做与模型无关的归一化（千分位、全角、单位、城市名后缀）。
  5. 基准真值：backend/exports 中该项目历史产出（Claude 生成，任务标记 accuracy=1.0）。

用法：
    set DEEPSEEK_API_KEY=sk-xxx
    python scripts/ab_compare.py --case city100
    python scripts/ab_compare.py --case city100 --case covid
    python scripts/ab_compare.py --all --dry-run     # 只校验输入构造，不调 API

产出：
    ab_report/<case>_<timestamp>.md     人读报告
    ab_report/<case>_<timestamp>.json   原始结果（便于复算）
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import httpx
import openpyxl

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "backend"
DB_PATH = BACKEND / "app.db"
EXPORT_DIR = BACKEND / "exports"
UPLOAD_DIR = BACKEND / "uploads"
REPORT_DIR = ROOT / "ab_report"

# ── 与项目保持一致的分批参数（见 routers/templates.py） ──────────────────
BATCH_MAX_CHARS = 15000
TARGET_PARALLEL = 4
MAX_TOKENS = 16000
CONCURRENCY = 4
TIMEOUT_SEC = 180

# ── 用例定义 ────────────────────────────────────────────────────────────
# 选用标准：源文档的 doc_chunk 必须是「完整正文」，否则测的是输入缺失而非模型能力。
# 注意：本项目对 xlsx 源只把「概览 + 样例行」写入 doc_chunk（20000 行仅存约 25 行样本），
# 因此 xlsx 源不适合做抽取层 A/B；只有 docx/md/txt 源的 chunk 是完整正文。
CASES: Dict[str, Dict[str, Any]] = {
    "city100": {
        "desc": "2025 中国城市经济百强（100 行 × 5 列；源为叙事型 docx，最考验段落内数值抽取）",
        "source_file_ids": [59],
        "template": "tpl_303e4b3615d14c41adc9c331c9377122_2025年中国城市经济百强全景报告-模板.xlsx",
        "baseline": "2025年中国城市经济百强全景报告-模板（副本4）.xlsx",
        "user_req": "智能填表",
    },
}


# ══════════════════════════════════════════════════════════════════════
# 输入构造
# ══════════════════════════════════════════════════════════════════════

def load_source_text(file_ids: List[int]) -> str:
    """按 chunk_index 顺序拼接源文档全文。"""
    conn = sqlite3.connect(DB_PATH)
    parts: List[str] = []
    for fid in file_ids:
        rows = conn.execute(
            "select chunk_text from doc_chunk where file_id=? order by chunk_index", (fid,)
        ).fetchall()
        for (t,) in rows:
            if t and t.strip():
                parts.append(t.strip())
    conn.close()
    return "\n\n".join(parts)


def load_template_columns(tpl_path: Path) -> List[str]:
    """读取模板表头（xlsx 取首个非空行；docx 取首个表格首行）。"""
    if tpl_path.suffix.lower() == ".xlsx":
        wb = openpyxl.load_workbook(tpl_path, data_only=True)
        ws = wb.worksheets[0]
        for row in ws.iter_rows(min_row=1, max_row=5, values_only=True):
            cols = [str(c).strip() for c in row if c is not None and str(c).strip()]
            if cols:
                wb.close()
                return cols
        wb.close()
        return []
    if tpl_path.suffix.lower() == ".docx":
        from docx import Document
        doc = Document(str(tpl_path))
        if doc.tables:
            hdr = [c.text.strip() for c in doc.tables[0].rows[0].cells]
            return [h for h in hdr if h]
    return []


def load_baseline_rows(path: Path, col_names: List[str]) -> List[Dict[str, str]]:
    """读取历史产出作为基准真值。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    out: List[Dict[str, str]] = []
    for row in ws.iter_rows(min_row=2, values_only=True):
        vals = ["" if c is None else str(c).strip() for c in row[: len(col_names)]]
        vals += [""] * (len(col_names) - len(vals))
        if any(vals):
            out.append(dict(zip(col_names, vals)))
    wb.close()
    return out


def build_batches(text: str) -> List[str]:
    """复刻 templates.py 的动态分批：目标 3~5 批，单批上限 BATCH_MAX_CHARS。"""
    total = len(text)
    dynamic = max(2000, total // TARGET_PARALLEL) if total > 0 else BATCH_MAX_CHARS
    eff = min(BATCH_MAX_CHARS, dynamic)
    batches, buf, blen = [], [], 0
    for para in text.split("\n\n"):
        if blen + len(para) > eff and buf:
            batches.append("\n\n".join(buf))
            buf, blen = [], 0
        buf.append(para)
        blen += len(para)
    if buf:
        batches.append("\n\n".join(buf))
    return batches or [text]


def build_sys_prompt(col_names: List[str], user_req: str) -> str:
    """与 routers/templates.py::_fill_table_with_llm_fallback 保持一致。"""
    c0 = col_names[0]
    c1 = col_names[1] if len(col_names) > 1 else "列2"
    return f"""你是数据提取助手。根据文档内容，提取表格数据。
模板列名（你必须原样使用这些列名作为JSON的key，不要改动任何字符）：{json.dumps(col_names, ensure_ascii=False)}
用户要求：{user_req or '智能填表'}

【极其重要】
1. JSON中的key必须和上面给出的模板列名完全一致，一个字符都不能改
2. 数值只输出纯数字，不要加千分位逗号（如写 14415.9 而不是 14,415.9）
3. 每行对应一个实体；第一列/名称列的取值须与列名语义一致：若列名含「省」「自治区」「特别行政区」等省级含义，应填省级行政区名称，不要用地级市名填省级列；若列名表示城市/地市，填城市简称且不要带「市」字（如上海、北京、深圳、重庆）。提取所有符合条件的行，不限数量，绝不截断
4. 如果某个字段在文本中找不到，设为 null
5. 不要自行添加"合计""总计""小计"等汇总行
6. 返回JSON数组：[{{"{c0}": "值1", "{c1}": 数值, ...}}, ...]
只返回JSON数组，不要解释。"""


# ══════════════════════════════════════════════════════════════════════
# 模型调用
# ══════════════════════════════════════════════════════════════════════

def read_claude_conf() -> Dict[str, str]:
    conf = {
        "base": os.getenv("LLM_API_BASE", ""),
        "key": os.getenv("LLM_API_KEY", ""),
        "model": os.getenv("LLM_MODEL", ""),
        "fmt": os.getenv("LLM_API_FORMAT", "anthropic"),
    }
    env_file = BACKEND / ".env"
    if env_file.exists():
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            if k == "LLM_API_BASE" and not os.getenv("LLM_API_BASE"):
                conf["base"] = v
            elif k == "LLM_API_KEY" and not os.getenv("LLM_API_KEY"):
                conf["key"] = v
            elif k == "LLM_MODEL" and not os.getenv("LLM_MODEL"):
                conf["model"] = v
            elif k == "LLM_API_FORMAT" and not os.getenv("LLM_API_FORMAT"):
                conf["fmt"] = v
    # .env 已切到 DeepSeek 时，Claude 侧配置视为不可用，避免拿 DeepSeek 的 key 去请求旧端点
    if "deepseek" in (conf.get("base") or "").lower():
        conf["key"] = ""
    return conf


def read_deepseek_conf() -> Dict[str, str]:
    """DeepSeek 侧配置。

    优先级：环境变量 DEEPSEEK_* > backend/.env 中的 LLM_*（当该项目已把 DeepSeek 配为主 LLM 时）。
    这样用户只需在 backend/.env 里填一次 key，A/B 脚本即可复用。
    """
    base = os.getenv("DEEPSEEK_API_BASE", "https://api.deepseek.com")
    key = os.getenv("DEEPSEEK_API_KEY", "")
    model = os.getenv("DEEPSEEK_MODEL", "deepseek-flash")

    if not key:
        env_file = BACKEND / ".env"
        if env_file.exists():
            vals: Dict[str, str] = {}
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip()
            llm_base = vals.get("LLM_API_BASE", "")
            if "deepseek" in llm_base.lower() and vals.get("LLM_API_KEY"):
                key = vals["LLM_API_KEY"]
                model = vals.get("LLM_MODEL") or model
                # 本项目 .env 用 anthropic 兼容地址；本脚本走 OpenAI 格式，需取根地址
                base = "https://api.deepseek.com"

    return {"base": base, "key": key, "model": model, "fmt": "openai"}


async def _post(client: httpx.AsyncClient, url: str, headers: dict, body: dict) -> dict:
    """带重试的 POST，返回解析后的 JSON。"""
    last = None
    for attempt in range(3):
        try:
            r = await client.post(url, json=body, headers=headers, timeout=TIMEOUT_SEC)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                last = f"HTTP {r.status_code}: {r.text[:300]}"
                continue
            r.raise_for_status()
            return r.json()
        except httpx.TimeoutException as e:
            last = f"timeout: {e}"
            if attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            raise RuntimeError(last)
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"HTTP {e.response.status_code}: {e.response.text[:400]}")
    raise RuntimeError(last or "unknown error")


async def call_claude(client, conf, sys_prompt, user_text) -> Dict[str, Any]:
    url = f"{conf['base'].rstrip('/')}/v1/messages"
    headers = {
        "Content-Type": "application/json",
        "x-api-key": conf["key"],
        "anthropic-version": "2023-06-01",
    }
    body = {
        "model": conf["model"],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        "system": sys_prompt,
        "messages": [{"role": "user", "content": user_text}],
    }
    t0 = time.perf_counter()
    data = await _post(client, url, headers, body)
    elapsed = time.perf_counter() - t0
    text = "\n".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
    usage = data.get("usage", {}) or {}
    return {
        "text": text,
        "elapsed": elapsed,
        "in_tokens": usage.get("input_tokens", 0),
        "out_tokens": usage.get("output_tokens", 0),
        "stop": data.get("stop_reason", ""),
    }


async def call_deepseek(client, conf, sys_prompt, user_text) -> Dict[str, Any]:
    url = f"{conf['base'].rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {conf['key']}"}
    body = {
        "model": conf["model"],
        "max_tokens": MAX_TOKENS,
        "temperature": 0.0,
        # 关键：关闭思考模式。否则 temperature 失效、延迟与 token 成本大幅上升，
        # 且多轮 tools 场景必须回传 reasoning_content（本项目 agent 循环未回传）。
        "thinking": {"type": "disabled"},
        "messages": [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user_text},
        ],
    }
    t0 = time.perf_counter()
    data = await _post(client, url, headers, body)
    elapsed = time.perf_counter() - t0
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message", {}) or {}
    text = msg.get("content") or ""
    text = re.sub(r"<think>[\s\S]*?</think>", "", text).strip()
    usage = data.get("usage", {}) or {}
    return {
        "text": text,
        "elapsed": elapsed,
        "in_tokens": usage.get("prompt_tokens", 0),
        "out_tokens": usage.get("completion_tokens", 0),
        "stop": choice.get("finish_reason", ""),
    }


# ══════════════════════════════════════════════════════════════════════
# 解析与归一化
# ══════════════════════════════════════════════════════════════════════

def parse_json_array(text: str) -> Tuple[List[dict], bool]:
    """解析 LLM 返回的 JSON 数组，返回 (rows, truncated_repaired)。"""
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        t = t.rsplit("```", 1)[0]
    t = t.strip()
    m = re.search(r"\[[\s\S]*\]", t)
    if m:
        try:
            v = json.loads(m.group())
            return (v if isinstance(v, list) else []), False
        except json.JSONDecodeError:
            pass
    # 截断修复
    m2 = re.search(r"\[[\s\S]*", t)
    if m2:
        partial = m2.group().rstrip().rstrip(",")
        lb = partial.rfind("}")
        if lb > 0:
            try:
                v = json.loads(partial[: lb + 1] + "]")
                if isinstance(v, list):
                    return v, True
            except json.JSONDecodeError:
                pass
    return [], False


_SUFFIXES = ("特别行政区", "自治区", "自治州", "地区", "市", "省", "县", "区")


def norm_entity(s: Any) -> str:
    """实体名归一化：去空白、去行政后缀，用于跨模型对齐。"""
    v = str(s or "").strip()
    v = re.sub(r"[\s\u3000]+", "", v)
    v = v.replace("（", "(").replace("）", ")")
    for suf in _SUFFIXES:
        if v.endswith(suf) and len(v) > len(suf) + 1:
            v = v[: -len(suf)]
            break
    return v


def to_number(s: Any) -> Optional[float]:
    if s is None:
        return None
    if isinstance(s, (int, float)):
        return float(s)
    v = str(s).strip().replace(",", "").replace("，", "").replace(" ", "")
    v = v.replace("％", "%")
    v = re.sub(r"[^\d.\-eE+]", "", v)
    if not v or v in ("-", ".", "-."):
        return None
    try:
        return float(v)
    except ValueError:
        return None


def cell_equal(a: Any, b: Any, rel: float = 0.005) -> bool:
    """数值按相对误差比较，文本按归一化后全等比较。"""
    sa, sb = ("" if a is None else str(a).strip()), ("" if b is None else str(b).strip())
    if not sa and not sb:
        return True
    if not sa or not sb:
        return False
    na, nb = to_number(sa), to_number(sb)
    if na is not None and nb is not None:
        if na == nb:
            return True
        denom = max(abs(na), abs(nb), 1e-9)
        return abs(na - nb) / denom <= rel
    na_s = re.sub(r"[\s,，]", "", sa)
    nb_s = re.sub(r"[\s,，]", "", sb)
    return na_s == nb_s


def normalize_rows(rows: List[dict], col_names: List[str]) -> List[Dict[str, str]]:
    """把 LLM 输出对齐到模板列名，丢弃非数据行。"""
    out: List[Dict[str, str]] = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        item: Dict[str, str] = {}
        for c in col_names:
            val = r.get(c)
            if val is None:
                # 容错：列名轻微不一致时按归一化匹配
                for k, v in r.items():
                    if _norm_header(k) == _norm_header(c):
                        val = v
                        break
            item[c] = "" if val is None else str(val).strip()
        if not any(item.values()):
            continue
        first = item[col_names[0]]
        if re.search(r"^(合计|总计|小计|全国|平均)", first):
            continue
        out.append(item)
    return out


def _norm_header(s: Any) -> str:
    return re.sub(r"[\s\(\)（）\[\]【】、,，.。:：;；'\"\\/\-_!！?？*%％]", "", str(s or "")).lower()


# ══════════════════════════════════════════════════════════════════════
# 评分
# ══════════════════════════════════════════════════════════════════════

def evaluate(rows: List[Dict[str, str]], baseline: List[Dict[str, str]],
             col_names: List[str], raw_keys_compliance: Optional[float] = None) -> Dict[str, Any]:
    """以 baseline 为真值，计算覆盖率与逐格准确率。"""
    base_map: Dict[str, Dict[str, str]] = {}
    for b in baseline:
        k = norm_entity(b.get(col_names[0], ""))
        if k:
            base_map.setdefault(k, b)

    pred_map: Dict[str, Dict[str, str]] = {}
    dup = 0
    for r in rows:
        k = norm_entity(r.get(col_names[0], ""))
        if not k:
            continue
        if k in pred_map:
            dup += 1
            continue
        pred_map[k] = r

    matched = [k for k in pred_map if k in base_map]
    missing = [k for k in base_map if k not in pred_map]
    extra = [k for k in pred_map if k not in base_map]

    metric_cols = col_names[1:]
    cell_total = len(matched) * len(metric_cols)
    cell_ok = 0
    cell_bad: List[Dict[str, Any]] = []
    for k in matched:
        for c in metric_cols:
            pv, bv = pred_map[k].get(c, ""), base_map[k].get(c, "")
            if cell_equal(pv, bv):
                cell_ok += 1
            elif len(cell_bad) < 40:
                cell_bad.append({"entity": k, "col": c, "pred": pv, "base": bv})

    return {
        "baseline_rows": len(base_map),
        "pred_rows_raw": len(rows),
        "pred_rows_dedup": len(pred_map),
        "duplicate_rows": dup,
        "matched": len(matched),
        "missing": len(missing),
        "extra": len(extra),
        "coverage": round(len(matched) / len(base_map), 4) if base_map else 0.0,
        "cell_total": cell_total,
        "cell_ok": cell_ok,
        "cell_accuracy": round(cell_ok / cell_total, 4) if cell_total else 0.0,
        "colname_compliance": round(raw_keys_compliance, 4) if raw_keys_compliance is not None else None,
        "missing_sample": missing[:15],
        "extra_sample": extra[:15],
        "cell_mismatch_sample": cell_bad,
    }


def key_compliance(raw_rows: List[dict], col_names: List[str]) -> float:
    """原始返回的 key 与模板列名完全一致的比例。"""
    if not raw_rows:
        return 0.0
    want = set(col_names)
    ok = sum(1 for r in raw_rows if isinstance(r, dict) and set(r.keys()) == want)
    return ok / len(raw_rows)


# ══════════════════════════════════════════════════════════════════════
# 主流程
# ══════════════════════════════════════════════════════════════════════

async def run_case(case_name: str, dry_run: bool, smoke: bool = False,
                   args_side: str = "both") -> Dict[str, Any]:
    case = CASES[case_name]
    print(f"\n{'='*70}\n用例: {case_name} — {case['desc']}\n{'='*70}")

    tpl_path = UPLOAD_DIR / case["template"]
    base_path = EXPORT_DIR / case["baseline"]
    if not tpl_path.exists():
        raise FileNotFoundError(f"模板不存在: {tpl_path}")
    if not base_path.exists():
        raise FileNotFoundError(f"基准产物不存在: {base_path}")

    col_names = load_template_columns(tpl_path)
    baseline = load_baseline_rows(base_path, col_names)
    source_text = load_source_text(case["source_file_ids"])
    batches = build_batches(source_text)
    sys_prompt = build_sys_prompt(col_names, case["user_req"])

    if smoke:
        # 只跑首批、只对比首批覆盖范围内的基准行，用于低成本验证连通性
        batches = batches[:1]
        baseline = baseline[:25]

    print(f"  列名({len(col_names)}): {col_names}")
    print(f"  源文档: {len(source_text)} 字符 → {len(batches)} 批"
          + ("（smoke：仅首批）" if smoke else ""))
    print(f"  基准行数: {len(baseline)}" + ("（smoke：前 25 行）" if smoke else ""))

    if dry_run:
        print(f"  [dry-run] prompt 长度 {len(sys_prompt)} 字符，未调用 API")
        return {"case": case_name, "dry_run": True, "columns": col_names,
                "batches": len(batches), "source_chars": len(source_text),
                "baseline_rows": len(baseline)}

    cc, dc = read_claude_conf(), read_deepseek_conf()
    run_a = args_side in ("both", "claude")
    run_b = args_side in ("both", "deepseek")
    if run_a and not cc["key"]:
        raise RuntimeError("未找到 Claude 配置（backend/.env 的 LLM_API_KEY）")
    if run_b and not dc["key"]:
        raise RuntimeError(
            "未找到 DeepSeek API Key。二选一：\n"
            "  a) 在 backend/.env 中填写 LLM_API_KEY=sk-xxx（推荐，项目与脚本共用）\n"
            "  b) 设置环境变量 DEEPSEEK_API_KEY=sk-xxx\n"
            "  或加 --side claude 只跑另一侧"
        )

    sem = asyncio.Semaphore(CONCURRENCY)

    async def run_side(label: str, caller, conf) -> Dict[str, Any]:
        async with httpx.AsyncClient(proxy=None) as client:
            async def one(i: int, b: str):
                async with sem:
                    user_text = (f"从以下文档中提取所有符合条件的数据（第{i+1}批，共{len(batches)}批），"
                                 f"必须提取全部数据，不能遗漏：\n\n{b}")
                    try:
                        r = await caller(client, conf, sys_prompt, user_text)
                        r["batch"] = i
                        return r
                    except Exception as e:
                        return {"batch": i, "text": "", "error": str(e)[:300],
                                "elapsed": 0.0, "in_tokens": 0, "out_tokens": 0, "stop": "error"}
            t0 = time.perf_counter()
            res = await asyncio.gather(*[one(i, b) for i, b in enumerate(batches)])
            wall = time.perf_counter() - t0

        all_rows: List[dict] = []
        repaired = 0
        errors = []
        for r in res:
            if r.get("error"):
                errors.append(r["error"])
                continue
            rows, rep = parse_json_array(r.get("text", ""))
            all_rows.extend(rows)
            repaired += 1 if rep else 0

        comp = key_compliance(all_rows, col_names)
        norm = normalize_rows(all_rows, col_names)
        metrics = evaluate(norm, baseline, col_names, comp)
        metrics.update({
            "wall_sec": round(wall, 2),
            "sum_batch_sec": round(sum(r.get("elapsed", 0) for r in res), 2),
            "in_tokens": sum(r.get("in_tokens", 0) for r in res),
            "out_tokens": sum(r.get("out_tokens", 0) for r in res),
            "truncated_repairs": repaired,
            "batch_errors": errors,
            "model": conf["model"],
        })
        print(f"  [{label}] {conf['model']}: 行={metrics['pred_rows_dedup']} "
              f"覆盖={metrics['coverage']:.1%} 逐格={metrics['cell_accuracy']:.1%} "
              f"列名合规={metrics['colname_compliance']:.1%} 耗时={metrics['wall_sec']}s "
              f"输出token={metrics['out_tokens']}"
              + (f" 错误{len(errors)}" if errors else ""))
        return metrics

    a = await run_side("Claude", call_claude, cc) if run_a else None
    b = await run_side("DeepSeek", call_deepseek, dc) if run_b else None

    if a and b:
        delta = {
            "coverage": round(b["coverage"] - a["coverage"], 4),
            "cell_accuracy": round(b["cell_accuracy"] - a["cell_accuracy"], 4),
            "colname_compliance": round((b["colname_compliance"] or 0) - (a["colname_compliance"] or 0), 4),
            "wall_sec": round(b["wall_sec"] - a["wall_sec"], 2),
            "out_tokens": b["out_tokens"] - a["out_tokens"],
        }
        print(f"  Δ(DeepSeek-Claude): 覆盖 {delta['coverage']:+.1%} | 逐格 {delta['cell_accuracy']:+.1%} "
              f"| 列名 {delta['colname_compliance']:+.1%} | 耗时 {delta['wall_sec']:+.1f}s")
    else:
        delta = None
        print(f"  （单侧运行，未计算 Δ）")

    return {
        "case": case_name, "desc": case["desc"], "columns": col_names,
        "source_chars": len(source_text), "batches": len(batches),
        "baseline_rows": len(baseline), "smoke": smoke,
        "claude": a, "deepseek": b, "delta": delta,
    }


def render_md(results: List[Dict[str, Any]]) -> str:
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    L = [f"# DeepSeek vs Claude 抽取层 A/B 报告", "", f"生成时间：{ts}", ""]
    L += ["## 方法说明", "",
          "- 输入：同一份源文档全文（doc_chunk），**不走向量检索**，消除检索随机性。",
          "- Prompt / 分批 / 并发 / max_tokens / temperature 全部一致。",
          "- DeepSeek 已关闭思考模式（`thinking.type=disabled`）。",
          "- 真值：`backend/exports` 中该项目历史产出（Claude 生成，任务标记 accuracy=1.0）。",
          "- 数值按 0.5% 相对误差判等；实体名归一化后对齐（去行政后缀）。", ""]
    for r in results:
        if r.get("error"):
            L += [f"## {r['case']} — 运行失败", "", "```", r["error"], "```", ""]
            continue
        if r.get("dry_run"):
            L += [f"## {r['case']} (dry-run)", "",
                  f"- 列名：{r['columns']}", f"- 批次：{r['batches']}",
                  f"- 源文档字符：{r['source_chars']}", f"- 基准行数：{r['baseline_rows']}", ""]
            continue
        a, b, d = r["claude"], r["deepseek"], r["delta"]

        def cell(side, key, fmt=None, default="—"):
            if not side:
                return default
            v = side.get(key)
            if v is None:
                return default
            return format(v, fmt) if fmt else str(v)

        def dlt(key, fmt):
            if not d:
                return "—"
            return format(d[key], fmt)

        row_delta = "—" if not (a and b) else f"{b['pred_rows_dedup'] - a['pred_rows_dedup']:+d}"
        err_a = "—" if not a else str(len(a["batch_errors"]))
        err_b = "—" if not b else str(len(b["batch_errors"]))

        L += [f"## {r['case']} — {r['desc']}", ""]
        if r.get("smoke"):
            L += ["> ⚠️ smoke 模式：仅首批、仅对比前 25 行，**不代表全量准确率**，仅用于验证连通性。", ""]
        L += ["| 指标 | Claude | DeepSeek | Δ |", "|---|---|---|---|",
              f"| 抽取行数(去重) | {cell(a,'pred_rows_dedup')} | {cell(b,'pred_rows_dedup')} | {row_delta} |",
              f"| 实体覆盖率 | {cell(a,'coverage','.1%')} | {cell(b,'coverage','.1%')} | {dlt('coverage','+.1%')} |",
              f"| 逐格准确率 | {cell(a,'cell_accuracy','.1%')} | {cell(b,'cell_accuracy','.1%')} | {dlt('cell_accuracy','+.1%')} |",
              f"| 列名合规率 | {cell(a,'colname_compliance','.1%')} | {cell(b,'colname_compliance','.1%')} | {dlt('colname_compliance','+.1%')} |",
              f"| 重复行 | {cell(a,'duplicate_rows')} | {cell(b,'duplicate_rows')} | |",
              f"| 输出 token | {cell(a,'out_tokens')} | {cell(b,'out_tokens')} | {dlt('out_tokens','+d')} |",
              f"| 墙钟耗时(s) | {cell(a,'wall_sec')} | {cell(b,'wall_sec')} | {dlt('wall_sec','+.1f')} |",
              f"| 截断修复次数 | {cell(a,'truncated_repairs')} | {cell(b,'truncated_repairs')} | |",
              f"| 批次报错 | {err_a} | {err_b} | |", ""]

        for side_name, s in (("Claude", a), ("DeepSeek", b)):
            if s and s["batch_errors"]:
                L += [f"**{side_name} 报错**："] + [f"- `{e}`" for e in s["batch_errors"][:3]] + [""]
        for side_name, s in (("Claude", a), ("DeepSeek", b)):
            if not s:
                continue
            if s["missing_sample"]:
                L += [f"**{side_name} 漏抽实体（前 15）**：{', '.join(s['missing_sample'])}", ""]
            if s["extra_sample"]:
                L += [f"**{side_name} 多抽实体（前 15）**：{', '.join(s['extra_sample'])}", ""]
            if s["cell_mismatch_sample"]:
                L += [f"**{side_name} 格子差异样例**：", "", "| 实体 | 列 | 模型输出 | 基准 |", "|---|---|---|---|"]
                for m in s["cell_mismatch_sample"][:12]:
                    L.append(f"| {m['entity']} | {m['col']} | {m['pred']} | {m['base']} |")
                L.append("")
    return "\n".join(L)


async def main_async(args) -> int:
    cases = list(CASES) if args.all else (args.case or ["city100"])
    unknown = [c for c in cases if c not in CASES]
    if unknown:
        print(f"未知用例: {unknown}；可选: {list(CASES)}")
        return 2

    results = []
    for c in cases:
        try:
            results.append(await run_case(c, args.dry_run, args.smoke, args.side))
        except Exception as e:
            print(f"  [失败] {c}: {e}")
            results.append({"case": c, "error": str(e)})

    REPORT_DIR.mkdir(exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    tag = "dryrun" if args.dry_run else "ab"
    md_path = REPORT_DIR / f"{tag}_{ts}.md"
    json_path = REPORT_DIR / f"{tag}_{ts}.json"
    md_path.write_text(render_md(results), encoding="utf-8")
    json_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n报告已生成：\n  {md_path}\n  {json_path}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="DeepSeek vs Claude 模板填表 A/B 对比")
    ap.add_argument("--case", action="append", help="用例名，可重复")
    ap.add_argument("--all", action="store_true", help="跑全部用例")
    ap.add_argument("--dry-run", action="store_true", help="只校验输入构造，不调用 API")
    ap.add_argument("--smoke", action="store_true",
                    help="快速验证：只跑首批 + 对比前 25 行，成本极低，用于确认 key 与连通性")
    ap.add_argument("--side", choices=["both", "claude", "deepseek"], default="both",
                    help="只跑某一侧（缺 key 时可用于单独建基线）")
    args = ap.parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
