# -*- coding: utf-8 -*-
"""大表索引截断回归：证明「按字符预算分批」真的没有丢行，且旧实现确实会丢。

背景（实测）：`_sheet_matrix_to_chunks` 旧实现固定 40 行一批，再用
`text[:CHUNK_MAX*2]` 硬截断。宽表每行约 280 字，一批 40 行 ≈ 11000 字，
远超 1600 字上限，结果每批只留前 5~6 行，其余静默丢弃。
`MAX_INDEX_ROWS_PER_SHEET=24000` 的承诺因此不成立（COVID 38473 行实际只索引约 4800 行）。

本脚本用**合成宽表**做阳性对照：
  1. 旧逻辑（本文件内复刻）→ 必须丢行（否则说明测试没造出触发条件）
  2. 新逻辑（直接调被测函数）→ 必须一行不丢
  3. 真实 COVID 源文件 → 第 195 行的 10255 必须能被索引到

    python scripts/adversarial_parse_truncation.py

退出码：0 通过；1 失败。
"""
import os
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from services.document_parser import (  # noqa: E402
    CHUNK_MAX,
    MAX_INDEX_ROWS_PER_SHEET,
    _sheet_matrix_to_chunks,
)

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


# ---------------------------------------------------------------------------
# 旧实现复刻：固定 40 行/批 + 硬截断，用来证明触发条件是真的
# ---------------------------------------------------------------------------

def old_impl_chunks(header: list, data_rows: list) -> list:
    """复刻修复前的逻辑（只保留用于对照，不要在生产代码里用）。"""
    chunks = []
    batch_size = 40
    index_rows = data_rows[:MAX_INDEX_ROWS_PER_SHEET]
    for start in range(0, len(index_rows), batch_size):
        batch = index_rows[start:start + batch_size]
        lines = []
        for ri, row in enumerate(batch, start + 1):
            vals = [str(c) if c is not None else "" for c in row]
            if not any(v and v != "None" for v in vals):
                continue
            pairs = []
            for j in range(min(len(header), len(vals))):
                v = vals[j]
                if v and v != "None":
                    h = header[j] if header[j] and header[j] != "None" else f"列{j + 1}"
                    pairs.append(f"{h}: {v}")
            if pairs:
                lines.append(f"[行{ri}] " + " | ".join(pairs))
        if lines:
            text = "\n".join(lines)
            if len(text) > CHUNK_MAX * 2:
                text = text[:CHUNK_MAX * 2]
            chunks.append(text)
    return chunks


def make_wide_table(n_rows: int = 2000) -> tuple:
    """造一张 15 列的宽表，每行带唯一标记 ROW-{i}，便于统计存活行数。"""
    header = [
        "国家/地区", "大洲", "纬度", "经度", "年平均温度", "每千人医院床位数",
        "每千人医生数", "人均GDP", "人口", "年龄中位数", "65岁及以上人口占比",
        "日期", "每日检测数", "病例数", "死亡数",
    ]
    rows = []
    for i in range(n_rows):
        rows.append((
            f"ROW-{i}", "Europe", 41.15, 20.17, 14, 2.89, 1.29,
            5353.2, 2873457, 38, 14, f"2020-09-{(i % 28) + 1:02d}", 847, 10255 + i, 316,
        ))
    return header, rows


def survived(chunks: list, n_rows: int) -> tuple:
    """统计有多少个 ROW-{i} 标记出现在索引文本里。"""
    blob = "\n".join(chunks)
    hit = [i for i in range(n_rows) if f"ROW-{i}" in blob]
    return len(hit), n_rows


def main() -> int:
    N = 2000
    header, rows = make_wide_table(N)
    all_rows = [tuple(header)] + rows

    # --- 1) 阳性对照：旧实现必须丢行 ---
    old_chunks = old_impl_chunks(header, rows)
    old_hit, _ = survived(old_chunks, N)
    check(
        "阳性对照：旧实现（40 行/批 + 硬截断）确实丢行",
        old_hit < N,
        f"存活 {old_hit}/{N} 行，chunk {len(old_chunks)} 个",
    )

    # --- 2) 新实现：一行不丢 ---
    new_chunks, _ = _sheet_matrix_to_chunks("Data", all_rows, 0)
    texts = [c["chunk_text"] for c in new_chunks]
    new_hit, _ = survived(texts, N)
    check(
        "新实现（按字符预算分批）一行不丢",
        new_hit == N,
        f"存活 {new_hit}/{N} 行，chunk {len(new_chunks)} 个",
    )

    # --- 3) 单 chunk 仍受控（不能把整张表塞进一个 chunk）---
    over = [t for t in texts if len(t) > CHUNK_MAX * 2 * 2]
    check(
        "单个 chunk 长度仍受控",
        not over,
        f"超长 chunk {len(over)} 个（阈值 {CHUNK_MAX * 2 * 2} 字）",
    )

    # --- 4) 真实 COVID 源文件：第 195 行的 10255 必须可检索 ---
    covid = os.path.join(ROOT, "docs", "demo测试集", "Excel", "COVID-19全球数据集（节选）.xlsx")
    if os.path.exists(covid):
        import shutil
        import tempfile
        import openpyxl
        from services.ooxml_compat import normalize_ooxml_inplace

        tmp = os.path.join(tempfile.mkdtemp(), "covid.xlsx")
        shutil.copy2(covid, tmp)
        try:
            normalize_ooxml_inplace(tmp)
        except Exception:
            pass
        wb = openpyxl.load_workbook(tmp, data_only=True, read_only=True)
        ws = wb.worksheets[0]
        # 只取前 2000 行做验证：命中判据是「源文件里存在、索引里也要存在」
        sample = []
        for i, row in enumerate(ws.iter_rows(values_only=True)):
            sample.append(tuple(row))
            if i >= 1999:
                break
        wb.close()

        c_chunks, _ = _sheet_matrix_to_chunks("Data", sample, 0)
        c_text = "\n".join(c["chunk_text"] for c in c_chunks)
        check(
            "真实 COVID 表：Albania 2020-09-06 的 10255 已进索引",
            "10255" in c_text,
            f"chunk {len(c_chunks)} 个，索引文本 {len(c_text)} 字",
        )
        old_c = "\n".join(old_impl_chunks(list(sample[0]), sample[1:]))
        check(
            "阳性对照：旧实现在同一份数据上确实丢了 10255",
            "10255" not in old_c,
            f"旧实现索引文本 {len(old_c)} 字",
        )
    else:
        print("[SKIP] 找不到 COVID 源文件，跳过真实数据验证")

    print("\n" + "=" * 62)
    print(f"通过 {len(PASS)}/{len(PASS) + len(FAIL)}")
    if FAIL:
        print("失败项: " + "; ".join(FAIL))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())
