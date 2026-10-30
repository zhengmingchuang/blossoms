import os
import time
import uuid
from pathlib import Path
from typing import List, Optional
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db, SessionLocal
from models import FileInfo, DocChunk, ExtractResult, TaskInfo, OperationLog, User, fmt_time
from config import UPLOAD_DIR
from routers.auth import get_current_user
from services.document_parser import parse_document
from services.vector_store import add_chunks, remove_file
from services.extractor import hybrid_extract, rule_extract

router = APIRouter(prefix="/api/documents", tags=["文档管理"])


def _unique_display_name(original_name: str, existing_names: set) -> str:
    """根据已有名称集合，为上传文件生成唯一显示名。
    重名时追加 (1)(2)... 区分，如 '报告.docx' -> '报告(1).docx'。
    """
    if original_name not in existing_names:
        return original_name
    stem = Path(original_name).stem
    suffix = Path(original_name).suffix
    idx = 1
    while True:
        candidate = f"{stem}({idx}){suffix}"
        if candidate not in existing_names:
            return candidate
        idx += 1


def _log(db, user, action, detail=""):
    db.add(OperationLog(user_id=user.id, username=user.username, action=action, detail=detail))
    db.commit()


def _fast_parse(file_id: int, file_path: str, task_id: int, upload_ts: float = 0):
    """只做本地解析+切片+规则提取，不调LLM，秒完成"""
    db = SessionLocal()
    t_start = upload_ts or time.time()
    try:
        fi = db.query(FileInfo).filter(FileInfo.id == file_id).first()
        task = db.query(TaskInfo).filter(TaskInfo.id == task_id).first()
        if not fi:
            return
        fi.parse_status = "parsing"
        if task:
            task.status = "running"
            task.start_time = datetime.now()
        db.commit()

        chunks = parse_document(file_path)
        for ch in chunks:
            db.add(DocChunk(file_id=file_id, chunk_index=ch["chunk_index"], chunk_text=ch["chunk_text"], chunk_type=ch["chunk_type"], source_position=ch["source_position"]))
        db.commit()
        add_chunks(file_id, fi.file_name, chunks, user_id=fi.upload_user)

        full = "\n\n".join(c["chunk_text"] for c in chunks)
        rule_fields = rule_extract(full)
        field_count = 0
        for k, v in rule_fields.items():
            values = v if isinstance(v, list) else [v]
            for vi, item in enumerate(values):
                val_str = str(item)
                best_pos = ""
                for ch in chunks:
                    if val_str in ch["chunk_text"]:
                        best_pos = ch.get("source_position", "")
                        break
                field_name = k if vi == 0 else f"{k}_{vi + 1}"
                db.add(ExtractResult(file_id=file_id, field_name=field_name, field_value=val_str, confidence=0.90, extract_method="rule", source_position=best_pos))
                field_count += 1
        db.commit()

        elapsed = round(time.time() - t_start, 2)
        fi.parse_status = "done"
        fi.parse_time = elapsed
        fi.chunk_count = len(chunks)
        fi.field_count = field_count
        if task:
            task.status = "done"
            task.elapsed = elapsed
            task.end_time = datetime.now()
            task.result_summary = f"解析{len(chunks)}个片段，规则提取{field_count}个字段"
        db.commit()
    except Exception as e:
        fi = db.query(FileInfo).filter(FileInfo.id == file_id).first()
        task = db.query(TaskInfo).filter(TaskInfo.id == task_id).first()
        if fi:
            fi.parse_status = "error"
        if task:
            task.status = "error"
            task.elapsed = round(time.time() - t_start, 2)
            task.end_time = datetime.now()
            task.result_summary = str(e)[:500]
        db.commit()
    finally:
        db.close()


