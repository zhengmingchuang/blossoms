"""
开放API接口 —— 供 n8n / Coze / 外部工作流调用
鉴权方式：Header 带 X-API-Key（值为系统配置的 LLM_API_KEY 或自定义 webhook key）

n8n 配置示例：
  HTTP Request 节点:
    Method: POST
    URL: http://你的IP:8000/api/open/upload
    Headers: X-API-Key = your-key
    Body: form-data, file 字段

Coze 配置示例：
  插件 -> HTTP 请求:
    同上，POST 调用各接口
"""
import uuid
import time
from pathlib import Path
from typing import Optional, List
from fastapi import APIRouter, HTTPException, UploadFile, File, Header
from pydantic import BaseModel
from database import SessionLocal
from models import FileInfo, DocChunk, ExtractResult, TaskInfo, User
from config import UPLOAD_DIR, SECRET_KEY
from services.document_parser import parse_document
from services.vector_store import add_chunks, search as vs_search
from services.extractor import hybrid_extract, rule_extract
from services.fusion import fuse
from services.llm_service import call_llm_text

router = APIRouter(prefix="/api/open", tags=["开放API"])

WEBHOOK_KEY = "docfusion-webhook-2026"


def _auth(api_key: Optional[str]):
    if not api_key:
        raise HTTPException(401, "缺少 X-API-Key")
    if api_key not in (WEBHOOK_KEY, SECRET_KEY):
        raise HTTPException(403, "API Key 无效")


def _get_user(db):
    u = db.query(User).first()
    return u.id if u else 1


