"""按模板表头声明的单位做数值归一化。

核心原则（2026-11-07 修订）：**表头有单位以表头为准，表头没单位以文中单位为准**。

- 表头写了单位（如「人均GDP（万元）」），则把带单位的值换算到该单位后写纯数值；
- 表头没写单位时，**不再原样保留文本**：从该列源值里统计出主单位（多数值自带的单位），
  把整列换算到该单位后写纯数值，保证同一列可排序、可计算；
- 用户明确要求目标单位（「统一换算成亿元」「以万元为单位」）时以用户为准，优先级最高；
- 量词不同（人民币「元」vs「美元」）不做换算，只告警——跨币种换算需要汇率口径，不能猜。
"""

import re
from typing import Any, Dict, List, Optional, Tuple

# 数量级前缀 -> 倍率
_PREFIX_SCALE = {
    "万亿": 1e12,
    "亿": 1e8,
    "万": 1e4,
    "": 1.0,
}

# 可识别的量词（去掉数量级前缀后的部分）
_QUANTIFIERS = {
    "元", "美元", "欧元", "英镑", "日元", "港元",
    "人", "份", "次", "人次", "例", "个", "台", "套", "件", "张", "座", "辆",
    "户", "家", "所", "条", "只", "头", "亩", "公顷", "平方公里", "平方米",
    "吨", "千克", "公斤", "克", "米", "公里", "千米", "度", "千瓦时",
}

# 值的前导修饰词，解析时剥离
_LEAD_WORDS = ("约", "大约", "约计", "近", "接近", "超过", "逾", "不足", "低于", "高于", "达", "共计", "累计")

# 「12.6万份」「7.3万元」「1,234.5」「5353.2」「5353.2亿元（现价）」
# 结尾的括号注释（如「（现价）」「(2024)」）不属于单位，先剥掉再匹配
_VALUE_RE = re.compile(
    r"^(?P<num>-?\d[\d,]*(?:\.\d+)?)\s*(?P<unit>[万亿]{0,2}[^\d\s（(]{0,6})?"
    r"\s*(?:[（(][^）)]*[）)])*\s*$"
)

# 数据质量标记：监测类数据常见「16(H)」「4985(PF)」「32*」。
# 只认短拉丁字母括号或星号/井号结尾——「（现价）」「（2024）」属于口径注释，不算标记。
_QUALITY_MARKER_RE = re.compile(r"[（(]\s*[A-Za-z]{1,6}\s*[）)]\s*$|[*#]\s*$")

# 表头里的单位：括号内容 / 斜杠前后
_BRACKET_RE = re.compile(r"[（(]([^）)]*)[）)]")
_SLASH_RE = re.compile(r"([^\s/／]{1,12})\s*[/／]\s*([^\s/／]{1,8})\s*$")

# 单位片段前的冗余前缀：「单位：万元」「计量单位 万元」
_UNIT_LEAD_RE = re.compile(r"^(?:计量单位|单位)\s*[:：]?\s*")


def split_unit(unit: str) -> Tuple[Optional[str], Optional[float]]:
    """把「万元」拆成 (量词='元', 倍率=1e4)。

    纯数量级（「亿」「万」这种没写量词的）返回 (None, 倍率)，
    表示「数量级已知、量词未知」，换算时按表头量词处理。
    无法识别返回 (None, None)。
    """
    if not unit:
        return None, None
    u = unit.strip()
    if u in _PREFIX_SCALE and u:
        return None, _PREFIX_SCALE[u]
    for prefix in ("万亿", "亿", "万"):
        if u.startswith(prefix):
            rest = u[len(prefix):]
            if rest in _QUANTIFIERS:
                return rest, _PREFIX_SCALE[prefix]
    if u in _QUANTIFIERS:
        return u, 1.0
    return None, None


def _unit_from_piece(piece: str) -> Optional[str]:
    """尝试把一段文本识别为单位，识别不到返回 None。"""
    p = _UNIT_LEAD_RE.sub("", str(piece or "").strip()).strip()
    if not p:
        return None
    quant, scale = split_unit(p)
    # 允许「（万）」这种只写数量级的简写
    if quant or scale:
        return p
    return None


