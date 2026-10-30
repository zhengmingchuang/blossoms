import re
from typing import Dict, List, Any, Optional
from services.llm_service import extract_fields

PHONE_RE = re.compile(r'(?<!\d)(?:1[3-9]\d{9}|0\d{2,3}-?\d{7,8})(?!\d)')
EMAIL_RE = re.compile(r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}')
DATE_RE = re.compile(r'\d{4}[-/年.]\d{1,2}[-/月.]\d{1,2}[日号]?')
ID_RE = re.compile(r'(?<!\d)\d{17}[\dXx](?!\d)')

MONEY_RE = re.compile(
    r'(?:人民币|￥|¥|RMB|USD|\$|€|£)?\s*'
    r'(\d{1,3}(?:[,，]\d{3})*(?:\.\d{1,4})?)\s*'
    r'(万亿元|亿元|万元|元|万亿|亿|万|美元|欧元|英镑|日元)?'
)
ADDR_RE = re.compile(
    r'(?:[\u4e00-\u9fff]{2,8}(?:省|自治区))?'
    r'(?:[\u4e00-\u9fff]{2,8}(?:市|地区|自治州|盟))?'
    r'(?:[\u4e00-\u9fff]{2,8}(?:区|县|旗|市))?'
    r'(?:[\u4e00-\u9fff]{2,20}(?:街道|镇|乡|路|街|巷|号|弄|栋|楼|室|层|单元))'
)
ORG_RE = re.compile(
    r'[\u4e00-\u9fff\w]{2,20}'
    r'(?:有限公司|股份有限公司|集团|公司|研究院|研究所|大学|学院|医院|银行|局|厅|部|委员会|中心|协会|基金会)'
)
URL_RE = re.compile(r'https?://[^\s<>"\'）)]+')
POSTCODE_RE = re.compile(r'(?<!\d)\d{6}(?!\d)')
PLATE_RE = re.compile(r'[京津沪渝冀豫云辽黑湘皖鲁新苏浙赣鄂桂甘晋蒙陕吉闽贵粤川青藏琼宁][A-HJ-NP-Z][A-HJ-NP-Z0-9]{4,5}[A-HJ-NP-Z0-9挂学警港澳]')
SOCIAL_CREDIT_RE = re.compile(r'[0-9A-HJ-NP-RTUW]{2}\d{6}[0-9A-HJ-NP-RTUW]{10}')

STAT_RE = re.compile(
    r'([\u4e00-\u9fff\w]{2,20})'
    r'(?:为|达到|达|共|共有|共计|约|累计|增长到|下降到|超过|接近|不足)?'
    r'\s*'
    r'(\d+(?:\.\d+)?)\s*'
    r'(亿元|万亿元|万元|元|亿美元|万美元|美元|亿人|万人|亿人次|万人次|人次|人|'
    r'亿吨|万吨|吨|亿千瓦时|万千瓦时|千瓦时|亿度|万度|'
    r'万辆|辆|万台|台|万套|套|万个|个|万家|家|万户|户|万件|件|万项|项|'
    r'万平方米|平方米|万平方公里|平方公里|万公里|公里|万亩|亩|万公顷|公顷|'
    r'万册|册|万册次|册次|万部|部|万场|场|'
    r'亿册|亿次|亿件|万次|次|万所|所|万座|座|'
    r'万张|张|万间|间|万条|条|万处|处|'
    r'%|个百分点|倍|岁|天|年|月|小时|分钟)')

# 值在遇到「空格|空格」时结束：与 document_parser 中 Excel 行序列化格式一致，避免整行多列被当成单个字段值
KV_RE = re.compile(
    r'([\u4e00-\u9fff\w]{2,25}?)'
    r'[：:]\s*'
    r'(.{1,100}?)(?=\s+\|\s+|[。；\n]|$)')

PERCENT_RE = re.compile(
    r'([\u4e00-\u9fff\w]{2,20})'
    r'(?:为|占|达到|达|约|超过)?'
    r'\s*'
    r'(\d+(?:\.\d+)?)\s*%')

GROWTH_RE = re.compile(
    r'([\u4e00-\u9fff\w]{2,20})'
    r'(?:同比|环比|较上年|比上年)'
    r'(?:增长|下降|增加|减少|上升|提高|降低)'
    r'\s*'
    r'(\d+(?:\.\d+)?)\s*'
    r'(%|个百分点)?')


