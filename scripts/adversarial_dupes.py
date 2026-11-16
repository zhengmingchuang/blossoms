"""同名文件堆积 / 索引失效的对抗验证。

覆盖四件事（都是「不报错但结果错」的类型）：
1. 同名文件反复生成，file_info 不能堆出多条同路径记录；
2. edit_document 改完内容后，索引必须换成新文本（否则追问拿到旧内容）；
3. 「已修改的文档」返回的条目带稳定 key，同一文件只出现一次；
4. 聊天文件选择器同一路径只列一次。

说明：直接调用 execute_tool / _index_generated_file / _collect_edited_files，
不经过 LLM。这几个函数就是 _run_tool 内部的真实路径，LLM 只是决定「调哪个工具」，
不参与这些落库与索引逻辑。这样即使模型额度用完也能验证。

用法：python scripts/adversarial_dupes.py
"""
import os
import sys
import uuid
import sqlite3
import asyncio

import httpx

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BACKEND = os.path.join(ROOT, "backend")
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

BASE = "http://127.0.0.1:8000"
USER = "admin"
PWD = os.getenv("DOCFUSION_TEST_PASSWORD", "")

results = []


def rec(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def db():
    return sqlite3.connect(os.path.join(BACKEND, "app.db"))


def _control():
    """阳性对照：把写入函数换回「每次都新插一行」的旧实现，确认测试能抓到这个缺陷。

    没有这一步，「13/13 通过」可能只是因为断言写得太松。
    """
    import json
    from datetime import datetime
    from database import SessionLocal
    from models import FileInfo
    import services.doc_editor as de

    def _old_insert(db, user_id, file_path, file_name, file_type):
        fi = FileInfo(
            file_name=file_name, file_type=file_type, file_path=os.path.abspath(file_path),
            file_size=os.path.getsize(file_path), upload_user=user_id,
            upload_time=datetime.now(), parse_status="completed",
            chunk_count=0, field_count=0,
        )
        db.add(fi)
        db.commit()
        db.refresh(fi)
        return fi

    orig = de._upsert_generated_file
    de._upsert_generated_file = _old_insert
    fname = f"ctrl_{uuid.uuid4().hex[:6]}.xlsx"
    try:
        for _ in range(2):
            asyncio.run(de.execute_tool(
                "create_spreadsheet",
                {"file_name": fname, "sheet_name": "Sheet1",
                 "data": [["城市", "GDP"], ["甲市", 1]]},
                1,
            ))
    finally:
        de._upsert_generated_file = orig
    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM file_info WHERE file_name=? AND upload_user=1",
                        (fname,)).fetchone()[0]
    print(f"[对照] 旧实现（无条件 insert）生成两次 -> 库里 {n} 条同名记录")
    ok = n == 2
    rec("阳性对照：旧实现确实会堆出重复记录", ok, f"{n} 条")
    return ok


