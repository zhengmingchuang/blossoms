"""LLM 主导填表的准确率基准：脏数据 / 同义列名 / 质量标记 / 缺值 / 中文数字。

每个用例造一份只有段落的 docx（无表格，强制走 LLM 抽取）+ 一份空模板 xlsx，
期望值在脚本里独立写死（不读系统自报 accuracy），逐格比对。

判定口径（面向用户）：
  - 数值：解析成 float 后比较，容差 1e-6；千分位、单位换算必须做对
  - 文本：去空白、统一全半角括号后比较（"38(PF)" 与 "38（PF）" 视为相同）
  - 缺值：源文没有的指标必须留空 —— 填了数字算错，比留空严重
  - 多出的行不算错，但会单独计数

    python scripts/llm_fill_bench.py [--tag before]
"""
import json
import os
import re
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
WORK = os.path.join(ROOT, ".workbuddy-ai", "outputs", "llm_bench")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
os.makedirs(WORK, exist_ok=True)

from fastapi.testclient import TestClient  # noqa: E402
import main as app_main  # noqa: E402
import docx  # noqa: E402
import openpyxl  # noqa: E402

PASSWORD = os.getenv("DOCFUSION_TEST_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("请先设置环境变量 DOCFUSION_TEST_PASSWORD")

CASES = [
    {
        "key": "1_千分位与单位",
        "text": [
            "2024 年杭州市地区生产总值 21,860.32 亿元，常住人口 1,252.2 万人。",
            "2024 年宁波市地区生产总值 18,147.5 亿元，常住人口 969.7 万人。",
        ],
        "headers": ["城市", "GDP总量（亿元）", "常住人口（万人）"],
        "expected": {
            "杭州市": {"GDP总量（亿元）": 21860.32, "常住人口（万人）": 1252.2},
            "宁波市": {"GDP总量（亿元）": 18147.5, "常住人口（万人）": 969.7},
        },
    },
    {
        "key": "2_同义列名",
        "text": [
            "2024 年苏州市实现地区生产总值 26727.0 亿元，年末常住人口 1295.8 万人。",
        ],
        "headers": ["城市", "GDP总量（亿元）", "常住人口（万人）"],
        "expected": {
            "苏州市": {"GDP总量（亿元）": 26727.0, "常住人口（万人）": 1295.8},
        },
    },
    {
        "key": "3_质量标记保留",
        "text": [
            "2024 年济南市空气质量综合指数 4.85，PM2.5 年均浓度 38(PF) 微克/立方米，优良天数 221 天。",
        ],
        "headers": ["城市", "PM2.5浓度（微克/立方米）", "优良天数（天）"],
        "expected": {
            "济南市": {"PM2.5浓度（微克/立方米）": "38(PF)", "优良天数（天）": 221},
        },
    },
    {
        "key": "4_缺值不许编",
        "text": [
            "2024 年青岛市地区生产总值 16719.5 亿元。",
        ],
        "headers": ["城市", "GDP总量（亿元）", "常住人口（万人）"],
        "expected": {
            "青岛市": {"GDP总量（亿元）": 16719.5, "常住人口（万人）": ""},
        },
    },
    {
        "key": "5_中文数字",
        "text": [
            "2024 年福建省常住人口约四千一百万，地区生产总值约五点四万亿元。",
        ],
        "headers": ["省份", "GDP总量（亿元）", "常住人口（万人）"],
        "expected": {
            "福建省": {"GDP总量（亿元）": 54000, "常住人口（万人）": 4100},
        },
    },
    {
        "key": "6_多句合并同实体",
        "text": [
            "2024 年苏州市常住人口 1295.8 万人。",
            "同年苏州市地区生产总值 26727.0 亿元，同比增长 5.1%。",
        ],
        "headers": ["城市", "GDP总量（亿元）", "常住人口（万人）"],
        "expected": {"苏州市": {"GDP总量（亿元）": 26727.0, "常住人口（万人）": 1295.8}},
    },
    {
        "key": "7_表头无单位",
        "text": ["2024 年无锡市地区生产总值 1.62 万亿元，常住人口 752 万人。"],
        "headers": ["城市", "GDP总量", "常住人口"],
        # 表头没写单位 → 按文中单位换算到基本单位（元/人），与 unit_normalize 的策略一致
        "expected": {"无锡市": {"GDP总量": 1620000000000, "常住人口": 7520000}},
    },
    {
        "key": "8_百分比干扰",
        "text": ["2024 年杭州市地区生产总值 21860.32 亿元，同比增长 5.2%，常住人口 1252.2 万人。"],
        "headers": ["城市", "GDP总量（亿元）", "常住人口（万人）"],
        "expected": {"杭州市": {"GDP总量（亿元）": 21860.32, "常住人口（万人）": 1252.2}},
    },
]

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _norm_text(s):
    s = str(s if s is not None else "").strip()
    s = s.replace("（", "(").replace("）", ")").replace("，", ",")
    return re.sub(r"\s+", "", s)


def _to_num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)
    s = _norm_text(v).replace(",", "")
    m = _NUM_RE.fullmatch(s)
    return float(m.group()) if m else None


