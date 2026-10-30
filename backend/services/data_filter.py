"""数据源条件筛选引擎 - 从已上传的 xlsx/docx 中按条件筛选数据"""
import os
import re
import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
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
            if sum(1 for x in labels if x) >= 3:
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
        if sum(1 for x in labels if x) >= 3:
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
    for raw in candidates:
        s = (raw or "").strip()
        if not s or len(s) > 24:
            continue
        if any(b in s for b in _NARRATIVE_REGION_NOISE_SUBSTR):
            continue
        if s in ("行政区", "涵盖城市", "部分城市", "监测数据"):
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


def parse_filter_conditions(user_requirement: str) -> Dict[str, Any]:
    """从用户要求中解析筛选条件（规则方式，不调LLM）"""
    conditions = {}
    req = str(user_requirement or "").strip()
    if not req:
        return conditions
    
    # 日期范围: "从2020/7/1到2020/8/31" 或 "2025-11-25"
    date_range = re.findall(r'从\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})\s*到\s*(\d{4}[/-]\d{1,2}[/-]\d{1,2})', req)
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

    # 年份解析
    years = re.findall(r'((?:19|20)\d{2})\s*年?', req)
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
    "datetime_column": "监测时间"  // 可选，时间列名
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

    return {
        "filters": filters,
        "column_mapping": safe_mapping,
        "group_by": group_by,
    }


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
        nonempty = [x for x in labels if x]
        if len(nonempty) < 3:
            continue
        columns = _columns_from_label_list(labels)
        if len([c for c in columns if not str(c).startswith("col_")]) < 3:
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


def find_best_data_source(template_columns: List[str], uploaded_files: List[Dict]) -> Optional[Dict]:
    """找到与模板列名最匹配的数据源文件。
    
    uploaded_files: [{"id": N, "file_name": "...", "file_path": "...", "file_type": "xlsx/docx"}]
    返回最佳匹配的文件信息 + 列名映射
    优先选择有实际数据的 xlsx 文件
    """
    candidates: List[Dict] = []
    if not uploaded_files:
        logger.warning("[find_best] 没有上传文件")
        return None

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
        return None
    candidates.sort(key=lambda x: (x["match_score"], x.get("priority", 0)), reverse=True)
    best = candidates[0]
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
