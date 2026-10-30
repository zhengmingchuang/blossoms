"""模板识别与填充引擎 - 支持表头批量填充和字段填充两种模式"""
import re
import os
import copy
from pathlib import Path
from typing import List, Dict, Any, Optional, Tuple
import openpyxl
import docx


def identify_fields(file_path: str) -> Dict[str, Any]:
    """识别模板结构，返回模板元信息。
    
    返回格式:
    {
        "mode": "table_fill" | "field_fill",
        "columns": [...],          # table_fill 模式
        "tables": [...],           # docx table_fill 模式（多表）
        "start_row": N,            # xlsx table_fill 起始行
        "sheet": "Sheet1",         # xlsx sheet名
        "fields": [...]            # field_fill 模式（兼容旧逻辑）
    }
    """
    ext = Path(file_path).suffix.lower()
    if ext in (".xlsx", ".xls"):
        return _identify_xlsx(file_path)
    if ext in (".docx", ".doc"):
        return _identify_docx(file_path)
    return {"mode": "field_fill", "fields": []}


def _identify_xls(fp: str) -> Dict[str, Any]:
    """Excel 97-2003 .xls：openpyxl 无法打开，用 xlrd 做表头检测。"""
    import xlrd
    from openpyxl.utils import get_column_letter

    book = xlrd.open_workbook(fp, formatting_info=False)
    for si in range(book.nsheets):
        sheet = book.sheet_by_index(si)
        sn = sheet.name
        if sheet.nrows < 1:
            continue

        best_header_row_idx = 0
        best_header_cells: List[Dict[str, Any]] = []
        best_score = -1.0
        for scan_ri in range(min(sheet.nrows, 15)):
            row_cells = []
            row_vals = sheet.row_values(scan_ri)
            for col_idx, v in enumerate(row_vals):
                if v is None:
                    continue
                if isinstance(v, str) and not v.strip():
                    continue
                text = str(v).strip()
                if not text or text.count("\n") > 1:
                    continue
                if len(text) > 200 and "\n" in text:
                    continue
                excel_col = col_idx + 1
                row_cells.append({"col": excel_col, "col_letter": get_column_letter(excel_col), "name": text})

            if len(row_cells) < 3:
                continue

            score = 0.0
            for c in row_cells:
                name = c["name"]
                if name.startswith("="):
                    continue
                if name.replace(".", "").replace("-", "").replace(":", "").isdigit():
                    score += 0.2
                elif len(name) <= 50:
                    score += 1.0
                else:
                    score += 0.5

            if score > best_score:
                best_score = score
                best_header_cells = row_cells
                best_header_row_idx = scan_ri

        if not best_header_cells:
            row_vals = sheet.row_values(0) if sheet.nrows else []
            for col_idx, v in enumerate(row_vals):
                if v is None or (isinstance(v, str) and not str(v).strip()):
                    continue
                text = str(v).strip()
                excel_col = col_idx + 1
                best_header_cells.append({"col": excel_col, "col_letter": get_column_letter(excel_col), "name": text})
            best_header_row_idx = 0

        if not best_header_cells:
            continue

        header_row = best_header_cells
        has_data_below = False
        empty_rows = 0
        hdr_1based = best_header_row_idx + 1
        for ri in range(best_header_row_idx + 1, min(sheet.nrows, best_header_row_idx + 12)):
            row_full = list(sheet.row_values(ri))
            row_vals = []
            for h in header_row:
                ci = h["col"] - 1
                row_vals.append(row_full[ci] if ci < len(row_full) else None)
            non_empty = sum(1 for v in row_vals if v is not None and str(v).strip())
            if non_empty == 0:
                empty_rows += 1
            else:
                has_data_below = True

        # 与 _identify_xlsx 一致：表头下有空行、或仅有表头、或已有数据行（.xls 不能走 openpyxl 字段识别，一律按表头批量填）
        if empty_rows >= 1 or sheet.nrows <= best_header_row_idx + 1 or has_data_below:
            return {
                "mode": "table_fill",
                "format": "xlsx",
                "sheet": sn,
                "columns": [{"col": h["col"], "col_letter": h["col_letter"], "name": h["name"]} for h in header_row],
                "start_row": hdr_1based + 1,
                "header_row": hdr_1based,
                "fields": [{"field_name": h["name"], "location_type": "table_header"} for h in header_row],
            }

        continue

    first_sn = book.sheet_names()[0] if book.nsheets else "Sheet1"
    return {"mode": "table_fill", "format": "xlsx", "sheet": first_sn, "columns": [], "start_row": 2, "fields": []}


def _resolve_merged_value(ws, row: int, col: int):
    """解析合并单元格：如果 (row,col) 在合并区域内，返回左上角单元格的值。"""
    for rng in ws.merged_cells.ranges:
        if rng.min_row <= row <= rng.max_row and rng.min_col <= col <= rng.max_col:
            return ws.cell(row=rng.min_row, column=rng.min_col).value
    return ws.cell(row=row, column=col).value


