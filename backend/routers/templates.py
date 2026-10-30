import uuid
import json
import time
import re
import unicodedata
import asyncio
import traceback
import logging
import hashlib
import os
from collections import OrderedDict
from pathlib import Path
from typing import List, Optional, Dict, Tuple
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import or_
from sqlalchemy.orm import Session
from database import get_db
from models import TemplateInfo, TemplateMapping, FileInfo, ExtractResult, TaskInfo, OperationLog, User, fmt_time
from config import UPLOAD_DIR, EXPORT_DIR
from routers.auth import get_current_user
from services.template_filler import identify_fields, fill_template, fill_table_template
from services.fusion import fuse
from services.vector_store import get_context, search_for_fields
from services.llm_service import match_fields, call_llm
from services.data_filter import (
    load_xlsx_as_table, load_docx_tables, find_best_data_source, try_weak_table_source,
    parse_filter_conditions, parse_filter_with_llm,
    filter_data, map_columns, sort_data, sort_data_by_first_numeric_column,
    _is_city_level_header,
    _is_province_level_header,
)

logger = logging.getLogger("templates")

# 单批提取：max_tokens 需足够容纳大量行的 JSON 输出（100 城市 ≈ 10000+ tokens）
_TABLE_FILL_BATCH_MAX_TOKENS = int(os.getenv("TABLE_FILL_BATCH_MAX_TOKENS", "16000"))
_TABLE_FILL_GAP_MAX_TOKENS = int(os.getenv("TABLE_FILL_GAP_MAX_TOKENS", os.getenv("TABLE_FILL_LLM_MAX_TOKENS", "24000")))
# LLM 调用 wait_for 超时（秒）；小批默认略短，避免单批卡死拖长总时间
_TABLE_FILL_LLM_TIMEOUT_SEC = int(os.getenv("TABLE_FILL_LLM_TIMEOUT_SEC", "150"))
# 向量片段最低分，过滤噪声（过滤后为空则回退）
_TABLE_FILL_CHUNK_SCORE_MIN = float(os.getenv("TABLE_FILL_CHUNK_SCORE_MIN", "0.05"))
# LLM 分批提取并发上限（Semaphore），避免打满 API 限流
_TABLE_FILL_LLM_CONCURRENCY = max(1, int(os.getenv("TABLE_FILL_LLM_CONCURRENCY", "8")))


def _llm_stopped_by_output_limit(stop_reason: str) -> bool:
    """Anthropic: max_tokens；OpenAI: length。"""
    s = (stop_reason or "").strip().lower()
    return s in ("max_tokens", "length")


def _strip_llm_row_meta(rows: list) -> None:
    """写入 Excel 前去掉内部分批元数据。"""
    for row in rows:
        if not isinstance(row, dict):
            continue
        row.pop("_llm_batch_support", None)
        row.pop("_llm_from_gap", None)


def _compute_llm_multi_batch_agreement(rows: list, nb_nonempty_batches: int) -> float:
    """按实体在多批中的出现比例求平均，作为表格级多批一致性 [0,1]。
    单批提取时返回 1.0（无需跨批一致性校验）。
    多批分工提取（各批提取不同实体、无重叠）时也返回高分，因为这是正常的分片行为。"""
    if not rows or nb_nonempty_batches < 1:
        return 0.75
    if nb_nonempty_batches == 1:
        return 1.0
    nb = max(1, nb_nonempty_batches)
    scores = []
    gap_count = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        gap = row.get("_llm_from_gap")
        sup = int(row.get("_llm_batch_support") or 0)
        if gap:
            scores.append(0.45)
            gap_count += 1
        else:
            scores.append(min(sup, nb) / nb)
    if not scores:
        return 0.75
    raw_avg = sum(scores) / len(scores)
    # 当多批分工提取（每个实体只在 1 批出现）且总行数充足时，
    # 这是正常的分片行为而非不一致，给予高分
    non_gap_rows = len(scores) - gap_count
    if non_gap_rows > 0 and raw_avg < 0.5:
        coverage = non_gap_rows / max(non_gap_rows, 1)
        if coverage >= 0.8:
            return max(raw_avg, 0.85)
    return raw_avg


# 缓存 parse_filter_with_llm 的结果（LRU 防内存无限增长）
_FILTER_CACHE_MAX = int(os.getenv("TABLE_FILL_FILTER_CACHE_MAX", "128"))
_filter_cache: "OrderedDict[str, Dict]" = OrderedDict()
_LLM_FALLBACK_MIN_SCORE = 0.35
_WEAK_FALLBACK_CONFIDENCE_CAP = float(os.getenv("TABLE_FILL_WEAK_CONFIDENCE_CAP", "0.65"))


def _filter_cache_get(key: str) -> Optional[Dict]:
    if key not in _filter_cache:
        return None
    val = _filter_cache[key]
    _filter_cache.move_to_end(key)
    return val


def _filter_cache_set(key: str, val: Dict) -> None:
    _filter_cache[key] = val
    _filter_cache.move_to_end(key)
    while len(_filter_cache) > _FILTER_CACHE_MAX:
        _filter_cache.popitem(last=False)
_LLM_FALLBACK_CONFIDENCE_CAP = 0.95


def _make_export_filename(template_name: str, export_dir: Path, prefix: str = "") -> str:
    """生成导出文件名：基于模板原名，与 exports 目录下已有文件重名时追加（副本1）（副本2）…

    返回纯文件名（不含目录），如 "2025年报告.xlsx" 或 "2025年报告（副本2）.xlsx"。
    prefix 可选，如 "filled_" 等前缀；留空则不加前缀。
    """
    stem = Path(template_name).stem
    suffix = Path(template_name).suffix or ".xlsx"
    if prefix:
        base = f"{prefix}{stem}"
    else:
        base = stem
    candidate = f"{base}{suffix}"
    if not (export_dir / candidate).exists():
        return candidate
    idx = 1
    while True:
        candidate = f"{base}（副本{idx}）{suffix}"
        if not (export_dir / candidate).exists():
            return candidate
        idx += 1


def _safe_json_preview(obj: object, limit: int = 300) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return str(obj)[:limit]


def _calc_confidence(
    col_name: str,
    rows: list,
    col_mapping: dict = None,
    match_score: float = None,
    mode: str = "table",
    llm_multi_batch_agreement: Optional[float] = None,
) -> float:
    """多维度加权计算置信度。
    
    mode="table": 表格直接匹配模式
    mode="llm":   LLM 提取模式
    返回 0-1 的置信度分数
    """
    if not rows:
        return 0.0

    # --- 维度1: 非空率 ---
    values = [row.get(col_name) for row in rows]
    non_null = [v for v in values if v is not None and str(v).strip() and str(v).strip() not in ("None", "null", "nan")]
    fill_rate = len(non_null) / len(values) if values else 0
    if fill_rate == 0:
        return 0.0

    # --- 维度2: 列名匹配度 ---
    col_match_score = 0.7  # 默认模糊
    if col_mapping:
        src_col = col_mapping.get(col_name, "")
        if src_col:
            src_lower = src_col.lower().replace(" ", "").replace("（", "(").replace("）", ")")
            tgt_lower = col_name.lower().replace(" ", "").replace("（", "(").replace("）", ")")
            if src_lower == tgt_lower:
                col_match_score = 1.0  # 精确匹配
            elif src_lower in tgt_lower or tgt_lower in src_lower:
                col_match_score = 0.9  # 包含关系
            else:
                col_match_score = 0.75  # 模糊匹配（语义近似）
        else:
            col_match_score = 0.55  # 未映射到数据源列

    # --- 维度3: 数据类型一致性 ---
    # 判断列名暗示的类型，检查实际值是否匹配
    sample = non_null[:50]
    numeric_hints = ("数", "量", "GDP", "人口", "收入", "指数", "值", "率", "额", "价", "面积", "万", "亿", "元")
    expects_numeric = any(h in col_name for h in numeric_hints)
    
    if expects_numeric:
        numeric_count = sum(1 for v in sample if _is_numeric(str(v)))
        type_score = numeric_count / len(sample) if sample else 0
    else:
        # 文本列：只要不是全空就算一致
        type_score = 1.0 if sample else 0

    # --- 维度4: 数据一致性（同列格式是否统一）---
    if len(sample) >= 3:
        # 检查值的"类型签名"是否统一（全数字 / 全文本 / 混合）
        sigs = [_value_signature(str(v)) for v in sample]
        most_common = max(set(sigs), key=sigs.count)
        consistency = sigs.count(most_common) / len(sigs)
    else:
        consistency = 0.95  # 样本太少，倾向认为一致

    # --- 维度5: 值合理性 ---
    lengths = [len(str(v).strip()) for v in sample]
    avg_len = sum(lengths) / len(lengths) if lengths else 0
    if avg_len == 0:
        reasonability = 0
    elif avg_len > 200:
        reasonability = 0.65  # 值太长，可能是整段文本塞进来了
    else:
        # 检查是否有异常值（长度偏差过大）
        if len(lengths) >= 3:
            mean_l = sum(lengths) / len(lengths)
            variance = sum((l - mean_l) ** 2 for l in lengths) / len(lengths)
            std = variance ** 0.5
            cv = std / mean_l if mean_l > 0 else 0  # 变异系数
            reasonability = max(0.5, 1.0 - cv * 0.2)  # CV 越大越不合理
        else:
            reasonability = 0.95

    # --- 加权汇总 ---
    if mode == "table":
        # 表格直接匹配：列名匹配度权重高
        src_score = match_score if match_score is not None else 0.85
        conf = (
            col_match_score * 0.25 +
            src_score * 0.25 +
            type_score * 0.20 +
            fill_rate * 0.15 +
            consistency * 0.10 +
            reasonability * 0.05
        )
    else:
        # LLM 提取：基础分反映 LLM 提取的可靠性
        conf = (
            0.95 * 0.20 +          # LLM 基础分
            fill_rate * 0.20 +
            type_score * 0.20 +
            consistency * 0.25 +
            reasonability * 0.15
        )
        # 多批独立采样一致性：仅当有多个非空批次时才作为惩罚因子
        # 单批提取（nb=1）时 agreement 无意义，不应降低置信度
        if llm_multi_batch_agreement is not None and llm_multi_batch_agreement < 1.0:
            a = min(max(llm_multi_batch_agreement, 0.0), 1.0)
            conf = conf * (0.70 + 0.30 * a)

    return round(min(max(conf, 0), 1), 2)


def _is_numeric(s: str) -> bool:
    """判断字符串是否为数值（含逗号分隔的数字）"""
    s = s.strip().replace(",", "").replace("，", "")
    if not s:
        return False
    try:
        float(s)
        return True
    except ValueError:
        return False


def _value_signature(s: str) -> str:
    """返回值的类型签名：num / text / mixed / dash"""
    s = s.strip()
    if not s or s in ("—", "-", "N/A", "无", "/"):
        return "dash"
    if _is_numeric(s):
        return "num"
    if re.match(r'^[\u4e00-\u9fff]+$', s):
        return "text"
    return "mixed"


def _to_float_value(v) -> Optional[float]:
    s = str(v).strip() if v is not None else ""
    if not s:
        return None
    s = s.replace(",", "").replace("，", "")
    if re.fullmatch(r'-?\d+(?:\.\d+)?', s):
        return float(s)
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    if not m:
        return None
    try:
        return float(m.group(0))
    except Exception:
        return None


def _merge_retrieved_chunks(*chunk_lists: List[List[dict]]) -> List[dict]:
    """按 chunk_text 去重，保留 score 较高的一条，再按 score 降序（用于合并主检索与叙事补充检索）。"""
    best: Dict[str, dict] = {}
    for lst in chunk_lists:
        for c in lst or []:
            t = (c.get("chunk_text") or "").strip()
            if not t:
                continue
            prev = best.get(t)
            sc = float(c.get("score") or 0)
            if prev is None or sc > float(prev.get("score") or 0):
                best[t] = dict(c)
    return sorted(best.values(), key=lambda x: float(x.get("score") or 0), reverse=True)


def _normalize_llm_json_text(s: str) -> str:
    """去掉模型常见的 ```json ... ``` 包裹和 <think>...</think> 思维链，便于提取 JSON 数组。"""
    s = (s or "").strip()
    s = re.sub(r"<think>[\s\S]*?</think>", "", s, flags=re.IGNORECASE).strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", s, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    return s


def _clean_llm_numeric(v):
    """清洗 LLM 返回的数值：去除千分位逗号，转为 float/int。非数值原样返回。"""
    if v is None:
        return v
    s = str(v).strip()
    if not s:
        return v
    s_clean = s.replace(",", "").replace("，", "")
    try:
        fval = float(s_clean)
        if fval == int(fval) and '.' not in s_clean:
            return int(fval)
        return fval
    except (ValueError, TypeError):
        return v


