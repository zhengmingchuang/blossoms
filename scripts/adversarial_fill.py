"""对抗式填表探针：真实 LLM 链路，重点看「结果是否被悄悄改错」。

用例：
  1. 筛选条件（GDP>5万亿）在真实端到端里是否生效
  2. 同一模板连填两次是否幂等（会不会把上一次的结果叠进去）
  3. 模板行数少于数据行数时，是否会丢行（德州 25 站点 vs 模板 24 行）
  4. 单位口径：表头「亿元」+ 源文「5.67万亿元」是否换算成 56700 而不是 5.67

    python scripts/adversarial_fill.py
"""
import io
import json
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
DEMO = os.path.join(ROOT, "docs", "demo测试集", "包含模板文件")
OUT = os.path.join(ROOT, ".workbuddy-ai", "outputs", "adversarial")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402


# 测试凭据只从环境变量读取，源码里不留明文（仓库公开等于口令公开）。
TEST_USER = os.getenv("DOCFUSION_TEST_USER", "admin")
TEST_PASSWORD = os.getenv("DOCFUSION_TEST_PASSWORD", "")
if not TEST_PASSWORD:
    raise SystemExit("请先设置环境变量 DOCFUSION_TEST_PASSWORD（管理员口令）再运行本脚本；否则会以空口令调用接口")


os.makedirs(OUT, exist_ok=True)


def login(c, u=None, p=None):
    r = c.post("/api/auth/login", data={"username": u or TEST_USER, "password": p or TEST_PASSWORD})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def up(c, h, path):
    with open(path, "rb") as f:
        r = c.post("/api/documents/upload", files={"files": (os.path.basename(path), f)}, headers=h)
    r.raise_for_status()
    return r.json()["files"][0]["file_id"]


def up_tpl(c, h, path):
    with open(path, "rb") as f:
        r = c.post("/api/templates/upload", files={"file": (os.path.basename(path), f)}, headers=h)
    r.raise_for_status()
    return r.json().get("template_id") or r.json().get("id")


def wait_done(c, h, fid, timeout=180):
    t0 = time.time()
    while time.time() - t0 < timeout:
        j = c.get("/api/documents/list?page=1&page_size=200", headers=h).json()
        for it in (j.get("items") or j.get("files") or []):
            if it.get("id") == fid:
                if it.get("parse_status") in ("done", "error"):
                    return it["parse_status"]
        time.sleep(1)
    return "timeout"


def fill(c, h, tid, req, src_ids):
    c.put(f"/api/templates/{tid}/requirement", json={"user_requirement": req}, headers=h)
    r = c.post(f"/api/templates/{tid}/fill", json={"source_file_ids": src_ids}, headers=h)
    return r


def read_xlsx_rows(task_id, h, c):
    r = c.get(f"/api/templates/history/{task_id}/download", headers=h)
    r.raise_for_status()
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(r.content))
    ws = wb.worksheets[0]
    return [list(r) for r in ws.iter_rows(values_only=True)]