def parse_header_unit(header: str) -> Optional[str]:
    """从表头中提取单位声明，如「人均GDP（万元）」-> 万元；无声明返回 None。"""
    if not header:
        return None
    h = str(header)
    for raw in _BRACKET_RE.findall(h):
        # 括号里可能是「万元，现价」「单位：亿元」这类复合描述，逐个片段尝试
        for piece in re.split(r"[,，、;；/／:：\s]+", raw):
            unit = _unit_from_piece(piece)
            if unit:
                return unit
    m = _SLASH_RE.search(h)
    if m:
        # 「万元/人」「元/吨」：分子是数量级所在，优先取左侧；
        # 左侧不是单位时（如「2024/2025」）再看右侧，两侧都不是则放弃。
        left, right = m.group(1), m.group(2)
        for cand in (left[-4:], left[-3:], left[-2:], left, right):
            unit = _unit_from_piece(cand)
            if unit:
                return unit
    return None


_PERCENT_UNITS = {"%", "％"}
# 财务报表的括号负数：(1,234) = -1234。只认「纯数字括号」，
# 「38(PF)」「16(H)」这类质量标记不会被误判。
_ACCT_NEG_RE = re.compile(r"^[（(]\s*([\d,，]+(?:\.\d+)?)\s*[）)]$")


def parse_value_with_unit(value: Any) -> Tuple[Optional[float], Optional[str]]:
    """解析「7.3万元」-> (7.3, '万元')；纯数字 -> (7.3, None)；无法解析 -> (None, None)。"""
    if value is None or isinstance(value, bool):
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    s = str(value).strip()
    if not s:
        return None, None
    # 从网页/PDF 复制来的负号常是「−」「–」「－」，不归一就整格解析失败，
    # 结果是负数以文本形式写进表里，用户没法排序也没法求和。
    s = s.replace("−", "-").replace("–", "-").replace("－", "-")
    _m_neg = _ACCT_NEG_RE.match(s)
    if _m_neg:
        s = "-" + _m_neg.group(1)
    for w in _LEAD_WORDS:
        if s.startswith(w):
            s = s[len(w):].strip()
            break
    m = _VALUE_RE.match(s)
    if not m:
        return None, None
    num_s = m.group("num").replace(",", "").replace("，", "")
    try:
        num = float(num_s)
    except ValueError:
        return None, None
    unit = (m.group("unit") or "").strip()
    if not unit:
        return num, None
    if unit in _PERCENT_UNITS:
        # 「3.5%」是 3.5 个百分点，不是 0.035：表头写「（%）」时单元格里就该是 3.5。
        # 原样保留成字符串会让整列变成文本，排序和求和都失效。
        return num, None
    # 单位里可能带「人民币」等后缀，归一掉
    unit = unit.replace("人民币", "").replace("RMB", "").replace("rmb", "").strip()
    if not unit:
        return num, None
    quant, scale = split_unit(unit)
    if quant is None and scale is None:
        # 有未知后缀，当作无单位数值，保留原字符串更安全
        return None, None
    return num, unit


def header_scale(header: str) -> Optional[float]:
    """表头单位声明的数量级倍率；表头无单位或只写量词（元/人）返回 None。"""
    tgt_unit = parse_header_unit(header)
    if not tgt_unit:
        return None
    _quant, scale = split_unit(tgt_unit)
    if not scale or scale == 1.0:
        return None
    return scale


def _base_unit_label(header: str) -> str:
    """表头单位对应的基本单位名，用于告警里给出两个候选口径。"""
    tgt_unit = parse_header_unit(header)
    quant, _scale = split_unit(tgt_unit) if tgt_unit else (None, None)
    return quant or "基本单位"


def is_scale_ambiguous(value: Any, header: str) -> bool:
    """源值能解析成数值但没标单位，而表头声明了数量级（万元/亿元/万人…）。

    这种组合下数值口径不可判定：`78661` 既可能是 78661 元，也可能是 78661 万元，
    相差一万倍，且数据本身不含任何可用于区分的信号。判定为「需人工确认」。
    """
    num, src_unit = parse_value_with_unit(value)
    if num is None or src_unit is not None:
        return False
    return header_scale(header) is not None


