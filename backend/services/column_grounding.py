# -*- coding: utf-8 -*-
"""列名接地检查：模板要的字段，源文里到底有没有。

为什么需要这一层：
    覆盖率闸门只能发现「没填满」，发现不了「填满了但填错」。
    实测用例：模板是「产品名称 / 单价 / 库存量」，数据源是 COVID 各国疫情表，
    模型把 Belgium 填成产品名、把确诊数填成单价、把人口填成库存量，
    三列全满、覆盖率 100%、填后审查也 100% 命中（数字确实在源文里），
    但整张表对用户毫无意义。这类错误必须按「列语义」拦，不能按「格数」拦。

判定口径（刻意宽松，只拦「完全无交集」）：
    列名的候选词（原名 / 去括号 / 去尾部装饰词 / 同义词组成员）
    只要有一个作为整词子串出现在源文里，就算这一列「接地」。
    所有列都不接地才拒绝；有一列能对上就放行，由覆盖率闸门继续管其余列。
"""
import re
from typing import Dict, List, Tuple

_BRACKET_RE = re.compile(r"[（(][^）)]*[）)]")
_NON_WORD_RE = re.compile(r"[\s\u3000:：/\\|,，.。;；'\"“”‘’]+")

# 尾部装饰词：去掉之后才是列名的「主词」
_TAIL_DECOR = (
    "名称", "数量", "总量", "总计", "合计", "小计", "数值", "数据",
    "明细", "情况", "统计值", "统计", "值", "数", "量", "名",
)

# 同义词组：模板与源文用不同说法时，靠组内成员互相兜底
_SYNONYM_GROUPS: List[set] = [
    {"gdp", "地区生产总值", "生产总值", "国内生产总值", "地区gdp"},
    {"人口", "常住人口", "总人口", "户籍人口", "人口数", "年末常住人口"},
    {"确诊", "累计确诊", "确诊病例", "累计确诊病例", "感染", "病例", "感染者"},
    {"死亡", "死亡病例", "累计死亡", "病亡", "死亡人数"},
    {"治愈", "治愈病例", "累计治愈", "康复", "治愈人数"},
    {"城市", "城市名", "城市名称", "地市", "地级市", "city"},
    {"省份", "省", "省级", "自治区", "特别行政区", "省份名称", "province"},
    {"国家", "国家地区", "国别", "地区", "country", "region"},
    {"日期", "时间", "统计日期", "监测日期", "统计时间", "date", "time"},
    {"单价", "价格", "售价", "unitprice", "price"},
    {"库存", "库存量", "存货", "库存数量", "stock"},
    {"产品", "产品名称", "商品", "商品名称", "品名", "product"},
    {"产量", "生产量", "产出", "output"},
    {"销量", "销售量", "销售额", "sales"},
]


def _norm(text: str) -> str:
    return _NON_WORD_RE.sub("", str(text or "")).lower()


def _strip_brackets(name: str) -> str:
    return _BRACKET_RE.sub("", str(name or "")).strip()


def _candidates(col_name: str) -> List[str]:
    """列名的候选词，按「越具体越靠前」排列。"""
    base = _norm(_strip_brackets(col_name))
    if not base:
        return []

    cands: List[str] = [base]
    # 去尾部装饰词（可能叠加，如「产品名称」→「产品」）
    cur = base
    for _ in range(2):
        for dec in sorted(_TAIL_DECOR, key=len, reverse=True):
            if cur.endswith(dec) and len(cur) - len(dec) >= 2:
                cur = cur[: -len(dec)]
                cands.append(cur)
                break
        else:
            break

    # 同义词组：候选命中任意组员，就把整组都算进来
    for cand in list(cands):
        for grp in _SYNONYM_GROUPS:
            if cand in grp:
                cands.extend(_norm(g) for g in grp)
                break

    # 去重保序，过滤空串
    out: List[str] = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def _bigrams(s: str) -> List[str]:
    return [s[i:i + 2] for i in range(len(s) - 1)]


