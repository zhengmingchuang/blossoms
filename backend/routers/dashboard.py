from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session
from sqlalchemy import func
from database import get_db
from models import FileInfo, ExtractResult, TemplateInfo, TaskInfo, User, fmt_time
from routers.auth import get_current_user

router = APIRouter(prefix="/api/dashboard", tags=["工作台"])


@router.get("/stats")
def stats(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    uid = user.id
    fc = db.query(FileInfo).filter(FileInfo.upload_user == uid).count()
    pc = db.query(FileInfo).filter(FileInfo.upload_user == uid, FileInfo.parse_status == "done").count()
    tc = db.query(TemplateInfo).filter(TemplateInfo.upload_user == uid).count()
    ec = db.query(ExtractResult).join(FileInfo).filter(FileInfo.upload_user == uid).count()

    task_total = db.query(TaskInfo).filter(TaskInfo.user_id == uid).count()
    task_done = db.query(TaskInfo).filter(TaskInfo.user_id == uid, TaskInfo.status == "done").count()
    task_error = db.query(TaskInfo).filter(TaskInfo.user_id == uid, TaskInfo.status == "error").count()

    avg_time_row = db.query(func.avg(TaskInfo.elapsed)).filter(TaskInfo.user_id == uid, TaskInfo.status == "done", TaskInfo.elapsed.isnot(None)).scalar()
    avg_time = round(float(avg_time_row), 2) if avg_time_row else 0

    avg_acc_row = db.query(func.avg(TaskInfo.accuracy)).filter(TaskInfo.user_id == uid, TaskInfo.accuracy.isnot(None)).scalar()
    avg_accuracy = round(float(avg_acc_row) * 100, 1) if avg_acc_row else 0

    success_rate = round(task_done / max(task_total, 1) * 100, 1)

    ts = db.query(FileInfo.file_type, func.count(FileInfo.id)).filter(FileInfo.upload_user == uid).group_by(FileInfo.file_type).all()

    recent_tasks = db.query(TaskInfo).filter(TaskInfo.user_id == uid).order_by(TaskInfo.start_time.desc()).limit(10).all()
    rf = db.query(FileInfo).filter(FileInfo.upload_user == uid).order_by(FileInfo.upload_time.desc()).limit(5).all()

    return {
        "file_count": fc, "parsed_count": pc, "template_count": tc, "extract_count": ec,
        "task_total": task_total, "task_done": task_done, "task_error": task_error,
        "avg_time": avg_time, "avg_accuracy": avg_accuracy, "success_rate": success_rate,
        "file_type_stats": {t: c for t, c in ts},
        "recent_files": [{"id": f.id, "file_name": f.file_name, "file_type": f.file_type, "parse_status": f.parse_status, "upload_time": fmt_time(f.upload_time)} for f in rf],
        "recent_tasks": [{"id": t.id, "task_type": t.task_type, "task_name": t.task_name, "status": t.status, "elapsed": t.elapsed, "accuracy": t.accuracy, "start_time": fmt_time(t.start_time), "result_summary": t.result_summary or ""} for t in recent_tasks],
    }
