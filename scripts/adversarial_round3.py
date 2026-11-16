# -*- coding: utf-8 -*-
"""本轮改动的离线专项验证（不调 LLM，全部本地可复现）。

覆盖：
  1. 单位口径：表头有单位 / 表头无单位且全带单位 / 混裸数字 / 用户指定目标单位 / 跨量词
  2. 日期列：模板无日期列时自动追加（用真实 COVID-19 模板）
  3. 多表 docx：某张表没有数值列时保持模板原样、不写空（用真实构造的 docx）
  4. 数值条件：结构化数据按阈值过滤，边界值与单位换算正确
  5. 填后审查：能定位「源文里找不到」的单元格

    backend/.venv/Scripts/python.exe scripts/adversarial_round3.py
"""
import os
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = Path(SCRIPTS_DIR).parent
BACKEND = ROOT / "backend"
sys.path.insert(0, str(BACKEND))

import docx  # noqa: E402
import openpyxl  # noqa: E402

from services.unit_normalize import normalize_rows_by_source_unit  # noqa: E402
from services.template_filler import (  # noqa: E402
    fill_table_template,
    identify_fields,
    _pick_extra_date_column,
)
from services.data_filter import apply_numeric_filters  # noqa: E402
from services.fill_audit import audit_llm_rows, audit_summary_message  # noqa: E402

TMP = ROOT / ".workbuddy-ai" / "tmp" / "adv_round3"
TMP.mkdir(parents=True, exist_ok=True)

PASS = FAIL = 0


def check(name: str, got, want, note: str = "") -> None:
    global PASS, FAIL
    ok = got == want
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}: {got!r}" + ("" if ok else f"  期望 {want!r}"))
    if note and ok:
        print(f"         {note}")
    if ok:
        PASS += 1
    else:
        FAIL += 1


def case_units() -> None:
    print("\n=== 1. 单位口径 ===")

    out, notes = normalize_rows_by_source_unit(
        [{"人均GDP": "7.3万元"}, {"人均GDP": "5.2万元"}], ["人均GDP"]
    )
    check("表头无单位+全带单位 → 统一到万元", [r["人均GDP"] for r in out], [7.3, 5.2])

    out, _ = normalize_rows_by_source_unit(
        [{"人均GDP": "7.3万元"}, {"人均GDP": "8.1万元"}, {"人均GDP": 78661}], ["人均GDP"]
    )
    check("混裸数字 → 统一到基本单位（元）", [r["人均GDP"] for r in out], [73000, 81000, 78661],
          "否则会出现 7.3 与 78661 同列、看似差一万倍")

    out, _ = normalize_rows_by_source_unit(
        [{"人口": "1.26亿"}, {"人口": "0.58亿"}, {"人口": 4813608}], ["人口"]
    )
    check("亿+裸数字 → 基本单位（人）", [r["人口"] for r in out], [126000000, 58000000, 4813608])

    out, _ = normalize_rows_by_source_unit(
        [{"GDP": "7300万元"}, {"GDP": "52000万元"}], ["GDP"], {"GDP": "亿元"}
    )
    check("用户指定亿元 → 按用户换算", [r["GDP"] for r in out], [0.73, 5.2])

    out, notes = normalize_rows_by_source_unit(
        [{"人均GDP": "7.3万元"}, {"人均GDP": "8.1万元"}], ["人均GDP"], {"人均GDP": "美元"}
    )
    check("元→美元跨量词 → 不换算", [r["人均GDP"] for r in out], ["7.3万元", "8.1万元"])
    check("跨量词有告警", any("量词不同" in n for n in notes), True)

    out, _ = normalize_rows_by_source_unit(
        [{"AQI": "4985(PF)"}, {"AQI": "32*"}, {"AQI": "54"}], ["AQI"]
    )
    check("质量标记原样保留", [r["AQI"] for r in out], ["4985(PF)", "32*", "54"])

    # 表头已声明单位时，前置不动（交给 normalize_cell 按表头换算，避免二次换算）
    out, _ = normalize_rows_by_source_unit([{"GDP（亿元）": "5.2万亿元"}], ["GDP（亿元）"])
    check("表头有单位 → 前置不改", out[0]["GDP（亿元）"], "5.2万亿元")

    # 对抗：不是单位的要求不能被当成单位
    from services.unit_normalize import parse_target_units
    check("「以GDP为核心」不误判为单位", parse_target_units("以GDP为核心筛选城市"), [])
    check("「高于五万亿」不算目标单位要求", parse_target_units("把GDP高于五万亿的城市挑出来"), [])
    check("「以亿元为单位」可解析", parse_target_units("以亿元为单位"), [("亿元", "以亿元为单位")])


