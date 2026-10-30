"""文档智能操作引擎 - 通过自然语言指令执行文档编辑、排版、格式调整、内容提取"""
import os
import re
import copy
import json
import logging
from pathlib import Path
from typing import Dict, Any, List, Optional

logger = logging.getLogger("doc_editor")


# ============================================================
# 1. 文档内容编辑
# ============================================================

def _replace_in_paragraph(para, target: str, value: str) -> int:
    """跨 run 替换：将段落内所有 run 文本拼合后替换，再重新分配回 run 中，保留首个 run 的格式。"""
    full = para.text
    if target not in full:
        return 0
    new_text = full.replace(target, value)
    count = full.count(target)
    if para.runs:
        for i, run in enumerate(para.runs):
            if i == 0:
                run.text = new_text
            else:
                run.text = ""
    return count


def edit_docx_content(file_path: str, operations: List[Dict]) -> Dict[str, Any]:
    """对 docx 执行编辑操作。
    operations: [{"action":"replace","target":"旧文本","value":"新文本"}, ...]
    action: replace / delete / append / insert_after
    """
    import docx
    doc = docx.Document(file_path)
    results = []
    for op in operations:
        action = op.get("action", "replace")
        target = op.get("target", "")
        value = op.get("value", "")
        count = 0
        if action == "replace" and target:
            for para in doc.paragraphs:
                count += _replace_in_paragraph(para, target, value)
            for table in doc.tables:
                for row in table.rows:
                    for cell in row.cells:
                        for para in cell.paragraphs:
                            count += _replace_in_paragraph(para, target, value)
            results.append({"action": action, "target": target, "matches": count})
        elif action == "delete" and target:
            for para in list(doc.paragraphs):
                if target in para.text:
                    para._element.getparent().remove(para._element)
                    count += 1
            results.append({"action": action, "target": target, "deleted": count})
        elif action == "append" and value:
            doc.add_paragraph(value)
            results.append({"action": action, "added": True})
        elif action == "insert_after" and target and value:
            for para in doc.paragraphs:
                if target in para.text:
                    new_p = doc.add_paragraph(value)
                    para._element.addnext(new_p._element)
                    count += 1
                    break
            results.append({"action": action, "inserted": count > 0})
    doc.save(file_path)
    return {"success": True, "file": os.path.basename(file_path), "operations": results}


def edit_xlsx_content(file_path: str, operations: List[Dict]) -> Dict[str, Any]:
    """对 xlsx 执行编辑操作。
    operations: [{"action":"set_cell","sheet":"Sheet1","cell":"A1","value":"新值"}, ...]
    action: set_cell / replace / clear_cell
    """
    import openpyxl
    wb = openpyxl.load_workbook(file_path)
    results = []
    for op in operations:
        action = op.get("action", "set_cell")
        sn = op.get("sheet", wb.sheetnames[0])
        if sn not in wb.sheetnames:
            sn = wb.sheetnames[0]
        ws = wb[sn]
        if action == "set_cell":
            coord = op.get("cell", "")
            val = op.get("value", "")
            if coord:
                ws[coord] = val
                results.append({"action": action, "cell": f"{sn}!{coord}", "value": val})
        elif action == "replace":
            target = op.get("target", "")
            value = op.get("value", "")
            count = 0
            if target:
                for row in ws.iter_rows():
                    for cell in row:
                        if cell.value and target in str(cell.value):
                            cell.value = str(cell.value).replace(target, value)
                            count += 1
            results.append({"action": action, "target": target, "matches": count})
        elif action == "clear_cell":
            coord = op.get("cell", "")
            if coord:
                ws[coord] = None
                results.append({"action": action, "cell": f"{sn}!{coord}"})
    wb.save(file_path)
    return {"success": True, "file": os.path.basename(file_path), "operations": results}


def create_xlsx(file_path: str, data: List[List], sheet_name: str = "Sheet1") -> Dict[str, Any]:
    """从二维数组创建新的 xlsx 文件。data[0] 为表头行。"""
    import openpyxl
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = sheet_name
    for r_idx, row in enumerate(data, 1):
        for c_idx, val in enumerate(row, 1):
            ws.cell(row=r_idx, column=c_idx, value=val)
    wb.save(file_path)
    return {
        "success": True,
        "file": os.path.basename(file_path),
        "rows": len(data),
        "cols": len(data[0]) if data else 0,
    }


