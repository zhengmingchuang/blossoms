"""表格精确查找：给定关键词（日期、实体名、数值），直接把命中的行取出来。

为什么需要它：聊天里的检索是「语义/词频 top-k」，对「某国某一天的某列」这种
点查询并不合适——一张 15,000 行的表有 380 个片段，top-10 很可能漏掉目标行，
模型于是回答「文档里没有」。而这类问题的答案在表里是确定的，直接查比检索可靠。

设计约束：
  - 只读，不改任何文件；
  - 命中条件是「每个关键词都出现」，避免单关键词召回过多；
  - 按 mtime 缓存解析结果，避免每次提问都重读大表；
  - 只返回有限行（默认 20），防止把整张表灌进上下文。
"""
import os
import re
import threading
from typing import Any, Dict, List, Tuple

_CACHE: Dict[str, Tuple[float, List[List[Any]]]] = {}
_LOCK = threading.Lock()
_CACHE_MAX_FILES = 8


def _norm(v: Any) -> str:
    return re.sub(r"\s+", "", str(v if v is not None else "")).replace(",", "").replace("，", "")


def _load_rows(path: str) -> List[List[Any]]:
    st = os.stat(path)
    key = os.path.abspath(path)
    with _LOCK:
        hit = _CACHE.get(key)
        if hit and hit[0] == st.st_mtime:
            return hit[1]
    rows: List[List[Any]] = []
    ext = os.path.splitext(path)[1].lower()
    if ext == ".xlsx":
        import openpyxl

        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        for ws in wb.worksheets:
            for r in ws.iter_rows(values_only=True):
                if any(v is not None and str(v).strip() != "" for v in r):
                    rows.append(list(r))
        wb.close()
    with _LOCK:
        if len(_CACHE) >= _CACHE_MAX_FILES:
            _CACHE.pop(next(iter(_CACHE)))
        _CACHE[key] = (st.st_mtime, rows)
    return rows


def find_rows(file_path: str, needles: List[str], max_rows: int = 20) -> List[List[Any]]:
    """返回所有单元格拼起来同时包含全部 needle 的行（含表头行）。"""
    ns = [_norm(n) for n in needles if _norm(n)]
    if not ns or not os.path.exists(file_path):
        return []
    try:
        rows = _load_rows(file_path)
    except Exception:
        return []
    if not rows:
        return []
    header = rows[0]
    out = []
    for r in rows[1:]:
        joined = _norm(" | ".join(str(v) for v in r if v is not None))
        if all(n in joined for n in ns):
            out.append(r)
            if len(out) >= max_rows:
                break
    return [header] + out if out else []


_DATE_PATTERNS = [
    re.compile(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})"),
    re.compile(r"(?<!\d)(\d{1,2})[-/月](\d{1,2})(?!\d)"),
]


def extract_date_needles(text: str) -> List[str]:
    """从提问里抽出日期形态的关键词，返回多种写法（兼容表里 2020-08-31 / 2020/8/31）。"""
    out: List[str] = []
    for pat in _DATE_PATTERNS:
        for m in pat.finditer(text or ""):
            g = m.groups()
            if len(g) == 3:
                y, mo, d = int(g[0]), int(g[1]), int(g[2])
                out += [f"{y:04d}-{mo:02d}-{d:02d}", f"{y}/{mo}/{d}", f"{y}年{mo}月{d}日"]
            elif len(g) == 2:
                mo, d = int(g[0]), int(g[1])
                out += [f"-{mo:02d}-{d:02d}", f"/{mo}/{d}"]
    return list(dict.fromkeys(out))


def find_rows_for_query(file_path: str, query: str, max_rows: int = 20) -> List[List[Any]]:
    """按「日期 + 实体名」精确取行。

    实体名的判定不靠分词：直接看该行第一列的值是否出现在提问里。
    对「Costa Rica 在 2020-08-31 的病例数」这类问题，这比任何检索都稳。
    """
    dates = extract_date_needles(query)
    if not dates or not os.path.exists(file_path):
        return []
    try:
        rows = _load_rows(file_path)
    except Exception:
        return []
    if not rows:
        return []
    q = _norm(query)
    header, out = rows[0], []
    for r in rows[1:]:
        joined = _norm(" | ".join(str(v) for v in r if v is not None))
        if not any(d in joined for d in dates):
            continue
        ent = _norm(r[0]) if r else ""
        if ent and ent in q:
            out.append(r)
            if len(out) >= max_rows:
                break
    return [header] + out if out else []


def rows_to_text(rows: List[List[Any]], max_rows: int = 20) -> str:
    if not rows:
        return ""
    header = [str(h) if h is not None else "" for h in rows[0]]
    lines = []
    for r in rows[1:max_rows + 1]:
        pairs = []
        for i, v in enumerate(r):
            if v is None or str(v).strip() == "":
                continue
            h = header[i] if i < len(header) and header[i] else f"列{i+1}"
            pairs.append(f"{h}: {v}")
        if pairs:
            lines.append("- " + " | ".join(pairs))
    return "\n".join(lines)