def _format_number(v: float) -> str:
    iv = int(v)
    if abs(v - iv) < 1e-10:
        return str(iv)
    return f"{round(v, 6):.6f}".rstrip("0").rstrip(".")


def _norm_field_key(s: str) -> str:
    return re.sub(r'\s+', '', str(s or '').strip().lower().replace("（", "(").replace("）", ")"))


def _resolve_value_by_key(data: Dict[str, str], key: str):
    if key in data:
        return data[key]
    nk = _norm_field_key(key)
    for k, v in data.items():
        if _norm_field_key(k) == nk:
            return v
    return key


def _merge_retrieved_chunks(*chunk_lists: List[List[dict]]) -> List[dict]:
    """多路检索结果按 chunk_text 去重，保留较高 score，再按分数降序排列。"""
    by_text: Dict[str, dict] = {}
    for lst in chunk_lists:
        if not lst:
            continue
        for c in lst:
            t = (c.get("chunk_text") or "").strip()
            if not t:
                continue
            prev = by_text.get(t)
            sc = float(c.get("score") or 0)
            if prev is None or sc > float(prev.get("score") or 0):
                by_text[t] = dict(c)
    return sorted(by_text.values(), key=lambda x: float(x.get("score") or 0), reverse=True)


_SUMMARY_KEYWORDS = {
    "合计", "总计", "行合计", "列合计", "小计", "汇总", "总和",
    "行求和", "列求和", "求和", "sum", "total", "subtotal",
    "合计（行求和）", "合计(行求和)", "合计（列求和）", "合计(列求和)",
    "最大值", "最大", "max", "maximum",
    "最小值", "最小", "min", "minimum",
    "平均值", "平均", "均值", "avg", "average",
    "中位数", "median",
}


def _is_summary_row(row: dict, col_names: list) -> bool:
    if not col_names:
        return False
    first_val = str(row.get(col_names[0], "")).strip()
    normalized = first_val.lower().rstrip("：:").strip()
    if normalized in _SUMMARY_KEYWORDS:
        return True
    for kw in _SUMMARY_KEYWORDS:
        if kw in normalized:
            return True
    return False


# 直辖市全称 → 填表常用简称（与「城市名」类表头一致）
_CITY_FULL_TO_SHORT = {
    "上海市": "上海",
    "北京市": "北京",
    "天津市": "天津",
    "重庆市": "重庆",
}


def _is_city_name_column(header: str) -> bool:
    """首列是否为城市/地市类实体（与 data_filter._is_city_level_header 对齐，并含直辖市表头）。"""
    if not (header or "").strip():
        return False
    if _is_city_level_header(header):
        return True
    s = re.sub(r"\s+", "", str(header).strip().lower().replace("（", "(").replace("）", ")"))
    return "直辖市" in s


def _entity_label_is_province_or_autonomous_region(label: str) -> bool:
    """数据源混排时识别省级汇总行：…省、以自治区结尾、以特别行政区结尾（保留香港、澳门等简称）。"""
    s = str(label or "").strip()
    if not s:
        return False
    if s.endswith("特别行政区"):
        return True
    if s.endswith("省"):
        return True
    if s.endswith("自治区"):
        return True
    return False


def _city_table_should_apply_row_noise_filter(col_names: List[str]) -> bool:
    if not col_names:
        return False
    if _is_province_level_header(col_names[0]):
        return False
    return _is_city_name_column(col_names[0])


def _city_table_drop_provincial_rows(rows: List[dict], col_names: List[str]) -> Tuple[List[dict], int]:
    hdr = col_names[0]
    out: List[dict] = []
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        lab = _row_entity_label_for_header(row, hdr)
        if _entity_label_is_province_or_autonomous_region(lab):
            n += 1
            continue
        out.append(row)
    return out, n


def _city_table_drop_empty_metric_rows(rows: List[dict], col_names: List[str]) -> Tuple[List[dict], int]:
    if len(col_names) <= 1:
        return rows, 0
    hdr = col_names[0]
    out: List[dict] = []
    n = 0
    for row in rows:
        if not isinstance(row, dict):
            out.append(row)
            continue
        lab = _row_entity_label_for_header(row, hdr)
        if lab and _non_empty_data_cells(row, col_names) == 0:
            n += 1
            continue
        out.append(row)
    return out, n


def _apply_city_table_row_cleanup_pre_merge(rows_data, col_names: List[str]):
    if not _city_table_should_apply_row_noise_filter(col_names):
        return rows_data
    if isinstance(rows_data, list):
        out, dropped = _city_table_drop_provincial_rows(rows_data, col_names)
        if dropped:
            logger.info(f"[table_fill] 城市表剔除省级/自治区/特别行政区汇总行: {dropped} 行")
        return out
    if isinstance(rows_data, dict):
        acc = {}
        total = 0
        for k, v in rows_data.items():
            lst = v if isinstance(v, list) else []
            out, dropped = _city_table_drop_provincial_rows(lst, col_names)
            total += dropped
            acc[k] = out
        if total:
            logger.info(f"[table_fill] 城市表剔除省级/自治区/特别行政区汇总行(多表合计): {total} 行")
        return acc
    return rows_data


def _apply_city_table_row_cleanup_post_merge(rows_data, col_names: List[str]):
    if not _city_table_should_apply_row_noise_filter(col_names):
        return rows_data
    if isinstance(rows_data, list):
        out, dropped = _city_table_drop_empty_metric_rows(rows_data, col_names)
        if dropped:
            logger.info(f"[table_fill] 城市表剔除有名称无指标占位行: {dropped} 行")
        return out
    if isinstance(rows_data, dict):
        acc = {}
        total = 0
        for k, v in rows_data.items():
            lst = v if isinstance(v, list) else []
            out, dropped = _city_table_drop_empty_metric_rows(lst, col_names)
            total += dropped
            acc[k] = out
        if total:
            logger.info(f"[table_fill] 城市表剔除有名称无指标占位行(多表合计): {total} 行")
        return acc
    return rows_data


def _canonical_city_name(label: str) -> str:
    """城市名列：统一为不带「市」的常用名（上海、深圳、长春）。"""
    s = str(label).strip()
    if not s:
        return s
    if s in _CITY_FULL_TO_SHORT:
        return _CITY_FULL_TO_SHORT[s]
    if s.endswith("市") and len(s) > 2:
        return s[:-1]
    return s


# 模型可能用「城市」等 key，与模板「城市名」不一致
_CITY_LABEL_EXTRA_KEYS = frozenset({"城市", "城市名称", "市名", "地市", "地市名"})


def _row_entity_label_for_header(row: dict, hdr: str) -> str:
    """取实体名列取值：优先模板列名，城市表兼容常见别名。"""
    if not isinstance(row, dict):
        return ""
    v = row.get(hdr)
    if not _is_empty_table_cell(v):
        return str(v).strip()
    if _is_city_name_column(hdr):
        for alt in _CITY_LABEL_EXTRA_KEYS:
            if alt == hdr:
                continue
            v2 = row.get(alt)
            if not _is_empty_table_cell(v2):
                return str(v2).strip()
    return ""


def _table_first_col_dedupe_key(first_col_header: str, label: str) -> str:
    raw = str(label).strip()
    if not raw:
        return ""
    if _is_city_name_column(first_col_header):
        return _canonical_city_name(raw)
    return raw


def _is_empty_table_cell(val) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and not val.strip():
        return True
    s = str(val).strip().lower()
    if s in ("none", "null"):
        return True
    return False


def _non_empty_data_cells(row: dict, col_names: List[str]) -> int:
    n = 0
    for c in col_names[1:]:
        if not _is_empty_table_cell(row.get(c)):
            n += 1
    return n


def _is_station_granularity_city_table(col_names: List[str]) -> bool:
    """首列是城市，但每行是同城不同站点（区/站名不同），不得按城市键合并成一行。"""
    if not col_names:
        return False
    joined = "".join(str(c) for c in col_names)
    joined_lower = joined.lower()
    if any(k in joined for k in ("站点名称", "站点编码", "监测点", "站名")):
        return True
    # 环境监测类明细表：多站点同行城市
    if any(
        k in joined or k in joined_lower
        for k in (
            "PM10",
            "PM2.5",
            "PM2",
            "AQI",
            "aqi",
            "首要污染物",
            "空气质量指数",
            "污染类型",
            "监测值",
            "二氧化硫",
            "二氧化氮",
            "臭氧",
        )
    ):
        return True
    if len(col_names) >= 2:
        c2 = str(col_names[1]).strip().replace(" ", "")
        if c2 in ("区", "区县", "县", "区/县", "行政区"):
            return True
    return False


def _data_implies_station_granularity(rows: List[dict], col_names: List[str], hdr: str) -> bool:
    """数据驱动：多行规范化城市相同，但区/站点名称有多值 → 视为站点级表，禁止按城市合并。"""
    if len(rows) < 2 or not col_names:
        return False
    city_keys: List[str] = []
    for row in rows:
        if not isinstance(row, dict):
            return False
        raw = _row_entity_label_for_header(row, hdr)
        if not raw:
            return False
        city_keys.append(_canonical_city_name(raw))
    if len(set(city_keys)) != 1:
        return False

    qu_col = None
    st_col = None
    for c in col_names:
        cs = str(c).strip().replace(" ", "").replace("\u3000", "")
        if cs in ("区", "区县", "区县名", "县", "区/县", "行政区"):
            qu_col = qu_col or c
        if "站点名称" in str(c) or cs in ("站名", "监测点"):
            st_col = st_col or c

    vals_q: set = set()
    vals_s: set = set()
    for row in rows:
        if qu_col:
            v = row.get(qu_col)
            if not _is_empty_table_cell(v):
                vals_q.add(str(v).strip())
        if st_col:
            v = row.get(st_col)
            if not _is_empty_table_cell(v):
                vals_s.add(str(v).strip())
    return len(vals_q) >= 2 or len(vals_s) >= 2


def _merge_city_duplicate_rows(rows: List[dict], col_names: List[str]) -> List[dict]:
    """城市名列：同一城市多种写法合并为一行，优先保留非空字段更多的行。"""
    if not col_names or not rows:
        return rows
    hdr = col_names[0]
    if not _is_city_name_column(hdr):
        return rows

    def _normalize_only() -> List[dict]:
        out: List[dict] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            r = dict(row)
            raw = _row_entity_label_for_header(r, hdr)
            if raw:
                r[hdr] = _canonical_city_name(raw)
            out.append(r)
        return out

    # 空气质量 / 站点级：同城多行不得合并
    if _is_station_granularity_city_table(col_names):
        logger.info(
            f"[table_fill] 站点级城市表(表头特征)：跳过按城市合并，保留 {len(rows)} 行"
        )
        return _normalize_only()
    if _data_implies_station_granularity(rows, col_names, hdr):
        logger.info(
            f"[table_fill] 站点级城市表(数据特征：同城多区/多站)：跳过按城市合并，保留 {len(rows)} 行"
        )
        return _normalize_only()
    groups: Dict[str, List[dict]] = {}
    order: List[str] = []
    dropped_no_entity = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw = _row_entity_label_for_header(row, hdr)
        if not raw:
            dropped_no_entity += 1
            continue
        ck = _canonical_city_name(raw)
        if ck not in groups:
            order.append(ck)
            groups[ck] = []
        groups[ck].append(row)
    out: List[dict] = []
    merged_any = False
    for ck in order:
        grp = groups[ck]
        if len(grp) == 1:
            r = dict(grp[0])
            r[hdr] = ck
            out.append(r)
            continue
        merged_any = True
        primary = max(grp, key=lambda r: _non_empty_data_cells(r, col_names))
        merged = dict(primary)
        for other in grp:
            if other is primary:
                continue
            for c in col_names:
                if _is_empty_table_cell(merged.get(c)) and not _is_empty_table_cell(other.get(c)):
                    merged[c] = other[c]
        merged[hdr] = ck
        out.append(merged)
    if merged_any:
        logger.info(f"[table_fill] 城市名列去重合并: {len(rows)} -> {len(out)} 行")
    if dropped_no_entity:
        logger.warning(
            f"[table_fill] 城市名列丢弃无名称的残缺行 {dropped_no_entity} 条"
            "（常见原因：模型漏写城市名、或仅输出片段数字；不再追加到表尾）"
        )
    return out


