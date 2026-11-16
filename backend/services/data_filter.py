"""数据源条件筛选引擎 - 从已上传的 xlsx/docx 中按条件筛选数据"""
import os
import re
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Any, Optional, Tuple
from pathlib import Path
from difflib import SequenceMatcher
import openpyxl

logger = logging.getLogger("data_filter")

# read_only 下部分 xlsx（例如工作簿缺省样式）会错误报告 max_row/max_column=1，
# 且 for cell in ws[row] 只遍历到一列；用 iter_rows 显式 max_col 可正常读出表头。
_OPENPYXL_READ_WIDE_MAX_COL = 512


def _iter_sheet_row_cells(ws, row_1based: int):
    """取工作表一行上的单元格序列（兼容 read_only 维度失真）。"""
    return next(
        ws.iter_rows(
            min_row=row_1based,
            max_row=row_1based,
            min_col=1,
            max_col=_OPENPYXL_READ_WIDE_MAX_COL,
            values_only=False,
        ),
        None,
    )


def _find_header_row(ws, max_scan: int = 15):
    """扫描前 max_scan 行，找到真实表头行。
    
    判断标准：
    - 至少2列有值（降低阈值以兼容合并单元格/空格占位场景）
    - 大多数列是短文本（非纯数字、非超长段落）
    - 优先选择非数字列最多的行
    跳过公司信息/标题等单列大段文本行。
    返回 (header_row_index_1based, columns_list)
    """
    best_ri = 1
    best_cols = []
    best_score = -1

    for scan_ri in range(1, max_scan + 1):
        row_cells = []
        row = _iter_sheet_row_cells(ws, scan_ri)
        if not row:
            continue
        for cell in row:
            v = cell.value
            # 处理合并单元格：尝试从合并区域获取值
            if v is None and hasattr(ws, 'merged_cells'):
                for rng in ws.merged_cells.ranges:
                    if rng.min_row <= scan_ri <= rng.max_row and rng.min_col <= cell.column <= rng.max_col:
                        v = ws.cell(row=rng.min_row, column=rng.min_col).value
                        break
            if v is None:
                continue
            raw = str(v)
            # 清理空格占位：全角空格、多个空格、制表符等
            cleaned = raw.replace('\u3000', ' ').replace('\t', ' ').strip()
            if not cleaned:
                continue
            text = ' '.join(part.strip() for part in cleaned.split('\n') if part.strip())
            if len(text) > 200 and '\n' in raw:
                continue
            if text.startswith('='):
                continue
            row_cells.append(text)

        if len(row_cells) < 2:
            continue

        header_score = 0
        for c in row_cells:
            if c.replace('.', '').replace('-', '').replace(':', '').replace(' ', '').isdigit():
                header_score += 0.2
            elif len(c) <= 60:
                header_score += 1.0
            else:
                header_score += 0.5

        if header_score > best_score:
            best_score = header_score
            best_cols = row_cells
            best_ri = scan_ri

    return best_ri, best_cols


def _find_header_row_idx_from_matrix(all_rows: List[List[Any]], max_scan: int = 15) -> Tuple[int, List[str]]:
    """与 _find_header_row 相同判定规则，但输入为行值矩阵（用于 .xls / xlrd）。返回 (表头行 0-based 索引, best_cols)。"""
    best_idx = 0
    best_cols: List[str] = []
    best_score = -1.0
    n = min(len(all_rows), max_scan)
    for scan_idx in range(n):
        row = all_rows[scan_idx]
        row_cells: List[str] = []
        for v in row:
            if v is None:
                continue
            raw = str(v).replace('\u3000', ' ').replace('\t', ' ').strip()
            if not raw:
                continue
            text = " ".join(part.strip() for part in raw.split("\n") if part.strip())
            if len(text) > 200 and "\n" in raw:
                continue
            if text.startswith("="):
                continue
            row_cells.append(text)
        if len(row_cells) < 2:
            continue
        header_score = 0.0
        for c in row_cells:
            if c.replace(".", "").replace("-", "").replace(":", "").replace(" ", "").isdigit():
                header_score += 0.2
            elif len(c) <= 60:
                header_score += 1.0
            else:
                header_score += 0.5
        if header_score > best_score:
            best_score = header_score
            best_cols = row_cells
            best_idx = scan_idx
    return best_idx, best_cols


def _matrix_row_to_header_labels(row: List[Any]) -> List[str]:
    """将矩阵中的一行转为表头标签列表（与 _find_header_row 单格规则一致）。"""
    labels: List[str] = []
    for v in row:
        if v is None:
            labels.append("")
            continue
        raw = str(v).strip()
        if not raw:
            labels.append("")
            continue
        text = " ".join(part.strip() for part in raw.split("\n") if part.strip())
        if len(text) > 200 and "\n" in raw:
            labels.append("")
            continue
        if text.startswith("="):
            labels.append("")
            continue
        labels.append(text)
    while labels and not labels[-1]:
        labels.pop()
    return labels


def _columns_from_label_list(labels: List[str]) -> List[str]:
    """空列用 col_i 占位，并去掉尾部空列，供语义匹配与加载共用。"""
    if not labels:
        return []
    columns = [lab if lab else f"col_{i}" for i, lab in enumerate(labels)]
    while columns and str(columns[-1]).startswith("col_"):
        columns.pop()
    return columns


def _read_openpyxl_row_header_labels(ws, row_1based: int) -> List[str]:
    """读取工作表指定行作为表头时的列标签（openpyxl，1-based 行号）。"""
    labels: List[str] = []
    row = _iter_sheet_row_cells(ws, row_1based)
    if not row:
        return []
    for cell in row:
        v = cell.value
        if v is None:
            labels.append("")
            continue
        raw = str(v).strip()
        if not raw:
            labels.append("")
            continue
        text = " ".join(part.strip() for part in raw.split("\n") if part.strip())
        if len(text) > 200 and "\n" in str(v):
            labels.append("")
            continue
        if text.startswith("="):
            labels.append("")
            continue
        labels.append(text)
    while labels and not labels[-1]:
        labels.pop()
    return labels


# 纯数值 / 日期形态：用于区分「表头的标签」和「数据行的值」
_VALUE_LIKE_RE = re.compile(
    r"^[+-]?\d[\d,]*(?:\.\d+)?\s*%?$"          # 1234 / 12.5 / 1,234 / 30%
    r"|^\d{4}\s*[-/年]\s*\d{1,2}"                 # 2024-07 / 2024年7月
    r"|^\d{4}\s*年?$"                               # 2024 / 2024年
)


def _looks_like_value(s: str) -> bool:
    return bool(_VALUE_LIKE_RE.match(str(s).strip()))


def _is_plausible_header_row(labels, min_labels: int = 3) -> bool:
    """判断某一行像不像表头。

    原实现硬要求「非空标签 ≥ 3」，副作用是「城市 / GDP」这类两列表格
    永远进不了结构化路径，只能退回 LLM 兜底（慢、按次计费、结果不稳定）。

    这里换成更本质的判据——表头由「标签」构成，数据行至少有一个「值」：
      - 非空 ≥ 3：直接通过（保持原行为，零回归）；
      - 非空 == 2：两个单元格都不能是纯数值/日期，排除「上海 / 52000」这类数据行。
    """
    nonempty = [str(x).strip() for x in (labels or []) if x is not None and str(x).strip()]
    if len(nonempty) >= min_labels:
        return True
    if len(nonempty) == 2 and not any(_looks_like_value(x) for x in nonempty):
        return True
    return False


def _enumerate_xlsx_header_candidate_rows(fp: str, sheet_name: str, max_scan: int = 30) -> List[Tuple[int, List[str]]]:
    """列出工作表前 max_scan 行中可作为表头的候选 (行号1-based, 标签列表)。"""
    out: List[Tuple[int, List[str]]] = []
    wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            return out
        ws = wb[sheet_name]
        last = max_scan
        mr = ws.max_row or 0
        if mr > 1:
            last = min(max_scan, mr)
        for ri in range(1, last + 1):
            labels = _read_openpyxl_row_header_labels(ws, ri)
            if _is_plausible_header_row(labels):
                out.append((ri, labels))
    finally:
        wb.close()
    return out


def _enumerate_xls_header_candidate_rows(file_path: str, sheet_name: str, max_scan: int = 30) -> List[Tuple[int, List[str]]]:
    import xlrd

    book = xlrd.open_workbook(file_path, formatting_info=False)
    all_names = book.sheet_names()
    sheet = book.sheet_by_name(sheet_name) if sheet_name and sheet_name in all_names else book.sheet_by_index(0)
    out: List[Tuple[int, List[str]]] = []
    last = min(max_scan, sheet.nrows)
    for ri in range(last):
        row = list(sheet.row_values(ri))
        labels = _matrix_row_to_header_labels(row)
        if _is_plausible_header_row(labels):
            out.append((ri + 1, labels))
    return out


def _read_xlsx_sample_dicts(
    fp: str,
    sheet_name: str,
    header_row_1based: int,
    columns: List[str],
    n: int = 18,
) -> List[Dict[str, Any]]:
    """表头行之后读取若干数据行（read_only），用于判别列值是否与表头语义一致。"""
    out: List[Dict[str, Any]] = []
    wb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
    try:
        if sheet_name not in wb.sheetnames:
            return out
        ws = wb[sheet_name]
        max_r = ws.max_row or 0
        end = min(max_r, header_row_1based + n) if max_r > header_row_1based else header_row_1based + n
        if end <= header_row_1based:
            return out
        mc = max(len(columns), 1)
        for row in ws.iter_rows(
            min_row=header_row_1based + 1,
            max_row=end,
            min_col=1,
            max_col=max(mc, _OPENPYXL_READ_WIDE_MAX_COL),
            values_only=True,
        ):
            rd: Dict[str, Any] = {}
            for i, col_name in enumerate(columns):
                rd[col_name] = row[i] if i < len(row) else None
            if any(v is not None and str(v).strip() not in ("", "None") for v in rd.values()):
                out.append(rd)
    finally:
        wb.close()
    return out


def _read_xls_sample_dicts(
    fp: str,
    sheet_name: str,
    header_row_1based: int,
    columns: List[str],
    n: int = 18,
) -> List[Dict[str, Any]]:
    import xlrd

    out: List[Dict[str, Any]] = []
    book = xlrd.open_workbook(fp, formatting_info=False)
    all_names = book.sheet_names()
    sh = book.sheet_by_name(sheet_name) if sheet_name and sheet_name in all_names else book.sheet_by_index(0)
    hi = max(0, int(header_row_1based) - 1)
    for rxi in range(hi + 1, min(sh.nrows, hi + 1 + n)):
        row = list(sh.row_values(rxi))
        rd = {columns[i]: row[i] if i < len(row) else None for i in range(len(columns))}
        if any(v is not None and str(v).strip() not in ("", "None") for v in rd.values()):
            out.append(rd)
    return out


