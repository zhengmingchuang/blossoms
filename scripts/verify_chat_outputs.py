# -*- coding: utf-8 -*-
"""核对智能交互两个自然语言用例的产出：逐格比数值 + 检查 docx 的表格与图片。"""
import io
import os
import sys

import httpx
import openpyxl
from docx import Document

BASE = "http://127.0.0.1:8000"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, ".workbuddy-ai", "outputs", "chat_verify")
os.makedirs(OUT, exist_ok=True)

pwd = open(os.path.join(ROOT, "backend", ".admin_initial_password"), encoding="utf-8").read().strip()
with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as c:
    tok = c.post("/api/auth/login", data={"username": "admin", "password": pwd}).json()["access_token"]
    h = {"Authorization": "Bearer " + tok}

    def fetch(fid, name):
        r = c.get(f"/api/documents/{fid}/download", headers=h)
        r.raise_for_status()
        p = os.path.join(OUT, name)
        with open(p, "wb") as f:
            f.write(r.content)
        return p

    p1x = fetch(24, "用户消费数据分析表.xlsx")
    p1d = fetch(25, "用户消费数据分析报告.docx")
    p2x = fetch(26, "门店近6个月产品月度销量统计.xlsx")
    p2d = fetch(27, "门店产品月度销量分析实验报告.docx")

print("=" * 70)
print("用例1 xlsx 全量内容")
wb = openpyxl.load_workbook(p1x)
for ws in wb.worksheets:
    print(f"[{ws.title}] {ws.max_row}x{ws.max_column} 图表 {len(getattr(ws, '_charts', []) or [])} 个")
    for row in ws.iter_rows(values_only=True):
        if any(v is not None for v in row):
            print("   ", [v for v in row])

print()
print("用例1 期望校验")
rows = [r for ws in wb.worksheets for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
flat = [[v for v in r] for r in rows]
exp_detail = [("U01", 238, "2025-10-05", "餐饮"), ("U02", 569, "2025-10-08", "服饰"),
              ("U03", 146, "2025-10-12", "餐饮"), ("U04", 725, "2025-10-15", "数码"),
              ("U05", 312, "2025-10-20", "服饰")]
ok = True
for e in exp_detail:
    hit = any(str(r[0]) == e[0] and r[1] == e[1] and str(r[2])[:10] == e[2] and str(r[3]) == e[3] for r in flat if len(r) >= 4)
    print(("  OK  " if hit else "  FAIL"), e)
    ok &= hit
total_hit = any(str(r[0]) == "合计" and r[1] == 1990 for r in flat if len(r) >= 2)
print(("  OK  " if total_hit else "  FAIL"), "合计 = 1990")
ok &= total_hit

print()
print("=" * 70)
print("用例2 xlsx 全量内容")
wb2 = openpyxl.load_workbook(p2x)
for ws in wb2.worksheets:
    print(f"[{ws.title}] {ws.max_row}x{ws.max_column} 图表 {len(getattr(ws, '_charts', []) or [])} 个")
    for row in ws.iter_rows(values_only=True):
        if any(v is not None for v in row):
            print("   ", [v for v in row])

print()
print("用例2 期望校验")
rows2 = [r for ws in wb2.worksheets for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
flat2 = [[v for v in r] for r in rows2]
exp_month = {"1月": (326, 189, 245, 760), "2月": (352, 203, 261, 816), "3月": (318, 176, 233, 727),
             "4月": (377, 225, 289, 891), "5月": (402, 241, 305, 948), "6月": (436, 258, 322, 1016)}
for m, e in exp_month.items():
    hit = any(str(r[0]) == m and tuple(r[1:5]) == e for r in flat2 if len(r) >= 5)
    print(("  OK  " if hit else "  FAIL"), m, e)
    ok &= hit
sum_hit = any(str(r[0]) == "半年总销量" and tuple(r[1:5]) == (2211, 1292, 1655, 5158) for r in flat2 if len(r) >= 5)
print(("  OK  " if sum_hit else "  FAIL"), "半年总销量 = 2211/1292/1655/5158")
ok &= sum_hit

print()
print("=" * 70)
for label, p in (("用例1 docx", p1d), ("用例2 docx", p2d)):
    d = Document(p)
    imgs = sum(1 for rel in d.part.rels.values() if "image" in rel.reltype)
    txt = "\n".join(x.text for x in d.paragraphs)
    print(f"{label}: 段落 {len(d.paragraphs)}, 表格 {len(d.tables)}, 图片 {imgs}, 正文字数 {len(txt)}")
    for i, t in enumerate(d.tables):
        print(f"   表{i}: {len(t.rows)}行 x {len(t.columns)}列  首行={[c.text.strip()[:10] for c in t.rows[0].cells]}")

print()
print("总体:", "全部通过" if ok else "存在不一致")
sys.exit(0 if ok else 1)