def _scan_one_sheet(ws, sn: str) -> Optional[Dict[str, Any]]:
    """扫描单个 sheet 检测表头，返回该 sheet 的模板信息或 None。"""
    if ws.max_row is None or ws.max_row < 1:
        return None

    best_header_row_idx = 1
    best_header_cells: List[Dict[str, Any]] = []
    best_score = -1.0

    for scan_ri in range(1, min(ws.max_row + 1, 16)):
        row_cells = []
        seen_cols = set()
        for cell in ws[scan_ri]:
            if cell.column in seen_cols:
                continue
            seen_cols.add(cell.column)
            v = _resolve_merged_value(ws, scan_ri, cell.column)
            if v is None:
                continue
            text = re.sub(r'[\s\u3000]+', ' ', str(v)).strip()
            if not text or text.count('\n') > 1:
                continue
            if len(text) > 200 and '\n' in text:
                continue
            row_cells.append({"col": cell.column, "col_letter": cell.column_letter, "name": text})

        if len(row_cells) < 2:
            continue

        score = 0.0
        for c in row_cells:
            name = c["name"]
            if name.startswith('='):
                continue
            if name.replace('.', '').replace('-', '').replace(':', '').isdigit():
                score += 0.2
            elif len(name) <= 50:
                score += 1.0
            else:
                score += 0.5

        if score > best_score:
            best_score = score
            best_header_cells = row_cells
            best_header_row_idx = scan_ri

    if not best_header_cells:
        for cell in ws[1]:
            v = _resolve_merged_value(ws, 1, cell.column)
            if v is not None:
                text = re.sub(r'[\s\u3000]+', ' ', str(v)).strip()
                if text:
                    best_header_cells.append({"col": cell.column, "col_letter": cell.column_letter, "name": text})
        best_header_row_idx = 1

    if not best_header_cells:
        return None

    empty_rows = 0
    has_data_below = False
    for ri in range(best_header_row_idx + 1, min(ws.max_row + 1, best_header_row_idx + 12)):
        row_vals = [_resolve_merged_value(ws, ri, h["col"]) for h in best_header_cells]
        non_empty = sum(1 for v in row_vals if v is not None and str(v).strip())
        if non_empty == 0:
            empty_rows += 1
        else:
            has_data_below = True

    if empty_rows >= 1 or ws.max_row == best_header_row_idx:
        return {
            "sheet": sn,
            "columns": [{"col": h["col"], "col_letter": h["col_letter"], "name": h["name"]} for h in best_header_cells],
            "start_row": best_header_row_idx + 1,
            "header_row": best_header_row_idx,
            "fields": [{"field_name": h["name"], "location_type": "table_header"} for h in best_header_cells],
        }

    if has_data_below:
        return None
    return None


def _identify_xlsx(fp: str) -> Dict[str, Any]:
    if fp.lower().endswith(".xls"):
        return _identify_xls(fp)
    wb = openpyxl.load_workbook(fp)

    sheets_info: List[Dict[str, Any]] = []
    for sn in wb.sheetnames:
        ws = wb[sn]
        result = _scan_one_sheet(ws, sn)
        if result:
            sheets_info.append(result)

    if not sheets_info:
        fields = _fields_xlsx_legacy(fp)
        if fields:
            return {"mode": "field_fill", "fields": fields}
        return {"mode": "table_fill", "format": "xlsx", "sheet": wb.sheetnames[0], "columns": [], "start_row": 2, "fields": []}

    if len(sheets_info) == 1:
        si = sheets_info[0]
        return {
            "mode": "table_fill", "format": "xlsx",
            "sheet": si["sheet"], "columns": si["columns"],
            "start_row": si["start_row"], "header_row": si.get("header_row"),
            "fields": si["fields"],
        }

    all_cols_map = {}
    for si in sheets_info:
        for c in si["columns"]:
            if c["name"] not in all_cols_map:
                all_cols_map[c["name"]] = c
    all_cols = list(all_cols_map.values())
    return {
        "mode": "table_fill", "format": "xlsx",
        "multi_sheet": True,
        "sheets": sheets_info,
        "sheet": sheets_info[0]["sheet"],
        "columns": all_cols,
        "start_row": sheets_info[0]["start_row"],
        "header_row": sheets_info[0].get("header_row"),
        "fields": [{"field_name": c["name"], "location_type": "table_header"} for c in all_cols],
    }


def _identify_docx(fp: str) -> Dict[str, Any]:
    path = fp
    cleanup = None
    if fp.lower().endswith(".doc"):
        from services.legacy_doc import convert_doc_to_docx_temp

        tmp = convert_doc_to_docx_temp(fp)
        if not tmp:
            return {"mode": "field_fill", "fields": []}
        path = tmp
        cleanup = tmp
    try:
        return _identify_docx_open(path)
    finally:
        if cleanup:
            try:
                os.unlink(cleanup)
            except OSError:
                pass


