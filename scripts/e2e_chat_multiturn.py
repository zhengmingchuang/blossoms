"""智能交互专项：多轮工具调用稳定性 + 数值精度 + 模糊指令不瞎编。

上一轮只有 2 个短指令单轮用例，多轮上下文与工具链稳定性没验过。这里补 4 组：

  A 多轮追问（同一 session）：先出表，再在「上表」基础上加合计列 —— 验上下文能否延续
  B 上传文件后操作：上传 xlsx → 排序导出 —— 验工具能否真读到用户文件
  C 模糊指令：只说「帮我分析一下」 —— 验会不会凭空编数据
  D 数值精度：6 行数据求和/均值 —— 逐格核对

判定全部落在可核对的事实上（文件是否生成、单元格数值、耗时、工具调用次数），
主观项单独标注，不混进通过率。

    python scripts/e2e_chat_multiturn.py
"""
import io
import json
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
OUT = os.path.join(ROOT, ".workbuddy-ai", "outputs", "chat_multiturn")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
os.makedirs(OUT, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
import openpyxl  # noqa: E402

PASSWORD = os.getenv("DOCFUSION_TEST_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("请先设置环境变量 DOCFUSION_TEST_PASSWORD")

RESULTS = []


def record(name, ok, detail=""):
    RESULTS.append({"case": name, "ok": bool(ok), "detail": detail})
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def login(c):
    r = c.post("/api/auth/login", data={"username": os.getenv("DOCFUSION_TEST_USER", "admin"), "password": PASSWORD})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def ask(c, h, text, session_id=None, timeout=600):
    body = {"message": text}
    if session_id:
        body["session_id"] = session_id
    t0 = time.time()
    r = c.post("/api/chat/ask", json=body, headers=h, timeout=timeout)
    return r, round(time.time() - t0, 1)


def fetch(c, h, fid, name):
    r = c.get(f"/api/documents/{fid}/download", headers=h)
    if r.status_code != 200:
        return None
    p = os.path.join(OUT, name)
    with open(p, "wb") as f:
        f.write(r.content)
    return p


def dump_xlsx(path, max_rows=12):
    wb = openpyxl.load_workbook(path, data_only=True)
    out = []
    for ws in wb.worksheets:
        out.append(f"[{ws.title}] {ws.max_row}x{ws.max_column}")
        for row in ws.iter_rows(max_row=max_rows, values_only=True):
            if any(v is not None for v in row):
                out.append("  " + str(row))
    wb.close()
    return "\n".join(out)


def nums_in_xlsx(path):
    """收集所有数值。数字样子的文本也计入——否则「270 被写成文本」这种
    真实缺陷会被判成「数值不存在」，掩盖问题而不是暴露问题。"""
    wb = openpyxl.load_workbook(path, data_only=True)
    vals = []
    for ws in wb.worksheets:
        for row in ws.iter_rows(values_only=True):
            for v in row:
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    vals.append(float(v))
                elif isinstance(v, str):
                    s = v.strip().replace(",", "")
                    try:
                        vals.append(float(s))
                    except ValueError:
                        pass
    wb.close()
    return vals


def main_flow():
    c = TestClient(main.app)
    h = login(c)

    # ---------- A 多轮追问 ----------
    print("\n=== A 多轮追问（同一 session）===")
    r, cost = ask(c, h, "把下面的数据做成一个 xlsx 表格，文件名 sales_multi.xlsx："
                        "A产品 1月 120 件、2月 150 件；B产品 1月 90 件、2月 110 件。")
    print(f"  第1轮 {cost}s status={r.status_code}")
    if r.status_code != 200:
        record("A1 首轮出表", False, r.text[:150])
        return
    j = r.json()
    sid = j.get("session_id")
    files1 = j.get("edited_files") or []
    tools1 = j.get("tool_actions") or []
    record("A1 首轮出表", bool(files1), f"文件 {[f['name'] for f in files1]} 工具 {len(tools1)} 次 {cost}s")
    if not files1:
        return
    p1 = fetch(c, h, files1[0]["id"], "A_" + files1[0]["name"])
    if p1:
        print("  首轮产物:\n" + dump_xlsx(p1))

    r, cost = ask(c, h, "在上表基础上增加一列「两月合计」，并告诉我 A 产品的合计是多少。", session_id=sid)
    print(f"  第2轮 {cost}s status={r.status_code}")
    j2 = r.json() if r.status_code == 200 else {}
    ans2 = j2.get("answer", "")
    files2 = j2.get("edited_files") or []
    record("A2 追问沿用上下文", bool(files2), f"文件 {[f['name'] for f in files2]} {cost}s")
    record("A2 回答含 A 合计 270", "270" in ans2.replace(",", ""), f"回答片段 {ans2[:120]!r}")
    if files2:
        p2 = fetch(c, h, files2[0]["id"], "A2_" + files2[0]["name"])
        if p2:
            vals = nums_in_xlsx(p2)
            record("A2 表格出现合计 270", any(abs(v - 270) < 1e-6 for v in vals), f"数值 {sorted(set(vals))[:12]}")
            print("  追问产物:\n" + dump_xlsx(p2))

    # ---------- B 上传文件后操作 ----------
    print("\n=== B 上传文件后按 GDP 排序 ===")
    buf = io.BytesIO()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["城市", "GDP总量（亿元）", "常住人口（万人）"])
    ws.append(["苏州", 27695.10, 1295.80])
    ws.append(["上海", 56708.71, 2487.45])
    ws.append(["杭州", 21860.32, 1252.20])
    wb.save(buf)
    buf.seek(0)
    r = c.post("/api/chat/upload", files={"file": ("city_gdp3.xlsx", buf.read(),
                                                   "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")},
               headers=h)
    if r.status_code != 200:
        record("B1 上传数据源", False, r.text[:150])
    else:
        fid = r.json()["id"]
        record("B1 上传数据源", True, f"file_id={fid}")
        time.sleep(1.0)  # 后台解析
        r, cost = ask(c, h, "把刚上传的表格按 GDP 从高到低排序，另存为 gdp_sorted.xlsx。", timeout=600)
        jb = r.json() if r.status_code == 200 else {}
        filesb = jb.get("edited_files") or []
        print(f"  回答片段: {(jb.get('answer') or '')[:160]!r}")
        record("B2 生成排序文件", bool(filesb), f"{[f['name'] for f in filesb]} {cost}s")
        if filesb:
            pb = fetch(c, h, filesb[0]["id"], "B_" + filesb[0]["name"])
            if pb:
                wb2 = openpyxl.load_workbook(pb, data_only=True)
                ws2 = wb2.worksheets[0]
                first = [c2.value for c2 in ws2[2]] if ws2.max_row >= 2 else []
                wb2.close()
                print(f"  首行数据: {first}")
                record("B2 首行为上海(最大)", first and "上海" in str(first[0]), f"{first}")

    # ---------- C 模糊指令 ----------
    print("\n=== C 模糊指令不瞎编 ===")
    r, cost = ask(c, h, "帮我分析一下。")
    jc = r.json() if r.status_code == 200 else {}
    ansc = jc.get("answer", "")
    filesc = jc.get("edited_files") or []
    record("C1 未凭空生成文件", not filesc, f"文件 {filesc} {cost}s")
    _err = "处理请求时出现错误" in ansc
    record("C1b 无接口异常", not _err, f"回答片段 {ansc[:80]!r}")
    _clarify = any(k in ansc for k in ("请提供", "请说明", "请告诉", "请问", "请您确认", "请确认", "确认一下", "需要您", "需要你", "具体", "哪方面", "哪一个"))
    record("C2 有澄清/索要信息", _clarify and not _err, f"回答片段 {ansc[:120]!r}")

    # ---------- D 数值精度 ----------
    print("\n=== D 数值精度（求和/均值）===")
    data = "1月 1024、2月 2048、3月 512、4月 4096、5月 8192、6月 1536"
    r, cost = ask(c, h, f"把下面 6 个月的销售额做成 xlsx（文件名 sum_check.xlsx），"
                        f"并加一行合计、一行平均值：{data}。")
    jd = r.json() if r.status_code == 200 else {}
    ansd = jd.get("answer", "")
    filesd = jd.get("edited_files") or []
    total = 1024 + 2048 + 512 + 4096 + 8192 + 1536      # 17408
    avg = total / 6                                      # 2901.333...
    record("D1 生成文件", bool(filesd), f"{[f['name'] for f in filesd]} {cost}s")
    if filesd:
        pd = fetch(c, h, filesd[0]["id"], "D_" + filesd[0]["name"])
        if pd:
            vals = nums_in_xlsx(pd)
            print("  数值:", sorted(set(vals))[:16])
            record("D2 合计 17408", any(abs(v - total) < 1e-6 for v in vals), f"期望 {total}")
            record("D3 均值 2901.33", any(abs(v - avg) < 0.02 for v in vals), f"期望 {avg:.2f}")
    else:
        record("D2 合计 17408", "17408" in ansd.replace(",", ""), "从回答文本核对")
        record("D3 均值 2901.33", "2901.33" in ansd.replace(",", ""), "从回答文本核对")


if __name__ == "__main__":
    main_flow()
    print("\n" + "=" * 60)
    ok = sum(1 for x in RESULTS if x["ok"])
    for x in RESULTS:
        if not x["ok"]:
            print(f"  未通过: {x['case']} — {x['detail']}")
    print(f"通过 {ok}/{len(RESULTS)}")
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2)
    sys.exit(0 if ok == len(RESULTS) else 1)