def _ground_one(col_name: str, src_norm: str) -> Tuple[bool, str]:
    cands = _candidates(col_name)
    for cand in cands:
        # 单字候选噪声太大（如「值」），要求至少 2 个字符
        if len(cand) < 2:
            continue
        if cand in src_norm:
            return True, cand

    # 强匹配全落空时，用 2-gram 覆盖率兜一层，专门救「表述略有出入」的列名：
    # 「2020年确诊」vs 源文「2020 年累计确诊」整词对不上，但 2-gram 命中 5/6。
    # 阈值 0.6 是为了挡住「产品名称」这类只蹭到「名称」两个字的假命中（1/3=0.33）。
    for cand in cands:
        if len(cand) < 3 or not re.search(r"[\u4e00-\u9fff]", cand):
            continue
        grams = _bigrams(cand)
        if not grams:
            continue
        hit = sum(1 for g in grams if g in src_norm)
        if hit / len(grams) >= 0.6:
            return True, f"~{cand}"
    return False, ""


# 维度列（行标识）：城市 / 省份 / 名称 / 日期 这类列，列名本身往往不在源文里出现
# （源文写「杭州市」，不会写「城市」），但它的值确实来自源文。
# 这类列不能按列名接地判断，否则会被误判成「源文没有」而清空整列实体名。
_DIMENSION_WORDS = {
    "城市", "城市名", "城市名称", "地市", "地级市", "省份", "省份名称", "省", "省级",
    "国家", "国家地区", "国别", "地区", "区域", "州", "县", "区",
    "日期", "时间", "统计日期", "监测日期", "年份", "年度", "月份", "季度",
    "编号", "序号", "代码", "编码", "id", "city", "province", "country", "region", "date", "time",
}
_DIMENSION_SUFFIX = (
    "名称", "姓名", "品名", "站名", "城市", "省份", "地区", "国家", "编号", "代码",
    "类型", "类别", "分类", "日期", "时间", "年份", "单位",
)
# 出现这些字样说明是「量」，即使名字里带「城市」也不算维度列（如「城市人口」）
_MEASURE_HINTS = (
    "值", "量", "数", "额", "率", "比", "占比", "总量", "浓度", "指数", "天数",
    "人口", "gdp", "收入", "支出", "产量", "销量", "单价", "价格", "库存", "面积",
    "温度", "湿度", "增速", "增幅", "得分", "排名",
)


def is_dimension_column(col_name: str) -> bool:
    """这一列是「行标识」（城市/省份/名称/日期…）还是「量」（GDP/人口/单价…）。"""
    plain = _norm(_strip_brackets(col_name))
    if not plain:
        return False
    if plain in _DIMENSION_WORDS:
        return True
    if any(h in plain for h in _MEASURE_HINTS):
        return False
    return any(plain.endswith(suf) for suf in _DIMENSION_SUFFIX)


def assess_columns(col_names: List[str], source_text: str) -> Dict:
    """逐列判断能否在源文里找到语义落点。

    返回 {total, grounded: [{column, hit}], ungrounded: [列名],
          ungrounded_measures: [列名], dimension: [列名], ratio}
    ratio 只按「量列」计算：维度列的列名天然不会出现在源文里（源文写「杭州市」不写「城市」），
    把它们计入分母会让正常模板被判成「数据源不匹配」。
    没有量列（整张表都是城市/日期这类）时不拦，交给填后审查逐格核对。
    """
    src_norm = _norm(source_text)
    grounded: List[Dict] = []
    ungrounded: List[str] = []
    dimension: List[str] = []
    for cn in col_names:
        _plain = _norm(_strip_brackets(cn))
        if not _plain:
            continue
        if len(_plain) <= 1:
            grounded.append({"column": cn, "hit": _plain})
            continue
        ok, hit = _ground_one(cn, src_norm)
        if ok:
            grounded.append({"column": cn, "hit": hit})
        else:
            ungrounded.append(cn)
            if is_dimension_column(cn):
                dimension.append(cn)

    ungrounded_measures = [c for c in ungrounded if c not in dimension]
    measures = [c for c in col_names if str(c or "").strip() and not is_dimension_column(c)]
    if measures:
        _hit = sum(1 for c in measures if c not in ungrounded_measures)
        ratio = _hit / len(measures)
    else:
        ratio = 1.0 if ungrounded else 0.0
    total = len(grounded) + len(ungrounded)
    return {
        "total": total,
        "grounded": grounded,
        "ungrounded": ungrounded,
        "ungrounded_measures": ungrounded_measures,
        "dimension": dimension,
        "ratio": ratio,
    }
