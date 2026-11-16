"""把被模型抹掉的质量标记补回去。

源数据里 `38(PF)`、`4985(H)`、`32*` 这类标记表示读数异常/待核实。
模型常常只取数字（38），结果是「异常读数被伪装成正常值」——这比留空更危险，
因为下游看表的人无从判断。

这里不做「猜」：只有当同一实体的段落里确实出现了「同一个数字 + 标记」时，
才把标记补回到该实体的单元格上；定位不到就原样保留，不冒险。
"""
import re
from typing import Any, Dict, List, Tuple

# 数字后紧跟括号标记（PF/H/EST…）或星号
_MARKED = re.compile(r"(\d+(?:\.\d+)?)\s*(?:[（(]\s*([A-Za-z]{1,4})\s*[）)]|\*+)")
_ANY_MARK = re.compile(r"[（(]\s*[A-Za-z]{1,4}\s*[）)]|\*")


def _norm_num(v: Any) -> str:
    s = str(v if v is not None else "").strip().replace(",", "").replace("，", "")
    try:
        f = float(s)
    except (TypeError, ValueError):
        return ""
    return str(int(f)) if f == int(f) else f"{f:g}"


def _has_marker(v: Any) -> bool:
    return bool(_ANY_MARK.search(str(v if v is not None else "")))


def restore_quality_markers(
    rows: List[Dict[str, Any]],
    col_names: List[str],
    segments: List[str],
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """返回 (rows, stats)。stats: restored / candidates"""
    stats = {"restored": 0, "candidates": 0, "samples": []}
    if not rows or not segments or not col_names:
        return rows, stats

    entity_col = col_names[0]
    # 预解析每个段落里的「数字 -> 带标记原串」
    seg_marked: List[Dict[str, str]] = []
    for seg in segments:
        m: Dict[str, str] = {}
        for mt in _MARKED.finditer(seg):
            num = _norm_num(mt.group(1))
            if not num:
                continue
            raw = mt.group(0).replace("（", "(").replace("）", ")")
            m.setdefault(num, raw)
        seg_marked.append(m)

    for r in rows:
        if not isinstance(r, dict):
            continue
        ent = str(r.get(entity_col) or "").strip()
        if not ent:
            continue
        # 找这个实体所在的段落（可能不止一段，取第一个命中带标记的）
        maps = [m for seg, m in zip(segments, seg_marked) if ent and ent in seg]
        if not maps:
            continue
        for col in col_names[1:]:
            val = r.get(col)
            if val is None or str(val).strip() == "" or _has_marker(val):
                continue
            num = _norm_num(val)
            if not num:
                continue
            for m in maps:
                if num in m:
                    stats["candidates"] += 1
                    r[col] = m[num]
                    stats["restored"] += 1
                    if len(stats["samples"]) < 5:
                        stats["samples"].append(f"{ent}.{col}: {val} → {m[num]}")
                    break
    return rows, stats