def rule_extract(text: str) -> Dict[str, Any]:
    out = {}
    phones = list(set(PHONE_RE.findall(text)))
    if phones:
        out["电话"] = phones[0] if len(phones) == 1 else phones
    emails = list(set(EMAIL_RE.findall(text)))
    if emails:
        out["邮箱"] = emails[0] if len(emails) == 1 else emails
    dates = list(set(DATE_RE.findall(text)))
    if dates:
        out["日期"] = dates[0] if len(dates) == 1 else dates
    ids = list(set(ID_RE.findall(text)))
    if ids:
        out["身份证号"] = ids[0] if len(ids) == 1 else ids

    # 提取表头信息（xlsx 解析后的格式）
    header_match = re.findall(r'\[.+?\]\s*表头:\s*(.+)', text)
    if header_match:
        cols = [c.strip() for c in header_match[0].split("|")]
        out["数据列名"] = ", ".join(cols)
    
    # 提取数据概览
    overview_match = re.findall(r'数据概览:\s*共(\d+)行,\s*(\d+)列', text)
    if overview_match:
        out["数据行数"] = overview_match[0][0]
        out["数据列数"] = overview_match[0][1]

    for m in STAT_RE.finditer(text[:30000]):
        key, val, unit = m.group(1), m.group(2), m.group(3)
        key = key.lstrip("，。、；的了和与在有为到")
        if len(key) < 2 or key in ("其中", "分别", "同比", "环比", "比上", "全年", "全国", "截至", "共有", "约为", "其中", "同时"):
            continue
        out[key] = f"{val}{unit}"

    for m in PERCENT_RE.finditer(text[:30000]):
        key, val = m.group(1), m.group(2)
        key = key.lstrip("，。、；的了和与在有为到")
        if len(key) >= 2 and key not in out:
            out[key] = f"{val}%"

    for m in GROWTH_RE.finditer(text[:20000]):
        key, val = m.group(1), m.group(2)
        unit = m.group(3) or "%"
        gkey = f"{key}增速"
        key = key.lstrip("，。、；的了和与在有为到")
        if len(key) >= 2:
            out[gkey] = f"{val}{unit}"

    kv_count = 0
    for m in KV_RE.finditer(text[:20000]):
        if kv_count > 50:
            break
        key, val = m.group(1).strip(), m.group(2).strip()
        if len(key) < 2 or len(key) > 20 or len(val) < 1 or len(val) > 100:
            continue
        if key not in out and val:
            out[key] = val
            kv_count += 1

    # 地址
    addrs = list(set(ADDR_RE.findall(text[:20000])))
    addrs = [a for a in addrs if len(a) >= 6]
    if addrs:
        out["地址"] = addrs[0] if len(addrs) == 1 else addrs

    # 金额
    money_matches = MONEY_RE.findall(text[:20000])
    moneys = []
    for num, unit in money_matches:
        num_clean = num.replace(",", "").replace("，", "")
        try:
            float(num_clean)
        except ValueError:
            continue
        if unit:
            moneys.append(f"{num}{unit}")
        elif float(num_clean) >= 100:
            moneys.append(num)
    moneys = list(set(moneys))[:10]
    if moneys:
        out["金额"] = moneys[0] if len(moneys) == 1 else moneys

    # 组织机构
    orgs = list(set(ORG_RE.findall(text[:20000])))
    orgs = [o for o in orgs if len(o) >= 4][:10]
    if orgs:
        out["组织机构"] = orgs[0] if len(orgs) == 1 else orgs

    # URL
    urls = list(set(URL_RE.findall(text[:10000])))[:5]
    if urls:
        out["网址"] = urls[0] if len(urls) == 1 else urls

    # 统一社会信用代码
    credits = list(set(SOCIAL_CREDIT_RE.findall(text[:20000])))
    if credits:
        out["统一社会信用代码"] = credits[0] if len(credits) == 1 else credits

    # 邮编
    postcodes = list(set(POSTCODE_RE.findall(text[:10000])))
    postcodes = [p for p in postcodes if not any(p == str(d) for d in dates)] if dates else postcodes
    if postcodes:
        out["邮编"] = postcodes[0] if len(postcodes) == 1 else postcodes

    # 车牌号
    plates = list(set(PLATE_RE.findall(text[:10000])))
    if plates:
        out["车牌号"] = plates[0] if len(plates) == 1 else plates

    return out


async def hybrid_extract(text: str, fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """混合提取：规则 + LLM，LLM 结果优先覆盖规则结果（更准确）。"""
    rule = rule_extract(text)
    llm = await extract_fields(text, fields=fields)
    merged = dict(rule)
    for k, v in llm.items():
        if not k.startswith("_"):
            if isinstance(v, str) and not v.strip():
                continue
            merged[k] = v
    return merged