async def _llm_extract(file_id: int, task_id: int, fields: Optional[List[str]] = None):
    """LLM深度提取，独立调用，可选传入 fields 指定提取字段"""
    db = SessionLocal()
    t0 = time.time()
    try:
        fi = db.query(FileInfo).filter(FileInfo.id == file_id).first()
        task = db.query(TaskInfo).filter(TaskInfo.id == task_id).first() if task_id else None
        if not fi:
            return
        if task:
            task.status = "running"
            db.commit()

        chunks = db.query(DocChunk).filter(DocChunk.file_id == file_id).order_by(DocChunk.chunk_index).all()
        full = "\n\n".join(c.chunk_text for c in chunks)

        extracted = await hybrid_extract(full[:15000], fields=fields)
        new_count = 0
        for k, v in extracted.items():
            if k.startswith("_"):
                continue
            existing = db.query(ExtractResult).filter(ExtractResult.file_id == file_id, ExtractResult.field_name == k).first()
            val_str = str(v)
            best_pos = ""
            for ch in chunks:
                if val_str in ch.chunk_text:
                    best_pos = ch.source_position or ""
                    break
            if existing:
                existing.field_value = val_str
                existing.confidence = 0.90
                existing.extract_method = "llm"
                existing.source_position = best_pos
            else:
                db.add(ExtractResult(file_id=file_id, field_name=k, field_value=val_str, confidence=0.85, extract_method="llm", source_position=best_pos))
                new_count += 1
        db.commit()

        total_fields = db.query(ExtractResult).filter(ExtractResult.file_id == file_id).count()
        fi.field_count = total_fields

        elapsed = round(time.time() - t0, 2)
        if task:
            task.status = "done"
            task.elapsed = elapsed
            task.end_time = datetime.now()
            task.result_summary = f"LLM提取完成，新增{new_count}个字段，共{total_fields}个"
        db.commit()
        return {"new_fields": new_count, "total_fields": total_fields, "elapsed": elapsed}
    except Exception as e:
        task = db.query(TaskInfo).filter(TaskInfo.id == task_id).first() if task_id else None
        if task:
            task.status = "error"
            task.elapsed = round(time.time() - t0, 2)
            task.end_time = datetime.now()
            task.result_summary = str(e)[:500]
        db.commit()
        raise
    finally:
        db.close()