def format_xlsx(file_path: str, formatting: Dict) -> Dict[str, Any]:
    """对 xlsx 执行格式化：字体/对齐/边框/填充/数字格式/列宽/行高。

    formatting: {
        "sheet": "Sheet1",
        "range": "A1:G1",          # 目标单元格范围
        "font_name": "宋体",
        "font_size": 12,
        "bold": true,
        "italic": false,
        "font_color": "FF0000",    # 6位十六进制
        "alignment_h": "center",   # left/center/right
        "alignment_v": "center",   # top/center/bottom
        "border": "all",           # all/outline/none
        "fill_color": "D9D9D9",    # 背景填充色
        "number_format": "#,##0.00", # Excel 数字格式代码
        "column_width": 15,        # 列宽（应用到范围内所有列）
        "row_height": 20,          # 行高（应用到范围内所有行）
        "wrap_text": false
    }
    """
    import openpyxl
    from openpyxl.styles import Font, Alignment, Border, Side, PatternFill
    from openpyxl.utils import get_column_letter

    wb = openpyxl.load_workbook(file_path)
    sn = formatting.get("sheet", wb.sheetnames[0])
    if sn not in wb.sheetnames:
        sn = wb.sheetnames[0]
    ws = wb[sn]

    cell_range = formatting.get("range", "")
    if not cell_range:
        return {"error": "必须指定 range 参数，如 'A1:G1'"}

    font_kwargs = {}
    if formatting.get("font_name"):
        font_kwargs["name"] = formatting["font_name"]
    if formatting.get("font_size"):
        font_kwargs["size"] = int(formatting["font_size"])
    if formatting.get("bold") is not None:
        font_kwargs["bold"] = bool(formatting["bold"])
    if formatting.get("italic") is not None:
        font_kwargs["italic"] = bool(formatting["italic"])
    if formatting.get("font_color"):
        font_kwargs["color"] = formatting["font_color"].lstrip("#")
    new_font = Font(**font_kwargs) if font_kwargs else None

    align_kwargs = {}
    h_map = {"left": "left", "center": "center", "right": "right"}
    v_map = {"top": "top", "center": "center", "bottom": "bottom"}
    if formatting.get("alignment_h") and formatting["alignment_h"] in h_map:
        align_kwargs["horizontal"] = h_map[formatting["alignment_h"]]
    if formatting.get("alignment_v") and formatting["alignment_v"] in v_map:
        align_kwargs["vertical"] = v_map[formatting["alignment_v"]]
    if formatting.get("wrap_text") is not None:
        align_kwargs["wrap_text"] = bool(formatting["wrap_text"])
    new_align = Alignment(**align_kwargs) if align_kwargs else None

    new_border = None
    border_style = formatting.get("border", "")
    if border_style == "all":
        thin = Side(style="thin")
        new_border = Border(left=thin, right=thin, top=thin, bottom=thin)
    elif border_style == "outline":
        thin = Side(style="thin")
        new_border = Border(left=thin, right=thin, top=thin, bottom=thin)

    new_fill = None
    if formatting.get("fill_color"):
        fc = formatting["fill_color"].lstrip("#")
        new_fill = PatternFill(start_color=fc, end_color=fc, fill_type="solid")

    num_fmt = formatting.get("number_format", "")

    count = 0
    for row in ws[cell_range]:
        if not isinstance(row, tuple):
            row = (row,)
        for cell in row:
            if new_font:
                cell.font = new_font
            if new_align:
                cell.alignment = new_align
            if new_border:
                cell.border = new_border
            if new_fill:
                cell.fill = new_fill
            if num_fmt:
                cell.number_format = num_fmt
            count += 1

    if formatting.get("column_width"):
        from openpyxl.utils.cell import range_boundaries
        min_col, min_row, max_col, max_row = range_boundaries(cell_range)
        for c in range(min_col, max_col + 1):
            ws.column_dimensions[get_column_letter(c)].width = float(formatting["column_width"])

    if formatting.get("row_height"):
        from openpyxl.utils.cell import range_boundaries
        min_col, min_row, max_col, max_row = range_boundaries(cell_range)
        for r in range(min_row, max_row + 1):
            ws.row_dimensions[r].height = float(formatting["row_height"])

    wb.save(file_path)
    return {"success": True, "file": os.path.basename(file_path), "cells_formatted": count}


def add_chart_xlsx(file_path: str, chart_config: Dict) -> Dict[str, Any]:
    """在 xlsx 中创建图表（饼图/折线图/柱状图/条形图）。

    chart_config: {
        "sheet": "Sheet1",
        "chart_type": "pie" | "line" | "bar" | "column",
        "title": "图表标题",
        "data_range": "B1:B6",       # 数据范围（数值列，含表头）
        "categories_range": "A1:A6", # 分类标签范围（含表头）
        "position": "E2",            # 图表放置位置
        "width": 15,                 # 图表宽度（cm）
        "height": 10,                # 图表高度（cm）
        "style": 10,                 # 图表样式编号（2-48）
        "multi_series": [            # 多系列（折线图/柱状图可选）
            {"data_range": "B1:B6", "title": "系列1"},
            {"data_range": "C1:C6", "title": "系列2"}
        ]
    }
    """
    import openpyxl
    from openpyxl.chart import PieChart, LineChart, BarChart, Reference
    from openpyxl.utils.cell import range_boundaries

    wb = openpyxl.load_workbook(file_path)
    sn = chart_config.get("sheet", wb.sheetnames[0])
    if sn not in wb.sheetnames:
        sn = wb.sheetnames[0]
    ws = wb[sn]

    ct = chart_config.get("chart_type", "column")
    title = chart_config.get("title", "")
    style = chart_config.get("style", 10)
    width = chart_config.get("width", 15)
    height = chart_config.get("height", 10)

    type_map = {"pie": PieChart, "line": LineChart, "bar": BarChart, "column": BarChart}
    ChartClass = type_map.get(ct, BarChart)
    chart = ChartClass()
    chart.title = title
    chart.style = style
    chart.width = width
    chart.height = height

    if ct == "column":
        chart.type = "col"
    elif ct == "bar":
        chart.type = "bar"

    multi = chart_config.get("multi_series")
    if multi and ct != "pie":
        cat_range = chart_config.get("categories_range", "")
        if cat_range:
            c1, r1, c2, r2 = range_boundaries(cat_range)
            cats = Reference(ws, min_col=c1, min_row=r1 + 1, max_row=r2)
            chart.set_categories(cats)
        for s in multi:
            dr = s.get("data_range", "")
            if not dr:
                continue
            dc1, dr1, dc2, dr2 = range_boundaries(dr)
            data = Reference(ws, min_col=dc1, min_row=dr1, max_col=dc2, max_row=dr2)
            chart.add_data(data, titles_from_data=True)
    else:
        data_range = chart_config.get("data_range", "")
        cat_range = chart_config.get("categories_range", "")
        if not data_range:
            return {"error": "必须指定 data_range"}

        dc1, dr1, dc2, dr2 = range_boundaries(data_range)
        data = Reference(ws, min_col=dc1, min_row=dr1, max_col=dc2, max_row=dr2)

        if cat_range:
            cc1, cr1, cc2, cr2 = range_boundaries(cat_range)
            cats = Reference(ws, min_col=cc1, min_row=cr1 + 1, max_row=cr2)
        else:
            cats = None

        chart.add_data(data, titles_from_data=True)
        if cats:
            chart.set_categories(cats)

        if ct == "pie" and hasattr(chart, 'dataLabels'):
            from openpyxl.chart.label import DataLabelList
            chart.dataLabels = DataLabelList()
            chart.dataLabels.showPercent = True
            chart.dataLabels.showCatName = True

    pos = chart_config.get("position", "E2")
    ws.add_chart(chart, pos)
    wb.save(file_path)
    return {"success": True, "file": os.path.basename(file_path), "chart_type": ct, "title": title, "position": pos}


