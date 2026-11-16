"""从结构化描述生成一份 Word 报告（标题 / 章节 / 正文 / 表格 / 图表）。

为什么单独有这一层：Agent 原有工具只能「改已有 docx」和「建 xlsx」，
用户说「整理成一份正式报告文档」时无处可落，只能把表格贴在聊天回复里。
这里把「文档结构」变成一次可校验的构建：表格直接写成 Word 表格，
图表用 matplotlib 渲染成 PNG 再嵌入，最终产出一个可下载的 .docx。
"""

import os
import tempfile
from typing import Any, Dict, List, Optional

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

# Windows 自带中文字体，按优先级取第一个存在的
_CJK_FONTS = [
    "C:/Windows/Fonts/msyh.ttc",
    "C:/Windows/Fonts/simhei.ttf",
    "C:/Windows/Fonts/simsun.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
]

_CHART_COLORS = ["#5B8FF9", "#5AD8A6", "#F6BD16", "#E8684A", "#6DC8EC", "#9270CA"]


def _cjk_font_path() -> Optional[str]:
    for p in _CJK_FONTS:
        if os.path.exists(p):
            return p
    return None


def _set_cell_shading(cell, hex_color: str) -> None:
    tc_pr = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:color"), "auto")
    shd.set(qn("w:fill"), hex_color)
    tc_pr.append(shd)


def _style_document(doc: Document) -> None:
    """统一正文字体与行距，避免默认 Calibri 导致中文回退成宋体不齐。"""
    style = doc.styles["Normal"]
    style.font.name = "宋体"
    style.font.size = Pt(10.5)
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "宋体")
    pf = style.paragraph_format
    pf.line_spacing = 1.5
    pf.space_after = Pt(6)


