# -*- coding: utf-8 -*-
"""填表结果审查（LLM 路径专用）。

**为什么不让第二个模型来审**：再调一次 LLM 做「审查」，本质是把幻觉风险翻倍——
审查模型同样会编，而且编得更像真的。这里做的是**可判定的核对**：
模型写进单元格的每个值，是否真的出现在它当时看到的源文片段里。
找不到的就是「源文无据」，可能是模型推断、跨段错配、或单位换算后的形态差异，
统一标出来交给人看，而不是假装审过。

局限（必须写在明面上）：
  - 字符串命中不等于语义正确（同一个数字可能出现在别的段落里）；
  - 换算过的值（如 5.2 万亿 → 52000）在源文里找不到原串，会被标为可疑，
    这是**有意的保守**：宁可多标，不可漏标。
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

_WS_RE = re.compile(r"[\s,，\u00a0]")
_NUM_RE = re.compile(r"^-?\d+(?:\.\d+)?$")
_UNIT_TAIL_RE = re.compile(r"(万亿|亿|万|千)?(元|美元|人|份|次|人次|例|个|台|套|件|张|座|辆|户|家|所|条|只|头|亩|吨|克|米|度|%)$")

# 中文数字：源文写「五点四万亿」「四千一百万」时，模型填的是 54000 / 41000000，
# 字符串对不上就会被报成「源文无据」，把正确结果说成可疑。
# 这里只把数字部分转成阿拉伯数字，万/亿留在原地当单位后缀（"五点四万亿" -> "5.4万亿"），
# 这样 54000 的候选写法 "5.4万" 就能命中。
_CN_DIGITS = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
              "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNITS = {"十": 10, "百": 100, "千": 1000}
_CN_RUN_RE = re.compile(r"[零一二两三四五六七八九十百千点]{2,}")


def _cn_int(s: str) -> Optional[float]:
    """「四千一百」-> 4100，「十二」-> 12，「五点四」-> 5.4。无法解析返回 None。"""
    if not s:
        return None
    if "点" in s:
        head, _, tail = s.partition("点")
        ip = _cn_int(head) if head else 0.0
        if ip is None:
            return None
        if not tail or any(ch not in _CN_DIGITS for ch in tail):
            return None
        frac = "".join(str(_CN_DIGITS[ch]) for ch in tail)
        return float(f"{int(ip)}.{frac}")
    total = section = 0
    num = 0
    for ch in s:
        if ch in _CN_DIGITS:
            num = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            section += (num or 1) * _CN_UNITS[ch]
            num = 0
        else:
            return None
    return float(total + section + num)


def _cn_to_arabic(text: str) -> str:
    def _rep(m):
        v = _cn_int(m.group(0))
        if v is None:
            return m.group(0)
        return f"{v:g}"
    return _CN_RUN_RE.sub(_rep, text)


def _norm(s: Any) -> str:
    return _WS_RE.sub("", str(s or ""))


def _numeric_candidates(raw: str) -> List[str]:
    """一个数值在源文里可能出现的几种写法。"""
    out: List[str] = []
    s = raw.strip().rstrip("%")
    s = _UNIT_TAIL_RE.sub("", s).strip()
    if not _NUM_RE.match(s):
        return out
    out.append(s)
    try:
        f = float(s)
    except ValueError:
        return out
    if f == int(f):
        out.append(str(int(f)))
    out.append(f"{f:g}")
    out.append(f"{f:.1f}")
    out.append(f"{f:.2f}")
    # 单位换算后的值必须能对上源文的原始写法，否则每一格换算过的数据都会被报成
    # 「源文无据」——实测 56700 亿元 对应源文「5.67 万亿元」，命中率被压到 50%，
    # 用户看到的是「一半数据有问题」，而实际上全部正确。
    # 只做「缩小」方向（元→万亿），并要求带单位后缀，避免 5.67 这种短数字巧合命中。
    for scale, unit in ((1e4, "万"), (1e8, "亿"), (1e12, "万亿")):
        if abs(f) < scale:
            continue
        v = f / scale
        if v < 0.01:
            continue
        out.append(f"{v:g}{unit}")
        out.append(f"{v:.1f}{unit}")
        out.append(f"{v:.2f}{unit}")
    # 去重保序
    seen = set()
    return [x for x in out if not (x in seen or seen.add(x))]


def audit_llm_rows(
    rows: List[Dict[str, Any]],
    col_names: List[str],
    source_text: str,
    max_samples: int = 8,
) -> Dict[str, Any]:
    """逐格核对模型输出能否在源文中定位。

    返回:
      checked / hits / misses / pass_rate / samples[{row, column, value}]
    pass_rate = 1.0 表示每个非空单元格都能在源文中找到原串（仍不等于语义正确）。
    """
    src = _norm(source_text)
    if src:
        src = _cn_to_arabic(src)
    if not src or not rows:
        return {"checked": 0, "hits": 0, "misses": 0, "pass_rate": None, "samples": []}

    checked = hits = 0
    samples: List[Dict[str, Any]] = []
    for ri, row in enumerate(rows):
        if not isinstance(row, dict):
            continue
        for col in col_names:
            val = row.get(col)
            s = str(val).strip() if val is not None else ""
            if not s or s in ("None", "-", "—", "/"):
                continue
            checked += 1
            cands = _numeric_candidates(s) or [_norm(s)[:40]]
            if any(c and c in src for c in cands):
                hits += 1
            elif len(samples) < max_samples:
                samples.append({"row": ri + 1, "column": col, "value": s})

    misses = checked - hits
    return {
        "checked": checked,
        "hits": hits,
        "misses": misses,
        "pass_rate": round(hits / checked, 4) if checked else None,
        "samples": samples,
    }


def audit_summary_message(audit: Dict[str, Any]) -> Optional[str]:
    """把审查结果转成面向用户的一句话；全部命中时返回 None。"""
    if not audit or not audit.get("checked"):
        return None
    rate = audit.get("pass_rate")
    if rate is None or rate >= 1.0:
        return None
    sample_txt = "、".join(
        f"第{s['row']}行「{s['column']}」={s['value']}" for s in (audit.get("samples") or [])[:3]
    )
    return (
        f"模型输出审查：{audit['checked']} 个单元格中有 {audit['misses']} 个未能在源文中定位"
        f"（{rate:.1%} 命中），可能为模型推断或跨段错配"
        + (f"，例如 {sample_txt}" if sample_txt else "")
        + "；请重点核对后再使用"
    )