def _identify_docx_open(fp: str) -> Dict[str, Any]:
    try:
        doc = docx.Document(fp)
    except Exception:
        return {"mode": "field_fill", "fields": []}

    # 按文档顺序收集元素（段落和表格交替出现）
    # 我们需要知道每个表格上方最近的段落是什么
    # python-docx 的 doc.element.body 按顺序包含所有元素
    from docx.oxml.ns import qn
    
    # 收集所有段落文本（用于全局描述）
    all_para_texts = []
    for p in doc.paragraphs:
        t = p.text.strip()
        if t:
            all_para_texts.append(t)

    # 为每个表格找到其上方的段落描述
    # 遍历 body 的子元素，记录段落和表格的顺序
    body = doc.element.body
    current_paras = []  # 当前累积的段落文本
    table_descriptions = {}  # table_index -> description
    table_idx = 0
    
    for child in body:
        if child.tag == qn('w:p'):
            text = child.text or ""
            # 也需要从 runs 中提取文本
            runs_text = ""
            for r in child.iter(qn('w:t')):
                if r.text:
                    runs_text += r.text
            text = runs_text.strip() if runs_text.strip() else text.strip()
            if text:
                current_paras.append(text)
        elif child.tag == qn('w:tbl'):
            # 找到一个表格，将之前累积的段落作为该表格的描述
            if table_idx < len(doc.tables):
                # 取最近的 1-2 个段落作为描述
                desc = "\n".join(current_paras[-2:]) if current_paras else ""
                table_descriptions[table_idx] = desc
            table_idx += 1
            current_paras = []  # 重置，下一个表格的描述从这里开始

    # 检查表格
    tables_info = []
    for ti, table in enumerate(doc.tables):
        if len(table.rows) < 2:
            continue
        # 读取表头（处理合并单元格：去重）
        raw_cells = [c.text.strip() for c in table.rows[0].cells]
        # python-docx 对合并单元格会返回重复的 cell 对象，需要去重
        header_cells = []
        seen_texts = set()
        for i, text in enumerate(raw_cells):
            # 如果同一个文本连续出现，说明是合并单元格，只保留第一个
            if i > 0 and text == raw_cells[i-1]:
                continue
            header_cells.append(text)
        
        if not any(header_cells):
            continue
        # 检查后续行是否为空
        empty_data_rows = 0
        total_data_rows = len(table.rows) - 1
        for ri in range(1, len(table.rows)):
            row_vals = [c.text.strip() for c in table.rows[ri].cells]
            if not any(row_vals):
                empty_data_rows += 1

        if empty_data_rows > 0:
            desc = table_descriptions.get(ti, "")
            tables_info.append({
                "table_index": ti,
                "columns": header_cells,
                "total_rows": len(table.rows),
                "empty_rows": empty_data_rows,
                "start_row": 1,  # 数据从第1行开始（0是表头）
                "description": desc,
            })

    if tables_info:
        # 所有列名汇总
        all_cols = []
        for t in tables_info:
            all_cols.extend(t["columns"])
        return {
            "mode": "table_fill",
            "format": "docx",
            "tables": tables_info,
            "columns": list(dict.fromkeys(all_cols)),  # 去重保序
            "description": "\n".join(all_para_texts),
            "fields": [{"field_name": c, "location_type": "table_header"} for c in dict.fromkeys(all_cols)],
        }

    # fallback: 旧的字段识别
    fields = _fields_docx_legacy(fp)
    return {"mode": "field_fill", "fields": fields}


def _fields_xlsx_legacy(fp: str) -> List[Dict[str, Any]]:
    """旧的xlsx字段识别逻辑（占位符/标签模式）"""
    wb = openpyxl.load_workbook(fp)
    fields = []
    for sn in wb.sheetnames:
        ws = wb[sn]
        rows = list(ws.iter_rows(values_only=False))
        for ri, row in enumerate(rows):
            for ci, cell in enumerate(row):
                v = str(cell.value).strip() if cell.value is not None else ""
                if not v:
                    continue
                ph = re.findall(r'[{【<](.+?)[}>】]', v)
                if ph:
                    fields.append({"field_name": ph[0], "target_cell": f"{sn}!{cell.coordinate}", "location_type": "placeholder"})
                    continue
                if v.endswith(("：", ":")):
                    if ci + 1 < len(row):
                        right = row[ci + 1]
                        fields.append({"field_name": v.rstrip("：: "), "target_cell": f"{sn}!{right.coordinate}", "location_type": "label_right"})
                    continue
                if ci + 1 < len(row):
                    right = row[ci + 1]
                    if right.value is None or str(right.value).strip() == "":
                        fields.append({"field_name": v, "target_cell": f"{sn}!{right.coordinate}", "location_type": "header_right"})
    return fields


def _fields_docx_legacy(fp: str) -> List[Dict[str, Any]]:
    """旧的docx字段识别逻辑"""
    try:
        doc = docx.Document(fp)
    except Exception:
        return []
    fields = []
    for pi, para in enumerate(doc.paragraphs):
        t = para.text.strip()
        for ph in re.findall(r'[{【<《](.+?)[}>】》]', t):
            fields.append({"field_name": ph, "location_type": "paragraph", "location_index": pi})
    for ti, table in enumerate(doc.tables):
        for ri, row in enumerate(table.rows):
            for ci, cell in enumerate(row.cells):
                v = cell.text.strip()
                for ph in re.findall(r'[{【<《](.+?)[}>】》]', v):
                    fields.append({"field_name": ph, "location_type": "table_cell", "table_index": ti, "row_index": ri, "col_index": ci})
                if ci + 1 < len(row.cells) and not row.cells[ci + 1].text.strip():
                    if v and not re.search(r'[{【<《]', v):
                        fields.append({"field_name": v.rstrip("：: "), "location_type": "table_cell", "table_index": ti, "row_index": ri, "col_index": ci + 1})
    return fields


# ============================================================
# 填充函数
# ============================================================

def fill_template(
    template_path: str,
    output_path: str,
    values: Dict[str, str],
    mappings,
    template_info: Dict = None,
    src_file: Optional[str] = None
) -> str:
    """填充模板。支持两种模式。"""
    ext = Path(template_path).suffix.lower()
    if template_info and template_info.get("mode") == "table_fill":
        return fill_table_template(template_path, output_path, values, template_info)
    # 旧的字段填充模式
    if ext in (".xlsx", ".xls"):
        return _fill_xlsx_legacy(template_path, output_path, values, mappings, src_file=src_file)
    if ext in (".docx", ".doc"):
        return _fill_docx_legacy(template_path, output_path, values, mappings)
    raise ValueError(f"不支持: {ext}")


def fill_table_template(template_path: str, output_path: str, rows_data: Any, template_info: Dict, src_file: str = None, sum_config: Dict = None, stats_config: Dict = None) -> str:
    """表格批量填充模式：将多行数据写入模板。
    
    rows_data: List[Dict] 或 Dict[sheet_name, List[Dict]] - 要填入的数据行
    src_file: 数据源文件路径（用于图片填充）
    sum_config: {"row": bool, "col": bool} - 是否需要行/列求和
    stats_config: {"avg": bool, "max": bool, "min": bool} - 统计行
    """
    fmt = template_info.get("format", "xlsx")
    if fmt == "xlsx":
        if template_info.get("multi_sheet") and template_info.get("sheets"):
            return _fill_xlsx_multi_sheet(template_path, output_path, rows_data, template_info, src_file=src_file, sum_config=sum_config, stats_config=stats_config)
        return _fill_xlsx_table(template_path, output_path, rows_data, template_info, src_file=src_file, sum_config=sum_config, stats_config=stats_config)
    elif fmt == "docx":
        return _fill_docx_table(template_path, output_path, rows_data, template_info)
    raise ValueError(f"不支持的模板格式: {fmt}")