def validate_xlsx(file_path: str, validations: List[Dict]) -> Dict[str, Any]:
    """为 xlsx 添加数据有效性规则。

    validations: [
        {
            "sheet": "Sheet1",
            "range": "C2:C100",
            "type": "whole",           # whole/decimal/list/textLength/custom
            "operator": "between",     # between/notBetween/equal/greaterThan/lessThan 等
            "formula1": "18",
            "formula2": "60",
            "allow_blank": true,
            "error_title": "输入错误",
            "error_message": "请输入18-60之间的整数",
            "prompt_title": "提示",
            "prompt_message": "请输入年龄"
        },
        {
            "type": "list",
            "range": "D2:D100",
            "formula1": "\"软件工程师,数据分析师,前端开发\"",
            ...
        },
        {
            "type": "custom",
            "range": "E2:E100",
            "formula1": "=COUNTIF($E:$E,E2)=1",
            ...
        }
    ]
    """
    import openpyxl
    from openpyxl.worksheet.datavalidation import DataValidation

    wb = openpyxl.load_workbook(file_path)
    results = []
    for v in validations:
        sn = v.get("sheet", wb.sheetnames[0])
        if sn not in wb.sheetnames:
            sn = wb.sheetnames[0]
        ws = wb[sn]

        vtype = v.get("type", "whole")
        operator = v.get("operator")
        formula1 = v.get("formula1", "")
        formula2 = v.get("formula2")
        cell_range = v.get("range", "")

        dv_kwargs = {"type": vtype}
        if operator:
            dv_kwargs["operator"] = operator
        if formula1:
            dv_kwargs["formula1"] = str(formula1)
        if formula2:
            dv_kwargs["formula2"] = str(formula2)
        dv_kwargs["allow_blank"] = v.get("allow_blank", True)

        dv = DataValidation(**dv_kwargs)

        if v.get("error_title") or v.get("error_message"):
            dv.error = v.get("error_message", "")
            dv.errorTitle = v.get("error_title", "错误")
            dv.showErrorMessage = True

        if v.get("prompt_title") or v.get("prompt_message"):
            dv.prompt = v.get("prompt_message", "")
            dv.promptTitle = v.get("prompt_title", "提示")
            dv.showInputMessage = True

        dv.add(cell_range)
        ws.add_data_validation(dv)
        results.append({"type": vtype, "range": cell_range, "applied": True})

    wb.save(file_path)
    return {"success": True, "file": os.path.basename(file_path), "validations": results}


# ============================================================
# 2. 格式调整与排版
# ============================================================

_CN_SECTION_L1 = re.compile(
    r'^[一二三四五六七八九十百]+[、.．]'
    r'|^第[一二三四五六七八九十百\d]+[章节篇部]'
    r'|^[ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+[、.．\s]'
)
_CN_SECTION_L2 = re.compile(
    r'^\([一二三四五六七八九十]+\)'
    r'|^（[一二三四五六七八九十]+）'
    r'|^\d+[、.．]\s*\S'
)
_CN_SECTION_L3 = re.compile(
    r'^\d+\.\d+[、.．\s]'
    r'|^[①②③④⑤⑥⑦⑧⑨⑩]'
)
_HEADING_KEYWORDS = {"摘要", "前言", "引言", "结语", "结论", "总结", "致谢",
                     "参考文献", "附录", "目录", "概述", "导言", "序言", "后记"}


def _all_runs_bold(para) -> bool:
    """段落中所有非空 run 是否均为粗体。"""
    runs = [r for r in para.runs if r.text.strip()]
    return bool(runs) and all(r.font.bold for r in runs)


def _max_run_font_pt(para) -> float:
    """段落中 run 的最大字号(pt)，无显式字号则返回 0。"""
    mx = 0.0
    for r in para.runs:
        if r.font.size:
            mx = max(mx, r.font.size.pt)
    return mx


def _is_heading(para) -> bool:
    """判断段落是否为标题。

    多信号综合判定，兼容不使用 Word 内置标题样式的中文文档：
    1. Word 内置 Heading / 标题 样式
    2. XML outlineLvl < 9
    3. 全 run 加粗 + 短文本 + 中文/数字编号模式
    4. 居中对齐 + 加粗 + 短文本（文档大标题）
    5. 全 run 加粗 + 极短文本且命中常见标题关键词
    6. 字号 >= 16pt（独立大字号）
    """
    text = para.text.strip()
    if not text:
        return False

    style_name = (para.style.name or "").lower()
    if style_name.startswith("heading") or style_name.startswith("toc"):
        return True
    if "标题" in (para.style.name or ""):
        return True

    try:
        from docx.oxml.ns import qn
        pPr = para._element.find(qn('w:pPr'))
        if pPr is not None:
            olv = pPr.find(qn('w:outlineLvl'))
            if olv is not None:
                val = int(olv.get(qn('w:val'), '9'))
                if val < 9:
                    return True
    except Exception:
        pass

    bold = _all_runs_bold(para)
    tlen = len(text)

    if bold and tlen <= 60:
        if _CN_SECTION_L1.match(text) or _CN_SECTION_L2.match(text) or _CN_SECTION_L3.match(text):
            return True
        from docx.enum.text import WD_ALIGN_PARAGRAPH
        if para.alignment == WD_ALIGN_PARAGRAPH.CENTER:
            return True
        if tlen <= 15 and text in _HEADING_KEYWORDS:
            return True
        if tlen <= 15:
            return True

    max_pt = _max_run_font_pt(para)
    if max_pt >= 16:
        return True

    return False


