# -*- coding: utf-8 -*-
"""对抗测试：fill_template_smart 的路径穿越（上一轮只做代码审查，未跑通攻击链）。

背景：`services/doc_editor.py::execute_tool` 里 `fill_template_smart` 的输出文件名
来自模型工具入参（即不可信输入）。上一轮安全审计记录该入口是「实测打通的攻击链」
的必经点，但当时没有实际跑过。本脚本用真实请求 + 真实 xlsx，逐个 payload 验证落点。

判定标准（不看代码看落点）：
  - 文件必须落在 `backend/uploads/<user_id>/` 内；
  - `backend/`、项目根、`uploads/` 根、系统临时目录都不得出现被写出的文件。

    python scripts/adversarial_smart_fill.py
"""
import asyncio
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = Path(SCRIPTS_DIR).parent
BACKEND = ROOT / "backend"

TMP_DB = ROOT / ".workbuddy-ai" / "tmp" / "adv_smart_fill.db"
TMP_DB.parent.mkdir(parents=True, exist_ok=True)
if TMP_DB.exists():
    TMP_DB.unlink()
os.environ["DATABASE_URL"] = f"sqlite:///{TMP_DB.as_posix()}"

sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from database import Base, engine, SessionLocal  # noqa: E402
import models  # noqa: E402
from models import FileInfo, User  # noqa: E402
from services.doc_editor import execute_tool  # noqa: E402

PAYLOADS = [
    "../../evil.xlsx",
    "..\\..\\evil.xlsx",
    "/tmp/evil_abs.xlsx",
    "....//....//evil_dotdot.xlsx",
    "a/b/../../../evil_nested.xlsx",
    "正常文件名.xlsx",
]


def _seed() -> int:
    Base.metadata.create_all(bind=engine)
    db = SessionLocal()
    try:
        u = User(username="adv_user", password="x", role="user")
        db.add(u)
        db.commit()
        db.refresh(u)
        uid = u.id
        tpl = BACKEND / ".." / "samples" / "template" / "区域经济指标汇总表.xlsx"
        src = BACKEND / ".." / "samples" / "source" / "2025年三市主要经济指标.xlsx"
        for p, name in ((tpl, "区域经济指标汇总表.xlsx"), (src, "2025年三市主要经济指标.xlsx")):
            db.add(FileInfo(
                file_name=name, file_type="xlsx", file_path=str(p.resolve()),
                file_size=p.stat().st_size, upload_user=uid, parse_status="done",
            ))
        db.commit()
        rows = db.query(FileInfo).all()
        return uid, rows[0].id, rows[1].id
    finally:
        db.close()


def main() -> int:
    uid, tpl_id, data_id = _seed()
    upload_dir = (BACKEND / "uploads" / str(uid)).resolve()
    print(f"用户 {uid}，输出目录应为 {upload_dir}\n")

    outside_probes = [
        BACKEND / "evil.xlsx",
        BACKEND / "uploads" / "evil.xlsx",
        ROOT / "evil.xlsx",
        Path(tempfile.gettempdir()) / "evil_abs.xlsx",
    ]
    for p in outside_probes:
        if p.exists():
            p.unlink()

    results = []
    for payload in PAYLOADS:
        out = asyncio.run(execute_tool(
            "fill_template_smart",
            {"template_file_id": tpl_id, "data_file_id": data_id, "output_name": payload},
            uid,
        ))
        try:
            resp = json.loads(out)
        except json.JSONDecodeError:
            resp = {"raw": out[:120]}
        err = resp.get("error")
        produced = None
        if resp.get("ok"):
            row = SessionLocal()
            try:
                fi = row.query(FileInfo).filter(FileInfo.id == resp["file_id"]).first()
                produced = Path(fi.file_path).resolve() if fi else None
            finally:
                row.close()
        inside = bool(produced and str(produced).startswith(str(upload_dir) + os.sep))
        results.append({
            "payload": payload,
            "ok": resp.get("ok", False),
            "error": err,
            "落点": str(produced) if produced else "-",
            "在用户目录内": inside,
        })
        print(f"payload={payload!r}")
        print(f"  ok={resp.get('ok')} error={err}")
        print(f"  落点={produced}  在目录内={inside}\n")

    leaked = [p for p in outside_probes if p.exists()]
    print("=" * 60)
    ok_all = all(r["在用户目录内"] for r in results if r["ok"])
    print(f"全部成功写出且都在用户目录内: {ok_all}")
    print(f"越界文件: {[str(p) for p in leaked] or '无'}")

    # 清理本次测试产生的文件（只删本脚本自己造出来的）
    for r in results:
        if r["ok"] and r["落点"] != "-":
            try:
                Path(r["落点"]).unlink()
            except OSError:
                pass
    if upload_dir.exists() and not any(upload_dir.iterdir()):
        shutil.rmtree(upload_dir, ignore_errors=True)
    return 0 if ok_all and not leaked else 1


if __name__ == "__main__":
    raise SystemExit(main())