def _fill_xlsx_multi_sheet(tp: str, op: str, rows_data: Any, info: Dict, src_file: str = None, sum_config: Dict = None, stats_config: Dict = None) -> str:
    """多表单填充：遍历每个 sheet 的模板信息，分别填充对应数据。
    功能与 _fill_xlsx_table 对齐：安全写入、千分位处理、DISPIMG 保留、自动小计、行列求和。

    rows_data 可以是:
      - Dict[sheet_name, List[Dict]]: 按 sheet 名分组的数据
      - List[Dict]: 所有数据统一，按列名匹配分发到各 sheet
    """
    from openpyxl.utils import get_column_letter
    wb = openpyxl.load_workbook(tp)
    sheets = info.get("sheets", [])

    def _ms_safe_write(cell, val):
        if type(cell).__name__ == 'MergedCell':
            return
        s = str(val).strip()
        if not s:
            return
        if _has_dispimg(s):
            cell.value = s
            return
        s_num = s.replace(",", "").replace("\uff0c", "")
        try:
            fval = float(s_num)
            if fval == int(fval) and '.' not in s_num:
                cell.value = int(fval)
            else:
                cell.value = fval
        except (ValueError, TypeError):
            cell.value = s

    for si in sheets:
        sn = si["sheet"]
        if sn not in wb.sheetnames:
            continue
        ws = wb[sn]
        columns = si.get("columns", [])
        start_row = si.get("start_row", 2)
        col_names = [c["name"] for c in columns]

        if isinstance(rows_data, dict):
            sheet_rows = rows_data.get(sn, [])
        else:
            sheet_rows = []
            for row in (rows_data or []):
                if isinstance(row, dict) and any(str(row.get(cn, "")).strip() for cn in col_names):
                    sheet_rows.append(row)

        if not sheet_rows:
            continue

        _tpl_row = start_row
        _tpl_height = ws.row_dimensions[_tpl_row].height

        col_pairs = [(c["name"], c.get("col_letter", get_column_letter(c["col"]))) for c in columns]
        SUB_KW = ('sub', 'subtotal', 'amount', '\u5c0f\u8ba1', 'total')
        QTY_KW = ('quantity', 'qty', '\u6570\u91cf', 'count')
        PRICE_KW = ('price', 'unitprice', '\u5355\u4ef7', '\u4ef7\u683c', 'unit')
        sub_col_letter = qty_col_name = price_col_name = None
        for cn, cl in col_pairs:
            cn_l = cn.lower().replace(' ', '')
            if any(kw in cn_l for kw in SUB_KW) and sub_col_letter is None:
                sub_col_letter = cl
            if any(kw in cn_l for kw in QTY_KW) and qty_col_name is None:
                qty_col_name = cn
            if any(kw in cn_l for kw in PRICE_KW) and price_col_name is None:
                price_col_name = cn

        for ri, row in enumerate(sheet_rows):
            excel_row = start_row + ri
            if _tpl_height:
                ws.row_dimensions[excel_row].height = _tpl_height

            for col_name, col_letter in col_pairs:
                val = ""
                if isinstance(row, dict):
                    val = row.get(col_name, "")
                val = "" if val is None else str(val).strip()

                target = ws[f"{col_letter}{excel_row}"]
                tpl_cell = ws[f"{col_letter}{_tpl_row}"]
                if type(target).__name__ != 'MergedCell' and type(tpl_cell).__name__ != 'MergedCell' and tpl_cell.has_style:
                    target.font = copy.copy(tpl_cell.font)
                    target.fill = copy.copy(tpl_cell.fill)
                    target.border = copy.copy(tpl_cell.border)
                    target.alignment = copy.copy(tpl_cell.alignment)
                    target.protection = copy.copy(tpl_cell.protection)
                    target.number_format = tpl_cell.number_format

                if val:
                    _ms_safe_write(target, val)

            if sub_col_letter and qty_col_name and price_col_name and isinstance(row, dict):
                try:
                    import re as _re
                    qty_raw = str(row.get(qty_col_name, '') or '').replace(',', '').strip()
                    price_raw = str(row.get(price_col_name, '') or '').replace(',', '').strip()
                    price_raw = _re.sub(r'[^\d.\-]', '', price_raw)
                    qty_raw = _re.sub(r'[^\d.\-]', '', qty_raw)
                    qty = float(qty_raw)
                    price = float(price_raw)
                    sub_cell = ws[f"{sub_col_letter}{excel_row}"]
                    if type(sub_cell).__name__ != 'MergedCell':
                        sub_val = qty * price
                        sub_cell.value = int(sub_val) if sub_val == int(sub_val) else round(sub_val, 4)
                except (ValueError, TypeError):
                    pass

        data_row_count = len(sheet_rows)
        last_data_row = start_row + data_row_count - 1
        header_row_idx = si.get("header_row", start_row - 1)

        if sum_config and data_row_count > 0:
            col_letters = [cl for _, cl in col_pairs]
            numeric_col_letters = col_letters[1:] if len(col_letters) > 1 else col_letters
            if sum_config.get("row"):
                max_col_num = max(c.get("col", 1) for c in columns)
                sum_col_num = max_col_num + 1
                scl = get_column_letter(sum_col_num)
                ws.cell(row=header_row_idx, column=sum_col_num, value="\u5408\u8ba1")
                first_nl = numeric_col_letters[0] if numeric_col_letters else col_letters[0]
                last_cl = col_letters[-1] if col_letters else "A"
                for r in range(start_row, last_data_row + 1):
                    ws[f"{scl}{r}"] = f"=SUM({first_nl}{r}:{last_cl}{r})"
            if sum_config.get("col"):
                sum_row = last_data_row + 1
                first_cl = col_letters[0] if col_letters else "A"
                ws[f"{first_cl}{sum_row}"] = "\u5408\u8ba1"
                for nl in numeric_col_letters:
                    ws[f"{nl}{sum_row}"] = f"=SUM({nl}{start_row}:{nl}{last_data_row})"

        if stats_config and data_row_count > 0:
            col_letters = [cl for _, cl in col_pairs]
            numeric_col_letters = col_letters[1:] if len(col_letters) > 1 else col_letters
            base_row = last_data_row + (2 if sum_config and sum_config.get("col") else 1)
            if stats_config.get("average"):
                first_cl = col_letters[0] if col_letters else "A"
                ws[f"{first_cl}{base_row}"] = "\u5e73\u5747\u503c"
                for nl in numeric_col_letters:
                    ws[f"{nl}{base_row}"] = f"=AVERAGE({nl}{start_row}:{nl}{last_data_row})"
                base_row += 1
            if stats_config.get("max"):
                first_cl = col_letters[0] if col_letters else "A"
                ws[f"{first_cl}{base_row}"] = "\u6700\u5927\u503c"
                for nl in numeric_col_letters:
                    ws[f"{nl}{base_row}"] = f"=MAX({nl}{start_row}:{nl}{last_data_row})"
                base_row += 1
            if stats_config.get("min"):
                first_cl = col_letters[0] if col_letters else "A"
                ws[f"{first_cl}{base_row}"] = "\u6700\u5c0f\u503c"
                for nl in numeric_col_letters:
                    ws[f"{nl}{base_row}"] = f"=MIN({nl}{start_row}:{nl}{last_data_row})"

    wb.save(op)
    return op