def load_xls_as_table(
    file_path: str,
    sheet_name: str = None,
    max_rows: int = 50000,
    header_row_1based: Optional[int] = None,
) -> Tuple[List[str], List[Dict]]:
    """Excel 97-2003 .xls（openpyxl 不支持）。"""
    import xlrd

    book = xlrd.open_workbook(file_path, formatting_info=False)
    names = book.sheet_names()
    sheet = book.sheet_by_name(sheet_name) if sheet_name and sheet_name in names else book.sheet_by_index(0)
    all_rows = [list(sheet.row_values(rx)) for rx in range(sheet.nrows)]
    if not all_rows:
        return [], []

    def _nn(h) -> bool:
        return h is not None and str(h).strip() != ""

    if header_row_1based is not None:
        hi = max(0, int(header_row_1based) - 1)
        if hi >= len(all_rows):
            return [], []
        raw_header = all_rows[hi]
        columns = [
            " ".join(str(h).strip().split()) if _nn(h) else f"col_{i}"
            for i, h in enumerate(raw_header)
        ]
        while columns and columns[-1].startswith("col_"):
            columns.pop()
        if sum(1 for h in raw_header if _nn(h)) <= 1:
            return [], []
        data: List[Dict] = []
        for row_idx in range(hi + 1, min(len(all_rows), hi + 1 + max_rows)):
            row_vals = all_rows[row_idx]
            row_dict: Dict[str, Any] = {}
            for i, col_name in enumerate(columns):
                row_dict[col_name] = row_vals[i] if i < len(row_vals) else None
            data.append(row_dict)
        return columns, data

    hi, best_cols = _find_header_row_idx_from_matrix(all_rows, max_scan=15)
    raw_header = all_rows[hi]

    nn_count = sum(1 for h in raw_header if _nn(h))
    non_none_indices = [i for i, h in enumerate(raw_header) if _nn(h)]

    if best_cols and len(best_cols) >= nn_count - 1:
        if len(non_none_indices) == len(best_cols):
            col_index_map = {non_none_indices[i]: best_cols[i] for i in range(len(best_cols))}
        else:
            col_index_map = None
    else:
        col_index_map = None

    if col_index_map is None:
        columns = [" ".join(str(h).strip().split()) if _nn(h) else f"col_{i}" for i, h in enumerate(raw_header)]
        while columns and columns[-1].startswith("col_"):
            columns.pop()
        col_index_map = {i: columns[i] for i in range(len(columns))}

    max_col = max(col_index_map.keys()) + 1 if col_index_map else 0
    columns = [col_index_map.get(i, f"col_{i}") for i in range(max_col)]
    while columns and columns[-1].startswith("col_"):
        columns.pop()

    if nn_count <= 1:
        return [], []

    data: List[Dict] = []
    for row_idx in range(hi + 1, min(len(all_rows), hi + 1 + max_rows)):
        row_vals = all_rows[row_idx]
        row_dict: Dict[str, Any] = {}
        for i, col_name in enumerate(columns):
            row_dict[col_name] = row_vals[i] if i < len(row_vals) else None
        data.append(row_dict)
    return columns, data