def main():
    if "--control" in sys.argv:
        return 0 if _control() else 1

    from database import SessionLocal
    from models import FileInfo, DocChunk
    from services.doc_editor import execute_tool
    from routers.chat import _collect_edited_files, _index_generated_file

    tag = uuid.uuid4().hex[:6]
    fname = f"dupe_{tag}.xlsx"
    data = [["城市", "GDP"], ["甲市", 100], ["乙市", 200]]

    # ---------- 1. 同名表格生成两次 ----------
    r1 = asyncio.run(execute_tool("create_spreadsheet",
                                  {"file_name": fname, "sheet_name": "Sheet1", "data": data}, 1))
    _index_generated_file("create_spreadsheet", r1)
    r2 = asyncio.run(execute_tool("create_spreadsheet",
                                  {"file_name": fname, "sheet_name": "Sheet1",
                                   "data": [["城市", "GDP"], ["丙市", 300], ["丁市", 400]]}, 1))
    _index_generated_file("create_spreadsheet", r2)

    import json
    j1, j2 = json.loads(r1), json.loads(r2)
    rec("两次生成都返回 file_id", bool(j1.get("file_id")) and bool(j2.get("file_id")),
        f"id1={j1.get('file_id')} id2={j2.get('file_id')}")
    rec("同名文件复用同一条记录（不堆行）", j1.get("file_id") == j2.get("file_id"),
        f"{j1.get('file_id')} vs {j2.get('file_id')}")

    with db() as con:
        n = con.execute("SELECT COUNT(*) FROM file_info WHERE file_name=? AND upload_user=1",
                        (fname,)).fetchone()[0]
    rec("库里该文件名只有 1 条记录", n == 1, f"实际 {n} 条")

    fid = j1.get("file_id")

    # ---------- 2. 同名 docx 生成两次 ----------
    dname = f"dupe_{tag}.docx"
    spec = {"file_name": dname, "title": f"测试报告{tag}",
            "sections": [{"heading": "一、结论", "paragraphs": ["第一版内容"]}], "file_id": None}
    d1 = json.loads(asyncio.run(execute_tool("create_document", spec, 1)))
    _index_generated_file("create_document", json.dumps(d1, ensure_ascii=False))
    spec2 = dict(spec, sections=[{"heading": "一、结论", "paragraphs": ["第二版内容"]}])
    d2 = json.loads(asyncio.run(execute_tool("create_document", spec2, 1)))
    _index_generated_file("create_document", json.dumps(d2, ensure_ascii=False))
    rec("同名 docx 复用同一条记录", d1.get("file_id") == d2.get("file_id"),
        f"{d1.get('file_id')} vs {d2.get('file_id')}")

    # ---------- 3. 编辑后索引必须更新 ----------
    did = d1.get("file_id")
    with db() as con:
        before = con.execute("SELECT COUNT(*) FROM doc_chunk WHERE file_id=?", (did,)).fetchone()[0]
        txt_before = " ".join(r[0] or "" for r in con.execute(
            "SELECT chunk_text FROM doc_chunk WHERE file_id=?", (did,)))
    rec("编辑前已有索引", before > 0, f"chunk={before}")
    rec("编辑前索引是第二版内容", "第二版内容" in txt_before, txt_before[:40].replace("\n", " "))

    e = json.loads(asyncio.run(execute_tool("edit_document", {
        "file_id": did,
        "operations": [{"action": "replace", "target": "第二版内容", "value": "第三版内容"}],
    }, 1)))
    _index_generated_file("edit_document", json.dumps(e, ensure_ascii=False), fid_override=did)
    with db() as con:
        after = con.execute("SELECT COUNT(*) FROM doc_chunk WHERE file_id=?", (did,)).fetchone()[0]
        txt_after = " ".join(r[0] or "" for r in con.execute(
            "SELECT chunk_text FROM doc_chunk WHERE file_id=?", (did,)))
    rec("编辑后索引已重建", after > 0, f"chunk={after}")
    rec("索引已换成第三版内容", "第三版内容" in txt_after, txt_after[:50].replace("\n", " "))
    rec("索引里不再残留第二版内容", "第二版内容" not in txt_after, txt_after[:50].replace("\n", " "))

    # ---------- 4. 已修改文档去重 ----------
    s = SessionLocal()
    try:
        fi = s.query(FileInfo).filter(FileInfo.id == fid).first()
        path = fi.file_path
        acts = [
            {"tool": "create_spreadsheet", "input": {"file_name": fname},
             "result": json.dumps({"file_id": fid})},
            {"tool": "create_spreadsheet", "input": {"file_name": fname},
             "result": json.dumps({"file_id": fid})},
            {"tool": "edit_document", "input": {"file_id": fid}, "result": "{}"},
        ]
        ef = _collect_edited_files(s, acts)
    finally:
        s.close()
    rec("同一文件多次操作只返回一条", len(ef) == 1, str([x["name"] for x in ef]))
    rec("条目带稳定去重 key（路径）", bool(ef and ef[0].get("key")) and ef[0]["key"] == path.lower(),
        str(ef[0].get("key")) if ef else "无")

    # ---------- 5. 聊天文件选择器不重复 ----------
    if PWD:
        with httpx.Client(base_url=BASE, timeout=60, trust_env=False) as c:
            r = c.post("/api/auth/login", data={"username": USER, "password": PWD})
            h = {"Authorization": "Bearer " + r.json()["access_token"]}
            fl = c.get("/api/chat/files", headers=h).json()
        hits = [x for x in fl if x.get("name") == fname]
        rec("选择器里同名文件只列一次", len(hits) == 1, f"{len(hits)} 条")
        # 库里已无同路径重复行（清理脚本跑过），这里再确认一次口径
        with db() as con:
            dup = con.execute("""
                SELECT COUNT(*) FROM (SELECT file_path FROM file_info
                WHERE file_path IS NOT NULL AND file_path<>''
                GROUP BY upload_user, file_path HAVING COUNT(*)>1)""").fetchone()[0]
        rec("库里不存在同路径重复记录", dup == 0, f"{dup} 组")
    else:
        rec("选择器去重（跳过：未提供口令）", True, "HTTP 部分未跑")

    ok = sum(1 for _, o, _ in results if o)
    print(f"\n=== 通过 {ok}/{len(results)} ===")
    return 0 if ok == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