def _has_dispimg(v: str) -> bool:
    """检查字符串是否包含 DISPIMG 公式（兼容 =DISPIMG 和 =_xlfn.DISPIMG）"""
    return 'DISPIMG' in v.upper()


def _fill_xlsx_table(tp: str, op: str, rows_data: list, info: Dict, src_file: str = None, sum_config: Dict = None, stats_config: Dict = None) -> str:
    """xlsx 表格批量填充：在表头下方逐行写入数据，支持图片填充、行列求和及统计公式"""
    import openpyxl.cell.cell as _cell_mod
    wb = openpyxl.load_workbook(tp)
    sn = info.get("sheet", wb.sheetnames[0])
    ws = wb[sn]
    columns = info.get("columns", [])
    start_row = info.get("start_row", 2)

    if not rows_data:
        wb.save(op)
        return op

    # 基于模板首行（start_row）复制样式，避免写入后丢失边框/字体/对齐格式
    _template_style_row = start_row
    _template_row_height = ws.row_dimensions[_template_style_row].height

    def _copy_cell_style(target_cell, template_cell):
        if type(target_cell).__name__ == 'MergedCell' or type(template_cell).__name__ == 'MergedCell':
            return
        if template_cell.has_style:
            target_cell.font = copy.copy(template_cell.font)
            target_cell.fill = copy.copy(template_cell.fill)
            target_cell.border = copy.copy(template_cell.border)
            target_cell.alignment = copy.copy(template_cell.alignment)
            target_cell.protection = copy.copy(template_cell.protection)
            target_cell.number_format = template_cell.number_format

    def _apply_row_style(excel_row: int):
        if _template_row_height is not None:
            ws.row_dimensions[excel_row].height = _template_row_height
        for c in columns:
            col_letter = c.get("col_letter")
            if not col_letter:
                continue
            target = ws[f"{col_letter}{excel_row}"]
            tpl = ws[f"{col_letter}{_template_style_row}"]
            _copy_cell_style(target, tpl)

    def _safe_write(cell, val):
        """安全写入单元格，跳过合并单元格的从属格"""
        if type(cell).__name__ == 'MergedCell':
            return
        s = str(val).strip()
        if not s:
            return
        if _has_dispimg(s):
            cell.value = s
            return
        # 去除千分位逗号后尝试数值转换，避免 "14,415.90" 被存为文本导致 SUM 失效
        s_num = s.replace(",", "").replace("，", "")
        try:
            fval = float(s_num)
            if fval == int(fval) and '.' not in s_num:
                cell.value = int(fval)
            else:
                cell.value = fval
        except (ValueError, TypeError):
            cell.value = s

    # 识别图片列：先按列名关键词找，再从实际数据值里找含 DISPIMG 的列
    pic_col_name = None
    pic_col_letter = None
    if isinstance(rows_data[0], dict):
        # 保留列顺序，支持多组重复表头（同名多列对应不同 col_letter）
        col_pairs = [(c["name"], c["col_letter"]) for c in columns if c.get("col_letter")]
        col_map_first = {}
        for name, letter in col_pairs:
            if name not in col_map_first:
                col_map_first[name] = letter

        def _fuzzy_get(row_dict: dict, col_name: str):
            """从 row_dict 中模糊匹配 col_name 取值，兼容括号/空格差异"""
            if col_name in row_dict:
                return row_dict[col_name]
            cn = re.sub(r'[\s（）()\u3000]', '', col_name.lower())
            for k, v in row_dict.items():
                kn = re.sub(r'[\s（）()\u3000]', '', k.lower())
                if cn == kn:
                    return v
            for k, v in row_dict.items():
                kn = re.sub(r'[\s（）()\u3000]', '', k.lower())
                if cn in kn or kn in cn:
                    return v
            return ""

        # 优先：列名含图片关键词 且 有 DISPIMG 值
        PIC_KW = ('picture', '图片', 'pic', 'image', '照片', 'photo')
        for col_name, col_letter in col_pairs:
            if any(kw in col_name.lower() for kw in PIC_KW):
                for row in rows_data[:5]:
                    v = str(row.get(col_name, '') or '')
                    if _has_dispimg(v):
                        pic_col_name = col_name
                        pic_col_letter = col_letter
                        break
            if pic_col_name:
                break

        # fallback：从 rows_data 的所有 key 里找含 DISPIMG 的列
        # （处理列名映射后 key 不在 col_map 里的情况）
        if not pic_col_name:
            for row in rows_data[:3]:
                for k, v in row.items():
                    if _has_dispimg(str(v or '')):
                        if k in col_map_first:
                            pic_col_name = k
                            pic_col_letter = col_map_first[k]
                        else:
                            for col_name, col_letter in col_pairs:
                                if any(kw in col_name.lower() for kw in PIC_KW):
                                    pic_col_name = k
                                    pic_col_letter = col_letter
                                    break
                        if pic_col_name:
                            break
                if pic_col_name:
                    break

        # 识别 SUB/小计列、数量列、单价列（用于自动计算）
        SUB_KW   = ('sub', 'subtotal', 'amount', '小计', 'total')
        QTY_KW   = ('quantity', 'qty', '数量', 'count')
        PRICE_KW = ('price', 'unitprice', '单价', '价格', 'unit')
        sub_col_letter = qty_col_name = price_col_name = None
        for col_name, col_letter in col_pairs:
            cn_l = col_name.lower().replace(' ', '')
            if any(kw in cn_l for kw in SUB_KW) and sub_col_letter is None:
                sub_col_letter = col_letter
            if any(kw in cn_l for kw in QTY_KW) and qty_col_name is None:
                qty_col_name = col_name
            if any(kw in cn_l for kw in PRICE_KW) and price_col_name is None:
                price_col_name = col_name

        for ri, row_dict in enumerate(rows_data):
            excel_row = start_row + ri
            _apply_row_style(excel_row)
            for col_name, col_letter in col_pairs:
                val = _fuzzy_get(row_dict, col_name)
                if val is not None and str(val).strip():
                    cell = ws[f"{col_letter}{excel_row}"]
                    _safe_write(cell, val)
            # 自动计算 SUB = Quantity × Unit Price
            if sub_col_letter and qty_col_name and price_col_name:
                try:
                    qty_raw   = str(row_dict.get(qty_col_name,   '') or '').replace(',', '').strip()
                    price_raw = str(row_dict.get(price_col_name, '') or '').replace(',', '').strip()
                    # 去掉货币符号（US$、$、¥、€ 等）
                    price_raw = re.sub(r'[^\d.\-]', '', price_raw)
                    qty_raw   = re.sub(r'[^\d.\-]', '', qty_raw)
                    qty   = float(qty_raw)
                    price = float(price_raw)
                    sub_cell = ws[f"{sub_col_letter}{excel_row}"]
                    if type(sub_cell).__name__ != 'MergedCell':
                        sub_val = qty * price
                        sub_cell.value = int(sub_val) if sub_val == int(sub_val) else round(sub_val, 4)
                except (ValueError, TypeError):
                    pass

    elif isinstance(rows_data[0], (list, tuple)):
        for ri, row_list in enumerate(rows_data):
            excel_row = start_row + ri
            _apply_row_style(excel_row)
            for ci, val in enumerate(row_list):
                if ci < len(columns):
                    col_letter = columns[ci]["col_letter"]
                    if val is not None and str(val).strip():
                        cell = ws[f"{col_letter}{excel_row}"]
                        _safe_write(cell, val)

    # 自动加宽 SUB 列，避免 ###### 显示
    if sub_col_letter:
        try:
            ws.column_dimensions[sub_col_letter].width = max(
                ws.column_dimensions[sub_col_letter].width or 8, 14
            )
        except Exception:
            pass

    # ---- 行列求和（所有数据写完后再添加公式） ----
    data_row_count = len(rows_data)
    last_data_row = start_row + data_row_count - 1
    header_row_idx = info.get("header_row", start_row - 1)

    if sum_config and data_row_count > 0:
        from openpyxl.utils import get_column_letter

        col_letters = [c.get("col_letter") for c in columns if c.get("col_letter")]
        first_col_letter = col_letters[0] if col_letters else "A"
        # 第二列起为数值列（第一列通常是名称/标签）
        numeric_col_letters = col_letters[1:] if len(col_letters) > 1 else col_letters
        first_numeric_letter = numeric_col_letters[0] if numeric_col_letters else first_col_letter
        last_col_letter = col_letters[-1] if col_letters else "A"

        sum_col_letter = None  # 行求和列字母，列求和也可能用到

        # --- 行求和：在最后一列右侧新增"合计"列 ---
        if sum_config.get("row"):
            max_col_num = max(c.get("col", 1) for c in columns)
            sum_col_num = max_col_num + 1
            sum_col_letter = get_column_letter(sum_col_num)

            header_cell = ws[f"{sum_col_letter}{header_row_idx}"]
            header_cell.value = "合计"
            _copy_cell_style(header_cell, ws[f"{first_col_letter}{header_row_idx}"])

            # SUM 公式只覆盖数值列（跳过名称列）
            for ri in range(data_row_count):
                excel_row = start_row + ri
                formula = f"=SUM({first_numeric_letter}{excel_row}:{last_col_letter}{excel_row})"
                sum_cell = ws[f"{sum_col_letter}{excel_row}"]
                sum_cell.value = formula
                _copy_cell_style(sum_cell, ws[f"{first_numeric_letter}{_template_style_row}"])

            try:
                ws.column_dimensions[sum_col_letter].width = max(
                    ws.column_dimensions[sum_col_letter].width or 8, 14
                )
            except Exception:
                pass

        # --- 列求和：在最后一行下方新增"合计"行 ---
        if sum_config.get("col"):
            sum_row = last_data_row + 1

            label_cell = ws[f"{first_col_letter}{sum_row}"]
            label_cell.value = "合计"
            _copy_cell_style(label_cell, ws[f"{first_col_letter}{_template_style_row}"])

            for cl in numeric_col_letters:
                formula = f"=SUM({cl}{start_row}:{cl}{last_data_row})"
                col_sum_cell = ws[f"{cl}{sum_row}"]
                col_sum_cell.value = formula
                _copy_cell_style(col_sum_cell, ws[f"{cl}{_template_style_row}"])

            if sum_config.get("row") and sum_col_letter:
                total_cell = ws[f"{sum_col_letter}{sum_row}"]
                total_cell.value = f"=SUM({sum_col_letter}{start_row}:{sum_col_letter}{last_data_row})"
                _copy_cell_style(total_cell, ws[f"{first_numeric_letter}{_template_style_row}"])

            if _template_row_height is not None:
                ws.row_dimensions[sum_row].height = _template_row_height

    # ---- 统计行：最大值 / 最小值 / 平均值（在所有数据和合计行之后追加） ----
    if stats_config and data_row_count > 0:
        from openpyxl.utils import get_column_letter as _gl

        col_letters = [c.get("col_letter") for c in columns if c.get("col_letter")]
        first_col_letter_s = col_letters[0] if col_letters else "A"
        numeric_col_letters_s = col_letters[1:] if len(col_letters) > 1 else col_letters

        # 确定起始行：在已有数据和合计行之后
        next_row = last_data_row + 1
        if sum_config and sum_config.get("col"):
            next_row += 1
        # 行求和列（如果有的话）
        sum_col_letter_s = None
        if sum_config and sum_config.get("row"):
            max_col_num = max(c.get("col", 1) for c in columns)
            sum_col_letter_s = _gl(max_col_num + 1)

        _stat_items = []
        if stats_config.get("max"):
            _stat_items.append(("最大值", "MAX"))
        if stats_config.get("min"):
            _stat_items.append(("最小值", "MIN"))
        if stats_config.get("avg"):
            _stat_items.append(("平均值", "AVERAGE"))

        for label, func in _stat_items:
            label_cell = ws[f"{first_col_letter_s}{next_row}"]
            label_cell.value = label
            _copy_cell_style(label_cell, ws[f"{first_col_letter_s}{_template_style_row}"])
            for cl in numeric_col_letters_s:
                cell = ws[f"{cl}{next_row}"]
                cell.value = f"={func}({cl}{start_row}:{cl}{last_data_row})"
                _copy_cell_style(cell, ws[f"{cl}{_template_style_row}"])
            if sum_col_letter_s:
                cell = ws[f"{sum_col_letter_s}{next_row}"]
                cell.value = f"={func}({sum_col_letter_s}{start_row}:{sum_col_letter_s}{last_data_row})"
                _copy_cell_style(cell, ws[f"{numeric_col_letters_s[0]}{_template_style_row}"])
            if _template_row_height is not None:
                ws.row_dimensions[next_row].height = _template_row_height
            next_row += 1

    wb.save(op)

    # 图片填充（方案A优先，fallback方案B）
    if pic_col_name and pic_col_letter and src_file and os.path.exists(src_file):
        try:
            from services.image_filler import fill_images
            fill_images(
                src_xlsx=src_file,
                dst_xlsx=op,
                rows_data=rows_data,
                pic_col_name=pic_col_name,
                col_letter=pic_col_letter,
                start_row=start_row,
            )
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning(f'[template_filler] 图片填充失败: {e}')

    return op