def _build_docx(path, paragraphs):
    d = docx.Document()
    for p in paragraphs:
        d.add_paragraph(p)
    d.save(path)


def _build_template(path, headers):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    wb.save(path)


def _read_out(path):
    wb = openpyxl.load_workbook(path, data_only=True)
    ws = wb.worksheets[0]
    rows = [list(r) for r in ws.iter_rows(values_only=True)]
    wb.close()
    return rows


def run_case(c, h, case):
    src = os.path.join(WORK, f"{case['key']}_src.docx")
    tpl = os.path.join(WORK, f"{case['key']}_tpl.xlsx")
    _build_docx(src, case["text"])
    _build_template(tpl, case["headers"])

    with open(src, "rb") as fp:
        r = c.post("/api/documents/upload", files=[("files", (os.path.basename(src), fp, "application/octet-stream"))], headers=h)
    r.raise_for_status()
    ids = [x["file_id"] for x in r.json()["files"] if x.get("file_id")]
    for _ in range(60):
        lst = c.get("/api/documents/list?page=1&page_size=50", headers=h).json()
        st = {f["id"]: f["parse_status"] for f in lst["files"] if f["id"] in ids}
        if st and all(v == "done" for v in st.values()):
            break
        time.sleep(0.5)

    with open(tpl, "rb") as fp:
        r = c.post("/api/templates/upload", files={"file": (os.path.basename(tpl), fp, "application/octet-stream")},
                   data={"user_requirement": "把数据填入模板"}, headers=h)
    r.raise_for_status()
    tid = r.json()["template_id"]

    t0 = time.time()
    r = c.post(f"/api/templates/{tid}/fill", json={"source_file_ids": ids}, headers=h)
    cost = round(time.time() - t0, 1)
    if r.status_code != 200:
        return {"case": case["key"], "ok": False, "error": r.text[:200], "cost": cost}

    res = r.json()
    out_name = res.get("output_file") or ""
    # 结果文件走历史记录下载口（/api/templates/download/{filename}）
    r2 = c.get(f"/api/templates/download/{out_name}", headers=h)
    if r2.status_code != 200 and res.get("task_id"):
        r2 = c.get(f"/api/templates/history/{res['task_id']}/download", headers=h)
    out_path = os.path.join(WORK, f"{case['key']}_out.xlsx")
    if r2.status_code == 200:
        with open(out_path, "wb") as f:
            f.write(r2.content)
    else:
        return {"case": case["key"], "ok": False, "error": f"下载失败 {r2.status_code}", "cost": cost}

    rows = _read_out(out_path)
    header = [str(x).strip() if x is not None else "" for x in rows[0]] if rows else []
    idx = {name: i for i, name in enumerate(header)}

    checks, fails = 0, []

    def _ent_key(x):
        s = _norm_text(x)
        return re.sub(r"(特别行政区|自治区|自治州|省|市|县|区)$", "", s)

    for ent, cols in case["expected"].items():
        hit = None
        want = _ent_key(ent)
        for r_ in rows[1:]:
            if r_ and r_[0] is not None and _ent_key(r_[0]) == want:
                hit = r_
                break
        if hit is None:
            fails.append(f"缺少实体行 {ent}")
            checks += len(cols)
            continue
        for col, exp in cols.items():
            checks += 1
            if col not in idx:
                fails.append(f"{ent}.{col} 列不存在")
                continue
            got = hit[idx[col]]
            if isinstance(exp, (int, float)):
                g = _to_num(got)
                if g is None or abs(g - exp) > 1e-6:
                    fails.append(f"{ent}.{col} 期望 {exp} 实得 {got!r}")
            else:
                if _norm_text(got) != _norm_text(exp):
                    fails.append(f"{ent}.{col} 期望 {exp!r} 实得 {got!r}")

    return {
        "case": case["key"], "ok": not fails, "checks": checks,
        "fails": fails, "cost": cost, "rows": max(0, len(rows) - 1),
        "audit": res.get("audit"), "warnings": (res.get("warnings") or [])[:2],
    }


def run():
    tag = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--tag" else "run"
    c = TestClient(app_main.app)
    r = c.post("/api/auth/login", data={"username": os.getenv("DOCFUSION_TEST_USER", "admin"), "password": PASSWORD})
    r.raise_for_status()
    h = {"Authorization": "Bearer " + r.json()["access_token"]}

    results = []
    for case in CASES:
        res = run_case(c, h, case)
        results.append(res)
        mark = "PASS" if res.get("ok") else "FAIL"
        print(f"[{mark}] {res['case']} {res.get('cost')}s 格数={res.get('checks')}")
        for f in res.get("fails") or []:
            print(f"        {f}")

    total_cells = sum(r.get("checks") or 0 for r in results)
    ok_cells = sum((r.get("checks") or 0) - len(r.get("fails") or []) for r in results)
    print("=" * 60)
    print(f"用例通过 {sum(1 for r in results if r.get('ok'))}/{len(results)} | 逐格 {ok_cells}/{total_cells} "
          f"= {ok_cells / total_cells * 100:.2f}%" if total_cells else "无有效格")
    with open(os.path.join(WORK, f"summary_{tag}.json"), "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)


if __name__ == "__main__":
    run()
