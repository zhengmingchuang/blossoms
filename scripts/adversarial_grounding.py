"""列名接地 / 填后审查的离线回归（不调 LLM）。

起因：上一轮给「错配数据源」加接地闸门后，把「城市」这类维度列也算进分母，
结果 8 个 LLM 填表用例全部变成 0 分——城市列被当成「源文没有」整列清空。
这个脚本锁住当时的判据，避免再改回按全列计算。

用法：python scripts/adversarial_grounding.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "..", "backend"))

from services.column_grounding import assess_columns, is_dimension_column  # noqa: E402
from services.fill_audit import audit_llm_rows  # noqa: E402

results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main():
    # ---------- 1. 维度列不能计入分母 ----------
    src = "2024 年杭州市地区生产总值 21,860.32 亿元，常住人口 1,252.2 万人。"
    g = assess_columns(["城市", "GDP总量（亿元）", "常住人口（万人）"], src)
    rec("城市列判为维度列", "城市" in g["dimension"], str(g["dimension"]))
    rec("维度列不参与 ratio 计算", g["ratio"] == 1.0, f"ratio={g['ratio']}")
    rec("没有需要清空的量列", g["ungrounded_measures"] == [], str(g["ungrounded_measures"]))

    # 阳性对照：按全列算会得到 2/3，且「城市」进清空名单 —— 就是被打成 0 分的那个口径
    old_ratio = len(g["grounded"]) / g["total"]
    rec("阳性对照：全列口径确实会误判", old_ratio < 1.0 and "城市" in g["ungrounded"],
        f"全列 ratio={old_ratio:.2f}, ungrounded={g['ungrounded']}")

    # ---------- 2. 真错配仍要拦 ----------
    covid = "国家/地区 大洲 人口 病例数 死亡数 Belgium Albania 46420.7 11372068"
    m = assess_columns(["产品名称", "单价", "库存量"], covid)
    rec("错配数据源 ratio=0（继续 422）", m["ratio"] == 0.0, f"ratio={m['ratio']}")
    rec("错配时点名量列（单价/库存量）",
        set(m["ungrounded_measures"]) == {"单价", "库存量"}, str(m["ungrounded_measures"]))

    # ---------- 3. 维度列分类 ----------
    dims = ["城市", "省份", "站点名称", "污染类型", "日期"]
    measures = ["GDP总量（亿元）", "常住人口（万人）", "单价", "库存量", "城市人口", "首要污染物"]
    rec("维度列识别正确", all(is_dimension_column(c) for c in dims),
        str([c for c in dims if not is_dimension_column(c)]))
    rec("量列不会被误判成维度", not any(is_dimension_column(c) for c in measures),
        str([c for c in measures if is_dimension_column(c)]))

    # ---------- 4. 审查：中文数字与万亿换算 ----------
    src5 = "2024 年福建省常住人口约四千一百万，地区生产总值约五点四万亿元。"
    a5 = audit_llm_rows([{"省份": "福建省", "GDP总量（亿元）": 54000, "常住人口（万人）": 4100}],
                        ["省份", "GDP总量（亿元）", "常住人口（万人）"], src5)
    rec("中文数字换算值能定位", a5["pass_rate"] == 1.0, f"{a5['hits']}/{a5['checked']}")

    src7 = "2024 年无锡市地区生产总值 1.62 万亿元，常住人口 752 万人。"
    a7 = audit_llm_rows([{"城市": "无锡市", "GDP总量": 1620000000000, "常住人口": 7520000}],
                        ["城市", "GDP总量", "常住人口"], src7)
    rec("万亿→基本单位换算值能定位", a7["pass_rate"] == 1.0, f"{a7['hits']}/{a7['checked']}")

    a_bad = audit_llm_rows([{"GDP总量（亿元）": 99999}], ["GDP总量（亿元）"], src5)
    rec("阳性对照：编造的值仍被标出", a_bad["pass_rate"] == 0.0, f"misses={a_bad['misses']}")

    ok = sum(1 for _, o, _ in results if o)
    print(f"\n=== 通过 {ok}/{len(results)} ===")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