# ============ 文档上传+快速解析 ============
@router.post("/upload")
async def upload(file: UploadFile = File(...), x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    ext = Path(file.filename).suffix.lower()
    if ext not in (".docx", ".doc", ".md", ".xlsx", ".xls", ".txt"):
        raise HTTPException(400, f"不支持: {ext}")
    db = SessionLocal()
    try:
        t0 = time.time()
        name = f"{uuid.uuid4().hex}_{file.filename}"
        path = UPLOAD_DIR / name
        content = await file.read()
        with open(path, "wb") as fp:
            fp.write(content)
        uid = _get_user(db)
        fi = FileInfo(file_name=file.filename, file_type=ext.lstrip("."), file_path=str(path), file_size=len(content), upload_user=uid, parse_status="parsing")
        db.add(fi)
        db.commit()
        db.refresh(fi)

        chunks = parse_document(str(path))
        for ch in chunks:
            db.add(DocChunk(file_id=fi.id, chunk_index=ch["chunk_index"], chunk_text=ch["chunk_text"], chunk_type=ch["chunk_type"], source_position=ch["source_position"]))
        db.commit()
        add_chunks(fi.id, fi.file_name, chunks)

        full = "\n\n".join(c["chunk_text"] for c in chunks)
        rule_fields = rule_extract(full)
        for k, v in rule_fields.items():
            val = str(v) if not isinstance(v, list) else str(v[0])
            db.add(ExtractResult(file_id=fi.id, field_name=k, field_value=val, confidence=0.90, extract_method="rule"))
        db.commit()

        elapsed = round(time.time() - t0, 2)
        fi.parse_status = "done"
        fi.parse_time = elapsed
        fi.chunk_count = len(chunks)
        fi.field_count = len(rule_fields)
        db.commit()

        return {"file_id": fi.id, "file_name": fi.file_name, "chunks": len(chunks), "rule_fields": len(rule_fields), "elapsed": elapsed,
                "fields": {k: (str(v) if not isinstance(v, list) else str(v[0])) for k, v in rule_fields.items()}}
    finally:
        db.close()


# ============ LLM深度提取 ============
class ExtractReq(BaseModel):
    file_id: int
    fields: Optional[List[str]] = None


@router.post("/extract")
async def extract(req: ExtractReq, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    db = SessionLocal()
    try:
        fi = db.query(FileInfo).filter(FileInfo.id == req.file_id).first()
        if not fi:
            raise HTTPException(404, "文件不存在")
        chunks = db.query(DocChunk).filter(DocChunk.file_id == req.file_id).order_by(DocChunk.chunk_index).all()
        full = "\n\n".join(c.chunk_text for c in chunks)

        t0 = time.time()
        extracted = await hybrid_extract(full[:15000], fields=req.fields)
        new_count = 0
        for k, v in extracted.items():
            if k.startswith("_"):
                continue
            val = str(v)
            existing = db.query(ExtractResult).filter(ExtractResult.file_id == req.file_id, ExtractResult.field_name == k).first()
            if existing:
                existing.field_value = val
                existing.confidence = 0.90
                existing.extract_method = "llm"
            else:
                db.add(ExtractResult(file_id=req.file_id, field_name=k, field_value=val, confidence=0.85, extract_method="llm"))
                new_count += 1
        db.commit()
        total = db.query(ExtractResult).filter(ExtractResult.file_id == req.file_id).count()
        fi.field_count = total
        db.commit()

        return {"file_id": req.file_id, "new_fields": new_count, "total_fields": total, "elapsed": round(time.time() - t0, 2), "extracted": {k: str(v) for k, v in extracted.items() if not k.startswith("_")}}
    finally:
        db.close()


# ============ 直接提取文本（不需要先上传） ============
class TextExtractReq(BaseModel):
    text: str
    fields: Optional[List[str]] = None


@router.post("/extract-text")
async def extract_text(req: TextExtractReq, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    t0 = time.time()
    result = await hybrid_extract(req.text[:15000], fields=req.fields)
    return {"elapsed": round(time.time() - t0, 2), "fields": {k: str(v) for k, v in result.items() if not k.startswith("_")}}


# ============ 查询已提取的字段 ============
@router.get("/fields")
def get_fields(file_id: Optional[int] = None, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    db = SessionLocal()
    try:
        q = db.query(ExtractResult)
        if file_id:
            q = q.filter(ExtractResult.file_id == file_id)
        rs = q.all()
        return {"total": len(rs), "fields": [{"file_id": r.file_id, "field_name": r.field_name, "field_value": r.field_value, "confidence": r.confidence, "method": r.extract_method} for r in rs]}
    finally:
        db.close()


# ============ 获取融合数据 ============
@router.get("/fused")
def get_fused(x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    db = SessionLocal()
    try:
        files = db.query(FileInfo).all()
        exts = []
        for f in files:
            rs = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).all()
            if rs:
                exts.append({"file_id": f.id, "file_name": f.file_name, "source_type": "table" if f.file_type in ("xlsx", "xls") else "paragraph", "fields": {r.field_name: r.field_value for r in rs}})
        fused_data = fuse(exts)
        return {"source_files": len(files), "fused_count": len(fused_data), "fused": [{k: v for k, v in r.items() if k != "all_candidates"} for r in fused_data]}
    finally:
        db.close()


# ============ 语义搜索 ============
class SearchReq(BaseModel):
    query: str
    top_k: Optional[int] = 5


@router.post("/search")
def semantic_search(req: SearchReq, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    results = vs_search(req.query, top_k=req.top_k or 5)
    return {"total": len(results), "results": [{"file_name": r.get("file_name", ""), "chunk_text": r["chunk_text"][:500], "score": r.get("score", 0)} for r in results]}


# ============ 智能问答 ============
class ChatReq(BaseModel):
    message: str


@router.post("/chat")
async def chat(req: ChatReq, x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    relevant = vs_search(req.message, top_k=8)
    ctx = [r["chunk_text"] for r in relevant]
    sys = "你是智能文档办公助手。根据文档内容回答问题。找不到信息如实告知。"
    msgs = []
    if ctx:
        msgs.append({"role": "user", "content": f"参考文档：\n\n" + "\n\n---\n\n".join(ctx)})
        msgs.append({"role": "assistant", "content": "好的，我已了解文档内容。"})
    msgs.append({"role": "user", "content": req.message})
    answer = await call_llm_text(msgs, system=sys, temperature=0.3)
    return {"answer": answer, "sources": [{"file_name": r.get("file_name", ""), "text": r["chunk_text"][:200]} for r in relevant[:3]]}


# ============ 列出所有文件 ============
@router.get("/files")
def list_files(x_api_key: Optional[str] = Header(None)):
    _auth(x_api_key)
    db = SessionLocal()
    try:
        fs = db.query(FileInfo).order_by(FileInfo.upload_time.desc()).all()
        return {"total": len(fs), "files": [{"id": f.id, "file_name": f.file_name, "file_type": f.file_type, "parse_status": f.parse_status, "chunk_count": f.chunk_count or 0, "field_count": f.field_count or 0} for f in fs]}
    finally:
        db.close()


# ============ API 文档首页 ============
@router.get("/")
def api_info():
    return {
        "system": "DocFusion 开放API",
        "version": "1.0",
        "auth": "Header: X-API-Key",
        "webhook_key": WEBHOOK_KEY,
        "endpoints": {
            "POST /api/open/upload": "上传文档并快速解析（form-data: file）",
            "POST /api/open/extract": "LLM深度提取（JSON: {file_id, fields?}）",
            "POST /api/open/extract-text": "直接提取文本（JSON: {text, fields?}）",
            "GET  /api/open/files": "列出所有文件",
            "GET  /api/open/fields?file_id=X": "查询提取字段",
            "GET  /api/open/fused": "获取融合数据",
            "POST /api/open/search": "语义搜索（JSON: {query, top_k?}）",
            "POST /api/open/chat": "智能问答（JSON: {message}）",
        }
    }