def _apply_city_row_normalization(rows_data, col_names: List[str]):
    """对 list 或 dict[table] 做城市名列规范化与重复合并。"""
    if not col_names:
        return rows_data
    if isinstance(rows_data, list):
        return _merge_city_duplicate_rows(rows_data, col_names)
    if isinstance(rows_data, dict):
        return {k: _merge_city_duplicate_rows(v if isinstance(v, list) else [], col_names) for k, v in rows_data.items()}
    return rows_data


async def _gap_fill_table_rows_llm(
    col_names: List[str],
    user_req: str,
    existing_keys: set,
    chunks: List[dict],
    *,
    reverse_order: bool = False,
) -> List[dict]:
    """主批次合并后，用「已有关键字 + 去重全文片段」补漏尚未提取的实体行。
    reverse_order=True 时按检索列表逆序拼接（优先低相似度/叙事片段），与正序各跑一轮可减少漏提。
    当 LLM 输出因 max_tokens 截断时，自动将已提取的 key 加入排除列表并重试（最多 5 轮）。"""
    if not col_names or not chunks:
        return []
    key_col = col_names[0]

    all_unique_texts = set()
    for c in chunks:
        t = (c.get("chunk_text", "") or "").strip()
        if t:
            all_unique_texts.add(t)
    total_unique = len(all_unique_texts)

    seq = list(reversed(chunks)) if reverse_order else list(chunks)
    seen_text = set()
    parts = []
    total = 0
    GAP_CTX_MAX = 110_000
    for c in seq:
        t = (c.get("chunk_text", "") or "").strip()
        if not t or t in seen_text:
            continue
        seen_text.add(t)
        remain = GAP_CTX_MAX - total - 4
        if remain <= 10:
            break
        piece = t if len(t) <= remain else t[:remain]
        parts.append(piece)
        total += len(piece) + 4
    if not parts:
        return []
    ctx = "\n\n---\n\n".join(parts)

    order_tag = "reverse" if reverse_order else "score_order"
    if len(seen_text) < total_unique:
        logger.warning(
            f"[table_fill] gap_fill({order_tag}): 预算 {GAP_CTX_MAX} 字内仅纳入 {len(seen_text)}/{total_unique} 个去重片段，"
            f"叙事/低相关段落可能在未纳入部分（易导致个别城市漏提）"
        )
    else:
        logger.info(
            f"[table_fill] gap_fill({order_tag}): 上下文含 {len(seen_text)} 个去重片段, 约 {total} 字"
        )

    city_line = ""
    if _is_city_name_column(key_col):
        city_line = (
            f"第一列「{key_col}」为城市时，名称用不带「市」字的简称（如上海、北京、深圳），"
            "与已列表中写法一致；已列出的城市不要再输出。\n"
        )

    all_out: List[dict] = []
    running_keys = set(existing_keys)
    MAX_GAP_ROUNDS = 5

    for gap_round in range(MAX_GAP_ROUNDS):
        known_sorted = sorted(running_keys, key=lambda x: (len(str(x)), str(x)))
        known_json = json.dumps(known_sorted, ensure_ascii=False)

        sys_prompt = f"""你是数据提取助手。用户已从文档中分批提取了部分表格行，但可能遗漏了个别实体。
模板列名（JSON 的 key 必须与下列完全一致）：{json.dumps(col_names, ensure_ascii=False)}
用于区分不同实体的第一列为「{key_col}」。

【已提取的 {key_col} 列表】（不要重复输出这些；语义相同视为同一实体）：
{known_json}

用户原始要求：{user_req or '智能填表'}

{city_line}任务：阅读文档，找出文档中**明确出现**且**对应一行实体数据**、但**不在上述列表**中的实体（例如遗漏的城市或省级行政区，须与「{key_col}」列语义一致：列名含省/自治区/特别行政区则补省级名，列名表示城市则补城市名），为每个此类实体输出一行 JSON 对象，字段与模板列名一致。数值只输出纯数字，不要千分位逗号。
若文档中确实没有新的实体可补充，必须返回空数组 []。
不要输出合计、总计、小计等汇总行。
只返回 JSON 数组，不要其他文字。"""

        try:
            raw = await asyncio.wait_for(
                call_llm(
                    [{"role": "user", "content": f"文档内容如下：\n\n{ctx}"}],
                    system=sys_prompt,
                    temperature=0.0,
                    max_tokens=_TABLE_FILL_GAP_MAX_TOKENS,
                ),
                timeout=_TABLE_FILL_LLM_TIMEOUT_SEC * 2,
            )
            r = _normalize_llm_json_text(raw.get("text") or "")
            was_truncated = _llm_stopped_by_output_limit(raw.get("stop_reason", ""))
            if was_truncated:
                logger.warning(
                    f"[table_fill] gap_fill({order_tag}) round {gap_round+1}: LLM 输出被截断，"
                    f"将解析已有部分并自动重试补漏"
                )
            m = re.search(r'\[[\s\S]*\]', r)
            if not m:
                # 截断可能导致 JSON 不完整，尝试修复
                m2 = re.search(r'\[[\s\S]*', r)
                if m2 and was_truncated:
                    partial = m2.group().rstrip().rstrip(',') + ']'
                    try:
                        rows = json.loads(partial)
                    except json.JSONDecodeError:
                        logger.warning(f"[table_fill] gap_fill({order_tag}) round {gap_round+1}: 截断 JSON 修复失败")
                        break
                else:
                    logger.warning(f"[table_fill] gap_fill({order_tag}) round {gap_round+1}: 未找到 JSON 数组")
                    break
            else:
                try:
                    rows = json.loads(m.group())
                except json.JSONDecodeError:
                    if was_truncated:
                        # 尝试修复截断的 JSON
                        partial_text = m.group().rstrip().rstrip(',')
                        if not partial_text.endswith(']'):
                            # 找到最后一个完整的 } 并截断
                            last_brace = partial_text.rfind('}')
                            if last_brace > 0:
                                partial_text = partial_text[:last_brace+1] + ']'
                        try:
                            rows = json.loads(partial_text)
                        except json.JSONDecodeError:
                            logger.warning(f"[table_fill] gap_fill({order_tag}) round {gap_round+1}: 截断 JSON 修复失败")
                            break
                    else:
                        break
            if not isinstance(rows, list):
                break

            round_new = 0
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row = {k: _clean_llm_numeric(v) for k, v in row.items()}
                if _is_summary_row(row, col_names):
                    continue
                key = _row_entity_label_for_header(row, key_col)
                dedupe = _table_first_col_dedupe_key(key_col, key)
                if not dedupe or dedupe in running_keys:
                    continue
                row[key_col] = dedupe
                all_out.append(row)
                running_keys.add(dedupe)
                round_new += 1

            logger.info(
                f"[table_fill] gap_fill({order_tag}) round {gap_round+1}: "
                f"模型返回 {len(rows)} 行, 本轮新增 {round_new} 行, 累计补漏 {len(all_out)} 行"
            )

            if not was_truncated or round_new == 0:
                break

        except asyncio.TimeoutError:
            logger.error(f"[table_fill] gap_fill({order_tag}) round {gap_round+1} 超时")
            break
        except Exception as e:
            logger.error(f"[table_fill] gap_fill({order_tag}) round {gap_round+1} 失败: {e}")
            break

    logger.info(f"[table_fill] gap_fill({order_tag}): 总计补漏 {len(all_out)} 行")
    return all_out


def _apply_requirement_formulas(matched: Dict[str, str], requirement: str) -> Dict[str, str]:
    if not requirement:
        return matched
    out = dict(matched)
    lines = [x.strip() for x in re.split(r'[\n;；]+', requirement) if x.strip()]
    for line in lines:
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        target = left.strip()
        expr = right.strip().replace("×", "*").replace("÷", "/")
        expr = re.sub(r'\s+[xX]\s+', ' * ', expr)
        m = re.match(r'^(.+?)\s*([+\-*/])\s*(.+?)$', expr)
        if not m:
            continue
        a_raw, op, b_raw = m.group(1).strip(), m.group(2), m.group(3).strip()
        a_val = _to_float_value(_resolve_value_by_key(out, a_raw))
        b_val = _to_float_value(_resolve_value_by_key(out, b_raw))
        if a_val is None or b_val is None:
            continue
        if op == "+":
            r = a_val + b_val
        elif op == "-":
            r = a_val - b_val
        elif op == "*":
            r = a_val * b_val
        else:
            if abs(b_val) < 1e-12:
                continue
            r = a_val / b_val
        out[target] = _format_number(r)
    return out


router = APIRouter(prefix="/api/templates", tags=["模板填报"])


def _log(db, user, action, detail=""):
    db.add(OperationLog(user_id=user.id, username=user.username, action=action, detail=detail))
    db.commit()


# ============================================================
# 上传模板
# ============================================================

@router.post("/upload")
async def upload(
    file: UploadFile = File(...),
    user_requirement: str = Form(default=""),
    requirement_file: Optional[UploadFile] = File(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user)
):
    ext = Path(file.filename).suffix.lower()
    if ext not in (".docx", ".doc", ".xlsx", ".xls"):
        raise HTTPException(400, "仅支持 Word 或 Excel")
    name = f"tpl_{uuid.uuid4().hex}_{file.filename}"
    path = UPLOAD_DIR / name
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)

    req_from_form = (user_requirement or "").strip()
    req_from_file = ""
    if requirement_file and requirement_file.filename:
        req_content = await requirement_file.read()
        try:
            req_from_file = req_content.decode("utf-8").strip()
        except UnicodeDecodeError:
            req_from_file = req_content.decode("gbk", errors="ignore").strip()
    if req_from_file and req_from_form:
        user_requirement = f"{req_from_form}\n\n{req_from_file}"
    elif req_from_file:
        user_requirement = req_from_file
    else:
        user_requirement = req_from_form

    tpl_info = identify_fields(str(path))
    mode = tpl_info.get("mode", "field_fill")
    fields = tpl_info.get("fields", [])
    columns = tpl_info.get("columns", [])

    t = TemplateInfo(
        template_name=file.filename, template_type=ext.lstrip("."),
        template_path=str(path), field_count=len(fields) if fields else len(columns),
        template_mode=mode, user_requirement=user_requirement,
        upload_user=user.id
    )
    db.add(t)
    db.commit()
    db.refresh(t)

    _log(db, user, "上传模板", f"{file.filename}，模式={mode}，{len(fields)}个字段")

    return {
        "message": "上传成功", "template_id": t.id, "template_name": file.filename,
        "user_requirement": t.user_requirement or "",
        "mode": mode, "field_count": len(fields) if fields else len(columns),
        "columns": [c if isinstance(c, str) else c.get("name", c) for c in columns] if columns else [],
        "fields": [{"field_name": f["field_name"], "location_type": f.get("location_type", "")} for f in fields],
        "multi_sheet": tpl_info.get("multi_sheet", False),
        "sheet_count": len(tpl_info.get("sheets", [])) if tpl_info.get("multi_sheet") else 1,
        "sheets": [
            {"sheet": s["sheet"], "columns": [c["name"] for c in s.get("columns", [])]}
            for s in tpl_info.get("sheets", [])
        ] if tpl_info.get("multi_sheet") else [],
    }


# ============================================================
# 更新用户要求
# ============================================================

class RequirementReq(BaseModel):
    user_requirement: str