def _add_table(doc: Document, spec: Dict[str, Any]) -> int:
    headers: List[str] = [str(h) for h in (spec.get("headers") or [])]
    rows: List[List[Any]] = spec.get("rows") or []
    if not headers and rows:
        headers = [str(i + 1) for i in range(len(rows[0]))]
    if not headers:
        return 0

    table = doc.add_table(rows=1, cols=len(headers))
    table.style = "Table Grid"
    table.alignment = WD_TABLE_ALIGNMENT.CENTER

    hdr = table.rows[0].cells
    for i, h in enumerate(headers):
        hdr[i].text = ""
        run = hdr[i].paragraphs[0].add_run(h)
        run.bold = True
        run.font.size = Pt(10)
        hdr[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER
        _set_cell_shading(hdr[i], "D9E2F3")

    for r in rows:
        cells = table.add_row().cells
        for i in range(len(headers)):
            v = r[i] if i < len(r) else ""
            cells[i].text = "" if v is None else str(v)
            cells[i].paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

    caption = spec.get("caption")
    if caption:
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(str(caption))
        run.font.size = Pt(9)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
    return len(rows)


def _render_chart(chart: Dict[str, Any], out_png: str) -> bool:
    """把 {type, title, categories, series:[{name, values}]} 渲染成 PNG。"""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except ImportError:
        return False

    font_path = _cjk_font_path()
    if font_path:
        from matplotlib import font_manager

        prop = font_manager.FontProperties(fname=font_path)
    else:
        prop = None

    kind = (chart.get("type") or "column").lower()
    title = chart.get("title") or ""
    cats = [str(c) for c in (chart.get("categories") or [])]
    series = chart.get("series") or []

    fig, ax = plt.subplots(figsize=(6.4, 3.6), dpi=150)
    try:
        if kind == "pie":
            values = series[0].get("values") if series else []
            values = [float(v) for v in values]
            labels = cats or [str(i + 1) for i in range(len(values))]
            wedges, _texts, autotexts = ax.pie(
                values, labels=labels, autopct="%1.1f%%",
                colors=_CHART_COLORS[: len(values)], startangle=90,
                textprops={"fontproperties": prop} if prop else None,
            )
            if prop:
                for t in autotexts:
                    t.set_fontproperties(prop)
        else:
            n = max(1, len(series))
            width = 0.8 / n if kind in ("column", "bar") else 0
            for i, s in enumerate(series):
                vals = [float(v) for v in (s.get("values") or [])]
                xs = list(range(len(cats) or len(vals)))
                if kind in ("column", "bar"):
                    offsets = [x + (i - (n - 1) / 2) * width for x in xs]
                    ax.bar(offsets, vals, width=width,
                           label=s.get("name", f"系列{i + 1}"), color=_CHART_COLORS[i % len(_CHART_COLORS)])
                    if cats:
                        ax.set_xticks(xs)
                        ax.set_xticklabels(cats, fontproperties=prop)
                    if kind == "bar":
                        ax.clear()
                        ax.barh(offsets, vals, height=width,
                                label=s.get("name", f"系列{i + 1}"), color=_CHART_COLORS[i % len(_CHART_COLORS)])
                        if cats:
                            ax.set_yticks(xs)
                            ax.set_yticklabels(cats, fontproperties=prop)
                else:
                    ax.plot(xs, vals, marker="o", label=s.get("name", f"系列{i + 1}"),
                            color=_CHART_COLORS[i % len(_CHART_COLORS)])
                    if cats:
                        ax.set_xticks(xs)
                        ax.set_xticklabels(cats, fontproperties=prop)
            if len(series) > 1 or kind in ("line", "column", "bar"):
                ax.legend(prop=prop)
        if title:
            ax.set_title(title, fontproperties=prop)
        fig.tight_layout()
        fig.savefig(out_png, bbox_inches="tight")
        return True
    finally:
        plt.close(fig)


def create_docx_report(path: str, spec: Dict[str, Any]) -> Dict[str, Any]:
    """按 spec 生成 .docx。

    spec = {
      "title": "报告标题",
      "subtitle": "副标题（可选）",
      "sections": [
        {"heading": "一、xx", "level": 1,
         "paragraphs": ["正文段落", ...],
         "table": {"headers": [...], "rows": [[...]], "caption": "表1 ..."},
         "charts": [{"type": "pie|line|column|bar", "title": "...",
                     "categories": [...], "series": [{"name": "...", "values": [...]}]}],
         "bullets": ["要点1", ...]}
      ]
    }
    """
    doc = Document()
    _style_document(doc)
    for section in doc.sections:
        section.left_margin = Cm(2.5)
        section.right_margin = Cm(2.5)

    title = spec.get("title") or "文档"
    h = doc.add_heading(title, level=0)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    for run in h.runs:
        run.font.name = "微软雅黑"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")

    if spec.get("subtitle"):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        run = p.add_run(str(spec["subtitle"]))
        run.font.size = Pt(10)
        run.font.color.rgb = RGBColor(0x66, 0x66, 0x66)

    stats = {"tables": 0, "rows": 0, "charts": 0, "paragraphs": 0, "charts_failed": 0}
    tmpdir = tempfile.mkdtemp(prefix="docxchart_")

    for section in spec.get("sections") or []:
        heading = section.get("heading")
        if heading:
            lvl = int(section.get("level") or 1)
            doc.add_heading(str(heading), level=min(max(lvl, 1), 4))

        for para in section.get("paragraphs") or []:
            text = str(para).strip()
            if not text:
                continue
            p = doc.add_paragraph(text)
            p.paragraph_format.first_line_indent = Pt(21)
            stats["paragraphs"] += 1

        for b in section.get("bullets") or []:
            p = doc.add_paragraph(str(b), style="List Bullet")
            stats["paragraphs"] += 1

        table_spec = section.get("table")
        if table_spec:
            n = _add_table(doc, table_spec)
            stats["tables"] += 1
            stats["rows"] += n

        for ci, chart in enumerate(section.get("charts") or []):
            png = os.path.join(tmpdir, f"chart_{stats['charts'] + stats['charts_failed']}_{ci}.png")
            if _render_chart(chart, png) and os.path.exists(png):
                doc.add_picture(png, width=Cm(15))
                doc.paragraphs[-1].alignment = WD_ALIGN_PARAGRAPH.CENTER
                if chart.get("caption"):
                    cp = doc.add_paragraph()
                    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    r = cp.add_run(str(chart["caption"]))
                    r.font.size = Pt(9)
                    r.font.color.rgb = RGBColor(0x66, 0x66, 0x66)
                stats["charts"] += 1
            else:
                stats["charts_failed"] += 1

    doc.save(path)
    return {
        "file_path": path,
        "title": title,
        "size_bytes": os.path.getsize(path),
        **stats,
    }
