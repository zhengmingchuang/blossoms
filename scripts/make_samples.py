"""生成 samples/ 演示样例包（合成数据，可重复生成）。

为什么需要这个脚本：
    仓库里 uploads/ 与 exports/ 都被 .gitignore 排除，别人 clone 下来是一个空系统，
    无法复现「上传文档 → 抽取 → 填模板」的完整链路。这里用纯合成数据生成一套
    最小可复现样例，评委按 README 走一遍即可看到效果。

数据说明：
    城市名（临江/云溪/青川）与全部数值均为虚构，仅用于演示抽取与填表能力，
    与任何真实地区无关。请勿当作真实统计口径引用。

用法:
    python scripts/make_samples.py            # 生成到 samples/
    python scripts/make_samples.py --force    # 覆盖已存在文件

依赖: python-docx、openpyxl（已在 backend/requirements.txt 中）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"

# ============ 合成数据（唯一数据源，改这里即可整体换一套样例） ============
COLUMNS = ["城市", "GDP(亿元)", "增长率(%)", "常住人口(万人)", "规上工业增加值(亿元)"]

ROWS = [
    {"城市": "临江市", "GDP(亿元)": 3482.65, "增长率(%)": 5.4, "常住人口(万人)": 268.4, "规上工业增加值(亿元)": 1126.30},
    {"城市": "云溪市", "GDP(亿元)": 2057.18, "增长率(%)": 4.1, "常住人口(万人)": 156.7, "规上工业增加值(亿元)": 623.45},
    {"城市": "青川市", "GDP(亿元)": 1486.92, "增长率(%)": 6.2, "常住人口(万人)": 98.3, "规上工业增加值(亿元)": 402.77},
]

BULLETIN_TITLE = "2025年临江市国民经济和社会发展统计公报（节选）"

# 叙事型正文：数值埋在自然语言里，模拟真实统计公报，用于考验抽取而非读表
BULLETIN_PARAS = [
    "一、综合",
    "2025年，临江市实现地区生产总值 3,482.65 亿元，按可比价格计算，比上年增长 5.4%。"
    "年末全市常住人口 268.4 万人，城镇化率达 71.3%，比上年末提高 0.6 个百分点。",
    "二、工业",
    "全年规模以上工业增加值 1,126.30 亿元，同比增长 6.8%，其中高技术制造业增加值增长 11.2%。",
    "三、分区县情况",
    "云溪市 2025 年地区生产总值 2,057.18 亿元，同比增长 4.1%；年末常住人口 156.7 万人；"
    "规模以上工业增加值 623.45 亿元，增长 3.2%。",
    "青川市 2025 年地区生产总值 1,486.92 亿元，同比增长 6.2%；年末常住人口 98.3 万人；"
    "规模以上工业增加值 402.77 亿元，增长 9.1%，增速居全市首位。",
    "四、说明",
    "本文件为 DocFusion 演示用合成样例，城市名称与全部数值均为虚构，与任何真实地区无关。",
]


def _set_cn_font(doc_or_style, font_name: str = "宋体", size=None):
    """给 docx 默认样式设置中文字体（python-docx 不会自动处理 eastAsia）。"""
    from docx.oxml.ns import qn
    from docx.shared import Pt

    style = doc_or_style
    style.font.name = font_name
    if size:
        style.font.size = Pt(size)
    rpr = style.element.get_or_add_rPr()
    rfonts = rpr.find(qn("w:rFonts"))
    if rfonts is None:
        rfonts = rpr.makeelement(qn("w:rFonts"), {})
        rpr.append(rfonts)
    rfonts.set(qn("w:eastAsia"), font_name)


def build_bulletin(path: Path):
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = docx.Document()
    _set_cn_font(doc.styles["Normal"], "宋体", 11)

    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = h.add_run(BULLETIN_TITLE)
    run.bold = True
    run.font.size = Pt(16)

    for p in BULLETIN_PARAS:
        para = doc.add_paragraph(p)
        para.paragraph_format.first_line_indent = Pt(22)
        para.paragraph_format.space_after = Pt(6)

    doc.save(path)


def build_source_xlsx(path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill

    wb = Workbook()
    ws = wb.active
    ws.title = "主要经济指标"
    ws.append(["2025年三市主要经济指标（合成示例）"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(COLUMNS)
    for c in ws[2]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="DDEBF7")
        c.alignment = Alignment(horizontal="center")
    for r in ROWS:
        ws.append([r[c] for c in COLUMNS])
    for col, width in zip("ABCDE", (12, 14, 12, 16, 22)):
        ws.column_dimensions[col].width = width
    wb.save(path)


def build_template_docx(path: Path):
    import docx
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.shared import Pt

    doc = docx.Document()
    _set_cn_font(doc.styles["Normal"], "宋体", 11)

    t = doc.add_paragraph()
    t.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = t.add_run("区域经济运行情况申报表")
    run.bold = True
    run.font.size = Pt(16)

    note = doc.add_paragraph("填表说明：依据源文档填写下列指标，单位见列名，数值保留两位小数。")
    note.runs[0].font.size = Pt(9)

    table = doc.add_table(rows=1 + len(ROWS), cols=len(COLUMNS))
    table.style = "Table Grid"
    for i, name in enumerate(COLUMNS):
        cell = table.rows[0].cells[i]
        cell.text = name
        cell.paragraphs[0].runs[0].bold = True
        cell.paragraphs[0].alignment = WD_ALIGN_PARAGRAPH.CENTER

    doc.save(path)


def build_template_xlsx(path: Path):
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

    wb = Workbook()
    ws = wb.active
    ws.title = "申报表"
    ws.append(["区域经济运行情况汇总表（待填）"])
    ws["A1"].font = Font(bold=True, size=13)
    ws.append(COLUMNS)

    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    for c in ws[2]:
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="E2EFDA")
        c.alignment = Alignment(horizontal="center", vertical="center")
        c.border = border
    for row in range(3, 3 + len(ROWS)):
        for col in range(1, len(COLUMNS) + 1):
            ws.cell(row=row, column=col).border = border
    for col, width in zip("ABCDE", (12, 14, 12, 16, 22)):
        ws.column_dimensions[col].width = width
    wb.save(path)


EXPECTED_MD = """# 期望结果（用于人工核对 / 回归比对）

