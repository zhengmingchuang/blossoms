from typing import List, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models import TaskInfo, OperationLog, User, fmt_time
from routers.auth import get_current_user
from config import EXPORT_DIR

router = APIRouter(prefix="/api/tasks", tags=["任务管理"])


def _log(db: Session, user: User, action: str, detail: str = "") -> None:
    try:
        db.add(OperationLog(user_id=user.id, username=user.username, action=action, detail=detail))
        db.commit()
    except Exception:
        db.rollback()


def _cleanup_task_export(task: TaskInfo) -> None:
    """对于填表任务，同步清理导出文件，避免磁盘残留。"""
    output_file = (task.output_file or "").strip() if task.output_file else ""
    if not output_file:
        return
    try:
        (EXPORT_DIR / output_file).unlink(missing_ok=True)
    except Exception:
        pass


@router.get("/list")
def list_tasks(page: int = 1, page_size: int = 20, task_type: Optional[str] = None, status: Optional[str] = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(TaskInfo).filter(TaskInfo.user_id == user.id)
    if task_type:
        q = q.filter(TaskInfo.task_type == task_type)
    if status:
        q = q.filter(TaskInfo.status == status)
    total = q.count()
    tasks = q.order_by(TaskInfo.start_time.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "tasks": [
        {"id": t.id, "task_type": t.task_type, "task_name": t.task_name,
         "status": t.status, "elapsed": t.elapsed, "accuracy": t.accuracy,
         "result_summary": t.result_summary or "",
         "start_time": fmt_time(t.start_time), "end_time": fmt_time(t.end_time)} for t in tasks
    ]}


@router.get("/stats")
def task_stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    uid = user.id
    total = db.query(TaskInfo).filter(TaskInfo.user_id == uid).count()
    done = db.query(TaskInfo).filter(TaskInfo.user_id == uid, TaskInfo.status == "done").count()
    running = db.query(TaskInfo).filter(TaskInfo.user_id == uid, TaskInfo.status == "running").count()
    error = db.query(TaskInfo).filter(TaskInfo.user_id == uid, TaskInfo.status == "error").count()
    return {"total": total, "done": done, "running": running, "error": error, "pending": total - done - running - error}


class BatchDeleteReq(BaseModel):
    ids: List[int]


@router.delete("/{task_id}")
def delete_task(task_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """删除一条任务记录。若为填表任务则同时清理导出文件。"""
    task = db.query(TaskInfo).filter(TaskInfo.id == task_id, TaskInfo.user_id == user.id).first()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")
    if task.status == "running":
        raise HTTPException(status_code=400, detail="任务正在运行，无法删除")
    name = task.task_name or ""
    _cleanup_task_export(task)
    db.delete(task)
    db.commit()
    _log(db, user, "删除任务", name)
    return {"message": "已删除"}


@router.post("/batch-delete")
def batch_delete_tasks(req: BatchDeleteReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """批量删除当前用户的任务记录；运行中的任务会被跳过。"""
    if not req.ids:
        return {"message": "无需删除", "deleted": 0, "skipped": 0}
    tasks = db.query(TaskInfo).filter(
        TaskInfo.id.in_(req.ids), TaskInfo.user_id == user.id
    ).all()
    deleted = 0
    skipped = 0
    for task in tasks:
        if task.status == "running":
            skipped += 1
            continue
        _cleanup_task_export(task)
        db.delete(task)
        deleted += 1
    db.commit()
    _log(db, user, "批量删除任务", f"删除{deleted}条，跳过{skipped}条运行中")
    msg = f"已删除 {deleted} 条记录"
    if skipped:
        msg += f"，跳过 {skipped} 条运行中"
    return {"message": msg, "deleted": deleted, "skipped": skipped}