def _heading_level(para) -> int:
    """返回标题级别：0=正文, 1=一级标题, 2=二级标题, 3=三级标题。"""
    if not _is_heading(para):
        return 0

    style_name = (para.style.name or "").lower()
    for i in range(1, 10):
        if f"heading {i}" in style_name or f"标题 {i}" in style_name or f"heading{i}" in style_name:
            return i

    try:
        from docx.oxml.ns import qn
        pPr = para._element.find(qn('w:pPr'))
        if pPr is not None:
            olv = pPr.find(qn('w:outlineLvl'))
            if olv is not None:
                val = int(olv.get(qn('w:val'), '9'))
                if 0 <= val < 9:
                    return val + 1
    except Exception:
        pass

    text = para.text.strip()
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    if para.alignment == WD_ALIGN_PARAGRAPH.CENTER:
        max_pt = _max_run_font_pt(para)
        if max_pt >= 16:
            return 1

    if _CN_SECTION_L1.match(text):
        return 1 if para.alignment != WD_ALIGN_PARAGRAPH.CENTER else 1
    if _CN_SECTION_L2.match(text):
        return 2
    if _CN_SECTION_L3.match(text):
        return 3

    return 1


def format_docx(file_path: str, formatting: Dict) -> Dict[str, Any]:
    """对 docx 执行格式调整，支持按段落样式精确筛选。
    formatting: {
        "target": "标题文本或all",
        "style_target": "heading|body|heading_1|heading_2|all",
        "font_name": "宋体",
        "font_size": 12,
        "bold": true,
        "italic": false,
        "alignment": "center",
        "line_spacing": 1.5,
        "first_line_indent": 2,
        "color": "FF0000"
    }
    style_target 优先级高于 target:
      - "heading"   : 仅匹配所有标题段落
      - "heading_1" : 仅匹配一级标题
      - "heading_2" : 仅匹配二级标题
      - "body"      : 仅匹配正文段落（非标题）
      - "all"       : 所有段落
    如果 style_target 未设置，则使用 target 做文本匹配（旧行为）。
    """
    import docx
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = docx.Document(file_path)
    target = formatting.get("target", "all")
    style_target = formatting.get("style_target", "")
    count = 0
    align_map = {
        "left": WD_ALIGN_PARAGRAPH.LEFT,
        "center": WD_ALIGN_PARAGRAPH.CENTER,
        "right": WD_ALIGN_PARAGRAPH.RIGHT,
        "justify": WD_ALIGN_PARAGRAPH.JUSTIFY,
    }

    def _apply_fmt(para):
        nonlocal count
        if formatting.get("alignment") and formatting["alignment"] in align_map:
            para.alignment = align_map[formatting["alignment"]]
        pf = para.paragraph_format
        if formatting.get("line_spacing"):
            pf.line_spacing = float(formatting["line_spacing"])
        if formatting.get("first_line_indent") is not None:
            chars = float(formatting["first_line_indent"])
            if chars > 0:
                pf.first_line_indent = Cm(chars * 0.74)
            elif chars == 0:
                pf.first_line_indent = None
        for run in para.runs:
            if formatting.get("font_name"):
                run.font.name = formatting["font_name"]
                try:
                    from docx.oxml.ns import qn
                    from docx.oxml import OxmlElement
                    rpr = run._element.get_or_add_rPr()
                    fonts = rpr.find(qn('w:rFonts'))
                    if fonts is None:
                        fonts = OxmlElement('w:rFonts')
                        rpr.insert(0, fonts)
                    fonts.set(qn('w:eastAsia'), formatting["font_name"])
                except Exception:
                    pass
            if formatting.get("font_size"):
                run.font.size = Pt(int(formatting["font_size"]))
            if formatting.get("bold") is not None:
                run.font.bold = bool(formatting["bold"])
            if formatting.get("italic") is not None:
                run.font.italic = bool(formatting["italic"])
            if formatting.get("color"):
                c = formatting["color"].lstrip("#")
                if len(c) == 6:
                    run.font.color.rgb = RGBColor(int(c[0:2], 16), int(c[2:4], 16), int(c[4:6], 16))
        count += 1

    def _match(para) -> bool:
        if not para.text.strip():
            return False
        if style_target:
            st = style_target.lower().strip()
            if st == "all":
                return True
            if st == "heading":
                return _is_heading(para)
            if st.startswith("heading_"):
                try:
                    lvl = int(st.split("_")[1])
                    return _heading_level(para) == lvl
                except (ValueError, IndexError):
                    return _is_heading(para)
            if st == "body":
                return not _is_heading(para)
            return False
        if target == "all":
            return True
        if target and target in para.text:
            return True
        return False

    for para in doc.paragraphs:
        if _match(para):
            _apply_fmt(para)

    doc.save(file_path)
    return {"success": True, "file": os.path.basename(file_path), "paragraphs_formatted": count}