@router.put("/{tid}/requirement")
def update_requirement(tid: int, req: RequirementReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = db.query(TemplateInfo).filter(TemplateInfo.id == tid, TemplateInfo.upload_user == user.id).first()
    if not t:
        raise HTTPException(404)
    t.user_requirement = req.user_requirement
    db.commit()
    return {"message": "已更新"}


# ============================================================
# 填充模板（核心）
# ============================================================

class FillRequest(BaseModel):
    source_file_ids: Optional[List[int]] = None  # 指定数据源文件ID列表，为空则自动匹配


@router.post("/{tid}/fill")
async def fill(tid: int, body: Optional[FillRequest] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t0 = time.time()
    t = db.query(TemplateInfo).filter(TemplateInfo.id == tid, TemplateInfo.upload_user == user.id).first()
    if not t:
        raise HTTPException(404)

    tpl_info = identify_fields(t.template_path)
    mode = tpl_info.get("mode", "field_fill")

    task = TaskInfo(task_type="fill", task_name=f"填写: {t.template_name}", status="running", user_id=user.id, related_id=tid)
    db.add(task)
    db.commit()
    db.refresh(task)

    source_file_ids = body.source_file_ids if body else None

    try:
        # 加 300 秒超时保护，防止卡死
        if mode == "table_fill":
            result = await asyncio.wait_for(_fill_table_mode(t, tpl_info, db, user, source_file_ids=source_file_ids), timeout=300)
        else:
            result = await asyncio.wait_for(_fill_field_mode(t, tpl_info, db, user, source_file_ids=source_file_ids), timeout=300)

        elapsed = round(time.time() - t0, 2)
        result["elapsed_seconds"] = elapsed

        task.status = "done"
        task.elapsed = elapsed
        task.accuracy = result.get("accuracy", 0)
        task.output_file = unicodedata.normalize("NFC", (result.get("output_file") or "").strip())
        task.end_time = datetime.now()
        task.result_summary = f"填写完成，{result.get('filled_count', 0)}/{result.get('field_count', 0)}，{elapsed}s"
        db.commit()

        _log(db, user, "模板填写", f"{t.template_name}，{result.get('filled_count', 0)}/{result.get('field_count', 0)}，{elapsed}s")
        result["task_id"] = task.id
        return result

    except asyncio.TimeoutError:
        logger.error(f"模板填充超时: {t.template_name}")
        task.status = "error"
        task.elapsed = round(time.time() - t0, 2)
        task.end_time = datetime.now()
        task.result_summary = "填写超时（超过300秒）"
        db.commit()
        raise HTTPException(500, "填写超时，请检查数据源文件大小或网络连接（300秒限制）")

    except Exception as e:
        logger.error(f"模板填充失败: {t.template_name}\n{traceback.format_exc()}")
        task.status = "error"
        task.elapsed = round(time.time() - t0, 2)
        task.end_time = datetime.now()
        task.result_summary = str(e)[:500]
        db.commit()
        raise HTTPException(500, f"填写失败: {str(e)[:200]}")


# ============================================================
# 表格填充模式（类型A/B：从数据源批量筛选填入）
# ============================================================

async def _fill_table_mode(t, tpl_info, db, user, source_file_ids=None):
    """表格批量填充：从数据源中筛选数据，按列名映射写入模板"""
    columns = tpl_info.get("columns", [])
    col_names = [c["name"] if isinstance(c, dict) else c for c in columns]
    user_req = t.user_requirement or ""
    fmt = tpl_info.get("format", "xlsx")

    # 1. 获取所有已上传的数据源文件
    # 1. 获取数据源文件列表
    if source_file_ids:
        ufiles = db.query(FileInfo).filter(FileInfo.id.in_(source_file_ids), FileInfo.upload_user == user.id, FileInfo.parse_status == "done").all()
    else:
        ufiles = db.query(FileInfo).filter(FileInfo.upload_user == user.id, FileInfo.parse_status == "done").all()
    file_list = [{"id": f.id, "file_name": f.file_name, "file_path": f.file_path, "file_type": f.file_type} for f in ufiles]

    if not file_list:
        logger.warning(
            "[table_fill] 无 parse_status=done 的数据源文档，无法做结构化匹配；"
            "请先在文档管理中上传并完成解析，或在填表时勾选数据源。"
        )

    # 2. 找到最匹配的数据源（宽表多组重复表头时按去重列名评分，避免总分被摊薄）
    unique_col_names = list(dict.fromkeys(col_names))
    if len(unique_col_names) < len(col_names):
        logger.info(
            f"[table_fill] 模板列 {len(col_names)} 个（去重后 {len(unique_col_names)} 个），"
            "数据源匹配按去重列名计算分数"
        )
    logger.info(f"[table_fill] user_requirement={user_req[:200]}")
    logger.info(f"[table_fill] 开始匹配数据源，共 {len(file_list)} 个文件，模板列: {col_names}")
    best_source = await asyncio.to_thread(find_best_data_source, unique_col_names, file_list)
    match_score = best_source.get("match_score", 0) if best_source else 0
    logger.info(f"[table_fill] 匹配结果: score={match_score}, file={best_source.get('file_name','') if best_source else 'None'}")
    logger.info(f"[table_fill] 列映射: {best_source.get('column_mapping', {}) if best_source else {}}")

    if not best_source or match_score < _LLM_FALLBACK_MIN_SCORE:
        weak = await asyncio.to_thread(try_weak_table_source, unique_col_names, file_list)
        wmc = weak.get("weak_mapped_count", 0) if weak else 0
        if weak and wmc >= 2:
            best_source = weak
            match_score = float(weak.get("match_score", 0))
            logger.warning(
                f"[table_fill] weak_fallback applied file={best_source.get('file_name')!r} "
                f"weak_mapped_count={wmc} positional_fallback={weak.get('positional_fallback')} "
                f"weak_match_score={match_score} min_score_used={weak.get('weak_min_score_used')}"
            )
        else:
            logger.warning(
                f"[table_fill] 数据源匹配度不足(score={match_score})，"
                f"threshold={_LLM_FALLBACK_MIN_SCORE}，弱兜底未通过，自动进入 LLM 提取模式"
                f"（置信度上限={_LLM_FALLBACK_CONFIDENCE_CAP}）"
            )
            return await _fill_table_with_llm_fallback(t, tpl_info, col_names, user_req, file_list, db, user)

    # 3. 加载数据源
    src_path = best_source["file_path"]
    src_type = best_source.get("file_type", "xlsx")
    col_mapping = best_source.get("column_mapping", {})

    source_type = best_source.get("source_type", "")
    logger.info(f"[table_fill] 加载数据源: {best_source.get('file_name', '')} (source_type={source_type})")
    if source_type == "docx_paragraph":
        from services.data_filter import load_docx_paragraphs_as_table
        para_result = await asyncio.to_thread(load_docx_paragraphs_as_table, src_path)
        if para_result:
            src_cols, src_data = para_result
        else:
            src_cols, src_data = [], []
    elif src_type in ("xlsx", "xls"):
        def _load_excel_table():
            return load_xlsx_as_table(
                src_path,
                sheet_name=best_source.get("sheet_name"),
                max_rows=50000,
                header_row_1based=best_source.get("header_row_1based"),
            )

        src_cols, src_data = await asyncio.to_thread(_load_excel_table)
    elif src_type in ("docx", "doc"):
        tables = await asyncio.to_thread(load_docx_tables, src_path)
        ti = best_source.get("table_index", 0)
        if ti < len(tables):
            src_cols, src_data = tables[ti]
        else:
            src_cols, src_data = [], []
    else:
        src_cols, src_data = [], []
    logger.info(f"[table_fill] 数据源加载完成: {len(src_cols)} 列, {len(src_data)} 行")

    # 3b. 检查其他文件是否有补充数据（如 docx 段落文本），用 LLM 提取后追加
    supplemental_rows = []
    other_files = [f for f in file_list if f["file_path"] != src_path and f.get("file_type") in ("docx", "doc", "txt")]
    if other_files:
        from services.llm_service import call_llm_text
        import re as _re
        for sup_f in other_files:
            try:
                sup_tables = await asyncio.to_thread(load_docx_tables, sup_f["file_path"])
                if sup_tables:
                    # docx 有表格，直接用
                    for tbl in sup_tables:
                        tbl_cols = tbl.get("headers", [])
                        tbl_rows = tbl.get("rows", [])
                        if tbl_rows:
                            sup_mapped = map_columns(tbl_rows, {tc: tc for tc in col_names if tc in tbl_cols})
                            supplemental_rows.extend(sup_mapped)
                else:
                    # docx 无表格，读段落文本用 LLM 提取
                    import docx as _docx
                    doc = _docx.Document(sup_f["file_path"])
                    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
                    if paras:
                        ctx = "\n".join(paras[:60])  # 最多60段
                        sys_p = f"""从以下文本中提取结构化数据，列名为：{json.dumps(col_names, ensure_ascii=False)}
每行数据对应一个实体（如省份、国家等），返回JSON数组：[{{"列名1":"值1",...}}]
只返回JSON数组，不要解释。"""
                        try:
                            r = await asyncio.wait_for(
                                call_llm_text([{"role": "user", "content": ctx}], system=sys_p, temperature=0.0, max_tokens=4000),
                                timeout=60
                            )
                            m = _re.search(r'\[[\s\S]*\]', r)
                            if m:
                                extra = json.loads(m.group())
                                extra = [{k: _clean_llm_numeric(v) for k, v in row.items()} for row in extra]
                                supplemental_rows.extend(extra)
                                logger.info(f"[table_fill] 从 {sup_f['file_name']} 补充提取 {len(extra)} 行")
                        except Exception as e:
                            logger.warning(f"[table_fill] 补充提取失败 {sup_f['file_name']}: {e}")
            except Exception as e:
                logger.warning(f"[table_fill] 读取补充文件失败 {sup_f['file_name']}: {e}")

    # 4. 解析筛选条件
    logger.info(f"[table_fill] 解析筛选条件: user_req={user_req[:200]}")
    
    # 如果用户要求为空，尝试从模板描述中推断筛选条件
    effective_req = user_req
    if not effective_req and tpl_info.get("description"):
        effective_req = tpl_info["description"]
        logger.info(f"[table_fill] 用户要求为空，使用模板描述推断: {effective_req[:150]}")
    
    # 先用规则解析
    try:
        rule_filters = parse_filter_conditions(effective_req) if effective_req else {}
    except Exception as e:
        logger.warning(f"[table_fill] 规则解析异常，降级为空条件: {e}")
        rule_filters = {}
    logger.info(f"[table_fill] parsed_conditions(rule)={_safe_json_preview(rule_filters, 500)}")
    
    # 检测多表格+多城市场景（如山东空气质量）
    tables_info = tpl_info.get("tables", []) if fmt == "docx" else []
    is_multi_table = len(tables_info) > 1
    cities = rule_filters.get("cities", [])
    datetime_exact = rule_filters.get("datetime_exact", [])
    
    # 如果没有从用户要求中提取到城市，尝试从每个表格的 description 中提取
    if is_multi_table and not cities:
        for tinfo in tables_info:
            desc = tinfo.get("description", "")
            # 更精确的城市提取：匹配 "XX市" 格式（2-4个汉字+市）
            desc_cities = re.findall(r'(?:^|[，。、；\s])?([\u4e00-\u9fff]{2,4}市)', desc)
            if desc_cities:
                # 去重，取第一个
                city = desc_cities[0]
                if city not in cities:
                    cities.append(city)
        if cities:
            logger.info(f"[table_fill] 从模板描述中提取城市: {cities}")
    
    # 如果没有时间条件，尝试从描述中提取
    if not datetime_exact and effective_req:
        dt_matches = re.findall(r'(\d{4})年(\d{1,2})月(\d{1,2})日(\d{2}:\d{2})', effective_req)
        if dt_matches:
            yr, mo, dy, tm = dt_matches[0]
            datetime_exact = [f"{yr}-{mo.zfill(2)}-{dy.zfill(2)} {tm}:00"]
            logger.info(f"[table_fill] 从描述中提取时间: {datetime_exact}")
    
    if effective_req and src_cols:
        # 用 LLM 补充解析（带缓存，相同模板+要求+数据源列名直接复用结果）
        _cache_key = hashlib.md5(
            f"{t.id}:{effective_req}:{','.join(src_cols)}".encode()
        ).hexdigest()
        cached = _filter_cache_get(_cache_key)
        if cached is not None:
            llm_parsed = cached
            logger.info(f"[table_fill] LLM筛选条件命中缓存，跳过LLM调用")
        else:
            avail_info = [{"file": best_source["file_name"], "columns": src_cols, "rows": len(src_data)}]
            try:
                llm_parsed = await asyncio.wait_for(
                    parse_filter_with_llm(effective_req, col_names, avail_info), timeout=60
                )
                _filter_cache_set(_cache_key, llm_parsed)
                logger.info(f"[table_fill] parsed_conditions(llm)={_safe_json_preview(llm_parsed, 500)}")
            except (asyncio.TimeoutError, Exception) as e:
                logger.warning(f"[table_fill] LLM解析筛选条件失败: {e}，使用规则解析")
                llm_parsed = {"filters": {}, "column_mapping": {}}
        
        # LLM 的列映射
        llm_col_map = llm_parsed.get("column_mapping", {})
        if llm_col_map:
            col_mapping.update(llm_col_map)
        group_by = llm_parsed.get("group_by")
        llm_filters = llm_parsed.get("filters", {})
        
        # LLM 返回的城市列表覆盖规则提取的（LLM 更准确）
        llm_exact = llm_filters.get("exact_match", {})
        for col_key in list(llm_exact.keys()):
            if "城市" in col_key or "city" in col_key.lower():
                llm_cities = llm_exact[col_key]
                if isinstance(llm_cities, list) and llm_cities:
                    cities = llm_cities
                    logger.info(f"[table_fill] LLM城市覆盖: {cities}")
                elif isinstance(llm_cities, str) and llm_cities:
                    cities = [llm_cities]
                    logger.info(f"[table_fill] LLM城市覆盖: {cities}")
        
        # LLM 返回的时间条件
        if llm_filters.get("datetime_exact") and not datetime_exact:
            datetime_exact = [llm_filters["datetime_exact"]]
            logger.info(f"[table_fill] LLM时间覆盖: {datetime_exact}")
        
        # LLM 返回的时间列名
        if llm_filters.get("datetime_column"):
            # 验证该列名确实存在于数据源中
            dt_col_name = llm_filters["datetime_column"]
            if dt_col_name in src_cols:
                logger.info(f"[table_fill] LLM指定时间列: {dt_col_name}")
    else:
        llm_filters = {}
        group_by = None

    # 5. 多表格+多城市场景：为每个表格单独筛选数据
    if is_multi_table and cities and len(cities) == len(tables_info):
        logger.info(f"[table_fill] 多表格多城市模式: {len(tables_info)} 表, 城市={cities}")
        
        # 找到城市列和时间列（在数据源列中查找）
        city_col = None
        time_col = None
        # 优先使用 LLM 指定的时间列
        llm_dt_col = llm_filters.get("datetime_column", "")
        for c in src_cols:
            cl = c.lower()
            if "城市" in c or "city" in cl:
                city_col = c
            if c == llm_dt_col:
                time_col = c
            elif time_col is None and ("监测时间" in c or "时间" in c or "time" in cl):
                time_col = c
        
        logger.info(f"[table_fill] 城市列={city_col}, 时间列={time_col}")
        
        # 先按时间筛选（所有表格共用同一个时间条件）
        time_filtered = src_data
        if datetime_exact and time_col:
            dt_str = datetime_exact[0]
            time_filtered = [r for r in src_data if _match_datetime_value(r.get(time_col), dt_str)]
            logger.info(f"[table_fill] 时间筛选后: {len(time_filtered)} 行 (条件: {dt_str})")
        elif llm_filters.get("datetime_exact") and time_col:
            dt_str = llm_filters["datetime_exact"]
            time_filtered = [r for r in src_data if _match_datetime_value(r.get(time_col), dt_str)]
            logger.info(f"[table_fill] LLM时间筛选后: {len(time_filtered)} 行")
        
        # 为每个表格按城市筛选
        rows_by_table = {}
        total_filled = 0
        for i, tinfo in enumerate(tables_info):
            city = cities[i] if i < len(cities) else ""
            if city and city_col:
                city_rows = [r for r in time_filtered if str(r.get(city_col, "")).strip() == city]
            else:
                city_rows = time_filtered
            
            # 列名映射
            city_mapped = await asyncio.to_thread(map_columns, city_rows, col_mapping)
            rows_by_table[tinfo["table_index"]] = city_mapped
            total_filled += len(city_mapped)
            logger.info(f"[table_fill] 表{i} ({city}): {len(city_mapped)} 行")
        
        rows_data = rows_by_table
        total_rows = total_filled
        # 用第一个非空表格的数据来计算 filled_cols
        sample_rows = []
        for v in rows_by_table.values():
            if v:
                sample_rows = v
                break
        mapped = sample_rows
    else:
        # 单表格或无城市分组的场景
        # 合并筛选条件
        filters = dict(llm_filters) if llm_filters else {}
        if rule_filters.get("date_range") and "date_range" not in filters:
            filters["date_range"] = rule_filters["date_range"]
        if rule_filters.get("datetime_exact") and "datetime_exact" not in filters:
            filters["datetime_exact"] = rule_filters["datetime_exact"][0]
            # 找时间列
            for c in src_cols:
                if "监测时间" in c or "时间" in c:
                    filters["datetime_column"] = c
                    break
        if cities and "exact_match" not in filters:
            filters["exact_match"] = {}
            city_col = None
            for c in src_cols:
                if "城市" in c or "city" in c.lower():
                    city_col = c
                    break
            if city_col:
                filters["exact_match"][city_col] = cities[0]
        
        # 如果有 group_by 且是多表格
        if is_multi_table and group_by and not cities:
            # 不加城市筛选，后面按 group_by 分组
            if "exact_match" in filters:
                # 移除城市的精确匹配（因为要分组）
                for k in list(filters["exact_match"].keys()):
                    if "城市" in k or "city" in k.lower():
                        del filters["exact_match"][k]
        
        logger.info(f"[table_fill] 筛选条件: {json.dumps(filters, ensure_ascii=False, default=str)[:300]}")
        try:
            filtered = await asyncio.to_thread(filter_data, src_data, filters) if filters else src_data
        except Exception as e:
            logger.warning(f"[table_fill] filter_data 异常，回退为未筛选数据: {e}")
            filtered = src_data
        # 用户未写要求、仅用模板说明推断时，城市名常被规则误解析；筛出 0 行则去掉城市精确匹配再试（保留时间等）
        if (
            not filtered
            and filters
            and filters.get("exact_match")
            and not (user_req or "").strip()
        ):
            em = {k: v for k, v in filters["exact_match"].items()}
            city_like_keys = [k for k in em if "城市" in str(k) or "city" in str(k).lower()]
            if city_like_keys:
                filters_retry = dict(filters)
                em2 = dict(filters_retry.get("exact_match") or {})
                for k in city_like_keys:
                    em2.pop(k, None)
                if em2:
                    filters_retry["exact_match"] = em2
                else:
                    filters_retry.pop("exact_match", None)
                try:
                    filtered = await asyncio.to_thread(filter_data, src_data, filters_retry) if filters_retry else src_data
                except Exception as e:
                    logger.warning(f"[table_fill] 去掉城市条件后 filter_data 异常: {e}")
                    filtered = src_data
                if filtered:
                    logger.warning(
                        "[table_fill] 城市精确匹配筛出 0 行且用户要求为空，已去掉城市条件重试；"
                        f"保留其它筛选，当前 {len(filtered)} 行"
                    )
                    filters = filters_retry
        logger.info(f"[table_fill] 筛选后: {len(filtered)} 行")
        logger.info(f"[table_fill] filtered_data_sample={_safe_json_preview(filtered[:3] if filtered else [], 700)}")

        # 安全限制：如果筛选后数据量过大，限制行数防止超时
        max_fill_rows = 500  # docx 写入上限
        if fmt == "docx" and len(filtered) > max_fill_rows:
            logger.warning(f"[table_fill] 数据量过大({len(filtered)}行)，截断到{max_fill_rows}行")
            filtered = filtered[:max_fill_rows]

        # 列名映射
        try:
            mapped = await asyncio.to_thread(map_columns, filtered, col_mapping)
        except Exception as e:
            logger.warning(f"[table_fill] map_columns 异常，回退原始筛选数据: {e}")
            mapped = filtered

        # 处理 docx 多表格分组
        if is_multi_table and group_by:
            groups = {}
            for row in mapped:
                key = str(row.get(group_by, "")).strip()
                if key not in groups:
                    groups[key] = []
                groups[key].append(row)
            
            rows_by_table = {}
            for i, tinfo in enumerate(tables_info):
                desc = tinfo.get("description", "")
                matched_group = None
                # 从描述中找到对应的分组键
                for gk, gv in groups.items():
                    if gk and gk in desc:
                        matched_group = gv
                        break
                if matched_group is None:
                    # 按顺序分配
                    group_keys = list(groups.keys())
                    if i < len(group_keys):
                        matched_group = groups[group_keys[i]]
                    else:
                        matched_group = []
                rows_by_table[tinfo["table_index"]] = matched_group
            rows_data = rows_by_table
        elif tpl_info.get("multi_sheet") and tpl_info.get("sheets"):
            # xlsx 多表单分发：按每个 sheet 的列名匹配数据
            sheets = tpl_info["sheets"]
            rows_by_sheet = {}
            for si in sheets:
                sn = si["sheet"]
                sheet_col_names = [c["name"] for c in si.get("columns", [])]
                sheet_rows = []
                for row in mapped:
                    if any(str(row.get(cn, "")).strip() for cn in sheet_col_names):
                        sheet_rows.append(row)
                rows_by_sheet[sn] = sheet_rows
                logger.info(f"[table_fill] 多表单分发: sheet={sn}, 列={sheet_col_names}, 行数={len(sheet_rows)}")
            rows_data = rows_by_sheet
        else:
            rows_data = mapped

        # 追加补充数据源（如 docx 段落提取的数据）
        if supplemental_rows and isinstance(rows_data, list):
            existing_keys = set()
            if col_names:
                for r in rows_data:
                    dk = _table_first_col_dedupe_key(
                        col_names[0], _row_entity_label_for_header(r, col_names[0])
                    )
                    if dk:
                        existing_keys.add(dk)
            added = 0
            for row in supplemental_rows:
                key_raw = _row_entity_label_for_header(row, col_names[0]) if col_names else ""
                dk = _table_first_col_dedupe_key(col_names[0], key_raw) if col_names else ""
                if dk and dk not in existing_keys:
                    row = dict(row)
                    row[col_names[0]] = dk
                    rows_data.append(row)
                    existing_keys.add(dk)
                    added += 1
            if added:
                logger.info(f"[table_fill] 追加补充数据 {added} 行，总计 {len(rows_data)} 行")
        
        total_rows = len(filtered) if not isinstance(rows_data, dict) else sum(len(v) for v in rows_data.values())

    # 7b. 过滤伪合计行
    if isinstance(rows_data, list):
        before_clean = len(rows_data)
        rows_data = [row for row in rows_data if not _is_summary_row(row, col_names)]
        if len(rows_data) < before_clean:
            logger.info(f"[table_fill] 清理伪合计行: 移除 {before_clean - len(rows_data)} 行")
    elif isinstance(rows_data, dict):
        for tbl_key in rows_data:
            before_clean = len(rows_data[tbl_key])
            rows_data[tbl_key] = [r for r in rows_data[tbl_key] if not _is_summary_row(r, col_names)]
            removed = before_clean - len(rows_data[tbl_key])
            if removed:
                logger.info(f"[table_fill] 表{tbl_key} 清理伪合计行: 移除 {removed} 行")

    rows_data = _apply_city_table_row_cleanup_pre_merge(rows_data, col_names)
    rows_data = _apply_city_row_normalization(rows_data, col_names)
    rows_data = _apply_city_table_row_cleanup_post_merge(rows_data, col_names)

    # 7c. 排序（用户明确要求优先；否则按模板列顺序选第一列数值为主的列降序排序）
    sort_config = rule_filters.get("sort")
    if sort_config:
        if isinstance(rows_data, dict):
            for tbl_key in rows_data:
                rows_data[tbl_key] = sort_data(rows_data[tbl_key], sort_config, col_names)
        elif isinstance(rows_data, list):
            rows_data = sort_data(rows_data, sort_config, col_names)
        logger.info(f"[table_fill] 排序完成: config={sort_config}")
    elif col_names:
        if isinstance(rows_data, dict):
            for tbl_key in rows_data:
                if rows_data[tbl_key]:
                    rows_data[tbl_key] = sort_data_by_first_numeric_column(
                        rows_data[tbl_key], col_names
                    )
        elif isinstance(rows_data, list) and rows_data:
            rows_data = sort_data_by_first_numeric_column(rows_data, col_names)
        logger.info("[table_fill] 未指定排序，已尝试按首列数值列默认降序排序")

    # 7c. 求和配置
    sum_config = rule_filters.get("sum")
    if sum_config:
        logger.info(f"[table_fill] 求和配置: row={sum_config.get('row')}, col={sum_config.get('col')}")

    # 7d. 统计配置（最大值/最小值/平均值）
    stats_config = rule_filters.get("stats")
    if stats_config:
        logger.info(f"[table_fill] 统计配置: max={stats_config.get('max')}, min={stats_config.get('min')}, avg={stats_config.get('avg')}")

    # 8. 填充并保存
    out_name = _make_export_filename(t.template_name, EXPORT_DIR)
    out_path = str(EXPORT_DIR / out_name)
    logger.info(f"[table_fill] 开始填充模板: {out_name}")
    _src_file = best_source.get("file_path") if best_source else None
    await asyncio.to_thread(fill_table_template, t.template_path, out_path, rows_data, tpl_info, _src_file, sum_config=sum_config, stats_config=stats_config)
    logger.info(f"[table_fill] 填充完成")

    # 9. 计算统计
    filled_cols = 0
    if col_names:
        all_check = mapped if mapped else []
        if not all_check and isinstance(rows_data, dict):
            for tbl_rows in rows_data.values():
                all_check.extend(tbl_rows)
        for c in col_names:
            has_any = any(row.get(c) is not None and str(row.get(c, "")).strip() and str(row.get(c, "")) != "None" for row in all_check[:100]) if all_check else False
            if has_any:
                filled_cols += 1

    accuracy = round(filled_cols / max(len(col_names), 1), 4) if col_names else 1.0

    # 10. 保存映射记录
    db.query(TemplateMapping).filter(TemplateMapping.template_id == t.id).delete()
    details = []
    for cn in col_names:
        src_col = col_mapping.get(cn, cn)
        # 检查映射后的数据中是否有该列的值
        has_data = False
        if mapped:
            has_data = any(str(row.get(cn, "")).strip() for row in mapped[:10])
        if not has_data and isinstance(rows_data, dict):
            # 多表格模式，检查所有表格
            for tbl_rows in rows_data.values():
                if any(str(row.get(cn, "")).strip() for row in tbl_rows[:10]):
                    has_data = True
                    break
        # 动态计算置信度：多维度加权评分
        all_check_rows = mapped if mapped else []
        if not all_check_rows and isinstance(rows_data, dict):
            for tbl_rows in rows_data.values():
                all_check_rows.extend(tbl_rows)
        
        src_match_score = best_source.get("match_score", 0.7)
        conf = _calc_confidence(cn, all_check_rows, col_mapping=col_mapping, match_score=src_match_score, mode="table")
        has_data = conf > 0
        is_weak = bool(best_source.get("weak_fallback"))
        if is_weak:
            conf = min(conf, _WEAK_FALLBACK_CONFIDENCE_CAP)
        need_confirm = 1 if is_weak else (0 if conf >= 0.90 else 1)
        src_pos = f"列映射: {cn} <- {src_col}（弱匹配兜底）" if is_weak else f"列映射: {cn} <- {src_col}"
        src_file_label = best_source.get("file_name", "")
        if is_weak and src_file_label:
            src_file_label = f"{src_file_label}（弱匹配兜底）"

        db.add(TemplateMapping(
            template_id=t.id, field_name=cn, matched_value=f"[{total_rows}行数据]" if has_data else "",
            confidence=conf, source_file=src_file_label,
            source_position=src_pos, need_confirm=need_confirm
        ))
        details.append({
            "field": cn, "value": f"[{total_rows}行数据]" if has_data else "",
            "confidence": conf, "source_file": src_file_label,
            "need_confirm": bool(need_confirm)
        })
    db.commit()

    return {
        "message": "表格填充完成", "template_id": t.id, "mode": "table_fill",
        "field_count": len(col_names), "filled_count": filled_cols,
        "total_rows": total_rows, "accuracy": accuracy,
        "output_file": out_name, "source_file": best_source.get("file_name", ""),
        "details": details,
    }


def _match_datetime_value(actual, expected_str) -> bool:
    """匹配日期时间值"""
    if actual is None:
        return False
    a = str(actual).strip()
    e = str(expected_str).strip()
    if a == e:
        return True
    # 日期部分匹配
    a_date = a[:10]
    e_date = e[:10]
    if a_date == e_date:
        if len(e) > 10:
            a_time = a[11:19] if len(a) > 11 else ""
            e_time = e[11:19] if len(e) > 11 else ""
            return a_time == e_time
        return True
    return False


def _infer_expected_row_count(template_name: str, user_req: str) -> Optional[int]:
    """从模板名或用户要求中推断预期行数（如「百强」→100，「十强」→10）。"""
    text = (template_name or "") + " " + (user_req or "")
    cn_map = {"百": 100, "十": 10, "二十": 20, "三十": 30, "五十": 50, "千": 1000}
    for cn, num in cn_map.items():
        if f"{cn}强" in text or f"{cn}大" in text:
            return num
    m = re.search(r'(\d+)\s*(?:强|大|名|个|座|城)', text)
    if m:
        return int(m.group(1))
    m2 = re.search(r'(?:top|TOP)\s*(\d+)', text)
    if m2:
        return int(m2.group(1))
    return None


async def _fill_table_with_llm_fallback(t, tpl_info, col_names, user_req, file_list, db, user):
    """当数据源匹配度低时，用 LLM 从文档中提取表格数据（分批提取）"""
    from services.vector_store import search as vs_search

    logger.info(
        f"[table_fill] LLM fallback: 小批提取 max_tokens={_TABLE_FILL_BATCH_MAX_TOKENS}, "
        f"gap补漏 max_tokens={_TABLE_FILL_GAP_MAX_TOKENS}, chunk分数≥{_TABLE_FILL_CHUNK_SCORE_MIN}, "
        f"并发≤{_TABLE_FILL_LLM_CONCURRENCY}"
    )
    # 搜索相关文档片段（两路并行检索，减少等待时间）
    query = " ".join(col_names[:5]) + " " + (user_req or "")
    narrative_q = (
        "GDP 人均 一般公共预算收入 常住人口 亿元 万元 地级市 直辖市 "
        "区域经济 万亿 最强 西部大开发 引擎 省份 城市"
    )
    chunks_main, chunks_extra = await asyncio.gather(
        asyncio.to_thread(vs_search, query, 320, user_id=user.id),
        asyncio.to_thread(vs_search, narrative_q, 160, user_id=user.id),
    )
    merged_pre = _merge_retrieved_chunks(chunks_main, chunks_extra)
    filtered = [c for c in merged_pre if c.get("score", 0) > _TABLE_FILL_CHUNK_SCORE_MIN]
    chunks = filtered if filtered else (merged_pre[:120] if len(merged_pre) > 60 else merged_pre[:60])
    logger.info(
        f"[table_fill] 检索合并: 主查询 {len(chunks_main)} 条 + 叙事补充 {len(chunks_extra)} 条 → "
        f"去重后 {len(merged_pre)} 条, 分数过滤后 {len(chunks)} 个有效 chunks"
    )
    
    # 小批量多批：降低单批噪声与输出压力，并行后总耗时接近「最慢一批」而非各批之和
    batches = []
    batch = []
    batch_len = 0
    BATCH_MAX_CHARS = int(os.getenv("TABLE_FILL_BATCH_MAX_CHARS", "15000"))
    CHUNK_TEXT_MAX = int(os.getenv("TABLE_FILL_CHUNK_TEXT_MAX", "8000"))

    all_chunk_texts = [(c.get("chunk_text", "") or "")[:CHUNK_TEXT_MAX] for c in chunks if (c.get("chunk_text") or "").strip()]
    total_chars = sum(len(t) for t in all_chunk_texts)

    # 动态分批：目标拆成 3~5 个并行批次，充分利用 API 并发降低总耗时
    # 每批输出行数少 → LLM 生成更快，多批并行 → 总耗时 ≈ 最慢一批
    TARGET_PARALLEL = int(os.getenv("TABLE_FILL_TARGET_PARALLEL", "4"))
    if total_chars > 0 and TARGET_PARALLEL > 1:
        dynamic_size = max(2000, total_chars // TARGET_PARALLEL)
        effective_batch_max = min(BATCH_MAX_CHARS, dynamic_size)
    else:
        effective_batch_max = BATCH_MAX_CHARS

    for text in all_chunk_texts:
        if batch_len + len(text) > effective_batch_max and batch:
            batches.append("\n\n---\n\n".join(batch))
            batch = []
            batch_len = 0
        batch.append(text)
        batch_len += len(text)
    if batch:
        batches.append("\n\n---\n\n".join(batch))
    
    if not batches:
        batches = ["\n\n---\n\n".join((c.get("chunk_text", "") or "")[:CHUNK_TEXT_MAX] for c in chunks if (c.get("chunk_text") or "").strip())] or ["(无可用文档片段)"]

    logger.info(f"[table_fill] 划分成 {len(batches)} 个批次: " + ", ".join(f"批次{i+1}:{len(b)}字符" for i, b in enumerate(batches)))

    sys_prompt = f"""你是数据提取助手。根据文档内容，提取表格数据。
模板列名（你必须原样使用这些列名作为JSON的key，不要改动任何字符）：{json.dumps(col_names, ensure_ascii=False)}
用户要求：{user_req or '智能填表'}

【极其重要】
1. JSON中的key必须和上面给出的模板列名完全一致，一个字符都不能改
2. 数值只输出纯数字，不要加千分位逗号（如写 14415.9 而不是 14,415.9）
3. 每行对应一个实体；第一列/名称列的取值须与列名语义一致：若列名含「省」「自治区」「特别行政区」等省级含义，应填省级行政区名称，不要用地级市名填省级列；若列名表示城市/地市，填城市简称且不要带「市」字（如上海、北京、深圳、重庆）。提取所有符合条件的行，不限数量，绝不截断
4. 如果某个字段在文本中找不到，设为 null
5. 不要自行添加"合计""总计""小计"等汇总行
6. 返回JSON数组：[{{"{col_names[0]}": "值1", "{col_names[1] if len(col_names) > 1 else '列2'}": 数值, ...}}, ...]
只返回JSON数组，不要解释。"""

    async def _extract_one_batch(bi: int, ctx: str, sem: asyncio.Semaphore) -> list:
        """提取单个批次，返回行列表（失败返回空列表）"""
        async with sem:  # 限制并发数，避免 API 限速
            try:
                logger.info(f"[table_fill] 开始处理批次 {bi+1}/{len(batches)}，字符数: {len(ctx)}")
                raw = await asyncio.wait_for(
                    call_llm(
                        [{"role": "user", "content": f"从以下文档中提取所有符合条件的数据（第{bi+1}批，共{len(batches)}批），必须提取全部数据，不能遗漏：\n\n{ctx}"}],
                        system=sys_prompt,
                        temperature=0.0,
                        max_tokens=_TABLE_FILL_BATCH_MAX_TOKENS,
                    ),
                    timeout=_TABLE_FILL_LLM_TIMEOUT_SEC,
                )
                r = _normalize_llm_json_text(raw.get("text") or "")
                if _llm_stopped_by_output_limit(raw.get("stop_reason", "")):
                    logger.warning(
                        f"[table_fill] LLM batch {bi+1}: 因输出上限停止(stop_reason={raw.get('stop_reason')!r})，"
                        f"本批 JSON 可能被截断；可提高 TABLE_FILL_BATCH_MAX_TOKENS（当前 {_TABLE_FILL_BATCH_MAX_TOKENS}）或略增大 TABLE_FILL_BATCH_MAX_CHARS"
                    )
                m = re.search(r'\[[\s\S]*\]', r)
                if not m:
                    # 截断时尝试修复不完整的 JSON 数组
                    if _llm_stopped_by_output_limit(raw.get("stop_reason", "")):
                        m2 = re.search(r'\[[\s\S]*', r)
                        if m2:
                            partial = m2.group().rstrip().rstrip(',')
                            last_brace = partial.rfind('}')
                            if last_brace > 0:
                                partial = partial[:last_brace+1] + ']'
                                try:
                                    rows = json.loads(partial)
                                    if isinstance(rows, list):
                                        logger.info(f"[table_fill] LLM batch {bi+1}: 截断 JSON 修复成功, 恢复 {len(rows)} 行")
                                        return rows
                                except json.JSONDecodeError:
                                    pass
                    logger.warning(f"[table_fill] LLM batch {bi+1}: 未找到JSON数组，响应长度={len(r)}")
                    return []
                try:
                    rows = json.loads(m.group())
                except json.JSONDecodeError as je:
                    # 截断导致 JSON 不完整，尝试修复
                    partial_text = m.group().rstrip().rstrip(',')
                    last_brace = partial_text.rfind('}')
                    if last_brace > 0:
                        partial_text = partial_text[:last_brace+1] + ']'
                        try:
                            rows = json.loads(partial_text)
                            if isinstance(rows, list):
                                logger.info(f"[table_fill] LLM batch {bi+1}: 截断 JSON 修复成功, 恢复 {len(rows)} 行")
                                return rows
                        except json.JSONDecodeError:
                            pass
                    logger.warning(
                        f"[table_fill] LLM batch {bi+1}: JSON 解析失败(常见原因为 max_tokens 截断): {je}; "
                        f"响应长度={len(r)}, 尾部预览={r[-200:]!r}"
                    )
                    return []
                if not isinstance(rows, list):
                    return []
                logger.info(f"[table_fill] LLM batch {bi+1}: 提取 {len(rows)} 行")
                return rows
            except Exception as e:
                logger.error(f"[table_fill] LLM batch {bi+1} 失败: {e}")
                return []

    # 并发上限由 TABLE_FILL_LLM_CONCURRENCY 控制，避免 API 限速
    sem = asyncio.Semaphore(_TABLE_FILL_LLM_CONCURRENCY)
    tasks = [_extract_one_batch(bi, ctx, sem) for bi, ctx in enumerate(batches)]
    batch_results = list(await asyncio.gather(*tasks))

    # 对失败的批次重试（两轮；同轮内并行重试，墙钟接近最慢一批）
    for attempt in range(1, 3):
        failed_indices = [i for i, r in enumerate(batch_results) if not r]
        if not failed_indices:
            break
        logger.info(f"[table_fill] {len(failed_indices)} 个批次失败，第{attempt}轮并行重试: {[i+1 for i in failed_indices]}")
        if attempt > 1:
            await asyncio.sleep(2.0)

        async def _retry_batch(fi: int):
            res = await _extract_one_batch(fi, batches[fi], sem)
            return fi, res

        retry_pairs = await asyncio.gather(*[_retry_batch(fi) for fi in failed_indices])
        for fi, retry_result in retry_pairs:
            if retry_result:
                batch_results[fi] = retry_result
                logger.info(f"[table_fill] 批次 {fi+1} 第{attempt}轮重试成功: {len(retry_result)} 行")
            else:
                logger.warning(f"[table_fill] 批次 {fi+1} 第{attempt}轮重试仍失败")

    nb_nonempty = sum(1 for br in batch_results if br)
    key_batch_presence: Dict[str, int] = {}
    for batch_rows in batch_results:
        if not batch_rows:
            continue
        keys_in_batch = set()
        hdr0 = col_names[0]
        for row in batch_rows:
            k_raw = _row_entity_label_for_header(row, hdr0)
            if k_raw:
                keys_in_batch.add(_table_first_col_dedupe_key(hdr0, k_raw))
        for k in keys_in_batch:
            key_batch_presence[k] = key_batch_presence.get(k, 0) + 1

    # 按批次顺序合并去重（保证结果稳定）
    all_rows = []
    existing_keys: set = set()
    hdr0 = col_names[0]
    for bi, batch_rows in enumerate(batch_results):
        new_count = 0
        for row in batch_rows:
            # 清洗 LLM 返回的千分位逗号数值，如 "14,415.90" → "14415.90"
            row = {k: _clean_llm_numeric(v) for k, v in row.items()}
            key_raw = _row_entity_label_for_header(row, hdr0)
            dedupe = _table_first_col_dedupe_key(hdr0, key_raw)
            if dedupe and dedupe not in existing_keys:
                row[hdr0] = dedupe
                row["_llm_batch_support"] = key_batch_presence.get(dedupe, 0)
                row["_llm_from_gap"] = False
                all_rows.append(row)
                existing_keys.add(dedupe)
                new_count += 1
        logger.info(f"[table_fill] batch {bi+1} 合并: 新增 {new_count} 行, 累计 {len(all_rows)} 行")

    # 补漏：正序（高相关优先）+ 逆序（低相关/叙事优先）各一轮，减轻单轮 11 万字截断导致漏提
    # 优化：从模板名/用户要求中推断预期行数，已满足时跳过 gap_fill 节省时间
    expected_row_count = _infer_expected_row_count(t.template_name, user_req)
    skip_gap = False
    if expected_row_count and len(all_rows) >= expected_row_count:
        logger.info(
            f"[table_fill] 主批次已提取 {len(all_rows)} 行 >= 预期 {expected_row_count} 行，跳过 gap_fill 补漏"
        )
        skip_gap = True

    gap_added = 0
    if not skip_gap:
        for rev in (False, True):
            gap_rows = await _gap_fill_table_rows_llm(
                col_names, user_req, existing_keys, chunks, reverse_order=rev
            )
            for row in gap_rows:
                key_raw = _row_entity_label_for_header(row, hdr0)
                dedupe = _table_first_col_dedupe_key(hdr0, key_raw)
                if dedupe and dedupe not in existing_keys:
                    row[hdr0] = dedupe
                    row["_llm_batch_support"] = 0
                    row["_llm_from_gap"] = True
                    all_rows.append(row)
                    existing_keys.add(dedupe)
                    gap_added += 1
            # 正序轮没有补到任何行，逆序轮大概率也补不到，跳过
            if not rev and gap_added == 0:
                logger.info("[table_fill] gap_fill 正序轮无新增，跳过逆序轮")
                break
    if gap_added:
        logger.info(f"[table_fill] gap_fill(正序+逆序) 合并新增 {gap_added} 行, 累计 {len(all_rows)} 行")

    rows_data = all_rows
    logger.info(f"[table_fill] LLM 总共提取 {len(rows_data)} 行")

    # 过滤 LLM 幻觉合计行
    before_clean = len(rows_data)
    rows_data = [row for row in rows_data if not _is_summary_row(row, col_names)]
    if len(rows_data) < before_clean:
        logger.info(f"[table_fill] 清理幻觉合计行: 移除 {before_clean - len(rows_data)} 行")

    rows_data = _apply_city_table_row_cleanup_pre_merge(rows_data, col_names)
    rows_data = _merge_city_duplicate_rows(rows_data, col_names)
    rows_data = _apply_city_table_row_cleanup_post_merge(rows_data, col_names)

    llm_batch_agreement = _compute_llm_multi_batch_agreement(rows_data, nb_nonempty)
    logger.info(
        f"[table_fill] 多批采样: 非空批次数={nb_nonempty}, 实体批次覆盖一致性(均值)={round(llm_batch_agreement, 4)} "
        f"（越高表示越多实体在多个独立批次中出现，受单批噪声影响越小）"
    )
    _strip_llm_row_meta(rows_data)

    # 排序（解析用户要求中的排序意图；无则按首列数值列默认降序）
    parsed_conditions = parse_filter_conditions(user_req)
    sort_config = parsed_conditions.get("sort")
    if sort_config and rows_data:
        rows_data = sort_data(rows_data, sort_config, col_names)
        logger.info(f"[table_fill] LLM fallback 排序完成: config={sort_config}")
    elif col_names and rows_data:
        rows_data = sort_data_by_first_numeric_column(rows_data, col_names)
        logger.info("[table_fill] LLM fallback 未指定排序，已尝试按首列数值列默认降序排序")

    # 求和
    sum_config = parsed_conditions.get("sum")
    if sum_config:
        logger.info(f"[table_fill] LLM fallback 求和配置: row={sum_config.get('row')}, col={sum_config.get('col')}")

    # 统计
    stats_config = parsed_conditions.get("stats")
    if stats_config:
        logger.info(f"[table_fill] LLM fallback 统计配置: max={stats_config.get('max')}, min={stats_config.get('min')}, avg={stats_config.get('avg')}")

    out_name = _make_export_filename(t.template_name, EXPORT_DIR)
    out_path = str(EXPORT_DIR / out_name)
    await asyncio.to_thread(fill_table_template, t.template_path, out_path, rows_data, tpl_info, sum_config=sum_config, stats_config=stats_config)

    # 实际数据源文件名（用户勾选的文件）
    source_names = ", ".join(f.get("file_name", "") for f in file_list if f.get("file_name")) or "LLM提取"

    # 动态计算置信度（多维度加权评分）
    db.query(TemplateMapping).filter(TemplateMapping.template_id == t.id).delete()
    details = []
    filled_cols = 0
    for cn in col_names:
        conf = min(
            _calc_confidence(cn, rows_data, mode="llm", llm_multi_batch_agreement=llm_batch_agreement),
            _LLM_FALLBACK_CONFIDENCE_CAP,
        )
        has_data = conf > 0
        if has_data:
            filled_cols += 1
        
        db.add(TemplateMapping(
            template_id=t.id, field_name=cn, matched_value=f"[{len(rows_data)}行]" if has_data else "",
            confidence=conf, source_file=source_names + "（LLM提取，请核实）",
            need_confirm=1
        ))
        details.append({"field": cn, "value": f"[{len(rows_data)}行]" if has_data else "", "confidence": conf, "source_file": source_names + "（LLM提取，请核实）", "need_confirm": True})
    db.commit()

    accuracy = round(filled_cols / max(len(col_names), 1), 4) if col_names else 1.0
    return {
        "message": "LLM提取填充完成", "template_id": t.id, "mode": "table_fill",
        "field_count": len(col_names), "filled_count": filled_cols,
        "total_rows": len(rows_data),
        "accuracy": accuracy,
        "output_file": out_name, "details": details,
    }


# ============================================================
# 字段填充模式（类型C：统计公报指标提取）
# ============================================================

async def _fill_field_mode(t, tpl_info, db, user, source_file_ids=None):
    """旧的字段填充模式：规则提取 + LLM 匹配"""
    fields = tpl_info.get("fields", [])
    if not fields:
        raise HTTPException(400, "未识别到字段")
    fnames = [f["field_name"] for f in fields]

    logger.info(f"[field_fill] user_requirement={str(t.user_requirement or '')[:200]}")
    logger.info(f"[field_fill] 模板字段({len(fnames)}): {fnames[:20]}")

    if source_file_ids:
        ufiles = db.query(FileInfo).filter(FileInfo.id.in_(source_file_ids), FileInfo.upload_user == user.id).all()
    else:
        ufiles = db.query(FileInfo).filter(FileInfo.upload_user == user.id).all()
    logger.info(f"[field_fill] 数据源文件数: {len(ufiles)}")
    exts = []
    all_extract_dict = {}
    for f in ufiles:
        rs = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).all()
        if rs:
            fdict = {r.field_name: r.field_value for r in rs}
            exts.append({"file_id": f.id, "file_name": f.file_name, "source_type": "table" if f.file_type in ("xlsx", "xls") else "paragraph", "fields": fdict})
            all_extract_dict.update(fdict)
    fused_list = fuse(exts)
    fused_dict = {r["field_name"]: r["field_value"] for r in fused_list}
    fused_source = {r["field_name"]: r for r in fused_list}

    direct = _direct_match(fnames, fused_dict, all_extract_dict)
    unmatched = [fn for fn in fnames if fn not in direct or not direct[fn]]
    matched = dict(direct)
    logger.info(f"[field_fill] direct_match={len(direct)}, unmatched={len(unmatched)}")

    if unmatched:
        search_queries = []
        for fn in unmatched:
            if len(fn) > 15:
                keywords = re.findall(r'[\u4e00-\u9fff]{2,6}', fn)
                search_queries.append(" ".join(keywords[:5]))
            else:
                search_queries.append(fn)

        relevant_chunks = []
        seen = set()
        try:
            for sq in search_queries:
                for h in search_for_fields([sq], top_k_per_field=3, user_id=user.id):
                    key = h.get("source_position", "") + h.get("chunk_text", "")[:50]
                    if key not in seen:
                        seen.add(key)
                        relevant_chunks.append(h)
        except Exception as e:
            logger.warning(f"[field_fill] 向量搜索异常，跳过: {e}")

        ctx_text = "\n\n---\n\n".join(list(dict.fromkeys(r["chunk_text"][:500] for r in relevant_chunks))[:20])
        try:
            llm_matched = await match_fields(unmatched, {**fused_dict, **all_extract_dict}, ctx_text, requirement=t.user_requirement or "")
        except Exception as e:
            logger.warning(f"[field_fill] LLM match_fields 第1轮异常: {e}")
            llm_matched = {}
        for fn in unmatched:
            if fn in llm_matched and llm_matched[fn]:
                val = str(llm_matched[fn]).strip()
                if val and val != fn:
                    matched[fn] = val
        logger.info(f"[field_fill] LLM 第1轮补充匹配: {len(llm_matched)} 项")

        still_unmatched = [fn for fn in unmatched if fn not in matched or not matched[fn]]
        if still_unmatched:
            extra = []
            try:
                for fn in still_unmatched:
                    extra.extend(search_for_fields([fn], top_k_per_field=5, user_id=user.id))
            except Exception as e:
                logger.warning(f"[field_fill] 向量搜索(第2轮)异常: {e}")
            if extra:
                extra_ctx = "\n\n---\n\n".join(list(dict.fromkeys(r["chunk_text"][:500] for r in extra))[:15])
                try:
                    llm2 = await match_fields(still_unmatched, {}, extra_ctx, requirement=t.user_requirement or "")
                except Exception as e:
                    logger.warning(f"[field_fill] LLM match_fields 第2轮异常: {e}")
                    llm2 = {}
                for fn in still_unmatched:
                    if fn in llm2 and llm2[fn]:
                        val = str(llm2[fn]).strip()
                        if val and val != fn:
                            matched[fn] = val
            logger.info(f"[field_fill] 第2轮后仍未匹配: {[fn for fn in still_unmatched if fn not in matched or not matched[fn]]}")

    image_src_file = None
    pic_kw = ("picture", "图片", "pic", "image", "照片", "photo", "图像")
    pic_fields = [fn for fn in fnames if any(k in fn.lower() for k in pic_kw)]
    xlsx_files = [f for f in ufiles if (f.file_type or "").lower() in ("xlsx", "xls") and f.file_path]
    if pic_fields and xlsx_files:
        for xf in xlsx_files:
            try:
                src_cols, src_data = await asyncio.to_thread(load_xlsx_as_table, xf.file_path)
            except Exception:
                continue
            if not src_cols or not src_data:
                continue
            for fn in pic_fields:
                if matched.get(fn):
                    continue
                fn_norm = fn.lower().replace(" ", "")
                candidate_cols = []
                if fn in src_cols:
                    candidate_cols.append(fn)
                for c in src_cols:
                    c_norm = c.lower().replace(" ", "")
                    if fn_norm and (fn_norm in c_norm or c_norm in fn_norm):
                        if c not in candidate_cols:
                            candidate_cols.append(c)
                if not candidate_cols:
                    for c in src_cols:
                        if any(k in c.lower() for k in pic_kw):
                            candidate_cols.append(c)
                found_val = ""
                for c in candidate_cols:
                    for row in src_data[:5000]:
                        v = str(row.get(c, "") or "").strip()
                        if "DISPIMG" in v.upper():
                            found_val = v
                            break
                    if found_val:
                        break
                if found_val:
                    matched[fn] = found_val
                    image_src_file = xf.file_path
            if image_src_file and all(matched.get(fn) for fn in pic_fields):
                break

    if any("DISPIMG" in str(v or "").upper() for v in matched.values()) and not image_src_file:
        try:
            from services.image_filler import parse_dispimg_map
            for xf in xlsx_files:
                if parse_dispimg_map(xf.file_path):
                    image_src_file = xf.file_path
                    break
        except Exception:
            pass

    matched = _apply_requirement_formulas(matched, t.user_requirement or "")

    db.query(TemplateMapping).filter(TemplateMapping.template_id == t.id).delete()
    details = []
    for fn in fnames:
        val = matched.get(fn, "")
        src = fused_source.get(fn, {})
        is_direct = fn in direct and direct.get(fn)
        if not val:
            conf = 0
        elif is_direct:
            conf = 0.98 if any(c.isdigit() for c in val) and len(val) < 50 else 0.95
        elif val == fn:
            conf = 0
            val = ""
        elif any(c.isdigit() for c in val) and len(val) < 100:
            conf = 0.92
        elif len(val) > 10:
            conf = 0.85
        else:
            conf = 0.78
        need_confirm = 1 if conf < 0.75 or not val else 0
        db.add(TemplateMapping(template_id=t.id, field_name=fn, matched_value=str(val) if val else "", confidence=conf, source_file=src.get("source_file", ""), source_position=src.get("source_type", ""), need_confirm=need_confirm))
        details.append({"field": fn, "value": str(val) if val else "", "confidence": round(conf, 2), "source_file": src.get("source_file", ""), "need_confirm": bool(need_confirm)})

    out_name = _make_export_filename(t.template_name, EXPORT_DIR)
    out_path = str(EXPORT_DIR / out_name)
    fill_template(t.template_path, out_path, matched, fields, tpl_info, src_file=image_src_file)
    db.commit()

    filled_count = sum(1 for d in details if d["value"])
    accuracy = round(filled_count / max(len(fnames), 1), 4)

    return {
        "message": "填写完成", "template_id": t.id, "mode": "field_fill",
        "field_count": len(fnames), "filled_count": filled_count,
        "accuracy": accuracy, "output_file": out_name, "details": details,
    }


def _direct_match(fnames, fused_dict, all_extract_dict):
    matched = {}
    all_data = {**fused_dict, **all_extract_dict}
    for fn in fnames:
        fn_norm = fn.strip().lower().replace(" ", "").replace("（", "(").replace("）", ")")
        for k, v in all_data.items():
            kn = k.strip().lower().replace(" ", "").replace("（", "(").replace("）", ")")
            if fn_norm == kn:
                val = str(v).strip()
                if val and val != fn and len(val) < 500:
                    matched[fn] = val
                    break
        if fn in matched:
            continue
        if len(fn.strip()) <= 8:
            for k, v in all_data.items():
                kn = k.strip().lower().replace(" ", "")
                val = str(v).strip()
                if (fn_norm in kn or kn in fn_norm) and val and val != fn and val != k and len(val) < 200:
                    if len(val) >= 2 and (any(c.isdigit() for c in val) or len(val) >= 4):
                        matched[fn] = val
                        break
    return matched


# ============================================================
# 其他接口
# ============================================================

def _fill_task_type_ok(col):
    """兼容早期 task_type 未写入或非 'fill' 的历史任务（仅有模板填写会写 output_file）。"""
    return or_(col == "fill", col.is_(None), col == "")


@router.get("/history/{task_id}/download")
def download_fill_by_task(
    task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    """按填写任务 ID 下载导出文件（路径中不含中文文件名，避免编码与库中字段不一致导致 403）。"""
    task = db.query(TaskInfo).filter(
        TaskInfo.id == task_id,
        TaskInfo.user_id == user.id,
        _fill_task_type_ok(TaskInfo.task_type),
        TaskInfo.status == "done",
    ).first()
    if not task or not (task.output_file or "").strip():
        raise HTTPException(404, "记录或文件不存在")
    filename = unicodedata.normalize("NFC", task.output_file.strip())
    if ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    p = EXPORT_DIR / filename
    if not p.exists():
        raise HTTPException(404, "文件不存在或已被清理，请重新填写导出")
    return FileResponse(str(p), filename=filename, media_type="application/octet-stream")


@router.delete("/history/{task_id}")
def delete_history(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """删除一条填写历史记录"""
    task = db.query(TaskInfo).filter(TaskInfo.id == task_id, TaskInfo.user_id == user.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="记录不存在")
    # 同时删除导出文件
    if task.output_file:
        try:
            (EXPORT_DIR / task.output_file).unlink(missing_ok=True)
        except Exception:
            pass
    db.delete(task)
    db.commit()
    return {"message": "已删除"}


class BatchDeleteReq(BaseModel):
    ids: List[int]


@router.post("/history/batch-delete")
def batch_delete_history(req: BatchDeleteReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """批量删除当前用户的填写历史记录"""
    if not req.ids:
        return {"message": "无需删除", "deleted": 0}
    tasks = db.query(TaskInfo).filter(TaskInfo.id.in_(req.ids), TaskInfo.user_id == user.id).all()
    deleted = 0
    for task in tasks:
        if task.output_file:
            try:
                (EXPORT_DIR / task.output_file).unlink(missing_ok=True)
            except Exception:
                pass
        db.delete(task)
        deleted += 1
    db.commit()
    _log(db, user, "批量删除填写记录", f"删除{deleted}条")
    return {"message": f"已删除 {deleted} 条记录", "deleted": deleted}


@router.post("/batch-delete")
def batch_delete_tpl(req: BatchDeleteReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """批量删除当前用户的模板"""
    if not req.ids:
        return {"message": "无需删除", "deleted": 0}
    tpls = db.query(TemplateInfo).filter(
        TemplateInfo.id.in_(req.ids), TemplateInfo.upload_user == user.id
    ).all()
    deleted = 0
    names = []
    for t in tpls:
        try:
            if t.template_path and os.path.exists(t.template_path):
                os.remove(t.template_path)
        except Exception:
            pass
        db.query(TemplateMapping).filter(TemplateMapping.template_id == t.id).delete(synchronize_session=False)
        names.append(t.template_name or "")
        db.delete(t)
        deleted += 1
    db.commit()
    _log(db, user, "批量删除模板", f"删除{deleted}个模板")
    return {"message": f"已删除 {deleted} 个模板", "deleted": deleted}


@router.get("/history")
def fill_history(page: int = 1, page_size: int = 5, keyword: str = "",
                 db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """获取当前用户的模板填写历史（分页+搜索）"""
    q = db.query(TaskInfo).filter(
        TaskInfo.user_id == user.id,
        _fill_task_type_ok(TaskInfo.task_type),
        TaskInfo.status == "done",
        TaskInfo.output_file != None,
        TaskInfo.output_file != ""
    )
    if keyword:
        q = q.filter(TaskInfo.task_name.contains(keyword))
    total = q.count()
    tasks = q.order_by(TaskInfo.end_time.desc()).offset((page - 1) * page_size).limit(page_size).all()
    history = []
    for t in tasks:
        exists = (EXPORT_DIR / t.output_file).exists() if t.output_file else False
        history.append({
            "id": t.id,
            "task_name": t.task_name or "",
            "output_file": t.output_file or "",
            "accuracy": t.accuracy or 0,
            "elapsed": t.elapsed or 0,
            "result_summary": t.result_summary or "",
            "fill_time": fmt_time(t.end_time or t.start_time),
            "file_exists": exists,
        })
    return {"total": total, "page": page, "page_size": page_size, "history": history}

@router.get("/list")
def list_tpl(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ts = db.query(TemplateInfo).filter(TemplateInfo.upload_user == user.id).order_by(TemplateInfo.upload_time.desc()).all()
    return {"total": len(ts), "templates": [
        {"id": t.id, "template_name": t.template_name, "template_type": t.template_type,
         "template_mode": t.template_mode or "field_fill",
         "user_requirement": t.user_requirement or "",
         "field_count": t.field_count or 0, "upload_time": fmt_time(t.upload_time)} for t in ts
    ]}


def _task_owns_export(t: TaskInfo, user: User, db: Session) -> bool:
    if t.related_id is not None:
        tpl = db.query(TemplateInfo).filter(TemplateInfo.id == t.related_id).first()
        if tpl and tpl.upload_user == user.id:
            return True
    return t.user_id is not None and t.user_id == user.id


def _output_file_matches_request(stored: str, req_nfc: str, req_nfd: str) -> bool:
    s = unicodedata.normalize("NFC", (stored or "").strip())
    if s == req_nfc:
        return True
    return unicodedata.normalize("NFD", (stored or "").strip()) == req_nfd


@router.get("/download/{filename}")
def download(filename: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    filename = unicodedata.normalize("NFC", (filename or "").strip())
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        raise HTTPException(400, "非法文件名")
    p = EXPORT_DIR / filename
    if not p.exists():
        raise HTTPException(404, "文件不存在或已被清理，请重新填写导出")
    if user.role == "admin":
        return FileResponse(str(p), filename=filename, media_type="application/octet-stream")

    fn_nfd = unicodedata.normalize("NFD", filename)
    name_variants = list(dict.fromkeys([filename, fn_nfd]))

    candidates = (
        db.query(TaskInfo)
        .filter(
            TaskInfo.output_file.in_(name_variants),
            TaskInfo.status == "done",
            _fill_task_type_ok(TaskInfo.task_type),
        )
        .order_by(TaskInfo.id.desc())
        .all()
    )
    task = next((t for t in candidates if _task_owns_export(t, user, db)), None)

    if not task:
        for t in (
            db.query(TaskInfo)
            .filter(
                TaskInfo.user_id == user.id,
                TaskInfo.status == "done",
                _fill_task_type_ok(TaskInfo.task_type),
            )
            .order_by(TaskInfo.id.desc())
            .limit(300)
            .all()
        ):
            if t.output_file and _output_file_matches_request(t.output_file, filename, fn_nfd):
                task = t
                break

    if not task:
        raise HTTPException(403, "无权下载此文件")
    return FileResponse(str(p), filename=filename, media_type="application/octet-stream")


@router.get("/{tid}/mappings")
def mappings(tid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = db.query(TemplateInfo).filter(TemplateInfo.id == tid, TemplateInfo.upload_user == user.id).first()
    if not t:
        raise HTTPException(403, "无权访问此模板")
    ms = db.query(TemplateMapping).filter(TemplateMapping.template_id == tid).all()
    return {"total": len(ms), "mappings": [
        {"id": m.id, "field_name": m.field_name, "matched_value": m.matched_value,
         "confidence": m.confidence or 0, "source_file": m.source_file or "",
         "source_position": m.source_position or "", "need_confirm": bool(m.need_confirm)} for m in ms
    ]}


@router.delete("/{tid}")
def delete_tpl(tid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    t = db.query(TemplateInfo).filter(TemplateInfo.id == tid, TemplateInfo.upload_user == user.id).first()
    if not t:
        raise HTTPException(404)
    import os
    if os.path.exists(t.template_path):
        os.remove(t.template_path)
    db.query(TemplateMapping).filter(TemplateMapping.template_id == tid).delete()
    db.delete(t)
    db.commit()
    _log(db, user, "删除模板", t.template_name)
    return {"message": "已删除"}
