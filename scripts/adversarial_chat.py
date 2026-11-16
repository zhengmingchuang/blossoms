"""对抗式对话探针：把「智能交互页面自然语言」里的两段原始需求直接丢给 Agent。

考察点不是「回答得好不好」，而是：
  - 有没有真的落成文件（而不是只在回答里贴一段 markdown 表格）
  - 表格里有没有汇总行、图表对象是否真的写进了 xlsx
  - 数据是否与需求里给的一致

    python scripts/adversarial_chat.py
"""
import json
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
NL_DIR = os.path.join(ROOT, "docs", "demo测试集", "智能交互页面自然语言")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402


# 测试凭据只从环境变量读取，源码里不留明文（仓库公开等于口令公开）。
TEST_USER = os.getenv("DOCFUSION_TEST_USER", "admin")
TEST_PASSWORD = os.getenv("DOCFUSION_TEST_PASSWORD", "")
if not TEST_PASSWORD:
    raise SystemExit("请先设置环境变量 DOCFUSION_TEST_PASSWORD（管理员口令）再运行本脚本；否则会以空口令调用接口")



def login(c):
    r = c.post("/api/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def ask(c, h, text):
    t0 = time.time()
    r = c.post("/api/chat/ask", json={"message": text}, headers=h, timeout=600)
    cost = round(time.time() - t0, 1)
    return r, cost


def inspect_xlsx(path):
    import openpyxl
    wb = openpyxl.load_workbook(path)
    info = []
    for ws in wb.worksheets:
        charts = len(getattr(ws, "_charts", []) or [])
        rows = []
        for row in ws.iter_rows(values_only=True):
            if any(v is not None for v in row):
                rows.append([v for v in row])
        info.append({"sheet": ws.title, "rows": len(rows), "charts": charts, "sample": rows[:8]})
    return info


def run_case(c, h, name):
    path = os.path.join(NL_DIR, name)
    text = open(path, encoding="utf-8").read().strip()
    print(f"\n{'=' * 70}\n### {name}（{len(text)} 字）")
    r, cost = ask(c, h, text)
    print(f"HTTP {r.status_code}  {cost}s")
    if r.status_code != 200:
        print("  body:", r.text[:300])
        return
    j = r.json()
    print(f"  answer 长度: {len(j.get('answer') or '')}")
    print(f"  回答前 200 字: {(j.get('answer') or '')[:200].replace(chr(10), ' ')}")
    acts = j.get("tool_actions") or []
    print(f"  工具调用 {len(acts)} 次: {[a.get('tool') or a.get('name') for a in acts]}")
    files = j.get("edited_files") or []
    print(f"  产出文件: {[(f.get('id'), f.get('name')) for f in files if isinstance(f, dict)]}")
    import io
    for f in files:
        if not isinstance(f, dict):
            continue
        fid = f.get("id")
        rr = c.get(f"/api/documents/{fid}/download", headers=h)
        if rr.status_code != 200:
            print(f"    下载 {fid} 失败 HTTP {rr.status_code}")
            continue
        name = f.get("name", "")
        if name.lower().endswith(".xlsx"):
            import openpyxl
            wb = openpyxl.load_workbook(io.BytesIO(rr.content))
            for ws in wb.worksheets:
                charts = len(getattr(ws, "_charts", []) or [])
                rows = [r for r in ws.iter_rows(values_only=True) if any(v is not None for v in r)]
                print(f"    [{ws.title}] {len(rows)} 行, 图表 {charts} 个")
                for row in rows[:8]:
                    print("      ", [v for v in row])
        else:
            print(f"    {name}: {len(rr.content)} bytes")


def main_():
    with TestClient(main.app) as c:
        h = login(c)
        run_case(c, h, "自然语言1.txt")
        run_case(c, h, "自然语言2.txt")


if __name__ == "__main__":
    main_()
