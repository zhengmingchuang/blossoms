"""大表深度行检索验证：问一个只出现在第 6684–7043 行的国家。

旧实现只把「前 50 行 + 末 10 行」写进 doc_chunk，中段的数据在聊天里查不到，
模型只能回答「文档里没有」。这里用真实文件 + 真实数值核对：
  Costa Rica / 2020-08-31 / 检测 1517 / 病例 41287 / 死亡 436
期望值由脚本直接读源文件算出，不依赖被测系统。

    python scripts/e2e_bigtable_qa.py
"""
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "backend")
SRC = os.path.join(ROOT, "docs", "demo测试集", "包含模板文件", "COVID-19数据集", "COVID-19全球数据集（节选）.xlsx")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from fastapi.testclient import TestClient  # noqa: E402
import main as app_main  # noqa: E402
import openpyxl  # noqa: E402

PASSWORD = os.getenv("DOCFUSION_TEST_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("请先设置环境变量 DOCFUSION_TEST_PASSWORD")

TARGET, DATE = "Costa Rica", "2020-08-31"


def expected_values():
    wb = openpyxl.load_workbook(SRC, read_only=True, data_only=True)
    ws = wb["Data"]
    for r in ws.iter_rows(min_row=2, values_only=True):
        if r[0] == TARGET and str(r[11])[:10] == DATE:
            wb.close()
            return {"检测": r[12], "病例": r[13], "死亡": r[14]}
    wb.close()
    return {}


def main():
    exp = expected_values()
    print(f"源文件期望值: {TARGET} {DATE} -> {exp}")
    assert exp, "源文件里找不到目标行"

    c = TestClient(app_main.app)
    r = c.post("/api/auth/login", data={"username": os.getenv("DOCFUSION_TEST_USER", "admin"), "password": PASSWORD})
    r.raise_for_status()
    h = {"Authorization": "Bearer " + r.json()["access_token"]}

    t0 = time.time()
    with open(SRC, "rb") as fp:
        r = c.post("/api/chat/upload", files={"file": (os.path.basename(SRC), fp, "application/octet-stream")}, headers=h)
    r.raise_for_status()
    fid = r.json()["id"]

    waited = 0.0
    while waited < 120:
        lst = c.get("/api/documents/list?page=1&page_size=50", headers=h).json()
        st = next((f for f in lst["files"] if f["id"] == fid), None)
        if st and st.get("parse_status") == "done":
            break
        time.sleep(1)
        waited += 1
    print(f"[解析] {time.time()-t0:.1f}s 状态={st.get('parse_status') if st else '?'} "
          f"chunks={st.get('chunk_count') if st else '?'}")

    q = f"{TARGET} 在 {DATE} 的检测数、病例数和死亡数分别是多少？只回答数字。"
    t1 = time.time()
    r = c.post("/api/chat/ask", json={"message": q, "file_id": fid}, headers=h)
    cost = time.time() - t1
    r.raise_for_status()
    ans = r.json().get("answer") or ""
    flat = ans.replace(",", "").replace("，", "")
    print(f"[问答] {cost:.1f}s 回答: {ans[:220]!r}")

    ok_all = True
    for k, v in exp.items():
        if v is None:
            continue
        hit = str(int(v)) in flat
        ok_all = ok_all and hit
        print(f"  [{'PASS' if hit else 'FAIL'}] {k}={v} 出现在回答中")

    print("=" * 56)
    print(f"结果: {'PASS' if ok_all else 'FAIL'}")
    return 0 if ok_all else 1


if __name__ == "__main__":
    sys.exit(main())
