"""智能模板填表服务

支持的场景：
1. 数据源 + 模板（带合并表头），按列名映射并填入数据
2. 自动跳过省份行 / 英文行 / 空行 / 标题合并行
3. 自动清洗脏数据（如 8659! / 6il3 / 1337() 等）
4. 模板若包含"总和" / "平均值"列，自动计算

实现细节：
- 直接复用模板文件作为输出（保留所有合并单元格、字体、列宽、对齐）
- 仅写入数据行的单元格值（不动样式）
"""
from __future__ import annotations

import re
import os
import shutil
from typing import Optional, Tuple, List, Dict, Any
import logging

import openpyxl
from openpyxl.utils import get_column_letter

logger = logging.getLogger("smart_fill")

# 省份/地区/直辖市常见后缀（用于识别省份行 vs 城市行）
_PROVINCE_SUFFIXES = ("省", "自治区", "特别行政区")
# 直辖市作为城市保留（不当作省份行）
_MUNICIPALITIES = {"北京市", "天津市", "上海市", "重庆市"}

# 数据清洗：保留数字、小数点、负号
_NUM_RE = re.compile(r"[^\d\.\-]")
# 仅保留中文/英文字母/数字用于列名规范化匹配
_NORM_RE = re.compile(r"[\s\(\)（）\[\]【】、,，.。:：;；'\"\\/\\-_!！?？*]+")


# ---------- 工具函数 ----------

def _norm_header(s: Optional[str]) -> str:
    if s is None:
        return ""
    s = str(s).strip()
    # 全角括号转半角
    s = s.replace("（", "(").replace("）", ")")
    # 去掉所有空白与标点
    return _NORM_RE.sub("", s).lower()


def _is_english_row(row_vals: List[Any]) -> bool:
    """判断一行是否主要由英文构成（数据源/模板里的英文表头）"""
    non_empty = [str(v).strip() for v in row_vals if v is not None and str(v).strip()]
    if not non_empty:
        return False
    # 必须含字母才算"英文"；数字单元格不算
    has_letter_cells = [v for v in non_empty if re.search(r"[A-Za-z]", v)]
    if not has_letter_cells:
        return False
    # 全部含字母的单元格都不含中文，且这种单元格至少占比 60%
    no_chinese = sum(1 for v in has_letter_cells if not re.search(r"[\u4e00-\u9fa5]", v))
    if no_chinese != len(has_letter_cells):
        return False
    return len(has_letter_cells) >= max(2, int(len(non_empty) * 0.5))


def _is_empty_row(row_vals: List[Any]) -> bool:
    return all(v is None or str(v).strip() == "" for v in row_vals)


def _is_province_row(row_vals: List[Any]) -> bool:
    """省份/地区分组行：只有第一列有值且像省份名"""
    if not row_vals:
        return False
    first = row_vals[0]
    rest = row_vals[1:]
    if first is None or str(first).strip() == "":
        return False
    if not all(v is None or str(v).strip() == "" for v in rest):
        return False
    name = str(first).strip()
    if name in _MUNICIPALITIES:
        return False
    return any(name.endswith(suf) for suf in _PROVINCE_SUFFIXES) or name.endswith("地区") or name.endswith("盟") or name.endswith("州")


def _looks_like_city(name: str) -> bool:
    name = str(name).strip()
    if not name:
        return False
    if name in _MUNICIPALITIES:
        return True
    if any(name.endswith(suf) for suf in _PROVINCE_SUFFIXES):
        return False
    return True


