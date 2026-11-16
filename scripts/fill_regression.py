"""填表链路回归测试：用进程内 ASGI 客户端跑通
「上传源文档 → 上传模板 → 填报 → 下载结果」，并把结果与
samples/expected/期望结果.md 逐格比对。

不启动 HTTP 服务，因此不受端口监听限制，可在 CI 里直接跑。

    python scripts/fill_regression.py

注意：叙事型 docx 作为唯一数据源时会走 LLM 抽取，需要可用的
LLM_API_KEY；结构化 xlsx 路径不依赖 LLM。
"""
import io
import json
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
OUT_DIR = os.path.join(ROOT, ".workbuddy-ai", "outputs")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)  # 让相对路径配置（uploads/exports）落在 backend 下

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402


# 测试凭据只从环境变量读取，源码里不留明文（仓库公开等于口令公开）。
TEST_USER = os.getenv("DOCFUSION_TEST_USER", "admin")
TEST_PASSWORD = os.getenv("DOCFUSION_TEST_PASSWORD", "")
if not TEST_PASSWORD:
    raise SystemExit("请先设置环境变量 DOCFUSION_TEST_PASSWORD（管理员口令）再运行本脚本；否则会以空口令调用接口")


SRC = [
    os.path.join(ROOT, "samples", "source", "2025年临江市国民经济和社会发展统计公报（节选）.docx"),
    os.path.join(ROOT, "samples", "source", "2025年三市主要经济指标.xlsx"),
]
TPL_XLSX = os.path.join(ROOT, "samples", "template", "区域经济指标汇总表.xlsx")
TPL_DOCX = os.path.join(ROOT, "samples", "template", "区域经济运行情况申报表.docx")

# 期望值（见 samples/expected/期望结果.md），列顺序：GDP / 增长率 / 常住人口 / 规上工业增加值
EXPECTED = {
    "临江": [3482.65, 5.4, 268.4, 1126.30],
    "云溪": [2057.18, 4.1, 156.7, 623.45],
    "青川": [1486.92, 6.2, 98.3, 402.77],
}
COLS = ["GDP", "增长率", "常住人口", "规上工业"]


def login(c):
    r = c.post("/api/auth/login", data={"username": TEST_USER, "password": TEST_PASSWORD})
    r.raise_for_status()
    return {"Authorization": "Bearer " + r.json()["access_token"]}


def upload_sources(c, h):
    files = [
        ("files", (os.path.basename(p), open(p, "rb"), "application/octet-stream")) for p in SRC
    ]
    r = c.post("/api/documents/upload", files=files, headers=h)
    r.raise_for_status()
    ids = [x["file_id"] for x in r.json()["files"] if x.get("file_id")]
    st = {}
    for _ in range(60):
        lst = c.get("/api/documents/list?page=1&page_size=50", headers=h).json()
        st = {f["id"]: f["parse_status"] for f in lst["files"] if f["id"] in ids}
        if len(st) == len(ids) and all(v == "done" for v in st.values()):
            break
        time.sleep(1)
    return ids, st


def upload_template(c, h, path):
    with open(path, "rb") as fp:
        r = c.post(
            "/api/templates/upload",
            files={"file": (os.path.basename(path), fp, "application/octet-stream")},
            data={"user_requirement": ""},
            headers=h,
        )
    r.raise_for_status()
    return r.json()


def num(v):
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


def verify(rows, label):
    """rows: list[list]，自动定位含「城市」的表头行，逐格比对。"""
    hdr_i = next(
        (i for i, r in enumerate(rows) if r and any(c and "城市" in str(c) for c in r)), None
    )
    if hdr_i is None:
        print(f"{label}: 未找到表头行（含「城市」），实际行数 {len(rows)}")
        return 0, len(EXPECTED) * len(COLS), ["表头缺失"]
    hdr = [str(c).strip() if c else "" for c in rows[hdr_i]]
    idx = [next((i for i, h in enumerate(hdr) if k in h), None) for k in COLS]
    ok = tot = 0
    bad = []
    for city, vals in EXPECTED.items():
        row = next((r for r in rows[hdr_i + 1 :] if r and r[0] and city in str(r[0])), None)
        tot += len(vals)
        if row is None:
            bad.append(f"{city}: 整行缺失")
            continue
        for k, i, exp in zip(COLS, idx, vals):
            got = num(row[i]) if i is not None and i < len(row) else None
            if got is not None and abs(got - exp) < 0.011:
                ok += 1
            else:
                bad.append(f"{city}/{k}: got={row[i] if i is not None else None!r} exp={exp}")
    status = "PASS" if ok == tot else "FAIL"
    print(f"[{status}] {label}: {ok}/{tot}" + ("" if not bad else " | " + "; ".join(bad)))
    print(f"        表头 {hdr}")
    for r in rows[hdr_i + 1 :]:
        print(f"        {list(r)}")
    return ok, tot, bad


def main_():
    os.makedirs(OUT_DIR, exist_ok=True)
    summary = {}
    with TestClient(main.app) as c:
        h = login(c)
        ids, st = upload_sources(c, h)
        print(f"[upload] file_ids={ids} parse={st}")

        for tpl_path, kind in ((TPL_XLSX, "xlsx"), (TPL_DOCX, "docx")):
            info = upload_template(c, h, tpl_path)
            print(f"\n[tpl:{kind}] id={info['template_id']} mode={info['mode']} cols={info.get('columns')}")
            t0 = time.time()
            r = c.post(
                f"/api/templates/{info['template_id']}/fill",
                json={"source_file_ids": ids},
                headers=h,
            )
            cost = round(time.time() - t0, 2)
            if r.status_code != 200:
                print(f"[fill:{kind}] HTTP {r.status_code} {r.text[:400]}")
                summary[kind] = {"error": r.text[:200]}
                continue
            res = r.json()
            print(
                f"[fill:{kind}] filled={res.get('filled_count')}/{res.get('field_count')} "
                f"rows={res.get('total_rows')} accuracy={res.get('accuracy')} "
                f"source={res.get('source_file')} {cost}s"
            )
            blob = c.get(f"/api/templates/history/{res['task_id']}/download", headers=h)
            if blob.status_code != 200:
                print(f"[download:{kind}] HTTP {blob.status_code}")
                summary[kind] = {"error": "download failed"}
                continue
            out_path = os.path.join(OUT_DIR, f"fill_result_{kind}.{kind}")
            with open(out_path, "wb") as fp:
                fp.write(blob.content)

            if kind == "xlsx":
                from openpyxl import load_workbook

                ws = load_workbook(io.BytesIO(blob.content), data_only=True).active
                ok, tot, bad = verify([list(r) for r in ws.iter_rows(values_only=True)], "xlsx 模板")
            else:
                import docx

                d = docx.Document(io.BytesIO(blob.content))
                ok = tot = 0
                bad = ["无表格"]
                for i, t in enumerate(d.tables):
                    o, tt, b = verify(
                        [[cell.text.strip() for cell in row.cells] for row in t.rows],
                        f"docx 模板 表{i}",
                    )
                    if o > ok:
                        ok, tot, bad = o, tt, b
            summary[kind] = {
                "ok": ok,
                "total": tot,
                "accuracy": res.get("accuracy"),
                "elapsed": cost,
                "output": out_path,
                "bad": bad,
            }

    print("\n===== 汇总 =====")
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if all(v.get("ok") == v.get("total") for v in summary.values() if "ok" in v) else 1


if __name__ == "__main__":
    sys.exit(main_())