def analyze_docx_structure(file_path: str) -> Dict[str, Any]:
    """分析 docx 文档结构，返回标题/正文段落摘要，供 LLM 决策格式化方案。"""
    import docx
    doc = docx.Document(file_path)

    headings = []
    body_sample = []
    total = 0
    heading_count = 0
    body_count = 0

    for i, para in enumerate(doc.paragraphs):
        text = para.text.strip()
        if not text:
            continue
        total += 1
        lvl = _heading_level(para)
        max_pt = _max_run_font_pt(para)
        bold = _all_runs_bold(para)
        if lvl > 0:
            heading_count += 1
            headings.append({
                "index": i,
                "level": lvl,
                "text": text[:80],
                "bold": bold,
                "font_size_pt": max_pt or None,
                "alignment": str(para.alignment) if para.alignment is not None else None,
            })
        else:
            body_count += 1
            if len(body_sample) < 3:
                body_sample.append({
                    "index": i,
                    "text": text[:80] + ("..." if len(text) > 80 else ""),
                    "bold": bold,
                    "font_size_pt": max_pt or None,
                })

    return {
        "total_paragraphs": total,
        "heading_count": heading_count,
        "body_count": body_count,
        "headings": headings,
        "body_sample": body_sample,
    }


# ============================================================
# 3. 内容提取（通过对话触发）
# ============================================================