def main_():
    with TestClient(main.app) as c:
        h = login(c)

        # ========== 1 & 2: 筛选条件 + 幂等 ==========
        d = os.path.join(DEMO, "2025年中国城市经济百强全景报告")
        src = up(c, h, os.path.join(d, "2025年中国城市经济百强全景报告.docx"))
        wait_done(c, h, src)
        tid = up_tpl(c, h, os.path.join(d, "2025年中国城市经济百强全景报告-模板.xlsx"))

        r1 = fill(c, h, tid, "提取gdp总量大于5万亿的城市", [src])
        j1 = r1.json()
        print(f"\n[1] 筛选 GDP>5万亿 -> HTTP {r1.status_code} rows={j1.get('total_rows')} "
              f"time={j1.get('elapsed_seconds')}s")
        if r1.status_code == 200:
            rows = read_xlsx_rows(j1["task_id"], h, c)[1:]
            names = [str(x[0]) for x in rows if x and x[0]]
            print(f"    城市: {names}")
            ok = len(names) == 2 and set(names) == {"上海", "北京"}
            print(f"    {'PASS' if ok else 'FAIL'} 期望 上海/北京 两行")

        # 幂等：同一模板再填一次（无筛选），再填一次（筛选），看是否叠加
        r2 = fill(c, h, tid, "帮我智能填表", [src])
        j2 = r2.json()
        print(f"\n[2] 无筛选填表 -> HTTP {r2.status_code} rows={j2.get('total_rows')}")
        r3 = fill(c, h, tid, "提取gdp总量大于5万亿的城市", [src])
        j3 = r3.json()
        print(f"    再填一次筛选 -> HTTP {r3.status_code} rows={j3.get('total_rows')}")
        if r3.status_code == 200:
            rows3 = read_xlsx_rows(j3["task_id"], h, c)[1:]
            names3 = [str(x[0]) for x in rows3 if x and x[0]]
            print(f"    {'PASS' if len(names3) == 2 else 'FAIL'} 未叠加（{len(names3)} 行，期望 2）")

        # ========== 3: 模板行数不足 ==========
        d2 = os.path.join(DEMO, "2025山东省环境空气质量监测数据信息")
        src2 = up(c, h, os.path.join(d2, "山东省环境空气质量监测数据信息202512171921_0.xlsx"))
        wait_done(c, h, src2, timeout=300)
        tid2 = up_tpl(c, h, os.path.join(d2, "2025山东省环境空气质量监测数据信息-模板.docx"))
        req2 = open(os.path.join(d2, "用户要求.txt"), encoding="utf-8").read().strip()
        r4 = fill(c, h, tid2, req2, [src2])
        j4 = r4.json()
        print(f"\n[3] 三表填表 -> HTTP {r4.status_code} rows={j4.get('total_rows')} "
              f"time={j4.get('elapsed_seconds')}s")
        if r4.status_code == 200:
            rr = c.get(f"/api/templates/history/{j4['task_id']}/download", headers=h)
            import docx
            doc = docx.Document(io.BytesIO(rr.content))
            counts = [len(t.rows) - 1 for t in doc.tables]
            print(f"    每表数据行: {counts}（源: 德州25 潍坊29 临沂25）")
            print(f"    {'PASS' if counts == [25, 29, 25] else 'FAIL'} 行数未丢")

        # ========== 4: 单位陷阱 ==========
        # 构造一份「5.67万亿元」的源文 + 表头「GDP总量（亿元）」的模板
        import docx as _docx
        from openpyxl import Workbook

        src_doc = os.path.join(OUT, "单位陷阱-源.docx")
        d = _docx.Document()
        d.add_paragraph("2025年主要城市经济数据如下：")
        d.add_paragraph("上海市GDP总量为5.67万亿元，常住人口2487.45万人。")
        d.add_paragraph("北京市GDP总量为5.21万亿元，常住人口2185.3万人。")
        d.save(src_doc)

        tpl_xlsx = os.path.join(OUT, "单位陷阱-模板.xlsx")
        wb = Workbook()
        ws = wb.active
        ws.append(["城市名", "GDP总量（亿元）", "常住人口（万）"])
        wb.save(tpl_xlsx)

        src3 = up(c, h, src_doc)
        wait_done(c, h, src3)
        tid3 = up_tpl(c, h, tpl_xlsx)
        r5 = fill(c, h, tid3, "帮我智能填表", [src3])
        j5 = r5.json()
        print(f"\n[4] 单位陷阱（源=万亿元，表头=亿元）-> HTTP {r5.status_code} rows={j5.get('total_rows')}")
        if r5.status_code == 200:
            rows5 = read_xlsx_rows(j5["task_id"], h, c)[1:]
            for x in rows5:
                print(f"    {x}")
            gdp = [x[1] for x in rows5 if x and x[0]]
            ok = all(isinstance(v, (int, float)) and v > 10000 for v in gdp) and gdp
            print(f"    {'PASS' if ok else 'FAIL'} GDP 应为 56700/52100 量级，不是 5.67")

        # ========== 5: 数据源与模板完全不匹配 ==========
        # 模板列是「产品名称/单价/库存量」，数据源是 COVID 各国疫情数据，两者无任何交集。
        # 正确行为是明确拒绝，而不是把「国家」当「产品名」硬凑出一张看起来正常的表。
        d3 = os.path.join(DEMO, "COVID-19数据集")
        src_bad = up(c, h, os.path.join(d3, "COVID-19全球数据集（节选）.xlsx"))
        wait_done(c, h, src_bad, timeout=300)
        tpl_disjoint = os.path.join(OUT, "错配-模板.xlsx")
        wbd = Workbook()
        wsd = wbd.active
        wsd.append(["产品名称", "单价", "库存量"])
        wbd.save(tpl_disjoint)
        tid5 = up_tpl(c, h, tpl_disjoint)
        r6 = fill(c, h, tid5, "帮我智能填表", [src_bad])
        j6 = r6.json() if r6.status_code == 200 else {}
        print(f"\n[5] 错配数据源（产品模板 + COVID 数据）-> HTTP {r6.status_code} "
              f"rows={j6.get('total_rows')} msg={str(j6.get('message') or r6.text)[:90]}")
        bad_rows = j6.get("total_rows") or 0
        print(f"    {'PASS' if r6.status_code != 200 or bad_rows == 0 else 'FAIL'} "
              f"未凭空产出数据（rows={bad_rows}）")

        # ========== 6: 无数据源（用全新账号，避免回落到库里已有文件） ==========
        import uuid as _uuid
        uname = f"nosrc_{_uuid.uuid4().hex[:6]}"
        c.post("/api/auth/register", json={"username": uname, "password": TEST_PASSWORD})
        h3 = login(c, uname, TEST_PASSWORD)
        tpl_xlsx2 = os.path.join(OUT, "无源-模板.xlsx")
        wb2 = Workbook()
        ws2 = wb2.active
        ws2.append(["城市名", "GDP总量（亿元）"])
        wb2.save(tpl_xlsx2)
        with open(tpl_xlsx2, "rb") as f:
            r = c.post("/api/templates/upload", files={"file": ("无源-模板.xlsx", f)}, headers=h3)
        tid4 = r.json().get("template_id")
        r7 = c.post(f"/api/templates/{tid4}/fill", json={}, headers=h3)
        print(f"\n[6] 无任何数据源 -> HTTP {r7.status_code} {str(r7.text)[:120]}")
        print(f"    {'PASS' if r7.status_code != 200 else 'FAIL'} 应明确拒绝而非返回空表")

        print("\n" + "=" * 70)


if __name__ == "__main__":
    main_()
