# -*- coding: utf-8 -*-
"""忠实度校验：检查模型输出的数值是否真实出现在源文档中（防编造）。

用途：A/B 对比以「历史产物」为真值时，若两侧同时抽错同一处，评估会漏判。
      本工具绕过基准，直接把模型输出回查源文档原文，作为独立交叉验证。

用法：
    python scripts/faithfulness_check.py
    python scripts/faithfulness_check.py --case city100
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import ab_compare as ab  # noqa: E402


def clean(s: str) -> str:
    return re.sub(r"[\s,，]", "", str(s))


def is_grounded(value: str, source_clean: str) -> bool:
    """数值是否能在源文档中找到（容忍千分位、末尾零、小数位差异）。"""
    nv = clean(value)
    if not nv:
        return True
    if nv in source_clean:
        return True
    alt = nv.rstrip("0").rstrip(".")
    return bool(alt) and alt in source_clean


def check_rows(rows, cols, source_clean, label: str):
    total = missing = 0
    suspects = []
    for row in rows:
        for c in cols[1:]:
            v = str(row.get(c, "") or "").strip()
            if not v:
                continue
            total += 1
            if not is_grounded(v, source_clean):
                missing += 1
                if len(suspects) < 20:
                    suspects.append({"entity": row.get(cols[0]), "col": c, "val": v})
    faith = (total - missing) / total if total else None
    print(f"[{label}] 数值单元格 {total}, 源文档中找不到 {missing}, "
          f"忠实度 {faith:.4%}" if faith is not None else f"[{label}] 无数值可校验")
    if suspects:
        print("  可疑值:")
        for m in suspects:
            print("   ", m)
    return {"total": total, "missing": missing, "faithfulness": faith, "suspects": suspects}


async def run_deepseek(case, cols):
    conf = ab.read_deepseek_conf()
    if not conf["key"]:
        raise RuntimeError("未找到 DeepSeek API Key（backend/.env 的 LLM_API_KEY 或 DEEPSEEK_API_KEY）")
    text = ab.load_source_text(case["source_file_ids"])
    batches = ab.build_batches(text)
    sys_prompt = ab.build_sys_prompt(cols, case["user_req"])
    sem = asyncio.Semaphore(ab.CONCURRENCY)

    async with httpx.AsyncClient(proxy=None) as client:
        async def one(i, b):
            async with sem:
                user_text = (f"从以下文档中提取所有符合条件的数据（第{i+1}批，共{len(batches)}批），"
                             f"必须提取全部数据，不能遗漏：\n\n{b}")
                return await ab.call_deepseek(client, conf, sys_prompt, user_text)

        results = await asyncio.gather(*[one(i, b) for i, b in enumerate(batches)])

    rows = []
    for r in results:
        parsed, _ = ab.parse_json_array(r.get("text", ""))
        rows.extend(parsed)
    return ab.normalize_rows(rows, cols)


def main():
    ap = argparse.ArgumentParser(description="数值忠实度校验（防编造）")
    ap.add_argument("--case", default="city100", choices=list(ab.CASES))
    args = ap.parse_args()

    case = ab.CASES[args.case]
    cols = ab.load_template_columns(ab.UPLOAD_DIR / case["template"])
    baseline = ab.load_baseline_rows(ab.EXPORT_DIR / case["baseline"], cols)
    source_clean = clean(ab.load_source_text(case["source_file_ids"]))

    print(f"用例: {args.case} — {case['desc']}")
    print(f"模板列名: {cols}\n")

    rows = asyncio.run(run_deepseek(case, cols))
    print(f"DeepSeek 抽取行数: {len(rows)}\n")

    ds = check_rows(rows, cols, source_clean, "DeepSeek")
    bl = check_rows(baseline, cols, source_clean, "基准/Claude")

    out = ROOT / "ab_report" / "faithfulness.json"
    out.parent.mkdir(exist_ok=True)
    out.write_text(json.dumps({"case": args.case, "deepseek": ds, "baseline": bl},
                              ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存 {out}")


if __name__ == "__main__":
    main()