def _fill_docx_table(tp: str, op: str, rows_data: list, info: Dict) -> str:
    """docx 表格批量填充：在每个表格的空行中写入数据"""
    doc = docx.Document(tp)
    tables_info = info.get("tables", [])

    if not tables_info or not rows_data:
        doc.save(op)
        return op

    # rows_data 可以是:
    # 1. 单个 List[Dict] -> 填入所有表格（按表格描述筛选）
    # 2. Dict[table_index -> List[Dict]] -> 按表格索引分配

    if isinstance(rows_data, dict):
        # 按表格索引分配
        for tinfo in tables_info:
            ti = tinfo["table_index"]
            t_rows = rows_data.get(ti, rows_data.get(str(ti), []))
            if t_rows and ti < len(doc.tables):
                _write_docx_table(doc.tables[ti], tinfo["columns"], t_rows, tinfo.get("start_row", 1))
    elif isinstance(rows_data, list):
        # 如果只有一个表格，直接填
        if len(tables_info) == 1:
            ti = tables_info[0]["table_index"]
            if ti < len(doc.tables):
                _write_docx_table(doc.tables[ti], tables_info[0]["columns"], rows_data, tables_info[0].get("start_row", 1))
        else:
            # 多个表格，rows_data 应该是按表格分组的
            for tinfo in tables_info:
                ti = tinfo["table_index"]
                if ti < len(doc.tables):
                    _write_docx_table(doc.tables[ti], tinfo["columns"], rows_data, tinfo.get("start_row", 1))

    doc.save(op)
    return op