> 数据与 `samples/source/` 完全一致，由 `scripts/make_samples.py` 生成。
> 允许的差异：千分位（3,482.65 与 3482.65 等价）、单位后缀（亿元）、百分号有无、全角半角。
> 另：系统会对行政区名做归一化，输出「临江 / 云溪 / 青川」（去掉「市」字）属正常，
> 与「临江市」等价。

## 一、逐格期望值

| 城市 | GDP(亿元) | 增长率(%) | 常住人口(万人) | 规上工业增加值(亿元) |
|---|---|---|---|---|
| 临江市 | 3482.65 | 5.4 | 268.4 | 1126.30 |
| 云溪市 | 2057.18 | 4.1 | 156.7 | 623.45 |
| 青川市 | 1486.92 | 6.2 | 98.3 | 402.77 |

共 3 行 × 5 列 = 15 个数据格。

## 二、容易抽错的点

1. **临江市有两个「增长」**：GDP 增长 5.4%，规上工业增加值增长 6.8%。按列名分别对应，不要串。
2. **城镇化率 71.3%、高技术制造业 11.2%、0.6 个百分点** 是干扰项，不属于表中任何一列，正确输出里不应出现。
3. **青川市 9.1%** 是「规上工业增加值增速」，不是 GDP 增长率（GDP 增长率是 6.2%）。
4. 单位已在列名里，单元格内不要再带「亿元」「%」。

## 三、两种输入路径

| 源文件 | 路径 | 考察点 |
|---|---|---|
| `source/2025年临江市国民经济和社会发展统计公报（节选）.docx` | 叙事型文本 → LLM 抽取 | 从自然语言段落中定位数值，抗干扰 |
| `source/2025年三市主要经济指标.xlsx` | 结构化表格 → 规则映射 | 表头语义匹配、数值直取 |
"""

SAMPLES_README = """# samples —— 演示样例包

一套**合成数据**（城市名与数值均为虚构），用于在不接触任何真实业务数据的前提下，
完整跑通「多源文档解析 → 字段抽取 → 模板填报」链路。

```
samples/
├── source/                                     # 数据源
│   ├── 2025年临江市国民经济和社会发展统计公报（节选）.docx   # 叙事型，考验 LLM 抽取
│   └── 2025年三市主要经济指标.xlsx                          # 结构化，走规则映射
├── template/                                   # 待填模板
│   ├── 区域经济运行情况申报表.docx                          # docx 表格（table_fill）
│   └── 区域经济指标汇总表.xlsx                              # xlsx 表格（table_fill）
└── expected/期望结果.md                        # 逐格期望值 + 易错点
```

## 演示步骤（约 3 分钟）

1. 启动系统，用 `admin / admin123` 登录。
2. **文档管理** → 上传 `source/` 下两个文件，等待解析完成。
3. **模板填报** → 上传 `template/区域经济指标汇总表.xlsx`，选择上述两份文档作为数据源，执行填报。
4. 下载结果，对照 `expected/期望结果.md` 核对 15 个数据格。
5. 换 `template/区域经济运行情况申报表.docx` 再跑一次，验证 docx 多表路径。
6. **智能对话** → 输入「把申报表里临江市的 GDP 改成 3500 亿元并导出」，验证 Agent 工具链与流式输出。

## 重新生成

```bash
python scripts/make_samples.py --force
```

改了 `scripts/make_samples.py` 顶部的 `ROWS` 就能整体换一套数据（记得同步更新期望结果，
脚本会一并重写 `expected/期望结果.md`）。
"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成 samples/ 演示样例包")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的文件")
    args = ap.parse_args()

    targets = {
        SAMPLES / "source" / "2025年临江市国民经济和社会发展统计公报（节选）.docx": build_bulletin,
        SAMPLES / "source" / "2025年三市主要经济指标.xlsx": build_source_xlsx,
        SAMPLES / "template" / "区域经济运行情况申报表.docx": build_template_docx,
        SAMPLES / "template" / "区域经济指标汇总表.xlsx": build_template_xlsx,
    }

    for p in targets:
        p.parent.mkdir(parents=True, exist_ok=True)

    skipped = []
    for path, builder in targets.items():
        if path.exists() and not args.force:
            skipped.append(path.name)
            continue
        builder(path)
        print(f"  ✓ {path.relative_to(ROOT)}")

    for name, content in (("README.md", SAMPLES_README), ("expected/期望结果.md", EXPECTED_MD)):
        p = SAMPLES / name
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists() and not args.force:
            skipped.append(p.name)
            continue
        p.write_text(content, encoding="utf-8", newline="\n")
        print(f"  ✓ {p.relative_to(ROOT)}")

    if skipped:
        print(f"  跳过已存在（加 --force 覆盖）: {', '.join(skipped)}")
    print(f"\n样例包已生成: {SAMPLES}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
