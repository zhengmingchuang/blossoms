"""多表 docx「无数值列被清空」对抗测试。

判定只看一件事：**模板原有内容是否还在**。
每张表在构造时都埋一段预填文本，填完之后逐个检查是否原样存在；
同时核对「有数据的表确实写进去了」，避免用「什么都不写」冒充「没清表」。

    python scripts/adversarial_multitable.py
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "backend"))

import docx  # noqa: E402
from services.template_filler import identify_fields, fill_table_template  # noqa: E402

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append({"case": name, "ok": bool(ok), "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def build(blocks):
    """blocks: [(标题段落, [表头...], [[预填行]...], 空行数)]"""
    d = docx.Document()
    for title, header, prefill, empty in blocks:
        if title:
            d.add_paragraph(title)
        rows = 1 + len(prefill) + empty
        t = d.add_table(rows=rows, cols=len(header))
        for i, h in enumerate(header):
            t.rows[0].cells[i].text = h
        for ri, prow in enumerate(prefill, start=1):
            for ci, v in enumerate(prow):
                t.rows[ri].cells[ci].text = v
    return d


def run_case(name, blocks, rows, expect_written_tables, expect_kept_texts):
    tmp = tempfile.mkdtemp(prefix="mt_")
    tpl = os.path.join(tmp, "tpl.docx")
    out = os.path.join(tmp, "out.docx")
    build(blocks).save(tpl)

    info = identify_fields(tpl)
    stats = {}
    fill_table_template(tpl, out, rows, info, stats_out=stats)

    d2 = docx.Document(out)
    all_text = []
    for tb in d2.tables:
        for r in tb.rows:
            all_text.append(" ".join(c.text for c in r.cells))
    joined = "\n".join(all_text)

    print(f"\n=== {name} ===")
    print(f"  识别表数={len(info.get('tables', []))} untouched={len(stats.get('untouched_tables') or [])} "
          f"empty_cols={len(stats.get('empty_columns') or [])}")

    for txt in expect_kept_texts:
        record(f"{name} 保留「{txt}」", txt in joined, "")

    written = len(info.get("tables", [])) - len(stats.get("untouched_tables") or [])
    record(f"{name} 写入表数={expect_written_tables}", written == expect_written_tables, f"实际 {written}")

    # 有数据的表必须真的写进去（防止「全都不写」也算通过）
    if expect_written_tables:
        record(f"{name} 确有数据写入", any("56708" in t or "上海" in t for t in all_text), "")


def main():
    # 1. 表二纯文本列 + 预填内容
    run_case(
        "1 纯文本列表",
        [("表一：经济指标", ["城市", "GDP总量（亿元）"], [], 2),
         ("表二：备注", ["城市", "备注"], [["模板原有", "这段内容必须保留"]], 1)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71"},
         {"城市": "北京", "GDP总量（亿元）": "52073.4"}],
        expect_written_tables=1,
        expect_kept_texts=["这段内容必须保留"],
    )

    # 2. 表二列名与数据完全不沾边
    run_case(
        "2 列名完全不沾边",
        [("表一", ["城市", "GDP总量（亿元）"], [], 2),
         ("表二", ["城市", "负责人", "联系电话"], [["旧负责人", "13800000000", "待定"]], 2)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71"},
         {"城市": "北京", "GDP总量（亿元）": "52073.4"}],
        expect_written_tables=1,
        expect_kept_texts=["旧负责人"],
    )

    # 3. 两张表列名相同，但只有一份数据
    run_case(
        "3 同名列双表",
        [("表一", ["城市", "GDP总量（亿元）"], [], 2),
         ("表二", ["城市", "GDP总量（亿元）"], [["占位", "999"]], 2)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71"}],
        expect_written_tables=2,   # 两张表列名一致，都应写入
        expect_kept_texts=[],
    )

    # 4. 表二部分列有值
    run_case(
        "4 部分列有值",
        [("表一", ["城市", "GDP总量（亿元）"], [], 2),
         ("表二", ["城市", "GDP总量（亿元）", "备注"], [["保留我", "1", "别动"]], 2)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71", "备注": ""}],
        expect_written_tables=2,
        expect_kept_texts=["别动"],
    )

    # 5. 合并表头（同名重复）
    run_case(
        "5 合并表头去重",
        [("表一", ["城市", "城市", "GDP总量（亿元）"], [], 2),
         ("表二", ["城市", "备注"], [["合并表头下的内容", "不许丢"]], 1)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71"}],
        expect_written_tables=1,
        expect_kept_texts=["不许丢"],
    )

    # 6. 只有一列的表：城市列有值就正常写入，但不能覆盖用户已填的城市名
    run_case(
        "6 单列表",
        [("表一", ["城市", "GDP总量（亿元）"], [], 2),
         ("表二", ["城市"], [["原有城市名"]], 1)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71"}],
        expect_written_tables=2,
        expect_kept_texts=["原有城市名"],
    )

    # 7. 三张表，只有一张有数据
    run_case(
        "7 三表其一有数",
        [("表一", ["城市", "GDP总量（亿元）"], [], 2),
         ("表二", ["城市", "备注"], [["A", "甲"]], 1),
         ("表三", ["城市", "负责人"], [["B", "乙"]], 1)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71"}],
        expect_written_tables=1,
        expect_kept_texts=["甲", "乙"],
    )

    # 8. 数据行数超过模板空行
    run_case(
        "8 行数超模板",
        [("表一", ["城市", "GDP总量（亿元）"], [], 1),
         ("表二", ["城市", "备注"], [["保留行", "保留值"]], 1)],
        [{"城市": "上海", "GDP总量（亿元）": "56708.71"},
         {"城市": "北京", "GDP总量（亿元）": "52073.4"},
         {"城市": "深圳", "GDP总量（亿元）": "38731.8"}],
        expect_written_tables=1,
        expect_kept_texts=["保留值"],
    )


if __name__ == "__main__":
    main()
    print("\n" + "=" * 60)
    ok = sum(1 for r in RESULTS if r["ok"])
    for r in RESULTS:
        if not r["ok"]:
            print(f"  未通过: {r['case']} — {r['detail']}")
    print(f"通过 {ok}/{len(RESULTS)}")
    sys.exit(0 if ok == len(RESULTS) else 1)