def has_quality_marker(value: Any) -> bool:
    """值是否带数据质量标记，如监测数据的「16(H)」「4985(PF)」「32*」。

    这类后缀不是单位、也不是「（现价）」这种口径注释，而是「这条读数可不可信」的标记。
    把它当注释剥掉，等于把一条异常读数伪装成正常值写进报表。
    """
    if not isinstance(value, str):
        return False
    return bool(_QUALITY_MARKER_RE.search(value.strip()))


def normalize_cell(value: Any, header: str) -> Tuple[Any, Optional[str]]:
    """按表头单位归一化单个单元格。

    返回 (写入值, 告警信息或 None)。告警用于提示「表头缺单位」「跨币种未换算」等情况。
    """
    num, src_unit = parse_value_with_unit(value)
    if num is None:
        # 解析不出数值（文本、图片标记等），原样返回
        return value, None

    if has_quality_marker(value):
        # 带质量标记的读数不做单位换算，也不抹标记，原样写入
        return value, None

    tgt_unit = parse_header_unit(header)

    if src_unit is None:
        # 源值没标单位。表头也没声明数量级时，写原值就是对的；
        # 但表头声明了数量级（如「万元」）时无法判定源值口径——
        # 78661 是 78661 元还是 78661 万元，差一万倍。此时不做任何换算，
        # 原样写入并由上层登记为「待人工确认」，绝不静默按表头单位吞掉。
        scale = header_scale(header)
        if scale is None:
            return num, None
        base = _base_unit_label(header)
        return value, (
            f"列「{header}」表头声明单位为「{tgt_unit}」，源值「{value}」是裸数字，"
            f"已按表头单位原样写入；若源值实际以「{base}」计，需先除以 {header_scale(header):.0f}"
        )

    src_quant, src_scale = split_unit(src_unit)

    if not tgt_unit:
        # 表头无单位：不猜口径，原样保留并告警
        return value, (
            f"列「{header}」表头未声明单位，源值「{value}」保留原样；"
            f"如需统一单位，请在表头补充单位标注，如「{header}（{src_unit}）」"
        )

    tgt_quant, tgt_scale = split_unit(tgt_unit)

    if tgt_quant is None and tgt_scale:
        # 表头只写了数量级（如「（万）」），量词沿用源值
        return num * src_scale / tgt_scale, None

    if src_quant is None:
        # 源值只有数量级没量词（如「1.26亿」），视为与表头量词一致
        if tgt_scale:
            return num * src_scale / tgt_scale, None
        return value, None

    if tgt_quant != src_quant:
        return value, (
            f"列「{header}」表头单位为「{tgt_unit}」，源值为「{value}」，"
            f"量词不同（{src_quant} vs {tgt_quant}）未换算，需人工确认口径"
        )

    return num * src_scale / tgt_scale, None


def collect_unit_warnings(headers: list) -> list:
    """检查表头是否声明单位，返回缺单位的列名列表。"""
    return [h for h in headers if h and parse_header_unit(h) is None]


# ---------------------------------------------------------------------------
# 口径待确认：列级聚合 + 用户决策回放
# ---------------------------------------------------------------------------
# 「源值无单位 + 表头带数量级」是数据本身无法判定的问题，逐单元格弹窗没有可操作性
# （一张几千行的表会产生几千个确认框），所以把决策单元收敛到「列」：
# 同一列在同一数据源里通常同口径，确认一次即可，确认结果按列名回放。

# 「基本单位」占位符：用于「亿」「万」这种只声明数量级、没写量词的目标单位。
# 换算到它等价于换算到量词继承的基本单位（元/人/份…），倍率为 1。
_BASE_UNIT = "__base__"

_OPTION_DESC = {
    "as_declared": "源值已是表头声明的单位，原值直接写入",
    "as_base": "源值是元/人/份等基本单位，先换算到表头单位再写入",
}


def _flatten_rows(rows_data: Any) -> List[dict]:
    """把 list / dict[sheet, list] 两种行容器摊平成行列表。"""
    if isinstance(rows_data, list):
        return [r for r in rows_data if isinstance(r, dict)]
    if isinstance(rows_data, dict):
        out: List[dict] = []
        for v in rows_data.values():
            if isinstance(v, list):
                out.extend(r for r in v if isinstance(r, dict))
        return out
    return []