def case_date_column() -> None:
    print("\n=== 2. 日期列自动追加（真实 COVID-19 模板）===")
    tpl = ROOT / "docs" / "demo测试集" / "包含模板文件" / "COVID-19数据集" / "COVID-19 模板.xlsx"
    if not tpl.exists():
        check("模板存在", False, True)
        return
    info = identify_fields(str(tpl))
    cols = [c["name"] for c in info.get("columns", [])]
    rows = [
        {"国家/地区": "Ireland", "大洲": "Europe", "人均GDP": "78661", "人口": "4813608",
         "每日检测数": "4551", "病例数": "25477", "日期": "2020/7/1"},
        {"国家/地区": "Ireland", "大洲": "Europe", "人均GDP": "78661", "人口": "4813608",
         "每日检测数": "3792", "病例数": "25489", "日期": "2020/7/2"},
        {"国家/地区": "Spain", "大洲": "Europe", "人均GDP": "27057", "人口": "46754778",
         "每日检测数": "4112", "病例数": "25490", "日期": "2020/7/1"},
    ]
    check("识别到日期列", _pick_extra_date_column(cols, rows), "日期")
    out = TMP / "covid_date.xlsx"
    fill_table_template(str(tpl), str(out), rows, info, stats_out={})
    wb = openpyxl.load_workbook(str(out), read_only=True)
    ws = wb.active
    header = [c.value for c in next(ws.iter_rows(max_row=1))]
    body = [r for r in ws.iter_rows(min_row=2, max_row=4, values_only=True)]
    wb.close()
    check("表头追加「日期」", header[-1], "日期")
    check("每行日期可区分", [r[-1] for r in body], ["2020/7/1", "2020/7/2", "2020/7/1"])

    # 模板本来就有日期列时不重复加
    check("模板已有日期列则不追加", _pick_extra_date_column(["城市", "日期"], rows), None)


def case_multitable() -> None:
    print("\n=== 3. 多表 docx：无数据列的表保持原样 ===")
    tpl = TMP / "多表模板.docx"
    d = docx.Document()
    d.add_paragraph("表一：城市经济指标")
    t1 = d.add_table(rows=3, cols=3)
    for i, h in enumerate(["城市", "GDP总量（亿元）", "常住人口（万人）"]):
        t1.rows[0].cells[i].text = h
    d.add_paragraph("表二：备注（纯文本表，没有数值列）")
    t2 = d.add_table(rows=2, cols=2)
    for i, h in enumerate(["城市", "备注"]):
        t2.rows[0].cells[i].text = h
    d.save(str(tpl))

    info = identify_fields(str(tpl))
    check("识别到 2 张表", len(info.get("tables", [])), 2)

    rows = [
        {"城市": "上海", "GDP总量（亿元）": "56708.71", "常住人口（万人）": "2487.45"},
        {"城市": "北京", "GDP总量（亿元）": "52073.4", "常住人口（万人）": "2185.3"},
    ]
    out = TMP / "多表结果.docx"
    stats = {}
    fill_table_template(str(tpl), str(out), rows, info, stats_out=stats)
    d2 = docx.Document(str(out))
    t2_cells = [[c.text for c in r.cells] for r in d2.tables[1].rows]
    check("表一已填入", [c.text for c in d2.tables[0].rows[1].cells], ["上海", "56708.71", "2487.45"])
    check("表二未被写入空值覆盖", t2_cells, [["城市", "备注"], ["", ""]])
    untouched = stats.get("untouched_tables") or []
    check("表二被标记为未写入并给出原因", bool(untouched) and "没有取值" in untouched[0]["reason"], True)


def case_numeric_filter() -> None:
    print("\n=== 4. 数值条件（单位换算后比较）===")
    rows = [
        {"城市": "上海", "GDP（亿元）": "5.2万亿元"},
        {"城市": "北京", "GDP（亿元）": "52000"},
        {"城市": "深圳", "GDP（亿元）": "38731.8"},
        {"城市": "杭州", "GDP（亿元）": "2.1万亿元"},
    ]
    filters = [{"keyword": "GDP", "op": ">", "value": 5, "unit": "万亿",
                "base_value": 5e12, "has_unit": True}]
    kept, notes = apply_numeric_filters(rows, ["城市", "GDP（亿元）"], filters)
    check(">5 万亿 只留上海/北京", [r["城市"] for r in kept], ["上海", "北京"])
    check("带单位的高值行没有被误删", "上海" in [r["城市"] for r in kept], True)

    # 整列没有可解析数值时不清空整表
    rows2 = [{"城市": "上海", "GDP（亿元）": "无数据"}, {"城市": "北京", "GDP（亿元）": ""}]
    kept2, notes2 = apply_numeric_filters(rows2, ["城市", "GDP（亿元）"], filters)
    check("该列无数值时不清表", len(kept2), 2)
    check("跳过条件有说明", any("跳过" in n for n in notes2), True)


def case_audit() -> None:
    print("\n=== 5. 填后审查 ===")
    src = "上海以56,708.71亿元的GDP总量稳居第一，常住人口2,487.45万。北京GDP达52,073.4亿元。"
    rows = [
        {"城市": "上海", "GDP": "56708.71", "人口": "2487.45"},
        {"城市": "北京", "GDP": "52073.4", "人口": "2185.3"},   # 人口源文没有
        {"城市": "深圳", "GDP": "38731.8", "人口": "1768.2"},   # 整段不在源文
    ]
    a = audit_llm_rows(rows, ["城市", "GDP", "人口"], src)
    check("核对格数", a["checked"], 9)
    check("未定位格数", a["misses"], 4)
    check("命中率", a["pass_rate"], 0.5556)
    check("能给出示例", len(a["samples"]) > 0, True)
    check("全部命中时不告警",
          audit_summary_message(audit_llm_rows([{"城市": "上海", "GDP": "56708.71"}], ["城市", "GDP"], src)),
          None)


def main() -> int:
    case_units()
    case_date_column()
    case_multitable()
    case_numeric_filter()
    case_audit()
    print("\n" + "=" * 56)
    print(f"通过 {PASS} / 失败 {FAIL}")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
