"""图片理解端到端验证：上传图片 → 对话问答 → 核对模型读出的数值。

走真实接口（TestClient 驱动 FastAPI，不 mock LLM），只做「发请求 + 比对答案」，
不做结果推断。失败即失败，输出原始回答供人工复核。

    python scripts/e2e_image_qa.py
"""
import os
import sys
import time

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from fastapi.testclient import TestClient  # noqa: E402
import main  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

PASSWORD = os.getenv("DOCFUSION_TEST_PASSWORD", "")
if not PASSWORD:
    raise SystemExit("请先设置环境变量 DOCFUSION_TEST_PASSWORD")

# 造一张只有图像能提供信息的图：答案 27695.10 不出现在任何文本里
IMG = os.path.join(ROOT, ".workbuddy-ai", "tmp", "e2e_vision.png")
os.makedirs(os.path.dirname(IMG), exist_ok=True)
img = Image.new("RGB", (560, 200), "white")
d = ImageDraw.Draw(img)
d.text((20, 40), "City: Suzhou", fill="black")
d.text((20, 90), "GDP: 27695.10 (100M CNY)", fill="black")
d.text((20, 140), "Population: 1295.80 (10K)", fill="black")
img.save(IMG)

EXPECT = "27695"


def main_flow():
    c = TestClient(main.app)
    r = c.post("/api/auth/login", data={"username": os.getenv("DOCFUSION_TEST_USER", "admin"), "password": PASSWORD})
    assert r.status_code == 200, r.text[:200]
    tok = r.json().get("access_token")
    H = {"Authorization": f"Bearer {tok}"}

    t0 = time.time()
    with open(IMG, "rb") as fh:
        # 图片走聊天上传口（文档上传口只收文档，图片在 /api/chat/upload）
        r = c.post("/api/chat/upload", files={"file": ("e2e_vision.png", fh, "image/png")}, headers=H)
    print(f"[上传] status={r.status_code} {time.time()-t0:.1f}s")
    assert r.status_code == 200, r.text[:300]
    info = r.json()
    fid = info.get("id")
    print(f"[上传] file_id={fid} 类型={info.get('type')} 状态={info.get('status')}")
    assert fid, f"未取到 file_id: {info}"

    t0 = time.time()
    r = c.post("/api/chat/ask", json={"message": "这张图里的 GDP 是多少？只回答数字。", "file_id": fid}, headers=H)
    dt = time.time() - t0
    print(f"[问答] status={r.status_code} {dt:.1f}s")
    assert r.status_code == 200, r.text[:300]
    ans = r.json().get("answer", "")
    print(f"[问答] 回答片段: {ans[:200]!r}")

    ok = EXPECT in ans.replace(",", "")
    print(f"[判定] 命中 {EXPECT}: {ok}")
    return ok, dt


if __name__ == "__main__":
    ok, dt = main_flow()
    print("=" * 50)
    print(f"结果: {'PASS' if ok else 'FAIL'}（{dt:.1f}s）")
    sys.exit(0 if ok else 1)