def _clean_number(v: Any) -> Optional[float]:
    """清洗为数字。修复像 8659! / 6il3 / 1337() 这种异常字符"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v).strip()
    if not s:
        return None
    # 把常见 OCR 错误 i/l/I -> 1, o/O -> 0
    s2 = s.translate(str.maketrans({"i": "1", "l": "1", "I": "1", "o": "0", "O": "0"}))
    s2 = _NUM_RE.sub("", s2)
    # 只保留首个 . 与首个 -
    if s2.count(".") > 1:
        first_dot = s2.find(".")
        s2 = s2[:first_dot + 1] + s2[first_dot + 1:].replace(".", "")
    if s2.count("-") > 1 or (s2.count("-") == 1 and not s2.startswith("-")):
        s2 = s2.replace("-", "")
    try:
        return float(s2) if s2 not in ("", "-", ".") else None
    except ValueError:
        return None


# ---------- 表头识别 ----------

def _read_all_rows(ws) -> List[List[Any]]:
    rows = []
    for r in range(1, ws.max_row + 1):
        rows.append([ws.cell(row=r, column=c).value for c in range(1, ws.max_column + 1)])
    return rows


_HEADER_A_KEYWORDS = {
    "城市", "城 市", "地区", "省份", "省/自治区", "省份/地区",
    "单位", "项目", "名称", "城市名", "城市名称", "地区名称", "区域", "县市", "区县",
    "省", "自治区",
}


def _detect_chinese_header_row(rows: List[List[Any]]) -> int:
    """找到中文细分字段表头所在行（1-based）。
    
    策略：A 列规范化值需精确等于「城市/地区/名称/单位...」之一，
    且其余列至少有 3 个含中文的字段。
    """
    norm_keywords = {_norm_header(k) for k in _HEADER_A_KEYWORDS}
    for i, row in enumerate(rows[:20]):
        a_norm = _norm_header(row[0])
        if not a_norm or a_norm not in norm_keywords:
            continue
        if len(a_norm) > 8:
            continue
        chinese_cells = sum(
            1 for v in row[1:]
            if v is not None and str(v).strip() and re.search(r"[\u4e00-\u9fa5]", str(v))
        )
        if chinese_cells >= 3:
            return i + 1
    # 退路：找含中文最多的前 8 行（且 A 列短）的那一行
    best_i, best_cnt = -1, -1
    for i, row in enumerate(rows[:8]):
        a = str(row[0] or "").strip()
        if not a or len(a) > 12:
            continue
        cnt = sum(1 for v in row[1:] if v is not None and str(v).strip() and re.search(r"[\u4e00-\u9fa5]", str(v)))
        if cnt > best_cnt:
            best_cnt = cnt
            best_i = i
    return (best_i + 1) if best_i >= 0 else 1


def _detect_data_start_row(ws_or_rows, header_row: int) -> int:
    """从 header_row+1 开始往下找数据起始行。
    
    跳过：英文标签行、空行
    """
    if isinstance(ws_or_rows, list):
        rows = ws_or_rows
    else:
        rows = _read_all_rows(ws_or_rows)
    r = header_row  # 1-based
    while r < len(rows):
        row = rows[r]  # 0-based -> rows[r] 是表格中第 r+1 行
        if _is_english_row(row) or _is_empty_row(row):
            r += 1
            continue
        return r + 1  # 转成 1-based
    return r + 1


def _build_column_map(template_headers: List[str], source_headers: List[str]) -> Dict[int, int]:
    """构建模板列索引(1-based) -> 数据源列索引(1-based) 的映射。
    
    逻辑：
    1. 规范化两边表头
    2. 精确匹配优先，否则按包含关系匹配，再否则按位置默认对齐
    """
    src_norm = [_norm_header(h) for h in source_headers]
    tpl_norm = [_norm_header(h) for h in template_headers]
    mapping: Dict[int, int] = {}
    used_src = set()

    # 第一轮：精确匹配
    for ti, t in enumerate(tpl_norm):
        if not t:
            continue
        for si, s in enumerate(src_norm):
            if si in used_src or not s:
                continue
            if t == s:
                mapping[ti + 1] = si + 1
                used_src.add(si)
                break

    # 第二轮：包含匹配
    for ti, t in enumerate(tpl_norm):
        if (ti + 1) in mapping or not t:
            continue
        for si, s in enumerate(src_norm):
            if si in used_src or not s:
                continue
            if t in s or s in t:
                mapping[ti + 1] = si + 1
                used_src.add(si)
                break

    # 第三轮：列名都没识别到的，按相同列位置兜底（适用于"模板与数据源同结构"）
    for ti in range(len(tpl_norm)):
        if (ti + 1) in mapping:
            continue
        if ti < len(src_norm) and (ti + 1) not in used_src and src_norm[ti]:
            mapping[ti + 1] = ti + 1
            used_src.add(ti + 1)
    return mapping


def _find_special_columns(template_headers: List[str]) -> Dict[str, int]:
    """识别模板中的「总和」「平均值」等计算列，返回 {kind: col_idx_1based}"""
    special = {}
    for i, h in enumerate(template_headers):
        n = _norm_header(h)
        if n in ("总和", "合计", "总计", "求和", "sum"):
            special["sum"] = i + 1
        elif n in ("平均值", "均值", "平均", "average", "avg", "mean"):
            special["avg"] = i + 1
    return special


# ---------- 主入口 ----------

def smart_fill(
    template_path: str,
    data_path: str,
    output_path: str,
    *,
    keep_province_groups: bool = False,
    sheet_name: Optional[str] = None,
) -> Dict[str, Any]:
    """智能填充模板。
    
    template_path: 模板 .xlsx 路径（带合并表头）
    data_path:     数据源 .xlsx 路径
    output_path:   输出文件路径
    keep_province_groups: 是否保留省份分组行（在城市间穿插省份名行）
    """
    if not os.path.exists(template_path):
        raise FileNotFoundError(f"模板文件不存在: {template_path}")
    if not os.path.exists(data_path):
        raise FileNotFoundError(f"数据源文件不存在: {data_path}")

    # ---- 1. 直接复制模板作为输出，保留所有合并单元格/样式 ----
    os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
    shutil.copyfile(template_path, output_path)

    # ---- 2. 读取数据源 ----
    src_wb = openpyxl.load_workbook(data_path, data_only=True)
    src_ws = src_wb[sheet_name] if sheet_name and sheet_name in src_wb.sheetnames else src_wb.active
    src_rows = _read_all_rows(src_ws)
    src_header_row = _detect_chinese_header_row(src_rows)
    src_headers = [str(v).strip() if v is not None else "" for v in src_rows[src_header_row - 1]]
    src_data_start = _detect_data_start_row(src_rows, src_header_row)

    # ---- 3. 读取模板（再次打开，用于写入）----
    out_wb = openpyxl.load_workbook(output_path)
    out_ws = out_wb[sheet_name] if sheet_name and sheet_name in out_wb.sheetnames else out_wb.active
    out_rows = _read_all_rows(out_ws)
    tpl_header_row = _detect_chinese_header_row(out_rows)
    tpl_headers = [str(v).strip() if v is not None else "" for v in out_rows[tpl_header_row - 1]]
    tpl_data_start = _detect_data_start_row(out_rows, tpl_header_row)

    # ---- 4. 构建列映射（模板列 -> 数据源列），识别特殊列 ----
    special_cols = _find_special_columns(tpl_headers)
    # 计算时排除 A 列（城市名）和特殊列
    excluded_for_calc = {1} | set(special_cols.values())
    col_map = _build_column_map(tpl_headers, src_headers)

    # ---- 5. 提取数据行（按城市行）----
    extracted: List[Tuple[Optional[str], List[Any]]] = []  # [(province_or_None, [row_vals])]
    cur_province: Optional[str] = None
    for r in range(src_data_start - 1, len(src_rows)):  # src_data_start 是 1-based
        row = src_rows[r]
        if _is_empty_row(row):
            continue
        if _is_english_row(row):
            continue
        if _is_province_row(row):
            cur_province = str(row[0]).strip()
            if keep_province_groups:
                extracted.append((cur_province, ["__PROVINCE__", cur_province]))
            continue
        a = row[0]
        if a is None or not str(a).strip():
            continue
        if not _looks_like_city(str(a).strip()):
            continue
        extracted.append((cur_province, row))

    # ---- 6. 写入模板 ----
    write_row = tpl_data_start
    filled_cities = 0
    cleaned_count = 0

    for province, row in extracted:
        if row and len(row) >= 1 and row[0] == "__PROVINCE__":
            # 省份分组行：只写第一列
            out_ws.cell(row=write_row, column=1, value=row[1])
            write_row += 1
            continue

        city_name = str(row[0]).strip()
        out_ws.cell(row=write_row, column=1, value=city_name)

        # 写入每个映射列
        numeric_vals: List[float] = []
        for tpl_col in range(2, len(tpl_headers) + 1):
            if tpl_col in special_cols.values():
                continue  # 总和/平均值留待后填
            src_col = col_map.get(tpl_col)
            if not src_col:
                continue
            raw = row[src_col - 1] if src_col - 1 < len(row) else None
            cleaned = _clean_number(raw)
            if cleaned is None and raw is not None and str(raw).strip():
                cleaned_count += 1
            if cleaned is not None:
                out_ws.cell(row=write_row, column=tpl_col, value=cleaned)
                if tpl_col not in excluded_for_calc:
                    numeric_vals.append(cleaned)
            else:
                out_ws.cell(row=write_row, column=tpl_col, value=None)

        # 总和 / 平均值
        if "sum" in special_cols:
            out_ws.cell(row=write_row, column=special_cols["sum"], value=round(sum(numeric_vals), 4) if numeric_vals else None)
        if "avg" in special_cols:
            avg_val = round(sum(numeric_vals) / len(numeric_vals), 4) if numeric_vals else None
            out_ws.cell(row=write_row, column=special_cols["avg"], value=avg_val)

        write_row += 1
        filled_cities += 1

    out_wb.save(output_path)
    out_wb.close()
    src_wb.close()

    return {
        "ok": True,
        "output_path": output_path,
        "template_header_row": tpl_header_row,
        "template_data_start": tpl_data_start,
        "source_header_row": src_header_row,
        "source_data_start": src_data_start,
        "column_mapping": col_map,
        "special_columns": special_cols,
        "filled_rows": filled_cities,
        "total_rows_written": write_row - tpl_data_start,
        "cleaned_dirty_values": cleaned_count,
        "template_columns": tpl_headers,
        "source_columns": src_headers,
    }