@router.post("/upload")
async def upload(files: List[UploadFile] = File(...), bg: BackgroundTasks = BackgroundTasks(), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # 查询当前用户已有文件名，用于去重
    existing = db.query(FileInfo.file_name).filter(FileInfo.upload_user == user.id).all()
    used_names = {row[0] for row in existing}

    results = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in (".docx", ".doc", ".md", ".xlsx", ".xls", ".txt"):
            results.append({"file": f.filename, "status": "error", "message": f"不支持: {ext}"})
            continue
        display_name = _unique_display_name(f.filename, used_names)
        used_names.add(display_name)
        disk_name = f"{uuid.uuid4().hex[:8]}_{display_name}"
        path = UPLOAD_DIR / disk_name
        content = await f.read()
        with open(path, "wb") as fp:
            fp.write(content)
        fi = FileInfo(file_name=display_name, file_type=ext.lstrip("."), file_path=str(path), file_size=len(content), upload_user=user.id)
        db.add(fi)
        db.commit()
        db.refresh(fi)
        task = TaskInfo(task_type="parse", task_name=f"解析: {f.filename}", status="pending", user_id=user.id, related_id=fi.id)
        db.add(task)
        db.commit()
        db.refresh(task)
        upload_ts = time.time()
        bg.add_task(_fast_parse, fi.id, str(path), task.id, upload_ts)
        results.append({"file": f.filename, "file_id": fi.id, "status": "processing"})
    _log(db, user, "上传文档", f"上传{len(results)}个文件")
    return {"message": f"已上传 {len(results)} 个文件", "files": results}


class ExtractReq(BaseModel):
    fields: Optional[List[str]] = None


@router.post("/{fid}/extract")
async def extract(fid: int, body: Optional[ExtractReq] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """手动触发LLM深度提取，可选传入 fields 指定要提取的字段列表"""
    fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user.id).first()
    if not fi:
        raise HTTPException(404)
    if fi.parse_status != "done":
        raise HTTPException(400, "请先等待文档解析完成")
    task = TaskInfo(task_type="extract", task_name=f"LLM提取: {fi.file_name}", status="pending", user_id=user.id, related_id=fi.id)
    db.add(task)
    db.commit()
    db.refresh(task)
    user_fields = body.fields if body and body.fields else None
    result = await _llm_extract(fid, task.id, fields=user_fields)
    _log(db, user, "LLM提取", fi.file_name)
    return {"message": "LLM提取完成", **result}


@router.post("/{fid}/extract-all")
async def extract_all(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """批量LLM提取所有已解析文件"""
    files = db.query(FileInfo).filter(FileInfo.upload_user == user.id, FileInfo.parse_status == "done").all()
    results = []
    for fi in files:
        try:
            task = TaskInfo(task_type="extract", task_name=f"LLM提取: {fi.file_name}", status="pending", user_id=user.id, related_id=fi.id)
            db.add(task)
            db.commit()
            db.refresh(task)
            r = await _llm_extract(fi.id, task.id)
            results.append({"file_id": fi.id, "file_name": fi.file_name, "status": "done", **r})
        except Exception as e:
            results.append({"file_id": fi.id, "file_name": fi.file_name, "status": "error", "error": str(e)[:200]})
    return {"message": f"批量提取完成", "total": len(results), "results": results}


@router.get("/list")
def list_docs(page: int = 1, page_size: int = 20, file_type: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(FileInfo).filter(FileInfo.upload_user == user.id)
    if file_type:
        q = q.filter(FileInfo.file_type == file_type)
    total = q.count()
    files = q.order_by(FileInfo.upload_time.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "files": [
        {"id": f.id, "file_name": f.file_name, "file_type": f.file_type, "file_size": f.file_size,
         "parse_status": f.parse_status, "parse_time": f.parse_time or 0,
         "chunk_count": f.chunk_count or 0, "field_count": f.field_count or 0,
         "upload_time": fmt_time(f.upload_time)} for f in files
    ]}


@router.get("/{fid}/download")
def download(fid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """下载原始上传文件"""
    fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user.id).first()
    if not fi:
        raise HTTPException(404, "文件不存在")
    if not fi.file_path or not os.path.exists(fi.file_path):
        raise HTTPException(404, "源文件已不存在或已被清理")
    return FileResponse(
        path=fi.file_path,
        filename=fi.file_name,
        media_type="application/octet-stream",
    )


@router.get("/{fid}")
def detail(fid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user.id).first()
    if not fi:
        raise HTTPException(404, "文件不存在")
    chunks = db.query(DocChunk).filter(DocChunk.file_id == fid).order_by(DocChunk.chunk_index).all()
    exts = db.query(ExtractResult).filter(ExtractResult.file_id == fid).all()
    return {
        "file": {"id": fi.id, "file_name": fi.file_name, "file_type": fi.file_type, "file_size": fi.file_size,
                 "parse_status": fi.parse_status, "parse_time": fi.parse_time or 0,
                 "chunk_count": fi.chunk_count or 0, "field_count": fi.field_count or 0,
                 "upload_time": fmt_time(fi.upload_time)},
        "chunks": [{"id": c.id, "chunk_index": c.chunk_index, "chunk_text": c.chunk_text, "chunk_type": c.chunk_type, "source_position": c.source_position} for c in chunks],
        "extractions": [{"id": e.id, "field_name": e.field_name, "field_value": e.field_value, "confidence": e.confidence or 0, "extract_method": e.extract_method, "source_position": e.source_position or ""} for e in exts],
    }


@router.delete("/{fid}")
def delete(fid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user.id).first()
    if not fi:
        raise HTTPException(404)
    if os.path.exists(fi.file_path):
        os.remove(fi.file_path)
    remove_file(fid)
    db.query(ExtractResult).filter(ExtractResult.file_id == fid).delete()
    db.query(DocChunk).filter(DocChunk.file_id == fid).delete()
    db.query(TaskInfo).filter(TaskInfo.related_id == fid, TaskInfo.task_type.in_(["parse", "reparse", "extract"])).delete(synchronize_session=False)
    db.delete(fi)
    db.commit()
    _log(db, user, "删除文档", fi.file_name)
    return {"message": "已删除"}


class BatchDeleteReq(BaseModel):
    ids: List[int]


@router.post("/batch-delete")
def batch_delete(req: BatchDeleteReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    files = db.query(FileInfo).filter(FileInfo.id.in_(req.ids), FileInfo.upload_user == user.id).all()
    names = []
    for fi in files:
        if os.path.exists(fi.file_path):
            os.remove(fi.file_path)
        remove_file(fi.id)
        db.query(ExtractResult).filter(ExtractResult.file_id == fi.id).delete()
        db.query(DocChunk).filter(DocChunk.file_id == fi.id).delete()
        db.query(TaskInfo).filter(TaskInfo.related_id == fi.id, TaskInfo.task_type.in_(["parse", "reparse", "extract"])).delete(synchronize_session=False)
        names.append(fi.file_name)
        db.delete(fi)
    db.commit()
    _log(db, user, "批量删除", f"删除{len(names)}个文件")
    return {"message": f"已删除 {len(names)} 个文件", "deleted": len(names)}


@router.post("/{fid}/reparse")
async def reparse(fid: int, bg: BackgroundTasks, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user.id).first()
    if not fi:
        raise HTTPException(404)
    db.query(ExtractResult).filter(ExtractResult.file_id == fid).delete()
    db.query(DocChunk).filter(DocChunk.file_id == fid).delete()
    remove_file(fid)
    fi.parse_status = "pending"
    fi.parse_time = 0
    fi.chunk_count = 0
    fi.field_count = 0
    db.commit()
    task = TaskInfo(task_type="reparse", task_name=f"重新解析: {fi.file_name}", status="pending", user_id=user.id, related_id=fi.id)
    db.add(task)
    db.commit()
    db.refresh(task)
    reparse_ts = time.time()
    bg.add_task(_fast_parse, fi.id, fi.file_path, task.id, reparse_ts)
    _log(db, user, "重新解析", fi.file_name)
    return {"message": "重新解析已提交"}