def _row_lookup(row: dict, col: str) -> Tuple[bool, Any]:
    """按模板列名取源值：先精确，再忽略空格/全半角括号做归一化匹配。"""
    if col in row:
        return True, row.get(col)
    cn = re.sub(r"[\s（）()\u3000]", "", str(col).lower())
    for k, v in row.items():
        if re.sub(r"[\s（）()\u3000]", "", str(k).lower()) == cn:
            return True, v
    return False, None


def collect_unit_confirmations(rows_data: Any, col_names: List[str], max_samples: int = 3) -> List[dict]:
    """扫描待写入数据，找出「表头声明数量级、源值却没标单位」的列。

    返回每个待确认列的上下文（影响行数、样例值、可选口径），供前端一次性确认。
    没有待确认列时返回空列表。
    """
    rows = _flatten_rows(rows_data)
    if not rows or not col_names:
        return []

    buckets: Dict[str, dict] = {}
    for col in col_names:
        tgt_unit = parse_header_unit(col)
        if not tgt_unit:
            continue
        quant, scale = split_unit(tgt_unit)
        if not scale or scale == 1.0:
            # 表头单位本身就是基本单位（元/人/份），写原值即正确，无需确认
            continue
        base = quant or "基本单位"
        buckets[col] = {
            "column": col,
            "header_unit": tgt_unit,
            "base_unit": base,
            "target_scale": scale,
            "affected_rows": 0,
            "numeric_rows": 0,
            "samples": [],
            "options": [
                {"key": "as_declared", "label": f"按表头单位（{tgt_unit}）", "desc": _OPTION_DESC["as_declared"]},
                {"key": "as_base", "label": f"按基本单位（{base}）换算", "desc": _OPTION_DESC["as_base"]},
            ],
        }
    if not buckets:
        return []

    for row in rows:
        for col, b in buckets.items():
            hit, val = _row_lookup(row, col)
            if not hit:
                continue
            num, src_unit = parse_value_with_unit(val)
            if num is None:
                continue  # 文本/空值/图片标记，不参与口径判定
            b["numeric_rows"] += 1
            if src_unit is not None:
                continue  # 源值自带单位，可自动换算，不属于待确认
            b["affected_rows"] += 1
            if len(b["samples"]) < max_samples:
                sv = str(val).strip()
                if sv not in b["samples"]:
                    b["samples"].append(sv)

    result = [b for b in buckets.values() if b["affected_rows"] > 0]
    result.sort(key=lambda x: x["affected_rows"], reverse=True)
    return result


def apply_unit_overrides(rows_data: Any, col_names: List[str], overrides: Dict[str, str]) -> Tuple[Any, List[dict]]:
    """按用户的列级口径决策重写数据。

    overrides: {列名: "as_declared" | "as_base"}。
    - as_declared：源值即表头单位，不改动；
    - as_base：源值是基本单位，除以数量级倍率换算到表头单位。
    返回 (新 rows_data, 已应用项列表)；未提供决策的列保持原样。
    """
    if not overrides or not rows_data:
        return rows_data, []

    scale_map: Dict[str, float] = {}
    applied: List[dict] = []
    for col in col_names:
        decision = (overrides.get(col) or "").strip()
        if decision != "as_base":
            continue
        scale = header_scale(col)
        if not scale:
            continue
        scale_map[col] = scale
        applied.append({"column": col, "decision": decision, "scale": scale})
    if not scale_map:
        return rows_data, []

    def _convert_row(row: dict) -> dict:
        new_row = dict(row)
        for col, scale in scale_map.items():
            hit, val = _row_lookup(new_row, col)
            if not hit:
                continue
            num, src_unit = parse_value_with_unit(val)
            if num is None or src_unit is not None:
                continue  # 解析不了或自带单位（自带单位由 normalize_cell 正常换算）
            conv = num / scale
            new_row[col] = int(conv) if conv == int(conv) else conv
        return new_row

    if isinstance(rows_data, list):
        return [_convert_row(r) if isinstance(r, dict) else r for r in rows_data], applied
    if isinstance(rows_data, dict):
        out = {}
        for k, v in rows_data.items():
            out[k] = [_convert_row(r) if isinstance(r, dict) else r for r in v] if isinstance(v, list) else v
        return out, applied
    return rows_data, applied


