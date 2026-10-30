from typing import Optional, List
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models import ExtractResult, FileInfo, DocChunk, User, fmt_time
from routers.auth import get_current_user
from services.fusion import fuse

router = APIRouter(prefix="/api/extraction", tags=["信息提取"])


class UpdateReq(BaseModel):
    field_value: str


class BatchDeleteReq(BaseModel):
    ids: List[int]


@router.get("/results")
def get_results(file_id: Optional[int] = None, field_name: Optional[str] = None, page: int = 1, page_size: int = 50, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    ufiles = db.query(FileInfo).filter(FileInfo.upload_user == user.id).all()
    uf_map = {f.id: f.file_name for f in ufiles}
    q = db.query(ExtractResult).filter(ExtractResult.file_id.in_(uf_map.keys()))
    if file_id:
        q = q.filter(ExtractResult.file_id == file_id)
    if field_name:
        q = q.filter(ExtractResult.field_name.like(f"%{field_name}%"))
    total = q.count()
    rs = q.order_by(ExtractResult.id.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "page_size": page_size, "results": [
        {"id": r.id, "file_id": r.file_id, "file_name": uf_map.get(r.file_id, ""),
         "field_name": r.field_name, "field_value": r.field_value,
         "confidence": r.confidence or 0, "extract_method": r.extract_method or "",
         "source_position": r.source_position or "",
         "create_time": fmt_time(r.create_time)} for r in rs
    ]}


@router.get("/fused")
def get_fused(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    files = db.query(FileInfo).filter(FileInfo.upload_user == user.id).all()
    exts = []
    for f in files:
        rs = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).all()
        if rs:
            exts.append({
                "file_id": f.id, "file_name": f.file_name,
                "source_type": "table" if f.file_type in ("xlsx", "xls") else "paragraph",
                "fields": {r.field_name: r.field_value for r in rs}
            })
    fused = fuse(exts)
    return {"total": len(fused), "source_files": len(files), "fused_results": fused}


@router.get("/files")
def get_files_with_fields(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    files = db.query(FileInfo).filter(FileInfo.upload_user == user.id, FileInfo.parse_status == "done").order_by(FileInfo.id.desc()).all()
    result = []
    for f in files:
        cnt = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).count()
        result.append({"file_id": f.id, "file_name": f.file_name, "file_type": f.file_type, "field_count": cnt})
    return {"total": len(result), "files": result}


@router.get("/files/{fid}/fields")
def get_file_fields(fid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    f = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user.id).first()
    if not f:
        raise HTTPException(404)
    rs = db.query(ExtractResult).filter(ExtractResult.file_id == fid).all()
    return {"file_id": fid, "file_name": f.file_name, "total": len(rs),
            "fields": [{"field_name": r.field_name, "field_value": r.field_value, "confidence": r.confidence or 0, "extract_method": r.extract_method or ""} for r in rs]}


@router.put("/results/{rid}")
def update(rid: int, req: UpdateReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    r = db.query(ExtractResult).filter(ExtractResult.id == rid).first()
    if not r:
        raise HTTPException(404)
    # 验证归属：提取结果对应的文件必须是当前用户的
    f = db.query(FileInfo).filter(FileInfo.id == r.file_id, FileInfo.upload_user == user.id).first()
    if not f:
        raise HTTPException(403, "无权修改此记录")
    r.field_value = req.field_value
    r.confidence = 1.0
    r.extract_method = "manual"
    db.commit()
    return {"message": "已更新"}


@router.post("/results/batch-delete")
def batch_delete_results(req: BatchDeleteReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    user_file_ids = {f.id for f in db.query(FileInfo).filter(FileInfo.upload_user == user.id).all()}
    rows = db.query(ExtractResult).filter(ExtractResult.id.in_(req.ids)).all()
    deleted = 0
    for r in rows:
        if r.file_id in user_file_ids:
            db.delete(r)
            deleted += 1
    db.commit()
    return {"message": f"已删除 {deleted} 条", "deleted": deleted}