async def extract_from_file(file_id: int, fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """从指定文件提取关键信息，可指定字段列表。"""
    from database import SessionLocal
    from models import FileInfo, DocChunk, ExtractResult
    from services.extractor import hybrid_extract

    db = SessionLocal()
    try:
        fi = db.query(FileInfo).filter(FileInfo.id == file_id).first()
        if not fi:
            return {"error": f"文件 ID {file_id} 不存在"}

        chunks = db.query(DocChunk).filter(DocChunk.file_id == file_id).order_by(DocChunk.chunk_index).all()
        if not chunks:
            return {"error": f"文件 {fi.file_name} 尚未解析"}

        full_text = "\n\n".join(c.chunk_text for c in chunks)
        extracted = await hybrid_extract(full_text[:15000], fields=fields)

        new_count = 0
        for k, v in extracted.items():
            if k.startswith("_"):
                continue
            val_str = str(v)
            existing = db.query(ExtractResult).filter(
                ExtractResult.file_id == file_id, ExtractResult.field_name == k
            ).first()
            if existing:
                existing.field_value = val_str
                existing.confidence = 0.90
                existing.extract_method = "chat_extract"
            else:
                db.add(ExtractResult(
                    file_id=file_id, field_name=k, field_value=val_str,
                    confidence=0.85, extract_method="chat_extract"
                ))
                new_count += 1
        db.commit()

        total = db.query(ExtractResult).filter(ExtractResult.file_id == file_id).count()
        fi.field_count = total
        db.commit()

        display = {k: v for k, v in extracted.items() if not k.startswith("_")}
        return {
            "success": True,
            "file_name": fi.file_name,
            "new_fields": new_count,
            "total_fields": total,
            "extracted": display,
        }
    finally:
        db.close()


# ============================================================
# 4. Agent 工具注册表 - 供 chat.py 的 tool-use 循环使用
# ============================================================

AGENT_TOOLS = [
    {
        "name": "search_documents",
        "description": "在用户上传的所有文档中搜索与查询相关的内容片段。用于回答文档内容问题、查找特定信息。",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询关键词"},
                "top_k": {"type": "integer", "description": "返回结果数量", "default": 8},
            },
            "required": ["query"],
        },
    },
    {
        "name": "extract_info",
        "description": "从指定文档中提取关键信息和实体数据。可以指定要提取的字段列表，也可以自动提取所有关键信息。提取结果会保存到数据库。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "要提取的文件ID"},
                "fields": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "要提取的字段名列表，为空则自动提取所有关键信息",
                },
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "list_files",
        "description": "列出用户已上传的所有文档文件，包含文件名、类型、解析状态、字段数等信息。",
        "input_schema": {
            "type": "object",
            "properties": {},
        },
    },
    {
        "name": "edit_document",
        "description": "对用户已上传的文档执行编辑操作。Word支持replace/delete/append/insert_after；Excel还支持set_cell/clear_cell。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "要编辑的文件ID"},
                "operations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "action": {"type": "string", "enum": ["replace", "delete", "append", "insert_after", "set_cell", "clear_cell"],
                                       "description": "操作类型。set_cell/clear_cell仅用于Excel"},
                            "target": {"type": "string", "description": "要操作的目标文本(replace/delete/insert_after)"},
                            "value": {"type": "string", "description": "新文本内容或单元格值"},
                            "sheet": {"type": "string", "description": "Excel工作表名称(仅Excel)"},
                            "cell": {"type": "string", "description": "Excel单元格坐标如A1(仅set_cell/clear_cell)"},
                        },
                    },
                    "description": "编辑操作列表",
                },
            },
            "required": ["file_id", "operations"],
        },
    },
    {
        "name": "analyze_document",
        "description": "分析 Word 文档结构：识别标题层级、正文段落数量、各段落格式信息。"
                       "在执行 format_document 之前必须先调用此工具，了解文档中哪些段落被识别为标题、哪些是正文。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "要分析的文件ID"},
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "format_document",
        "description": "对文档执行格式调整。支持按段落样式精确定位：标题、正文、指定级别标题。"
                       "重要：当用户要求同时格式化标题和正文但格式不同时，必须分两次调用，一次 style_target='heading'，一次 style_target='body'。"
                       "仅支持 docx 格式。调用前必须先用 analyze_document 确认文档结构。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "要格式化的文件ID"},
                "style_target": {
                    "type": "string",
                    "enum": ["heading", "heading_1", "heading_2", "heading_3", "body", "all"],
                    "description": "按段落样式筛选。heading=所有标题, heading_1=一级标题, heading_2=二级标题, body=仅正文段落, all=全部。"
                                   "优先使用此字段而非target。标题和正文格式不同时必须分别调用。",
                },
                "target": {"type": "string", "description": "按文本内容匹配段落(仅当style_target不适用时)，'all'表示全部段落"},
                "font_name": {"type": "string", "description": "字体名称，如宋体、黑体、Arial"},
                "font_size": {"type": "integer", "description": "字号大小(磅)"},
                "bold": {"type": "boolean", "description": "是否加粗"},
                "italic": {"type": "boolean", "description": "是否斜体"},
                "alignment": {"type": "string", "enum": ["left", "center", "right", "justify"], "description": "对齐方式"},
                "line_spacing": {"type": "number", "description": "行距倍数，如1.5"},
                "first_line_indent": {"type": "number", "description": "首行缩进字符数，如2表示缩进2字符，0表示无缩进"},
                "color": {"type": "string", "description": "字体颜色，如FF0000表示红色"},
            },
            "required": ["file_id"],
        },
    },
    {
        "name": "create_spreadsheet",
        "description": "创建新的 Excel 表格文件，传入二维数组数据。第一行为表头。创建后自动注册到用户的文件列表中，可继续用 format_spreadsheet / validate_spreadsheet 进行格式化。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_name": {"type": "string", "description": "文件名，如 '销售数据.xlsx'"},
                "sheet_name": {"type": "string", "description": "工作表名称，默认 Sheet1"},
                "data": {
                    "type": "array",
                    "items": {"type": "array", "items": {}},
                    "description": "二维数组，data[0] 为表头行，后续为数据行。如 [['姓名','年龄'],['张三',25]]",
                },
            },
            "required": ["file_name", "data"],
        },
    },
    {
        "name": "format_spreadsheet",
        "description": "对 Excel 表格指定范围执行格式化：字体、颜色、加粗、对齐、边框、底纹填充、数字格式、列宽行高等。需要格式化不同区域时分多次调用。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "文件ID"},
                "sheet": {"type": "string", "description": "工作表名称"},
                "range": {"type": "string", "description": "单元格范围，如 'A1:G1'（表头行）或 'A2:G5'（数据区域）"},
                "font_name": {"type": "string", "description": "字体名称，如 宋体、黑体"},
                "font_size": {"type": "integer", "description": "字号大小"},
                "bold": {"type": "boolean", "description": "是否加粗"},
                "italic": {"type": "boolean", "description": "是否斜体"},
                "font_color": {"type": "string", "description": "字体颜色6位十六进制，如 FF0000=红色"},
                "alignment_h": {"type": "string", "enum": ["left", "center", "right"], "description": "水平对齐"},
                "alignment_v": {"type": "string", "enum": ["top", "center", "bottom"], "description": "垂直对齐"},
                "border": {"type": "string", "enum": ["all", "outline", "none"], "description": "边框样式，all=所有框线"},
                "fill_color": {"type": "string", "description": "背景填充色6位十六进制，如 D9D9D9=浅灰色"},
                "number_format": {"type": "string", "description": "Excel数字格式代码。货币: ¥#,##0.00 日期: yyyy/mm/dd 百分比: 0.00%"},
                "column_width": {"type": "number", "description": "列宽"},
                "row_height": {"type": "number", "description": "行高"},
                "wrap_text": {"type": "boolean", "description": "是否自动换行"},
            },
            "required": ["file_id", "range"],
        },
    },
    {
        "name": "add_chart",
        "description": "在 Excel 表格中创建图表：支持饼图(pie)、折线图(line)、柱状图(column)、条形图(bar)。数据需要先通过 create_spreadsheet 创建好。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "文件ID"},
                "sheet": {"type": "string", "description": "工作表名称"},
                "chart_type": {"type": "string", "enum": ["pie", "line", "bar", "column"], "description": "图表类型：pie=饼图 line=折线图 column=柱状图 bar=条形图"},
                "title": {"type": "string", "description": "图表标题"},
                "data_range": {"type": "string", "description": "数据范围（含表头），如 'B1:B6'"},
                "categories_range": {"type": "string", "description": "分类标签范围（含表头），如 'A1:A6'"},
                "position": {"type": "string", "description": "图表放置的单元格位置，如 'E2'"},
                "width": {"type": "number", "description": "图表宽度(cm)，默认15"},
                "height": {"type": "number", "description": "图表高度(cm)，默认10"},
                "multi_series": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "data_range": {"type": "string", "description": "系列数据范围（含表头）"},
                            "title": {"type": "string", "description": "系列名称"}
                        }
                    },
                    "description": "多系列数据（折线图/柱状图可用）。使用此参数时 data_range 可不填"
                },
            },
            "required": ["file_id", "chart_type", "title"],
        },
    },
    {
        "name": "validate_spreadsheet",
        "description": "为 Excel 表格添加数据有效性规则：限制输入范围、创建下拉菜单、自定义公式验证等。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "文件ID"},
                "validations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "sheet": {"type": "string", "description": "工作表名称"},
                            "range": {"type": "string", "description": "应用范围，如 'H2:H100'"},
                            "type": {
                                "type": "string",
                                "enum": ["whole", "decimal", "list", "textLength", "custom"],
                                "description": "验证类型：whole=整数, decimal=小数, list=下拉列表, textLength=文本长度, custom=自定义公式",
                            },
                            "operator": {"type": "string", "enum": ["between", "notBetween", "equal", "notEqual", "greaterThan", "lessThan", "greaterThanOrEqual", "lessThanOrEqual"], "description": "比较运算符（list和custom类型不需要）"},
                            "formula1": {"type": "string", "description": "值1或公式。list类型用逗号分隔的选项如 '\"选项A,选项B,选项C\"'；custom类型为公式如 '=COUNTIF($E:$E,E2)=1'"},
                            "formula2": {"type": "string", "description": "值2（仅between/notBetween需要）"},
                            "error_title": {"type": "string", "description": "错误提示标题"},
                            "error_message": {"type": "string", "description": "错误提示消息"},
                            "prompt_title": {"type": "string", "description": "输入提示标题"},
                            "prompt_message": {"type": "string", "description": "输入提示消息"},
                            "allow_blank": {"type": "boolean", "description": "是否允许空值，默认true"},
                        },
                        "required": ["range", "type"],
                    },
                    "description": "验证规则列表",
                },
            },
            "required": ["file_id", "validations"],
        },
    },
    {
        "name": "fill_template_smart",
        "description": "【智能填表】基于模板和数据源，自动将数据按列名映射并填入模板。"
                       "适用场景：用户提供一个数据源 Excel（含杂乱数据，可能有省份分组行/英文表头/空行）和一个模板 Excel（带合并单元格表头），"
                       "需要按行业/字段对齐填充并保留模板格式。"
                       "自动处理：① 识别中文表头行，跳过标题/英文/空行 ② 跳过省份分组行（如「河北省」）③ 模糊匹配列名 "
                       "④ 清洗脏数据（如 8659! 6il3 1337()） ⑤ 模板若含「总和」「平均值」列自动计算 ⑥ 完整保留模板的合并单元格、字体、列宽。"
                       "结果会生成新的 xlsx 文件并自动注册，前端可下载。",
        "input_schema": {
            "type": "object",
            "properties": {
                "template_file_id": {"type": "integer", "description": "模板文件的 file_id（带表头但没有数据）"},
                "data_file_id": {"type": "integer", "description": "数据源文件的 file_id（含杂乱数据）"},
                "output_name": {"type": "string", "description": "输出文件名（可选），如 '任务表格_已填充.xlsx'"},
                "keep_province_groups": {"type": "boolean", "description": "是否保留省份分组行，默认 false"},
                "sheet_name": {"type": "string", "description": "工作表名称（可选），默认使用第一个 sheet"},
            },
            "required": ["template_file_id", "data_file_id"],
        },
    },
    {
        "name": "get_extract_results",
        "description": "获取指定文件已提取的所有字段和值。",
        "input_schema": {
            "type": "object",
            "properties": {
                "file_id": {"type": "integer", "description": "文件ID"},
            },
            "required": ["file_id"],
        },
    },
]


