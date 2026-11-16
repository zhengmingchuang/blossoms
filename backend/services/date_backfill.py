"""把「叙事文本里写明的日期」回填到行数据上。

场景：模板要日期列，数据一部分来自结构化表（行里自带日期），另一部分来自
叙述性文档（如「2020 年 7 月 27 日，湖北省常住人口约 5775 万人…」）。
后者提取出的行只有实体名和指标，日期留在句子里没被带出来，写进模板就是空白。

判定原则（面向用户，不是面向实现）：
  1. 只回填能定位到证据的日期——实体所在段落里明确写了日期；
  2. 段落里没有日期时，仅当整份文档的日期唯一且无冲突，才用这个全局日期；
  3. 都不满足就保持空白，并在告警里说明「N 行源文无日期」，绝不猜。
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

_DATE_FULL = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*日")
_YEAR = re.compile(r"(20\d{2})\s*年")


def _norm(s: Any) -> str:
    return re.sub(r"[\s\u3000]", "", str(s or ""))


def _fmt(y: int, m: int, d: int) -> str:
    return f"{y:04d}-{m:02d}-{d:02d}"


def _dates_in(text: str, fallback_year: Optional[int]) -> List[str]:
    out = []
    for m in _DATE_FULL.finditer(text):
        out.append(_fmt(int(m.group(1)), int(m.group(2)), int(m.group(3))))
    if not out and fallback_year:
        for m in _DATE_MD.finditer(text):
            out.append(_fmt(fallback_year, int(m.group(1)), int(m.group(2))))
    return list(dict.fromkeys(out))


def backfill_row_dates(
    rows: List[Dict[str, Any]],
    entity_col: str,
    date_key: str,
    segments: Iterable[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """给缺日期的行补日期。

    segments 是源文的自然段（或任意可定位的文本块）。返回 (rows, stats)，
    stats 里带 filled / unresolved / global_date，供上层决定要不要告警。
    """
    segs = [s for s in segments if s and str(s).strip()]
    stats: Dict[str, Any] = {"filled": 0, "unresolved": 0, "global_date": None, "reason": ""}
    if not rows or not segs:
        stats["unresolved"] = len(rows or [])
        return rows, stats

    year_hint = None
    for s in segs:
        m = _YEAR.search(s)
        if m:
            year_hint = int(m.group(1))
            break

    seg_dates = [_dates_in(s, year_hint) for s in segs]
    all_dates = {d for ds in seg_dates for d in ds}
    # 全篇只有一个日期 → 可以作为「文档基准日」，用于段落没写日期的行
    global_date = next(iter(all_dates)) if len(all_dates) == 1 else None
    stats["global_date"] = global_date

    for r in rows:
        if not isinstance(r, dict):
            continue
        if str(r.get(date_key) or "").strip():
            continue
        ent = _norm(r.get(entity_col))
        if not ent:
            stats["unresolved"] += 1
            continue
        hit = None
        ent_found = False
        for seg, ds in zip(segs, seg_dates):
            if ent and ent in _norm(seg):
                ent_found = True
                if ds:
                    hit = ds[0]
                    break
        # 全局日期只在「这个实体确实出现在源文里、只是所在段落没写日期」时才用。
        # 实体在源文中根本找不到时不能套用全局日期——那等于给一条来路不明的行
        # 贴上可信日期，比留空更危险。
        if not hit and ent_found and global_date:
            hit = global_date
        if hit:
            r[date_key] = hit
            stats["filled"] += 1
        else:
            stats["unresolved"] += 1

    if stats["filled"] and stats["unresolved"]:
        stats["reason"] = (
            f"{stats['filled']} 行按源文写明的日期回填，"
            f"{stats['unresolved']} 行源文没有日期（未猜）"
        )
    elif stats["filled"]:
        stats["reason"] = f"{stats['filled']} 行按源文写明的日期回填"
    elif stats["unresolved"]:
        stats["reason"] = f"{stats['unresolved']} 行源文没有日期（未猜）"
    return rows, stats