def _docx_row_fuzzy_get(row_dict: dict, col_name: str):
    """与 xlsx 表填一致：兼容表头括号、空格、全角空格等差异。"""
    if col_name in row_dict:
        return row_dict[col_name]
    cn = re.sub(r"[\s（）()\u3000]", "", str(col_name).lower())
    for k, v in row_dict.items():
        kn = re.sub(r"[\s（）()\u3000]", "", str(k).lower())
        if cn == kn:
            return v
    for k, v in row_dict.items():
        kn = re.sub(r"[\s（）()\u3000]", "", str(k).lower())
        if cn in kn or kn in cn:
            return v
    return ""


def _write_docx_table(table, columns: List[str], rows_data: List[Dict], start_row: int):
    """向 docx 表格中写入数据行，保留模板首行的段落样式"""
    from docx.oxml.ns import qn as _qn

    # 缓存模板首行的段落格式（字体大小、对齐等），用于复制到新行
    template_row = table.rows[start_row] if start_row < len(table.rows) else None
    _tpl_para_props: Dict[int, Any] = {}
    if template_row:
        for ci in range(len(template_row.cells)):
            cell = template_row.cells[ci]
            if cell.paragraphs:
                p = cell.paragraphs[0]
                _tpl_para_props[ci] = {
                    "style": p.style,
                    "alignment": p.alignment,
                    "pPr": copy.deepcopy(p._p.find(_qn("w:pPr"))) if p._p.find(_qn("w:pPr")) is not None else None,
                    "rPr": copy.deepcopy(p.runs[0]._r.find(_qn("w:rPr"))) if p.runs and p.runs[0]._r.find(_qn("w:rPr")) is not None else None,
                }

    for ri, row_dict in enumerate(rows_data):
        table_row_idx = start_row + ri
        if table_row_idx >= len(table.rows):
            row = table.add_row()
        else:
            row = table.rows[table_row_idx]
        for ci, col_name in enumerate(columns):
            if ci >= len(row.cells):
                continue
            val = _docx_row_fuzzy_get(row_dict, col_name)
            if val is None:
                continue
            cell = row.cells[ci]
            # 清除已有段落内容但保留段落节点
            for p in cell.paragraphs:
                for r in p.runs:
                    r.text = ""
            para = cell.paragraphs[0] if cell.paragraphs else cell.add_paragraph()
            # 复制模板样式
            tpl = _tpl_para_props.get(ci)
            if tpl:
                if tpl["style"]:
                    para.style = tpl["style"]
                if tpl["alignment"] is not None:
                    para.alignment = tpl["alignment"]
                if tpl["pPr"] is not None:
                    existing = para._p.find(_qn("w:pPr"))
                    new_pPr = copy.deepcopy(tpl["pPr"])
                    if existing is not None:
                        para._p.replace(existing, new_pPr)
                    else:
                        para._p.insert(0, new_pPr)
                run = para.add_run(str(val))
                if tpl["rPr"] is not None:
                    existing_rPr = run._r.find(_qn("w:rPr"))
                    new_rPr = copy.deepcopy(tpl["rPr"])
                    if existing_rPr is not None:
                        run._r.replace(existing_rPr, new_rPr)
                    else:
                        run._r.insert(0, new_rPr)
            else:
                para.add_run(str(val))