async def execute_tool(tool_name: str, tool_input: Dict, user_id: int) -> str:
    """执行指定工具，返回 JSON 字符串结果。"""
    from database import SessionLocal
    from models import FileInfo, ExtractResult, DocChunk

    db = SessionLocal()
    try:
        if tool_name == "search_documents":
            from services.vector_store import search as vs_search
            query = tool_input.get("query", "")
            top_k = tool_input.get("top_k", 8)
            chunks = vs_search(query, top_k, user_id=user_id)
            results = []
            for c in chunks[:top_k]:
                results.append({
                    "file": c.get("file_name", ""),
                    "text": c.get("chunk_text", "")[:500],
                    "score": round(c.get("score", 0), 3),
                })
            return json.dumps({"results": results, "count": len(results)}, ensure_ascii=False)

        elif tool_name == "extract_info":
            fid = tool_input.get("file_id")
            fields = tool_input.get("fields")
            result = await extract_from_file(fid, fields=fields)
            return json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "list_files":
            files = db.query(FileInfo).filter(FileInfo.upload_user == user_id).order_by(FileInfo.upload_time.desc()).all()
            items = [
                {"id": f.id, "name": f.file_name, "type": f.file_type, "status": f.parse_status,
                 "chunks": f.chunk_count or 0, "fields": f.field_count or 0}
                for f in files
            ]
            return json.dumps({"files": items, "total": len(items)}, ensure_ascii=False)

        elif tool_name == "edit_document":
            fid = tool_input.get("file_id")
            ops = tool_input.get("operations", [])
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user_id).first()
            if not fi:
                return json.dumps({"error": f"文件 ID {fid} 不存在或无权限"})
            fp = fi.file_path
            if fi.file_type in ("docx", "doc"):
                result = edit_docx_content(fp, ops)
            elif fi.file_type in ("xlsx", "xls"):
                result = edit_xlsx_content(fp, ops)
            else:
                return json.dumps({"error": f"不支持编辑 {fi.file_type} 格式"})
            return json.dumps(result, ensure_ascii=False)

        elif tool_name == "analyze_document":
            fid = tool_input.get("file_id")
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user_id).first()
            if not fi:
                return json.dumps({"error": f"文件 ID {fid} 不存在或无权限"})
            if fi.file_type not in ("docx", "doc"):
                return json.dumps({"error": "文档结构分析仅支持 Word 文档(.docx/.doc)"})
            result = analyze_docx_structure(fi.file_path)
            return json.dumps(result, ensure_ascii=False, default=str)

        elif tool_name == "format_document":
            fid = tool_input.get("file_id")
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user_id).first()
            if not fi:
                return json.dumps({"error": f"文件 ID {fid} 不存在或无权限"})
            if fi.file_type not in ("docx", "doc"):
                return json.dumps({"error": "格式调整仅支持 Word 文档(.docx/.doc)"})
            fmt = {k: v for k, v in tool_input.items() if k != "file_id" and v is not None}
            result = format_docx(fi.file_path, fmt)
            return json.dumps(result, ensure_ascii=False)

        elif tool_name == "create_spreadsheet":
            fname = tool_input.get("file_name", "new_table.xlsx")
            if not fname.endswith(".xlsx"):
                fname += ".xlsx"
            sheet_name = tool_input.get("sheet_name", "Sheet1")
            data = tool_input.get("data", [])
            if not data:
                return json.dumps({"error": "data 不能为空"})

            upload_dir = os.path.join(os.path.dirname(__file__), "..", "uploads", str(user_id))
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, fname)

            result = create_xlsx(file_path, data, sheet_name)

            from datetime import datetime
            fi = FileInfo(
                file_name=fname,
                file_type="xlsx",
                file_path=os.path.abspath(file_path),
                file_size=os.path.getsize(file_path),
                upload_user=user_id,
                upload_time=datetime.now(),
                parse_status="completed",
                chunk_count=0,
                field_count=0,
            )
            db.add(fi)
            db.commit()
            db.refresh(fi)
            result["file_id"] = fi.id
            return json.dumps(result, ensure_ascii=False)

        elif tool_name == "format_spreadsheet":
            fid = tool_input.get("file_id")
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user_id).first()
            if not fi:
                return json.dumps({"error": f"文件 ID {fid} 不存在或无权限"})
            if fi.file_type not in ("xlsx", "xls"):
                return json.dumps({"error": "格式化仅支持 Excel 文档(.xlsx)"})
            fmt = {k: v for k, v in tool_input.items() if k != "file_id" and v is not None}
            result = format_xlsx(fi.file_path, fmt)
            return json.dumps(result, ensure_ascii=False)

        elif tool_name == "add_chart":
            fid = tool_input.get("file_id")
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user_id).first()
            if not fi:
                return json.dumps({"error": f"文件 ID {fid} 不存在或无权限"})
            if fi.file_type not in ("xlsx", "xls"):
                return json.dumps({"error": "图表仅支持 Excel 文档(.xlsx)"})
            cfg = {k: v for k, v in tool_input.items() if k != "file_id" and v is not None}
            result = add_chart_xlsx(fi.file_path, cfg)
            return json.dumps(result, ensure_ascii=False)

        elif tool_name == "validate_spreadsheet":
            fid = tool_input.get("file_id")
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user_id).first()
            if not fi:
                return json.dumps({"error": f"文件 ID {fid} 不存在或无权限"})
            if fi.file_type not in ("xlsx", "xls"):
                return json.dumps({"error": "数据有效性仅支持 Excel 文档(.xlsx)"})
            vals = tool_input.get("validations", [])
            result = validate_xlsx(fi.file_path, vals)
            return json.dumps(result, ensure_ascii=False)

        elif tool_name == "fill_template_smart":
            from services.smart_fill import smart_fill
            tpl_id = tool_input.get("template_file_id")
            data_id = tool_input.get("data_file_id")
            output_name = tool_input.get("output_name") or ""
            keep_provinces = bool(tool_input.get("keep_province_groups", False))
            sheet_name = tool_input.get("sheet_name") or None

            tpl = db.query(FileInfo).filter(FileInfo.id == tpl_id, FileInfo.upload_user == user_id).first()
            data_fi = db.query(FileInfo).filter(FileInfo.id == data_id, FileInfo.upload_user == user_id).first()
            if not tpl:
                return json.dumps({"error": f"模板文件 ID {tpl_id} 不存在或无权限"})
            if not data_fi:
                return json.dumps({"error": f"数据源文件 ID {data_id} 不存在或无权限"})
            if tpl.file_type not in ("xlsx", "xls"):
                return json.dumps({"error": f"模板必须为 Excel 文件，当前: {tpl.file_type}"})
            if data_fi.file_type not in ("xlsx", "xls"):
                return json.dumps({"error": f"数据源必须为 Excel 文件，当前: {data_fi.file_type}"})

            if not output_name:
                base = os.path.splitext(tpl.file_name)[0]
                output_name = f"{base}_已填充.xlsx"
            if not output_name.endswith(".xlsx"):
                output_name += ".xlsx"

            upload_dir = os.path.join(os.path.dirname(__file__), "..", "uploads", str(user_id))
            os.makedirs(upload_dir, exist_ok=True)
            output_path = os.path.abspath(os.path.join(upload_dir, output_name))

            try:
                fill_res = smart_fill(
                    template_path=tpl.file_path,
                    data_path=data_fi.file_path,
                    output_path=output_path,
                    keep_province_groups=keep_provinces,
                    sheet_name=sheet_name,
                )
            except Exception as fe:
                logger.error(f"smart_fill 执行失败: {fe}", exc_info=True)
                return json.dumps({"error": f"填表失败: {str(fe)[:200]}"})

            from datetime import datetime
            fi = FileInfo(
                file_name=output_name,
                file_type="xlsx",
                file_path=output_path,
                file_size=os.path.getsize(output_path),
                upload_user=user_id,
                upload_time=datetime.now(),
                parse_status="completed",
                chunk_count=0,
                field_count=0,
            )
            db.add(fi)
            db.commit()
            db.refresh(fi)

            return json.dumps({
                "ok": True,
                "file_id": fi.id,
                "file_name": output_name,
                "filled_rows": fill_res.get("filled_rows"),
                "cleaned_dirty_values": fill_res.get("cleaned_dirty_values"),
                "special_columns": fill_res.get("special_columns"),
                "template_columns": fill_res.get("template_columns"),
                "column_mapping_count": len(fill_res.get("column_mapping") or {}),
            }, ensure_ascii=False)

        elif tool_name == "get_extract_results":
            fid = tool_input.get("file_id")
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user_id).first()
            if not fi:
                return json.dumps({"error": f"文件 ID {fid} 不存在"})
            rs = db.query(ExtractResult).filter(ExtractResult.file_id == fid).all()
            fields = [{"name": r.field_name, "value": r.field_value, "confidence": r.confidence, "method": r.extract_method} for r in rs]
            return json.dumps({"file": fi.file_name, "fields": fields, "total": len(fields)}, ensure_ascii=False)

        else:
            return json.dumps({"error": f"未知工具: {tool_name}"})
    except Exception as e:
        logger.error(f"工具执行失败 {tool_name}: {e}", exc_info=True)
        return json.dumps({"error": str(e)[:200]})
    finally:
        db.close()