# ---------------------------------------------------------------------------
# 表头无单位：以「文中单位」为准
# ---------------------------------------------------------------------------
# 旧策略是「表头没单位就原样保留 + 告警」。后果是整列保持 `7.3万元` / `4551` 混排，
# 用户拿到表既不能排序也不能求和——C 用例 2570 行整列都是文本，业务上等于不可用。
# 新策略：表头没单位时，从该列源值里统计主单位（自带单位的值按出现次数投票），
# 把整列换算到主单位后写纯数值。代价是「裸数字按主单位理解」这个假设必须留痕。


def infer_column_unit(values: List[Any], min_votes: int = 2, dominance: float = 0.6) -> Optional[str]:
    """统计一列源值里出现最多的单位，作为该列的主单位。

    只统计**自带单位**的值——裸数字不含任何口径信息，不能参与投票。
    投票数不足（< min_votes）或最高票占比不足（< dominance）时返回 None，
    宁可放弃归一化，也不要在两个单位势均力敌时硬选一个。
    """
    votes: Dict[str, int] = {}
    for v in values:
        if has_quality_marker(v):
            continue
        _num, unit = parse_value_with_unit(v)
        if not unit:
            continue
        votes[unit] = votes.get(unit, 0) + 1
    if not votes:
        return None
    total = sum(votes.values())
    best, n = max(votes.items(), key=lambda kv: kv[1])
    if n < min_votes or n / total < dominance:
        return None
    return best


def parse_target_units(requirement: str) -> List[Tuple[str, str]]:
    """从用户要求里解析「明确指定的目标单位」，返回 [(单位, 所在分句)]。

    只认「以亿元为单位」「换算成万元」「单位统一用亿元」这类显式说法；
    候选词必须能被 split_unit 解析成单位，否则丢弃——
    「以GDP为核心」里的「GDP」不会被误当成单位。
    """
    if not requirement:
        return []
    out: List[Tuple[str, str]] = []
    clauses = re.split(r"[，。；;、\n]", str(requirement))
    # 单位候选只允许「数量级前缀 + 已登记量词」，不做宽松的「任意 1~4 字」匹配——
    # 后者会把「以亿元为单位」里的「亿元为单位」整体吞掉，也会把「以GDP为核心」里的 GDP 当单位。
    quant_alt = "|".join(sorted(_QUANTIFIERS, key=len, reverse=True))
    pat = re.compile(
        r"(?:以|按|用|统一(?:为|用|成)?|换算(?:成|为|到)?|转换(?:成|为)?|单位(?:统一)?(?:用|为|是|按)?)"
        r"\s*(?P<unit>[万亿]{0,2}(?:" + quant_alt + r"))"
        r"\s*(?:为单位|单位|填|计|算|换算|统计|展示|显示|输出)?"
    )
    for clause in clauses:
        for m in pat.finditer(clause):
            cand = (m.group("unit") or "").strip()
            quant, scale = split_unit(cand)
            if quant is None and not scale:
                continue
            if (cand, clause) not in out:
                out.append((cand, clause))
    return out