def load_xlsx_as_table(
    file_path: str,
    sheet_name: str = None,
    max_rows: int = 50000,
    header_row_1based: Optional[int] = None,
) -> Tuple[List[str], List[Dict]]:
    """加载 xlsx 文件为表格数据。返回 (列名列表, 数据行列表[Dict])
    
    自动检测真实表头行（跳过公司信息/标题等前置行）。
    max_rows 限制最大读取行数，防止超大文件卡死。
    使用 data_only=False 以保留公式字符串（如 DISPIMG 图片公式）。
    header_row_1based: 若指定则固定该行作为表头（与模板匹配选出的表），不再自动探测。
    """
    if str(file_path).lower().endswith(".xls"):
        return load_xls_as_table(file_path, sheet_name, max_rows, header_row_1based)

    if header_row_1based is not None:
        wb = openpyxl.load_workbook(file_path, read_only=False, data_only=False)
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        header_ri = int(header_row_1based)
        max_r = ws.max_row or 0
        mc = ws.max_column or 0
        if header_ri < 1 or max_r < header_ri or mc < 1:
            wb.close()
            return [], []
        # 构建合并单元格映射，解决合并单元格读取为 None 的问题
        _merged = {}
        for rng in ws.merged_cells.ranges:
            top_val = ws.cell(row=rng.min_row, column=rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if r != rng.min_row or c != rng.min_col:
                        _merged[(r, c)] = top_val
        def _cell_val(r, c):
            if (r, c) in _merged:
                return _merged[(r, c)]
            return ws.cell(r, c).value
        raw_header = [_cell_val(header_ri, ci) for ci in range(1, mc + 1)]
        columns = [
            " ".join(str(h).strip().split()) if h is not None and str(h).strip() else f"col_{i}"
            for i, h in enumerate(raw_header)
        ]
        while columns and columns[-1].startswith("col_"):
            columns.pop()
        non_none = sum(1 for h in raw_header if h is not None and str(h).strip())
        if non_none <= 1:
            wb.close()
            return [], []
        data: List[Dict] = []
        end_ri = min(max_r, header_ri + max_rows)
        for ri in range(header_ri + 1, end_ri + 1):
            row_dict: Dict[str, Any] = {}
            for ci, col_name in enumerate(columns):
                val = _cell_val(ri, ci + 1)
                row_dict[col_name] = val
            data.append(row_dict)
        wb.close()
        return columns, data

    # 先用 read_only 模式检测表头行
    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
    header_ri, best_cols = _find_header_row(ws)
    wb.close()

    # 重新加载，data_only=False 保留公式字符串（DISPIMG 等），read_only=False 以读取合并单元格
    wb = openpyxl.load_workbook(file_path, read_only=False, data_only=False)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active

    # 构建合并单元格映射
    _merged = {}
    for rng in ws.merged_cells.ranges:
        top_val = ws.cell(row=rng.min_row, column=rng.min_col).value
        for r in range(rng.min_row, rng.max_row + 1):
            for c in range(rng.min_col, rng.max_col + 1):
                if r != rng.min_row or c != rng.min_col:
                    _merged[(r, c)] = top_val

    def _cell_val(r, c):
        if (r, c) in _merged:
            return _merged[(r, c)]
        return ws.cell(r, c).value

    mc = ws.max_column or 0
    raw_header = [_cell_val(header_ri, ci) for ci in range(1, mc + 1)]

    # 优先用 _find_header_row 返回的已处理列名（换行合并、空格压缩）
    # 如果 best_cols 列数和 raw_header 不一致，fallback 到自行处理
    if best_cols and len(best_cols) >= sum(1 for h in raw_header if h is not None) - 1:
        # 构建 col_index -> col_name 映射（best_cols 只包含非空列）
        non_none_indices = [i for i, h in enumerate(raw_header) if h is not None]
        if len(non_none_indices) == len(best_cols):
            col_index_map = {non_none_indices[i]: best_cols[i] for i in range(len(best_cols))}
        else:
            col_index_map = None
    else:
        col_index_map = None

    if col_index_map is None:
        # fallback：自行处理列名
        columns = [' '.join(str(h).strip().split()) if h is not None else f"col_{i}"
                   for i, h in enumerate(raw_header)]
        while columns and columns[-1].startswith("col_"):
            columns.pop()
        col_index_map = {i: columns[i] for i in range(len(columns))}

    # 用 col_index_map 构建最终列名列表（按原始顺序）
    max_col = max(col_index_map.keys()) + 1 if col_index_map else 0
    columns = [col_index_map.get(i, f"col_{i}") for i in range(max_col)]
    while columns and columns[-1].startswith("col_"):
        columns.pop()

    non_none = sum(1 for h in raw_header if h is not None)
    if non_none <= 1:
        wb.close()
        # fallback 到普通模式（read_only 可能读不到合并单元格）
        wb = openpyxl.load_workbook(file_path, data_only=False)
        ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
        header_ri2, best_cols2 = _find_header_row(ws)
        if ws.max_row is None or ws.max_row < header_ri2:
            wb.close()
            return [], []
        raw_h2 = [ws.cell(header_ri2, ci).value for ci in range(1, ws.max_column + 1)]
        columns = [' '.join(str(h).strip().split()) if h is not None else f"col_{i}"
                   for i, h in enumerate(raw_h2)]
        while columns and columns[-1].startswith("col_"):
            columns.pop()
        data = []
        for ri in range(header_ri2 + 1, min(ws.max_row + 1, header_ri2 + max_rows + 1)):
            row_dict = {}
            for ci, col_name in enumerate(columns):
                row_dict[col_name] = ws.cell(ri, ci + 1).value
            data.append(row_dict)
        wb.close()
        return columns, data

    data = []
    count = 0
    max_r = ws.max_row or 0
    for ri in range(header_ri + 1, min(max_r + 1, header_ri + max_rows + 1)):
        if count >= max_rows:
            break
        row_dict = {}
        all_empty = True
        for i in range(len(columns)):
            val = _cell_val(ri, i + 1)
            row_dict[columns[i]] = val
            if val is not None and str(val).strip():
                all_empty = False
        if not all_empty:
            data.append(row_dict)
        count += 1
    wb.close()
    return columns, data


def load_xlsx_columns_only(file_path: str, sheet_name: str = None) -> List[str]:
    """快速读取 xlsx 列名（自动检测真实表头行，跳过公司信息等前置行）"""
    if str(file_path).lower().endswith(".xls"):
        cols, _ = load_xls_as_table(file_path, sheet_name, max_rows=1)
        return cols

    wb = openpyxl.load_workbook(file_path, read_only=True, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
    header_ri, best_cols = _find_header_row(ws)
    wb.close()

    if best_cols:
        # 列名已在 _find_header_row 里合并换行，直接返回
        return best_cols

    # fallback：直接读表头行
    wb = openpyxl.load_workbook(file_path, data_only=True)
    ws = wb[sheet_name] if sheet_name and sheet_name in wb.sheetnames else wb.active
    columns = []
    for cell in ws[header_ri]:
        if cell.value is not None:
            columns.append(str(cell.value).strip().split('\n')[0].strip())
    wb.close()
    return columns


def _resolve_docx_path(file_path: str) -> Tuple[Optional[str], Optional[str]]:
    """python-docx 仅支持 .docx；老版 .doc 先转临时文件。返回 (路径, 临时文件路径或 None)。"""
    if not str(file_path).lower().endswith(".doc"):
        return file_path, None
    from services.legacy_doc import convert_doc_to_docx_temp

    tmp = convert_doc_to_docx_temp(file_path)
    if not tmp:
        logger.warning("[data_filter] 老版 .doc 无法转为 docx，已跳过: %s", file_path)
        return None, None
    return tmp, tmp


def load_docx_tables(file_path: str) -> List[Tuple[List[str], List[Dict]]]:
    """加载 docx 中的所有表格。返回 [(列名列表, 数据行列表)]"""
    import docx

    path, cleanup = _resolve_docx_path(file_path)
    if not path:
        return []
    try:
        doc = docx.Document(path)
        results = []
        for table in doc.tables:
            if len(table.rows) < 2:
                continue
            header = [c.text.strip() for c in table.rows[0].cells]
            data = []
            for ri in range(1, len(table.rows)):
                row_dict = {}
                for ci, cell in enumerate(table.rows[ri].cells):
                    if ci < len(header):
                        row_dict[header[ci]] = cell.text.strip()
                data.append(row_dict)
            results.append((header, data))
        return results
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


def load_docx_paragraphs_as_table(file_path: str) -> Optional[Tuple[List[str], List[Dict]]]:
    """从 docx 段落文本中识别结构化表格数据。
    
    处理如下格式的段落式表格：
      城市名：重庆  GDP总量（亿元）：33757.93  常住人口（万）：3191.43
    或以分隔符（制表符、多空格、竖线）分隔的行式数据。
    返回 (列名列表, 数据行列表) 或 None。
    """
    import docx

    path, cleanup = _resolve_docx_path(file_path)
    if not path:
        return None
    paragraphs: List[str] = []
    try:
        doc = docx.Document(path)
        paragraphs = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass

    if len(paragraphs) < 3:
        return None

    # 策略1: 检测"键：值"格式的段落 (如 "城市名：重庆  GDP：33757.93")
    kv_pattern = re.compile(r'([\u4e00-\u9fffA-Za-z()（）\s]{2,20})[：:]\s*([^\s：:]+)')
    kv_rows = []
    for para in paragraphs:
        matches = kv_pattern.findall(para)
        if len(matches) >= 3:
            row = {k.strip(): v.strip() for k, v in matches}
            kv_rows.append(row)
    if len(kv_rows) >= 5:
        all_keys = []
        for row in kv_rows:
            for k in row:
                if k not in all_keys:
                    all_keys.append(k)
        return all_keys, kv_rows

    # 策略2: 检测制表符/竖线分隔的行式数据（不用多空格，避免叙述文本误判）
    sep_patterns = ['\t', r'\s*\|\s*']
    for sep_re in sep_patterns:
        header_line = None
        header_idx = -1
        for i, para in enumerate(paragraphs[:15]):
            fields = [f.strip() for f in re.split(sep_re, para) if f.strip()]
            if len(fields) < 3 or len(fields) > 30:
                continue
            # 表头字段应为短文本（2-30字符），且大部分不是纯数字
            valid_headers = [f for f in fields if 1 <= len(f) <= 30]
            if len(valid_headers) < len(fields) * 0.8:
                continue
            non_numeric = sum(1 for f in fields if not f.replace('.', '').replace(',', '').replace('-', '').isdigit())
            if non_numeric >= len(fields) * 0.6:
                header_line = fields
                header_idx = i
                break
        if not header_line:
            continue

        data_rows = []
        for para in paragraphs[header_idx + 1:]:
            fields = [f.strip() for f in re.split(sep_re, para) if f.strip()]
            if len(fields) < len(header_line) * 0.6:
                continue
            row = {}
            for ci, col in enumerate(header_line):
                row[col] = fields[ci] if ci < len(fields) else ""
            data_rows.append(row)
        if len(data_rows) >= 5:
            return header_line, data_rows

    return None


# 说明性长文中「通用地名」正则容易扫到假城市名（如「山东省部分城市」「时刻德州市」）
_NARRATIVE_REGION_NOISE_SUBSTR = (
    "部分城", "涵盖城", "涵盖市", "便于", "结构与", "助力", "识别", "掌握",
    "系统记录", "系统", "时刻", "该行", "该表", "本省", "实时反映",
    "各监测", "各站点", "城市名", "核心指标", "数据信息", "空气质量监测数据",
)


def _filter_plausible_region_tokens(candidates: List[str]) -> List[str]:
    """去掉从模板说明/叙事文本里误扫出的地名碎片。"""
    out: List[str] = []
    expanded: List[str] = []
    for raw in candidates:
        # 「济南市和青岛市」会被正则整段吞成一个 token，匹配不到任何城市，
        # 城市条件等于失效（回退后变成「不过滤」）。在行政区后缀后接连接词处切开。
        parts = re.split(
            r"(?<=[省市县区])(?:和|与|及|、)(?=[\u4e00-\u9fff]{2,6}(?:市|省|区|县|自治区))",
            str(raw or ""),
        )
        expanded.extend(p.strip() for p in parts if p.strip())
    for raw in expanded:
        s = (raw or "").strip()
        if not s or len(s) > 24:
            continue
        # 「2024年山东省的数据」会被正则吞成「年山东省」：年份的「年」粘在了地名前面。
        # 只在去掉首字后仍以行政区后缀结尾时才剥，避免误伤「日照市」这类真地名。
        if s.startswith("年") and s[1:].endswith(("省", "市", "区", "县", "自治区", "特别行政区")):
            s = s[1:]
        # 「只填拉萨市」「筛选杭州市」这类动词前缀会被正则一起吞进来（「只填拉萨」+「市」），
        # 结果是城市条件永远匹配不到任何行。剥掉常见动词前缀，只留地名。
        for _vp in (
            "只保留", "只填", "只取", "只要", "筛选", "查询", "统计", "导出",
            "显示", "列出", "查看", "包含", "关于", "位于", "填入", "填写",
        ):
            if s.startswith(_vp) and len(s) > len(_vp) + 1:
                s = s[len(_vp):]
                break
        if any(b in s for b in _NARRATIVE_REGION_NOISE_SUBSTR):
            continue
        # 「中国城市」「全国城市」这类是泛称，不是地名；「城市」两个字单独出现同样不是。
        # 不能用「包含城市」判断——「聊城市」是真地名，会被误杀。
        if s.endswith("城市") and (
            not s[:-2]
            or s[:-2]
            in (
                "中国", "全国", "各", "本", "该", "省内", "地市", "省市", "全域",
                "所有", "全部", "部分", "各个", "华东", "华南", "华北", "华中", "西部", "东部",
            )
        ):
            continue
        if s in ("都市", "全市", "本市", "各市", "地市", "省市", "城区", "市区"):
            continue
        if s in ("行政区", "涵盖城市", "部分城市", "监测数据"):
            continue
        # 数值/单位片段（如「万亿的城市」「亿元」）不是地名
        if re.search(r"[\d]", s) or re.search(r"(万亿|千亿|百亿|亿元|万元|千万|百万)", s):
            continue
        # 「万的城市」「人口低于」这类比较句碎片不是地名
        if "的" in s or re.search(r"(大于|小于|低于|高于|超过|不少于|不低于|以上|以下)", s):
            continue
        # 「XX市」极长多为整句误吞；正常地级市多 ≤9 字（含「市」）
        if s.endswith("市") and len(s) > 9:
            continue
        out.append(s)
    return list(dict.fromkeys(out))


def _normalize_trailing_prefecture_city(token: str) -> Optional[str]:
    """从「时刻德州市」等片段中取末尾真实「××市」：自右取最短 2～4 个纯汉字再接「市」（正则 $ 无法避免「时刻」被吃入）。"""
    s = (token or "").strip()
    if not s.endswith("市") or len(s) < 3:
        return None
    body = s[:-1]
    for n in (2, 3, 4):
        if len(body) < n:
            continue
        tail = body[-n:]
        if re.match(r"^[\u4e00-\u9fff]+$", tail):
            return tail + "市"
    return None


def _extract_cities_before_jiance(req: str) -> List[str]:
    """在「各监测」前截取短上下文，取以「市」结尾的地级市名（最稳）。"""
    out: List[str] = []
    for m in re.finditer("各监测", req):
        seg = req[max(0, m.start() - 20) : m.start()].rstrip()
        if not seg.endswith("市"):
            continue
        c = _normalize_trailing_prefecture_city(seg)
        if c:
            out.append(c)
    return list(dict.fromkeys(out))


def _extract_prefecture_cities_from_narrative(req: str) -> List[str]:
    """从「德州市各监测站」「潍坊市…」等说明句式中提取地级市名（比全局 zh_regions 稳）。"""
    if not req:
        return []
    hits: List[str] = list(_extract_cities_before_jiance(req))
    pats = [
        r"([\u4e00-\u9fff]{2,8}市)(?:各监测|各站点|境内|地区的|的大气|的空气质量|环境状况)",
        r"(?:记录|反映|掌握)([\u4e00-\u9fff]{2,5}市)",
    ]
    for p in pats:
        for g in re.findall(p, req):
            tok = _normalize_trailing_prefecture_city(g) or g
            if tok:
                hits.append(tok)
    return _filter_plausible_region_tokens(list(dict.fromkeys(hits)))


_NUM_CMP_RE = re.compile(
    r"(?P<op>大于等于|不低于|不少于|不小于|超过|大于|高于|多于"
    r"|小于等于|不高于|不超过|不大于|低于|小于|少于"
    r"|>=|<=|≥|≤|>|<|＞|＜)"
    r"\s*(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>万亿|千亿|百亿|十亿|千万|百万|亿|万|千)?"
)

_NUM_UNIT_SCALE = {
    "万亿": 1e12, "千亿": 1e11, "百亿": 1e10, "十亿": 1e9,
    "千万": 1e7, "百万": 1e6, "亿": 1e8, "万": 1e4, "千": 1e3, "": 1.0,
}

# 后置式比较：「5万亿以上」「1000万以下」。必须带数量级，避免「100以上」这类
# 纯计数被误当量纲条件。
_NUM_SUFFIX_RE = re.compile(
    r"(?P<num>\d+(?:\.\d+)?)\s*(?P<unit>万亿|千亿|百亿|十亿|千万|百万|亿|万|千)\s*"
    r"(?P<sfx>及以上|及以下|以上|以下)"
)

# 关键字前面常见的动词/虚词，取列名关键词时要剥掉
# 引导语/动作词：用户口述条件时常带「填写/帮我填/把…填进去」，
# 这些词不是列名的一部分，必须在取关键字时剥掉，
# 否则「填写GDP超过5万亿」会得到 keyword=填写GDP，匹配不到「GDP总量（亿元）」，
# 条件被静默丢弃，最终返回全表（用户看到的「乱切」）。
_KW_LEAD_RE = re.compile(
    r"^(?:请帮我|麻烦帮我|帮我|帮忙|请|麻烦|然后|接着|再|并|把|将|对|按|按照|根据|就|在|从"
    r"|填写|填入|填充|填上|补全|补齐|完善|填|录入|写入|输入"
    r"|保留|过滤|提取|抽取|筛选|查询|统计|导出|输出|列出|列举|找出|选出|挑出|给出|显示|查看|展示|整理|汇总|生成"
    r"|只要|仅要|只需要|需要|所有|全部|其中|只|仅|的|是|为|有|含|包含)+"
)


def _norm_col_text(s: str) -> str:
    """列名/关键词归一化：小写、去括号单位、去标点空白。"""
    t = str(s or "").lower()
    t = re.sub(r"[（(][^）)]*[）)]", "", t)
    t = re.sub(r"[\s_\-/、，,：:（）()]", "", t)
    return t


def _parse_numeric_filters(req: str) -> List[Dict[str, Any]]:
    """解析数值比较条件，如「GDP总量大于5万亿」「人口低于1000万」。

    返回 [{"keyword": "gdp总量", "op": ">", "value": 5.0, "unit": "万亿", "base_value": 5e12}]
    base_value 已换算到基本单位（元/人/…），由调用方按列头单位二次换算。
    """
    out: List[Dict[str, Any]] = []
    for m, op, unit in _iter_numeric_matches(req):
        try:
            num = float(m.group("num"))
        except ValueError:
            continue
        # 关键字：运算符前最近的词，最多回看 20 个字符。
        # 多个条件并列（「GDP大于5万亿且人口低于2000万」）时，只取连接词之后的那一段；
        # 否则第二个条件的 keyword 会变成「GDP大于5万亿且人口」，匹配不到任何列，
        # 条件被静默丢弃——用户以为筛过了，其实一行没筛。
        ctx = req[max(0, m.start() - 20): m.start()].strip()
        ctx = re.split(r"[，,。；;、\s]|并且|同时|以及|而且|且", ctx)[-1] if ctx else ""
        # 没有连接词时（「…5万亿人口低于…」）剥掉开头的残留数值
        ctx = re.sub(r"^\d+(?:\.\d+)?\s*(?:万亿|千亿|百亿|十亿|千万|百万|亿|万|千)?", "", ctx).strip()
        ctx = _KW_LEAD_RE.sub("", ctx).strip()
        # 去掉结尾的「要/是/在」等残留，以及「GDP大于5万亿以上」这类重复表述留下的运算符
        ctx = re.sub(r"[要是为在的了]+$", "", ctx).strip()
        ctx = re.sub(
            r"(?:大于等于|不低于|不少于|不小于|超过|大于|高于|多于"
            r"|小于等于|不高于|不超过|不大于|低于|小于|少于)+$",
            "", ctx,
        ).strip()
        if not ctx:
            continue
        out.append({
            "keyword": ctx,
            "op": op,
            "value": num,
            "unit": unit,
            # 用户到底有没有说数量级，决定了阈值怎么换算：
            # 说了「5 万亿」→ 5e12 基本单位；只说「50000」→ 就是列头单位下的 50000。
            # 两者混为一谈会把「GDP 超过 50000」换算成 0.0005 亿元，条件形同虚设。
            "has_unit": bool(unit),
            "base_value": num * _NUM_UNIT_SCALE.get(unit, 1.0),
        })
    # 同一个关键字只保留最后一个条件，避免重复
    dedup: Dict[str, Dict[str, Any]] = {}
    for f in out:
        dedup[f["keyword"]] = f
    return list(dedup.values())


def _iter_numeric_matches(req: str):
    """产出 (match, 归一化运算符, 单位)：兼容「大于5万亿」与「5万亿以上」两种说法。"""
    op_map = {
        "大于等于": ">=", "不低于": ">=", "不少于": ">=", "不小于": ">=",
        "超过": ">", "大于": ">", "高于": ">", "多于": ">",
        "小于等于": "<=", "不高于": "<=", "不超过": "<=", "不大于": "<=",
        "低于": "<", "小于": "<", "少于": "<",
        "＞": ">", "＜": "<", "≥": ">=", "≤": "<=",
    }
    for m in _NUM_CMP_RE.finditer(req):
        yield m, op_map.get(m.group("op"), m.group("op")), (m.group("unit") or "")
    for m in _NUM_SUFFIX_RE.finditer(req):
        # 「大于5万亿以上」这类重复表述以前置运算符为准，避免同一条件产生两条
        head = req[max(0, m.start() - 8): m.start()]
        if _NUM_CMP_RE.search(head):
            continue
        op = ">=" if m.group("sfx") in ("以上", "及以上") else "<="
        yield m, op, (m.group("unit") or "")


# 列名里常见的量纲后缀，去掉后剩下的才是「指标主体」（GDP总量 → GDP）。
_COL_SUFFIX_RE = re.compile(
    r"(?:总量|总额|合计|总计|小计|总值|数量|数目|金额|数值|数据|指标|情况|水平|规模|收入|支出)$"
)


def _col_core_forms(nc: str) -> List[str]:
    """列名归一化后的候选指代形式：原样 + 去掉量纲后缀的主体。"""
    forms = [nc]
    core = _COL_SUFFIX_RE.sub("", nc)
    if core and core != nc and len(core) >= 2:
        forms.append(core)
    return forms


def _match_filter_column(keyword: str, col_names: List[str]) -> Optional[str]:
    """把「gdp总量」这类关键词匹配到模板列名（如「GDP总量（亿元）」）。

    用户口述往往裹着动作词（「填写GDP超过5万亿」），即使剥过引导词，
    关键字仍可能带残留，所以除了精确/双向包含，还补两级兜底：
    列名主体出现在关键字里（GDP ⊂ 填写GDP），以及逐字剥掉关键字前缀。
    """
    kw = _norm_col_text(keyword)
    if not kw:
        return None
    norm = [(_norm_col_text(c), c) for c in col_names if c]
    # 1) 完全一致
    for nc, c in norm:
        if nc == kw:
            return c
    # 2) 双向包含：关键词 ⊂ 列名，或列名 ⊂ 关键词
    for nc, c in norm:
        if kw in nc or (len(nc) >= 2 and nc in kw):
            return c
    # 3) 列名主体出现在关键词里：kw=填写gdp、nc=gdp总量 → 取最长主体，避免 gdp 误撞人均gdp
    best: Optional[Tuple[int, str]] = None
    for nc, c in norm:
        for form in _col_core_forms(nc):
            if len(form) >= 2 and form in kw:
                if best is None or len(form) > best[0]:
                    best = (len(form), c)
    if best:
        return best[1]
    # 4) 逐字剥掉关键词前缀再试：填写gdp → 写gdp → gdp
    for i in range(1, len(kw) - 1):
        tail = kw[i:]
        if len(tail) < 2:
            break
        for nc, c in norm:
            if tail == nc or tail in nc:
                return c
    return None


def usable_numeric_filters(
    numeric_filters: List[Dict[str, Any]], col_names: List[str]
) -> List[Dict[str, Any]]:
    """只保留能落到模板列上的数值条件。

    正则难免误命中（如「增速>5%」里的 `>`），若把这种无法匹配列的条件也当成
    「用户给了数值条件」，上层会据此跳过补漏，最终行数变少且没有任何提示。
    这里先做一次可落地性校验，全部匹配不上就当没解析到条件。
    """
    if not numeric_filters or not col_names:
        return []
    return [f for f in numeric_filters if _match_filter_column(f.get("keyword", ""), col_names)]


def apply_numeric_filters(
    rows: List[Dict[str, Any]],
    col_names: List[str],
    numeric_filters: List[Dict[str, Any]],
) -> Tuple[List[Dict[str, Any]], List[str]]:
    """按「GDP 大于 5 万亿」这类数值条件过滤行。

    单位换算以模板列头为准：条件里的 5 万亿先换算到基本单位（元），
    再除以列头单位倍率（亿元 → 1e8），得到 50000，与单元格数值直接比较。
    表头没写单位时按用户口述单位比较，并在说明里提示口径不确定。

    返回 (保留的行, 说明日志)。
    """
    from services.unit_normalize import normalize_cell, parse_header_unit, parse_value_with_unit, split_unit

    notes: List[str] = []
    if not numeric_filters or not rows:
        return rows, notes

    checks = []
    for f in numeric_filters:
        col = _match_filter_column(f.get("keyword", ""), col_names)
        if not col:
            notes.append(f"数值条件「{f.get('keyword')} {f.get('op')} {f.get('value')}{f.get('unit','')}」未匹配到列，已跳过")
            continue
        col_unit = parse_header_unit(col)
        _, col_scale = split_unit(col_unit) if col_unit else (None, None)
        # has_unit=False 表示用户只给了数字没给数量级（「GDP 超过 50000」），
        # 此时应按列头单位理解，而不是当成基本单位再除以倍率——
        # 否则 50000 会变成 0.0005 亿元，任何一行都满足，筛选等于没做。
        if col_scale and f.get("has_unit", True):
            threshold = f["base_value"] / col_scale
            notes.append(
                f"数值条件生效: {col} {f['op']} {round(threshold, 4)}（列头单位 {col_unit}，"
                f"原条件 {f['value']}{f.get('unit','')}）"
            )
        elif col_scale:
            threshold = f["value"]
            notes.append(
                f"数值条件生效: {col} {f['op']} {threshold}（用户未给数量级，按列头单位 {col_unit} 理解）"
            )
        else:
            threshold = f["value"]
            notes.append(
                f"数值条件生效: {col} {f['op']} {threshold}（列头无单位，按用户口述单位比较，口径可能不符）"
            )
        checks.append((col, f["op"], threshold))

    if not checks:
        return rows, notes

    ops = {
        ">": lambda a, b: a > b,
        ">=": lambda a, b: a >= b,
        "<": lambda a, b: a < b,
        "<=": lambda a, b: a <= b,
    }

    def _cell_number(row: Dict[str, Any], col: str) -> Optional[float]:
        # 单元格自带单位（如「5.2万亿」）必须先按列头单位换算，否则
        # 5.2 与 50000 直接比大小，会把本来符合条件的高值行整行丢掉。
        norm_val, _warn = normalize_cell(row.get(col), col)
        if isinstance(norm_val, (int, float)) and not isinstance(norm_val, bool):
            return float(norm_val)
        num, _unit = parse_value_with_unit(row.get(col))
        return num

    # 该列一整列都解析不出数值时（多表模板里某张表没有这一列），
    # 条件一律不成立会把整张表清空，比不筛选更糟——直接跳过这条条件。
    valid_checks = []
    for col, op, threshold in checks:
        if any(_cell_number(r, col) is not None for r in rows):
            valid_checks.append((col, op, threshold))
        else:
            notes.append(f"数值条件跳过: 列「{col}」在数据中没有可解析的数值")
    if not valid_checks:
        return rows, notes
    checks = valid_checks

    kept: List[Dict[str, Any]] = []
    dropped_unparsable = 0
    for row in rows:
        ok = True
        for col, op, threshold in checks:
            num = _cell_number(row, col)
            if num is None:
                dropped_unparsable += 1
                ok = False
                break
            if not ops.get(op, lambda a, b: True)(num, threshold):
                ok = False
                break
        if ok:
            kept.append(row)

    removed = len(rows) - len(kept)
    notes.append(
        f"数值条件过滤: {len(rows)} 行 → {len(kept)} 行（剔除 {removed} 行"
        + (f"，其中 {dropped_unparsable} 行数值无法解析" if dropped_unparsable else "")
        + "）"
    )
    return kept, notes


def parse_filter_conditions(user_requirement: str) -> Dict[str, Any]:
    """从用户要求中解析筛选条件（规则方式，不调LLM）"""
    conditions = {}
    req = str(user_requirement or "").strip()
    if not req:
        return conditions

    # 数值比较条件：「GDP总量大于5万亿」「人口低于1000万」
    numeric_filters = _parse_numeric_filters(req)
    if numeric_filters:
        conditions["numeric_filters"] = numeric_filters

    
    # 日期范围: "从2020/7/1到2020/8/31" / "2020-07-01至2020-08-31" / "2020/7/1~2020/8/31"
    date_range = re.findall(
        r'(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*(?:到|至|~|～|—|－)\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})',
        req,
    )
    if date_range:
        conditions["date_range"] = {"start": date_range[0][0], "end": date_range[0][1]}
    
    # 单个日期时间: "2025-11-25 09:00:00"
    single_datetime = re.findall(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', req)
    if single_datetime:
        conditions["datetime_exact"] = single_datetime
    
    single_date = re.findall(r'监测时间[：:]\s*(\d{4}-\d{2}-\d{2})', req)
    if single_date:
        conditions["date_exact"] = single_date
    
    # 城市名: "城市：德州市" 或 "城市：潍坊市"
    cities = re.findall(r'城市[：:]\s*(\S+?)(?:\s|$)', req)

    # 通用地名解析：如“筛选上海2023年数据”（长说明文中易误判，需过滤）
    zh_regions = re.findall(r'([\u4e00-\u9fff]{2,8}(?:市|省|区|县|自治区|特别行政区))', req)
    zh_regions = _filter_plausible_region_tokens(zh_regions)
    narrative_cities = _extract_prefecture_cities_from_narrative(req)

    short_city = re.findall(r'(?:筛选|查询|统计|导出)\s*([\u4e00-\u9fff]{2,6})\s*(?:\d{4}年)?(?:数据|记录|信息)?', req)
    short_city = _filter_plausible_region_tokens(short_city)

    # 说明文中已稳定抽出多个「XX市」时，不再合并泛扫的 zh_regions（易混入「山东省」等导致多表城市数对不齐）
    if len(narrative_cities) >= 2:
        merged_regions = list(dict.fromkeys([*cities, *narrative_cities]))
    else:
        merged_regions = list(dict.fromkeys([*cities, *narrative_cities, *zh_regions, *short_city]))
    merged_regions = _filter_plausible_region_tokens(merged_regions)
    if merged_regions:
        conditions["cities"] = merged_regions

    # 年份解析。
    # 不能见「19/20 开头的四位数」就当成年份：「人口低于2000万」里的 2000 是数量，
    # 被识别成年份后，年份筛选会把整批数据清空。数量级后缀紧随时一律不算年份。
    years = re.findall(
        r'((?:19|20)\d{2})(?!\s*(?:万亿|千亿|百亿|十亿|千万|百万|亿|万|千))',
        req,
    )
    if years:
        conditions["years"] = list(dict.fromkeys(years))

    # 排序解析：如"按降序排序"、"按GDP降序"、"升序排列"
    sort_col_match = re.search(
        r'(?:按|按照)\s*([\u4e00-\u9fffA-Za-z0-9()（）\s]{1,20}?)\s*(?:降序|升序|排序|排列)',
        req
    )
    has_desc = bool(re.search(r'降序|从大到小|从高到低|倒序', req))
    has_asc = bool(re.search(r'升序|从小到大|从低到高', req))
    has_sort = bool(re.search(r'排序|排列', req)) or has_desc or has_asc

    if has_sort:
        sort_order = "desc" if has_desc else ("asc" if has_asc else "desc")
        sort_column = sort_col_match.group(1).strip() if sort_col_match else None
        # 清理掉多余的动词/介词
        if sort_column:
            sort_column = re.sub(r'^(按|按照|并|并按)', '', sort_column).strip()
            if not sort_column or sort_column in ("降序", "升序", "排序", "排列"):
                sort_column = None
        conditions["sort"] = {"column": sort_column, "order": sort_order}

    # 求和需求解析：行求和 / 列求和 / 每行每列求和
    SUM_KW_ROW = r'行求和|每行求和|行合计|横向求和|横向合计'
    SUM_KW_COL = r'列求和|每列求和|列合计|纵向求和|纵向合计'
    SUM_KW_BOTH = r'每行每列求和|行列求和|求和|合计'
    want_row_sum = bool(re.search(SUM_KW_ROW, req))
    want_col_sum = bool(re.search(SUM_KW_COL, req))
    if not want_row_sum and not want_col_sum and re.search(SUM_KW_BOTH, req):
        want_row_sum = True
        want_col_sum = True
    if want_row_sum or want_col_sum:
        conditions["sum"] = {"row": want_row_sum, "col": want_col_sum}

    # 统计需求解析：求平均值 / 求最大值 / 求最小值
    want_avg = bool(re.search(r'平均值|平均|均值|avg|average', req, re.IGNORECASE))
    want_max = bool(re.search(r'最大值|最大|max(?!imum\s*order)', req, re.IGNORECASE))
    want_min = bool(re.search(r'最小值|最小|min(?!imum\s*order)', req, re.IGNORECASE))
    if want_avg or want_max or want_min:
        conditions["stats"] = {"avg": want_avg, "max": want_max, "min": want_min}

    return conditions


async def parse_filter_with_llm(user_requirement: str, template_columns: List[str], available_data_columns: List[Dict]) -> Dict[str, Any]:
    """用 LLM 解析用户要求，返回筛选条件和列映射"""
    from services.llm_service import call_llm_text
    
    data_info = json.dumps(available_data_columns, ensure_ascii=False)
    tpl_cols = json.dumps(template_columns, ensure_ascii=False)
    
    sys = """你是数据筛选助手。根据用户要求、模板列名和数据源信息，返回严格JSON：
{
  "filters": {
    "date_range": {"start": "2020-07-01", "end": "2020-08-31", "column": "日期"},  // 可选
    "exact_match": {"列名": "值", ...},  // 可选，精确匹配条件
    "datetime_exact": "2025-11-25 09:00:00.0",  // 可选，精确时间匹配
    "datetime_column": "监测时间",  // 可选，时间列名
    "numeric_filters": [{"column": "GDP总量", "op": ">", "value": 5, "unit": "万亿"}]  // 可选，数值比较
  },
  "column_mapping": {"模板列名": "数据源列名", ...},
  "group_by": "某列名"  // 可选，按该列分组（如多城市场景填城市列名；省级场景填省级列名）
}

重要规则：
1. column_mapping 中，key 是模板列名，value 是数据源中对应的列名
2. 如果模板列名和数据源列名完全一致，也要写入 column_mapping
3. 如果用户要求中提到多个城市/地区，设置 group_by 为对应实体列名（通常是城市列或地区列）
4. datetime_exact 用于精确时间匹配，datetime_column 指定时间列名
5. exact_match 中如果有多个值（如多个城市），用列表格式：{"城市": ["德州市", "潍坊市"]}
6. year_exact 如果可识别年份，给出四位年份字符串，如 "2023"
7. 行政级别：若模板列名含「省」「自治区」「特别行政区」等省级含义，column_mapping 中该模板列不得映射到数据源里仅表示「城市」「地级市」「city」的列（除非用户明确要求按城市填省级列）。若模板列明确表示城市，则可映射到城市列。
8. 禁止输出任何 markdown、注释或解释性文本，只能输出一个 JSON 对象

numeric_filters 规则（用户说了「超过/大于/不低于/低于/不超过…某个数量」时使用）：
- column：模板列名，或用户口述的指标名（如「GDP」「人口」「人均GDP」）
- op：只能是 > >= < <= 之一
- value：纯数字（中文数字要转成阿拉伯数字，「五万亿」→ 5）
- unit：只能是 万亿/千亿/百亿/十亿/千万/百万/亿/万/千 之一；用户没说数量级时给空字符串 ""
- 用户没有提出任何数值比较条件时，numeric_filters 给空数组 []
- 不要把「同比增长 5%」「排名前 10」这类当成 numeric_filters

只返回JSON，不要解释。"""
    
    prompt = f"用户要求：{user_requirement}\n\n模板列名：{tpl_cols}\n\n可用数据源：{data_info}"
    r = await call_llm_text([{"role": "user", "content": prompt}], system=sys, temperature=0.0, max_tokens=2000)

    # 严格 JSON 提取与校验，确保下游逻辑稳定
    parsed: Dict[str, Any] = {}
    for candidate in [r.strip(), *(m.group(0) for m in re.finditer(r'\{[\s\S]*?\}', r))]:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                break
        except Exception:
            continue

    if not isinstance(parsed, dict):
        return {"filters": {}, "column_mapping": {}}

    filters = parsed.get("filters")
    column_mapping = parsed.get("column_mapping")
    group_by = parsed.get("group_by")

    if not isinstance(filters, dict):
        filters = {}
    if not isinstance(column_mapping, dict):
        column_mapping = {}
    if group_by is not None and not isinstance(group_by, str):
        group_by = None

    # 过滤非法映射（必须是“模板列 -> 数据源列”的字符串映射）
    safe_mapping: Dict[str, str] = {}
    tpl_col_set = set(template_columns or [])
    src_cols = set()
    for item in available_data_columns or []:
        for c in item.get("columns", []) if isinstance(item, dict) else []:
            if isinstance(c, str):
                src_cols.add(c)
    for k, v in column_mapping.items():
        if isinstance(k, str) and isinstance(v, str) and (not tpl_col_set or k in tpl_col_set):
            if not src_cols or v in src_cols:
                if _admin_level_conflict(k, v):
                    continue
                safe_mapping[k] = v

    # 数值比较条件：LLM 的输出不可直接信任，必须逐项校验（运算符白名单、数值可解析、
    # 单位在白名单内），否则一个幻觉出来的阈值会把整张表筛空。
    if isinstance(filters.get("numeric_filters"), list):
        filters["numeric_filters"] = _normalize_llm_numeric_filters(filters["numeric_filters"])
    else:
        filters.pop("numeric_filters", None)

    return {
        "filters": filters,
        "column_mapping": safe_mapping,
        "group_by": group_by,
    }


_LLM_OP_MAP = {
    "大于": ">", "超过": ">", "高于": ">", "多于": ">", ">": ">", "＞": ">",
    "大于等于": ">=", "不低于": ">=", "不少于": ">=", "不小于": ">=", ">=": ">=", "≥": ">=",
    "小于": "<", "低于": "<", "少于": "<", "<": "<", "＜": "<",
    "小于等于": "<=", "不高于": "<=", "不超过": "<=", "不大于": "<=", "<=": "<=", "≤": "<=",
}


def _normalize_llm_numeric_filters(raw: Any) -> List[Dict[str, Any]]:
    """把 LLM 返回的 numeric_filters 归一化成与规则解析一致的结构。

    校验不放宽：运算符必须在白名单内、value 必须能转成有限浮点、
    unit 必须在数量级白名单内（不在就当作「无数量级」，而不是猜一个）。
    """
    out: List[Dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        col = str(item.get("column") or item.get("keyword") or "").strip()
        op = _LLM_OP_MAP.get(str(item.get("op") or "").strip())
        try:
            val = float(item.get("value"))
        except (TypeError, ValueError):
            continue
        if not col or not op or val != val or val in (float("inf"), float("-inf")):
            continue
        unit = str(item.get("unit") or "").strip()
        if unit not in _NUM_UNIT_SCALE:
            unit = ""
        out.append({
            "keyword": col,
            "op": op,
            "value": val,
            "unit": unit,
            "has_unit": bool(unit),
            "base_value": val * _NUM_UNIT_SCALE[unit],
            "source": "llm",
        })
    return out


def _nf_signature(f: Dict[str, Any]) -> Tuple[str, Any]:
    """把一条数值条件归一成可比较的签名，用于判断两路解析是否一致。

    只比「运算符 + 换算到基本单位后的阈值」，不比列名：
    规则路的列名是用户原话里的指标名（GDP），模型路给的是模板列名
    （GDP总量（亿元）），两者本来就不会字面相同，比列名等于每条都判冲突。
    「5 万亿」与「50000 亿」换算后都是 5e12，视为同一条件，不算冲突。
    """
    op = str(f.get("op") or "").strip()
    base = f.get("base_value")
    if base is None:
        raw = str(f.get("value", "")).replace(",", "").strip()
        try:
            val = float(raw)
        except (TypeError, ValueError):
            return (op, raw)
        unit = str(f.get("unit") or "").strip()
        base = val * _NUM_UNIT_SCALE.get(unit, 1)
    try:
        return (op, round(float(base), 6))
    except (TypeError, ValueError):
        return (op, base)


def merge_numeric_filters(
    rule_filters: List[Dict[str, Any]],
    llm_filters: List[Dict[str, Any]],
    col_names: List[str],
) -> Tuple[List[Dict[str, Any]], str]:
    """规则与 LLM 双路取并：规则优先，规则落不了地时用 LLM 兜底。

    为什么要两条路：规则精确、可解释、零成本，但只认固定句式；
    LLM 能听懂「达到五万亿」「GDP 别低于五万亿」这类变体，却可能幻觉。
    因此规则命中就用规则；规则没解析出（或解析出的落不到模板列）才启用 LLM，
    并把来源标出来，便于在结果页提示「该条件由模型解析，请核对」。

    返回的第二项是来源标记：
      rule          规则解析，且与模型一致（或模型没给出条件）
      rule_conflict 两路都给出了条件但阈值/运算符不同 —— 仍按规则执行，但必须提示用户核对。
                    静默采信任何一路都是错的：规则可能误匹配了指标名，模型可能编了阈值。
      llm           规则落不了地，由模型解析
      none          两路都没有可用条件
    """
    rule_ok = usable_numeric_filters(rule_filters or [], col_names)
    llm_ok = usable_numeric_filters(llm_filters or [], col_names)
    if rule_ok:
        if llm_ok:
            r_sig = {_nf_signature(f) for f in rule_ok}
            l_sig = {_nf_signature(f) for f in llm_ok}
            if r_sig != l_sig:
                return rule_ok, "rule_conflict"
        return rule_ok, "rule"
    if llm_ok:
        return llm_ok, "llm"
    return [], "none"


def filter_data(data: List[Dict], filters: Dict) -> List[Dict]:
    """根据筛选条件过滤数据行"""
    result = data
    
    # 日期范围筛选
    if "date_range" in filters:
        dr = filters["date_range"]
        col = dr.get("column", "日期")
        start = _parse_date(dr["start"])
        end = _parse_date(dr["end"])
        if start and end:
            filtered = []
            for row in result:
                val = row.get(col)
                d = _parse_date(val)
                if d and start <= d <= end:
                    filtered.append(row)
            result = filtered
    
    # 精确匹配
    if "exact_match" in filters:
        for col, val in filters["exact_match"].items():
            result = [r for r in result if _match_value(r.get(col), val)]
    
    # 精确日期时间
    if "datetime_exact" in filters:
        dt_str = filters["datetime_exact"]
        col = filters.get("datetime_column", "监测时间")
        result = [r for r in result if _match_datetime(r.get(col), dt_str)]

    # 年份筛选（支持 year_exact 或 years）
    if "year_exact" in filters or "years" in filters:
        year_candidates = []
        if filters.get("year_exact"):
            year_candidates.append(str(filters["year_exact"]))
        if isinstance(filters.get("years"), list):
            year_candidates.extend(str(y) for y in filters["years"])
        year_set = {y[:4] for y in year_candidates if re.match(r"^(19|20)\d{2}", str(y))}
        if year_set:
            year_col = filters.get("year_column")
            if year_col:
                result = [r for r in result if str(r.get(year_col, "")).strip()[:4] in year_set]
            else:
                filtered_by_year = []
                for row in result:
                    # 未指定列时，扫描行内是否包含目标年份
                    values_text = " | ".join(str(v) for v in row.values() if v is not None)
                    if any(y in values_text for y in year_set):
                        filtered_by_year.append(row)
                result = filtered_by_year
    
    return result


def map_columns(data: List[Dict], column_mapping: Dict[str, str]) -> List[Dict]:
    """将数据列名映射为模板列名"""
    if not column_mapping:
        return data
    mapped = []
    for row in data:
        new_row = {}
        for tpl_col, src_col in column_mapping.items():
            new_row[tpl_col] = row.get(src_col, row.get(tpl_col, ""))
        # 保留未映射的列
        for k, v in row.items():
            if k not in new_row:
                new_row[k] = v
        mapped.append(new_row)
    return mapped


def sort_data(data: List[Dict], sort_config: Dict[str, Any], columns: List[str] = None) -> List[Dict]:
    """根据排序配置对数据排序。

    sort_config: {"column": "GDP" | None, "order": "desc" | "asc"}
    columns: 模板列名列表，当 sort_config.column 为 None 时取第二列（跳过名称列）作为默认排序列；
             若只有一列则用该列。
    """
    if not data or not sort_config:
        return data

    order = sort_config.get("order", "desc")
    target_col = sort_config.get("column")

    # 如果未指定排序列，默认用 columns 的第二列（第一列通常是名称）
    if not target_col and columns:
        target_col = columns[1] if len(columns) > 1 else columns[0]

    if not target_col:
        return data

    # 模糊匹配：用户可能写"GDP"，但实际列名是"GDP总量（亿元）"
    actual_col = _resolve_sort_column(target_col, list(data[0].keys()) if data else [])
    if not actual_col:
        return data

    def _sort_key(row):
        v = row.get(actual_col)
        if v is None:
            return (1, 0.0)
        s = str(v).strip().replace(",", "").replace("，", "")
        try:
            return (0, float(s))
        except (ValueError, TypeError):
            return (1, 0.0)

    reverse = (order == "desc")
    return sorted(data, key=_sort_key, reverse=reverse)


def _parse_numeric_cell(v) -> Optional[float]:
    if v is None:
        return None
    s = str(v).strip().replace(",", "").replace("，", "")
    if not s or s.lower() == "none":
        return None
    try:
        return float(s)
    except (ValueError, TypeError):
        return None


def _pick_first_numeric_column(data: List[Dict], column_order: List[str]) -> Optional[str]:
    """按模板列顺序找到第一个「多数非空单元格为数字」的列，用于无用户排序要求时的默认排序。"""
    if not data or not column_order:
        return None
    keys0 = list(data[0].keys()) if data else []
    for col in column_order:
        actual = _resolve_sort_column(col, keys0) if keys0 else None
        if not actual and keys0 and col in data[0]:
            actual = col
        if not actual:
            continue
        nonempty = 0
        numeric = 0
        for row in data:
            v = row.get(actual)
            if v is None or str(v).strip() == "":
                continue
            nonempty += 1
            if _parse_numeric_cell(v) is not None:
                numeric += 1
        if nonempty == 0:
            continue
        if numeric >= 1 and numeric >= nonempty * 0.5:
            return actual
    return None


def sort_data_by_first_numeric_column(
    data: List[Dict], column_order: List[str], order: str = "desc"
) -> List[Dict]:
    """用户未指定排序时：按模板列顺序选第一列「数值为主」的列，按数字排序（默认降序）；无法判定则原样返回。"""
    if not data or not column_order:
        return data
    col = _pick_first_numeric_column(data, column_order)
    if not col:
        return data
    rev = order == "desc"

    def _key(row: Dict) -> float:
        v = _parse_numeric_cell(row.get(col))
        if v is None:
            return float("-inf") if rev else float("inf")
        return v

    out = sorted(data, key=_key, reverse=rev)
    logger.info(
        "[data_filter] 默认数值列排序: column=%s order=%s rows=%s",
        col,
        order,
        len(out),
    )
    return out


def _resolve_sort_column(user_col: str, available_cols: List[str]) -> Optional[str]:
    """将用户指定的排序列名模糊匹配到实际列名"""
    if not user_col or not available_cols:
        return None
    u = user_col.strip().lower().replace(" ", "")
    for c in available_cols:
        if c.strip().lower().replace(" ", "") == u:
            return c
    for c in available_cols:
        cl = c.strip().lower().replace(" ", "")
        if u in cl or cl in u:
            return c
    return None


def _norm_col_name(name: str) -> str:
    return re.sub(r"\s+", "", str(name or "").strip().lower().replace("（", "(").replace("）", ")"))


def _split_tokens(name: str) -> List[str]:
    n = _norm_col_name(name)
    if not n:
        return []
    zh_parts = re.findall(r"[\u4e00-\u9fff]{1,8}", n)
    en_parts = re.findall(r"[a-z]+", n)
    num_parts = re.findall(r"\d+", n)
    return [*zh_parts, *en_parts, *num_parts]


# 省级与市级分列，避免「省/自治区/…」模板列被错误匹配到数据源「城市」列（原同一组会导致 syn_hit 等同）
_COL_SYNONYM_GROUPS = [
    {"地区", "region", "地区名称", "行政区"},
    {"省", "省份", "自治区", "特别行政区", "province", "省级"},
    {"城市", "city", "地市", "地级市", "直辖市"},
    {"年份", "年", "年度", "year"},
    {"日期", "时间", "date", "time", "监测时间", "统计时间"},
    {"数量", "qty", "quantity", "件数", "个数", "count", "minimumorderquantity", "moq"},
    {"金额", "总额", "amount", "total", "subtotal", "小计"},
    {"单价", "price", "unitprice", "unit"},
    {"图片", "照片", "图像", "picture", "image", "photo", "pic"},
]


def _token_to_group(token: str) -> Optional[int]:
    for i, grp in enumerate(_COL_SYNONYM_GROUPS):
        if token in grp:
            return i
    return None


def _is_province_level_header(name: str) -> bool:
    """列名表示省级行政区（省、自治区、特别行政区等），不含「城市」类语义。"""
    s = _norm_col_name(name)
    if "城市" in s:
        return False
    return "自治区" in s or "特别行政区" in s or "province" in s or "省" in s


def _is_city_level_header(name: str) -> bool:
    """列名强调地级市/城市实体。"""
    s = _norm_col_name(name)
    sl = s.lower()
    return "城市" in s or "地级市" in s or "地市" in s or "city" in sl


def _admin_level_conflict(template_col: str, source_col: str) -> bool:
    """省级表头与纯城市列互斥，用于列映射与语义匹配降权。"""
    return (_is_province_level_header(template_col) and _is_city_level_header(source_col)) or (
        _is_city_level_header(template_col) and _is_province_level_header(source_col)
    )


def _semantic_match_score(template_col: str, source_col: str) -> float:
    t_norm = _norm_col_name(template_col)
    s_norm = _norm_col_name(source_col)
    if not t_norm or not s_norm:
        return 0.0
    if t_norm == s_norm:
        return 1.0
    if t_norm in s_norm or s_norm in t_norm:
        return 0.90

    ratio = SequenceMatcher(None, t_norm, s_norm).ratio()

    t_tokens = _split_tokens(t_norm)
    s_tokens = _split_tokens(s_norm)
    if not t_tokens or not s_tokens:
        return ratio

    overlap = len(set(t_tokens) & set(s_tokens)) / max(len(set(t_tokens)), 1)
    syn_hit = 0.0
    for tt in t_tokens:
        tg = _token_to_group(tt)
        if tg is None:
            continue
        if any(_token_to_group(st) == tg for st in s_tokens):
            syn_hit = 1.0
            break

    # 多因子融合：字符相似 + token重叠 + 语义组命中
    out = round(min(1.0, ratio * 0.45 + overlap * 0.35 + syn_hit * 0.20), 4)
    if _admin_level_conflict(template_col, source_col):
        out = min(out, 0.38)
    return out


def _build_semantic_mapping(template_columns: List[str], source_columns: List[str], min_score: float = 0.55) -> Tuple[Dict[str, str], float]:
    mapping: Dict[str, str] = {}
    total_score = 0.0
    used_source = set()
    for tc in template_columns:
        best_col = None
        best_score = 0.0
        for sc in source_columns:
            if sc in used_source:
                continue
            score = _semantic_match_score(tc, sc)
            if score > best_score:
                best_score = score
                best_col = sc
        if best_col and best_score >= min_score:
            mapping[tc] = best_col
            used_source.add(best_col)
            total_score += best_score

    overall = total_score / max(len(template_columns), 1)
    return mapping, round(overall, 4)


def _cell_looks_like_scientific_or_bulk_number(v) -> bool:
    """科学计数法、或超大数值（常见于误把 ISBN/ID 当性别列）。"""
    if v is None:
        return False
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        try:
            fv = float(v)
            if abs(fv) >= 1e11:
                return True
        except (ValueError, TypeError, OverflowError):
            pass
    s = str(v).strip()
    if not s:
        return False
    sl = s.lower()
    if "e+" in sl or "e-" in sl:
        return True
    core = s.replace(",", "").replace("，", "").replace(".", "").replace("-", "")
    if core.isdigit() and len(core) >= 13:
        return True
    return False


def _plausibility_for_mapped_samples(
    template_columns: List[str],
    col_map: Dict[str, str],
    samples: List[Dict[str, Any]],
) -> float:
    """用表头下若干行数据判断「列名-取值」是否合理，压低表头撞脸但数据错位的表。"""
    if not samples or not col_map:
        return 0.82
    col_scores: List[float] = []
    for tc, sc in col_map.items():
        if not sc or str(sc).startswith("col_"):
            continue
        blob = _norm_col_name(tc) + _norm_col_name(sc)
        vals = [row.get(sc) for row in samples if row.get(sc) is not None and str(row.get(sc)).strip() not in ("", "None")]
        if len(vals) < 2:
            continue
        if "性别" in blob:
            ok = 0
            for v in vals:
                t = str(v).strip()
                if t in ("男", "女"):
                    ok += 1
                elif t.lower() in ("m", "f", "male", "female"):
                    ok += 1
                elif not _cell_looks_like_scientific_or_bulk_number(v) and len(t) <= 8:
                    ok += 0.35
            col_scores.append(ok / len(vals))
        elif "年龄" in blob or "岁数" in blob or ("岁" in tc and len(tc) <= 8):
            ok = sum(
                1
                for v in vals
                if (n := _parse_numeric_cell(v)) is not None and 1 <= n <= 130
            )
            col_scores.append(ok / len(vals))
        elif "饮料" in blob:
            ok = sum(
                1
                for v in vals
                if not _cell_looks_like_scientific_or_bulk_number(v) and len(str(v).strip()) < 100
            )
            col_scores.append(ok / len(vals))
        elif "顾客" in blob and "性别" not in blob:
            bad = sum(1 for v in vals if _cell_looks_like_scientific_or_bulk_number(v))
            col_scores.append(max(0.0, 1.0 - bad / len(vals)))
    if not col_scores:
        return 0.88
    return max(0.08, min(1.0, sum(col_scores) / len(col_scores)))


def _pick_best_header_block_for_sheet(
    template_columns: List[str],
    fp: str,
    eff_ext: str,
    sheet_name: str,
    cand_rows: List[Tuple[int, List[str]]],
    min_score: float,
    top_semantic_probe: int = 14,
) -> Optional[Tuple[float, Dict[str, str], int, List[str], float, float]]:
    """在同一张表内：先按语义分列匹配筛候选，再用样本行加权，避免仅表头相似、数据错位。
    返回 (combined_score, col_map, header_row_1based, source_columns, semantic_score, plausibility)
    """
    ranked: List[Tuple[float, Dict[str, str], int, List[str]]] = []
    for row_1based, labels in cand_rows:
        if not _is_plausible_header_row(labels):
            continue
        columns = _columns_from_label_list(labels)
        # 与 _is_plausible_header_row 对齐：两列表格同样有效
        if len([c for c in columns if not str(c).startswith("col_")]) < 2:
            continue
        col_map, sem = _build_semantic_mapping(template_columns, columns, min_score=min_score)
        if sem < 0.06 and len(col_map) < 1:
            continue
        ranked.append((sem, col_map, row_1based, columns))
    if not ranked:
        return None
    ranked.sort(key=lambda x: -x[0])
    ranked = ranked[: max(6, top_semantic_probe)]

    best: Optional[Tuple[float, Dict[str, str], int, List[str], float, float]] = None
    best_key: Tuple[float, float, int] = (-1.0, -1.0, 0)

    for sem, col_map, row_1based, columns in ranked:
        if eff_ext == "xlsx":
            samples = _read_xlsx_sample_dicts(fp, sheet_name, row_1based, columns, n=18)
        else:
            samples = _read_xls_sample_dicts(fp, sheet_name, row_1based, columns, n=18)
        plaus = _plausibility_for_mapped_samples(template_columns, col_map, samples)
        combined = round(sem * (0.40 + 0.60 * plaus), 4)
        key = (combined, plaus, row_1based)
        if key > best_key:
            best_key = (combined, plaus, row_1based)
            best = (combined, col_map, row_1based, columns, sem, plaus)

    return best


def _excel_best_scored_block(
    template_columns: List[str],
    fp: str,
    eff_ext: str,
    min_score: float,
) -> Optional[Tuple[float, Dict[str, str], int, List[str], str]]:
    """遍历所有工作表及前若干行表头候选，返回与模板语义+样本合理性综合最优的一块。
    元组: (match_score, column_mapping, header_row_1based, source_columns, sheet_name)
    """
    best: Optional[Tuple[float, Dict[str, str], int, List[str], str, float, float]] = None
    if eff_ext == "xlsx":
        rb = openpyxl.load_workbook(fp, read_only=True, data_only=True)
        try:
            sheet_names = list(rb.sheetnames)
        finally:
            rb.close()
        for sn in sheet_names:
            cand_rows = _enumerate_xlsx_header_candidate_rows(fp, sn, max_scan=30)
            picked = _pick_best_header_block_for_sheet(
                template_columns, fp, eff_ext, sn, cand_rows, min_score=min_score
            )
            if picked is None:
                continue
            combined, col_map, hrow, cols, sem, plaus = picked
            if best is None:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
                continue
            b_comb, _, _, _, _, b_sem, b_plaus = best
            if combined > b_comb + 1e-9:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
            elif abs(combined - b_comb) < 1e-9 and plaus > b_plaus + 1e-9:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
            elif abs(combined - b_comb) < 1e-9 and abs(plaus - b_plaus) < 1e-9 and sem > b_sem + 1e-9:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
    elif eff_ext == "xls":
        import xlrd

        book = xlrd.open_workbook(fp, formatting_info=False)
        for sn in book.sheet_names():
            cand_rows = _enumerate_xls_header_candidate_rows(fp, sn, max_scan=30)
            picked = _pick_best_header_block_for_sheet(
                template_columns, fp, eff_ext, sn, cand_rows, min_score=min_score
            )
            if picked is None:
                continue
            combined, col_map, hrow, cols, sem, plaus = picked
            if best is None:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
                continue
            b_comb, _, _, _, _, b_sem, b_plaus = best
            if combined > b_comb + 1e-9:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
            elif abs(combined - b_comb) < 1e-9 and plaus > b_plaus + 1e-9:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
            elif abs(combined - b_comb) < 1e-9 and abs(plaus - b_plaus) < 1e-9 and sem > b_sem + 1e-9:
                best = (combined, col_map, hrow, cols, sn, sem, plaus)
    if best is None:
        return None
    combined, col_map, hrow, cols, sn, sem, plaus = best
    logger.info(
        "[find_best] sheet=%r header_row=%s sem=%s plaus=%s combined=%s",
        sn,
        hrow,
        sem,
        round(plaus, 3),
        combined,
    )
    return (combined, col_map, hrow, cols, sn)


def _collect_candidates_for_file(template_columns: List[str], f: Dict) -> List[Dict]:
    """扫描单个上传文件，返回 0..N 个候选（docx 多表可能多条）。供并行 find_best 使用。"""
    candidates: List[Dict] = []
    fname = f.get("file_name", "?")
    ext = f.get("file_type", "").lower()
    fp = f.get("file_path", "")
    if not fp:
        logger.info(f"[find_best] skip reason=no_file_path file={fname!r}")
        return candidates
    if not Path(fp).exists():
        logger.info(f"[find_best] skip reason=path_not_found file={fname!r} path={fp!r}")
        return candidates

    effective_ext = ext
    if effective_ext not in ("xlsx", "xls", "docx", "doc"):
        suffix = Path(fp).suffix.lower().lstrip(".")
        if suffix in ("xlsx", "xls", "docx", "doc"):
            logger.info(f"[find_best] 文件 '{fname}' file_type='{ext}' 不匹配，用扩展名推断为 '{suffix}'")
            effective_ext = suffix
        else:
            logger.info(
                f"[find_best] skip reason=unsupported_type file={fname!r} file_type={ext!r} suffix={suffix!r}"
            )
            return candidates

    try:
        if effective_ext in ("xlsx", "xls"):
            scored_block = _excel_best_scored_block(template_columns, fp, effective_ext, min_score=0.55)
            if scored_block is None:
                logger.info(
                    f"[find_best] skip reason=xlsx_no_matching_header file={fname!r} "
                    f"(多表/多工作表时请确认模板列与某一表头行能对上)"
                )
                return candidates
            score, col_map, hrow, cols, sn = scored_block
            logger.info(
                f"[find_best] 文件 '{fname}' 工作表={sn!r} 表头行={hrow} 列数={len(cols)} "
                f"列预览={cols[:8]!r} 匹配分={score}, 映射={col_map}"
            )
            if len(cols) <= 1:
                logger.info(
                    f"[find_best] skip reason=xlsx_too_few_columns file={fname!r} col_count={len(cols)}"
                )
                return candidates
            extra_cols = len(cols) - len(template_columns)
            priority = 1.0 if extra_cols > 0 else 0.5
            candidates.append({
                **f,
                "source_type": effective_ext,
                "sheet_name": sn,
                "header_row_1based": hrow,
                "source_columns": cols,
                "column_mapping": col_map,
                "match_score": score,
                "priority": priority,
            })

        elif effective_ext in ("docx", "doc"):
            tables = load_docx_tables(fp)
            for ti, (cols, data) in enumerate(tables):
                non_empty = sum(1 for row in data if any(str(v).strip() for v in row.values()))
                if non_empty == 0:
                    logger.info(
                        f"[find_best] skip reason=docx_no_table_rows file={fname!r} table_index={ti}"
                    )
                    continue
                col_map, score = _build_semantic_mapping(template_columns, cols, min_score=0.5)
                candidates.append({
                    **f,
                    "source_type": "docx_table",
                    "table_index": ti,
                    "source_columns": cols,
                    "column_mapping": col_map,
                    "match_score": score,
                    "row_count": non_empty,
                    "priority": 0.3,
                })
            if not tables:
                para_result = load_docx_paragraphs_as_table(fp)
                if para_result:
                    cols, data = para_result
                    logger.info(f"[find_best] 文件 '{fname}' 段落解析: {len(cols)} 列, {len(data)} 行, 列名={cols[:6]}")
                    col_map, score = _build_semantic_mapping(template_columns, cols, min_score=0.5)
                    logger.info(f"[find_best] 文件 '{fname}' 段落匹配分={score}, 映射={col_map}")
                    candidates.append({
                        **f,
                        "source_type": "docx_paragraph",
                        "source_columns": cols,
                        "column_mapping": col_map,
                        "match_score": score,
                        "row_count": len(data),
                        "priority": 0.2,
                    })
    except Exception as e:
        logger.info(
            f"[find_best] skip reason=exception file={fname!r} exc_type={type(e).__name__!r} detail={e!r}"
        )
        logger.error(f"[find_best] 处理文件 '{fname}' 异常: {e}", exc_info=True)
    return candidates


def try_weak_table_source(template_unique_columns: List[str], file_list: List[Dict]) -> Optional[Dict]:
    """find_best 无结果或分过低时的兜底：仅 xlsx/xls，低阈值语义映射 + 可选顺序补映射，mapped_count>=2 才返回。"""
    if os.getenv("TABLE_FILL_WEAK_FALLBACK_ENABLED", "true").lower() in ("0", "false", "no", "off"):
        return None
    if not template_unique_columns or not file_list:
        return None
    floor = float(os.getenv("TABLE_FILL_WEAK_MAP_MIN_SCORE", "0.25"))
    floor = max(0.05, min(floor, 0.55))

    for f in file_list:
        fname = f.get("file_name", "?")
        fp = f.get("file_path", "")
        ext = f.get("file_type", "").lower()
        if not fp or not Path(fp).exists():
            continue
        eff = ext
        if eff not in ("xlsx", "xls"):
            suf = Path(fp).suffix.lower().lstrip(".")
            if suf in ("xlsx", "xls"):
                eff = suf
            else:
                continue
        try:
            scored_block = _excel_best_scored_block(template_unique_columns, fp, eff, min_score=0.35)
            if scored_block is None:
                scored_block = _excel_best_scored_block(template_unique_columns, fp, eff, min_score=floor)
            if scored_block is None:
                continue
            score, col_map, hrow, cols, sn = scored_block
        except Exception as e:
            logger.info(
                f"[find_best] weak_skip file={fname!r} reason=read_columns_failed exc={type(e).__name__}"
            )
            continue
        if len(cols) < 2:
            continue

        positional_used = False
        min_used = 0.35
        if len(col_map) < 2:
            col_map, score = _build_semantic_mapping(template_unique_columns, cols, min_score=floor)
            min_used = floor
        if len(col_map) < 2:
            col_map = dict(col_map)
            n = min(len(template_unique_columns), len(cols))
            did_positional = False
            for i in range(n):
                tc = template_unique_columns[i]
                if tc not in col_map:
                    col_map[tc] = cols[i]
                    did_positional = True
            positional_used = did_positional
            min_used = floor
            # 弱分：按已映射列估算
            pair_scores = [
                _semantic_match_score(tc, col_map[tc])
                for tc in col_map
                if tc in template_unique_columns and col_map.get(tc)
            ]
            score = round(sum(pair_scores) / max(len(template_unique_columns), 1), 4) if pair_scores else 0.2

        mapped_count = len(col_map)
        if mapped_count < 2:
            continue

        priority = 0.4
        out = {
            **f,
            "source_type": eff,
            "sheet_name": sn,
            "header_row_1based": hrow,
            "source_columns": cols,
            "column_mapping": col_map,
            "match_score": min(score, 0.65),
            "priority": priority,
            "weak_fallback": True,
            "positional_fallback": positional_used,
            "weak_min_score_used": min_used,
            "weak_mapped_count": mapped_count,
        }
        logger.warning(
            f"[table_fill] weak_fallback candidate file={fname!r} sheet={sn!r} header_row={hrow} "
            f"mapped_count={mapped_count} weak_score={out['match_score']!r} min_score_used={min_used} positional={positional_used}"
        )
        return out
    return None


def find_data_source_candidates(
    template_columns: List[str], uploaded_files: List[Dict], top_k: int = 5
) -> List[Dict]:
    """按匹配分返回候选数据源（最多 top_k 个），供上层做「打平时由模型仲裁」。

    拆出来是因为：只按列名打分无法区分「2024年城市数据」和「2025年城市数据」——
    两者列名完全相同、得分都是 0.928，取排序第一个等于按上传顺序随机选。
    上层拿到候选列表后才能用用户要求去判断。
    """
    candidates: List[Dict] = []
    if not uploaded_files:
        logger.warning("[find_best] 没有上传文件")
        return []

    if len(uploaded_files) == 1:
        candidates.extend(_collect_candidates_for_file(template_columns, uploaded_files[0]))
    else:
        max_workers = min(8, len(uploaded_files))
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futures = [ex.submit(_collect_candidates_for_file, template_columns, f) for f in uploaded_files]
            for fut in as_completed(futures):
                try:
                    candidates.extend(fut.result())
                except Exception as e:
                    logger.error(f"[find_best] 并行扫描文件异常: {e}", exc_info=True)

    if not candidates:
        logger.warning(
            f"[find_best] 没有找到匹配的数据源，共检查 {len(uploaded_files)} 个文件；"
            "请查看上方各文件的 [find_best] skip 日志（reason=...）。"
            "常见原因：路径无效、类型非表格、xlsx 列数<=1、docx 表无数据行。"
        )
        return []
    candidates.sort(key=lambda x: (x["match_score"], x.get("priority", 0)), reverse=True)
    return candidates[: max(1, top_k)]


def find_best_data_source(template_columns: List[str], uploaded_files: List[Dict]) -> Optional[Dict]:
    """找到与模板列名最匹配的数据源文件。

    uploaded_files: [{"id": N, "file_name": "...", "file_path": "...", "file_type": "xlsx/docx"}]
    返回最佳匹配的文件信息 + 列名映射
    优先选择有实际数据的 xlsx 文件
    """
    cands = find_data_source_candidates(template_columns, uploaded_files, top_k=1)
    best = cands[0] if cands else None
    logger.info(f"[find_best] 最佳匹配: '{best.get('file_name')}', score={best['match_score']}")
    return best


def _parse_date(val) -> Optional[datetime]:
    """解析各种日期格式"""
    if val is None:
        return None
    if isinstance(val, datetime):
        return val
    s = str(val).strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M:%S.%f", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s[:19], fmt[:len(s)+2] if len(s) < 19 else fmt)
        except (ValueError, IndexError):
            continue
    # 尝试只取日期部分
    m = re.match(r'(\d{4})[/-](\d{1,2})[/-](\d{1,2})', s)
    if m:
        try:
            return datetime(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    return None


def _match_value(actual, expected) -> bool:
    if actual is None:
        return False
    return str(actual).strip().lower() == str(expected).strip().lower()


def _match_datetime(actual, expected_str) -> bool:
    if actual is None:
        return False
    a = str(actual).strip()
    e = str(expected_str).strip()
    # 精确匹配
    if a == e:
        return True
    # 日期部分匹配
    a_date = a[:10]
    e_date = e[:10]
    if a_date == e_date:
        # 如果时间部分也指定了
        if len(e) > 10:
            a_time = a[11:19] if len(a) > 11 else ""
            e_time = e[11:19] if len(e) > 11 else ""
            return a_time == e_time
        return True
    return False