# ============================================================
# 旧的填充函数（兼容 field_fill 模式）
# ============================================================

def _fill_xlsx_legacy(tp, op, values, mappings, src_file: Optional[str] = None):
    wb = openpyxl.load_workbook(tp)
    dispimg_cells = []
    if isinstance(mappings, list):
        for m in mappings:
            fn = m["field_name"] if isinstance(m, dict) else m
            tc = m.get("target_cell", "") if isinstance(m, dict) else ""
            val = values.get(fn, "") if isinstance(values, dict) else ""
            if not val or not tc:
                continue
            if "!" in tc:
                sn, cr = tc.split("!", 1)
            else:
                sn, cr = wb.sheetnames[0], tc
            if sn in wb.sheetnames:
                wb[sn][cr] = val
                if _has_dispimg(str(val or "")):
                    dispimg_cells.append({"sheet": sn, "cell": cr, "value": str(val)})
    wb.save(op)
    if src_file and os.path.exists(src_file) and dispimg_cells:
        try:
            from services.image_filler import insert_images_by_cells
            insert_images_by_cells(op, src_file, dispimg_cells)
        except Exception:
            pass
    return op


def _fill_docx_legacy(tp, op, values, mappings):
    doc = docx.Document(tp)
    if isinstance(mappings, list):
        for m in mappings:
            fn = m["field_name"] if isinstance(m, dict) else m
            val = values.get(fn, "") if isinstance(values, dict) else ""
            if not val:
                continue
            lt = m.get("location_type", "") if isinstance(m, dict) else ""
            if lt == "paragraph":
                idx = m.get("location_index", -1)
                if 0 <= idx < len(doc.paragraphs):
                    para = doc.paragraphs[idx]
                    for marker in [f"【{fn}】", f"{{{fn}}}", f"<{fn}>", f"《{fn}》"]:
                        if marker in para.text:
                            for run in para.runs:
                                run.text = run.text.replace(marker, val)
            elif lt == "table_cell":
                ti = m.get("table_index", -1)
                ri = m.get("row_index", -1)
                ci = m.get("col_index", -1)
                if 0 <= ti < len(doc.tables):
                    tbl = doc.tables[ti]
                    if 0 <= ri < len(tbl.rows) and 0 <= ci < len(tbl.rows[ri].cells):
                        cell = tbl.rows[ri].cells[ci]
                        if not cell.text.strip():
                            cell.paragraphs[0].text = val
    doc.save(op)
    return op