def normalize_rows_by_source_unit(
    rows_data: Any,
    col_names: List[str],
    user_targets: Optional[Dict[str, str]] = None,
) -> Tuple[Any, List[str]]:
    """把「表头无单位」的列按文中主单位换算成纯数值。

    - 表头**有**单位的列不动：写入时由 normalize_cell 按表头换算，避免二次换算；
    - 表头无单位、源值自带单位的列：按主单位换算；
    - 用户显式指定目标单位（user_targets）时优先，即使与表头冲突也以用户为准并告警。

    返回 (改写后的 rows_data, 说明列表)。裸数字按主单位理解这一假设会写进说明。
    """
    rows = _flatten_rows(rows_data)
    if not rows or not col_names:
        return rows_data, []

    notes: List[str] = []
    plan: Dict[str, Tuple[str, str]] = {}  # 列 -> (目标单位, 来源)
    for col in col_names:
        user_unit = (user_targets or {}).get(col)
        header_unit = parse_header_unit(col)
        if user_unit:
            plan[col] = (user_unit, "user")
            if header_unit and header_unit != user_unit:
                notes.append(
                    f"列「{col}」表头声明单位为「{header_unit}」，已按用户要求改为「{user_unit}」，"
                    f"表头文字与实际数值口径不一致，建议同步修改表头"
                )
            continue
        if header_unit:
            continue
        vals = [_row_lookup(r, col)[1] for r in rows if _row_lookup(r, col)[0]]
        inferred = infer_column_unit(vals)
        if not inferred:
            continue
        # 同列里既有「7.3万元」又有裸数字「78661」时，不能把前者换算成「万元」再和后者并排——
        # 那会得到 7.3 与 78661 并列，读者会以为差 1 万倍。裸数字按惯例是基本单位（元/人/份），
        # 所以这种混排列统一换算到**基本单位**，两边量级才对得上。
        has_bare = any(
            parse_value_with_unit(v)[0] is not None and parse_value_with_unit(v)[1] is None
            for v in vals
        )
        if has_bare:
            quant, scale = split_unit(inferred)
            if scale and scale > 1:
                # 「亿」「万」这类只写数量级、没写量词的，量词继承源值，用 __base__ 表示「基本单位」
                plan[col] = (quant or _BASE_UNIT, "inferred_base")
                continue
        plan[col] = (inferred, "inferred")
    if not plan:
        return rows_data, []

    stats: Dict[str, Dict[str, int]] = {
        c: {"converted": 0, "bare": 0, "skipped": 0} for c in plan
    }

    def _convert_row(row: dict) -> dict:
        new_row = dict(row)
        for col, (target, _src) in plan.items():
            hit, val = _row_lookup(new_row, col)
            if not hit:
                continue
            if has_quality_marker(val):
                stats[col]["skipped"] += 1
                continue
            num, src_unit = parse_value_with_unit(val)
            if num is None:
                stats[col]["skipped"] += 1
                continue
            tgt_quant, tgt_scale = split_unit(target)
            if target == _BASE_UNIT:
                tgt_quant, tgt_scale = None, 1.0
            if src_unit is None:
                # 裸数字：按主单位理解，原值不动（它已经以该单位计）
                stats[col]["bare"] += 1
                continue
            src_quant, src_scale = split_unit(src_unit)
            if tgt_quant and src_quant and tgt_quant != src_quant:
                # 元 vs 美元这种跨量词，没有汇率不能换算
                stats[col]["skipped"] += 1
                continue
            if not tgt_scale:
                continue
            conv = num * src_scale / tgt_scale
            conv = round(conv, 6)  # 0.58亿 → 57999999.99999999，不收敛会写进脏数
            new_row[col] = int(conv) if conv == int(conv) else conv
            stats[col]["converted"] += 1
        return new_row

    if isinstance(rows_data, list):
        out: Any = [_convert_row(r) if isinstance(r, dict) else r for r in rows_data]
    elif isinstance(rows_data, dict):
        out = {}
        for k, v in rows_data.items():
            out[k] = [_convert_row(r) if isinstance(r, dict) else r for r in v] if isinstance(v, list) else v
    else:
        return rows_data, []

    for col, (target, src) in plan.items():
        s = stats[col]
        if not s["converted"] and not s["bare"]:
            if s["skipped"]:
                notes.append(
                    f"列「{col}」要求统一为「{target}」，但 {s['skipped']} 个值因量词不同"
                    f"（如人民币 vs 美元）或无法解析而未换算，该列仍保留原值，需人工确认口径"
                )
            continue
        why = {"user": "用户指定", "inferred_base": "文中单位并换算到基本单位", "inferred": "文中主单位"}.get(src, "文中主单位")
        if target == _BASE_UNIT:
            notes.append(
                f"列「{col}」表头未声明单位，该列混有带单位与未标单位的值，已统一换算到基本单位："
                f"换算 {s['converted']} 个带单位值，{s['bare']} 个未标单位的值按基本单位理解"
                + (f"，{s['skipped']} 个值因量词不同或非数值未处理" if s["skipped"] else "")
            )
            continue
        notes.append(
            f"列「{col}」表头未声明单位，已按{why}「{target}」统一："
            f"换算 {s['converted']} 个带单位值，{s['bare']} 个未标单位的值按「{target}」理解"
            + (f"，{s['skipped']} 个值因量词不同或非数值未处理" if s["skipped"] else "")
        )
    return out, notes
