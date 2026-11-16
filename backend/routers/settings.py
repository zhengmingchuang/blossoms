from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session
from database import get_db
from models import SystemConfig, User, OperationLog
from models import fmt_time
from routers.auth import get_current_user
import config as cfg

router = APIRouter(prefix="/api/settings", tags=["设置"])


# ============ 系统配置 ============
class CfgReq(BaseModel):
    llm_api_base: Optional[str] = None
    llm_api_key: Optional[str] = None
    llm_model: Optional[str] = None
    llm_api_format: Optional[str] = None
    embedding_model: Optional[str] = None
    mcp_enabled: Optional[bool] = None


def _get(db, key, default=""):
    c = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    return c.config_value if c else default


def _set(db, key, val):
    c = db.query(SystemConfig).filter(SystemConfig.config_key == key).first()
    if c:
        c.config_value = val
    else:
        db.add(SystemConfig(config_key=key, config_value=val))
    db.commit()


def _require_admin(user: User):
    """配置与用户管理只允许管理员操作。

    此前这些接口只校验「已登录」，任何普通用户都能：
      - 改 LLM 网关地址，把 api_key 引向自己的服务器（SSRF + 密钥外带）；
      - 重置包括 admin 在内的任意账号口令（直接接管）；
      - 删除任意账号。
    """
    if (user.role or "") != "admin":
        raise HTTPException(403, "需要管理员权限")


@router.get("/config")
def get_config(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    # 读配置同样收口：它暴露 LLM 网关地址与「是否已配置密钥」，
    # 把「该往哪改」这个攻击面留给所有人没有意义。
    _require_admin(user)
    return {
        "llm_api_base": _get(db, "llm_api_base") or cfg.LLM_API_BASE,
        "llm_model": _get(db, "llm_model") or cfg.LLM_MODEL,
        "llm_api_format": _get(db, "llm_api_format") or cfg.LLM_API_FORMAT,
        "embedding_model": _get(db, "embedding_model") or cfg.EMBEDDING_MODEL,
        "mcp_enabled": (_get(db, "mcp_enabled") or str(cfg.MCP_ENABLED)).lower() == "true",
        "has_api_key": bool(_get(db, "llm_api_key") or cfg.LLM_API_KEY),
    }


@router.put("/config")
def set_config(req: CfgReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    if req.llm_api_base is not None:
        _set(db, "llm_api_base", req.llm_api_base); cfg.LLM_API_BASE = req.llm_api_base
    if req.llm_api_key is not None:
        _set(db, "llm_api_key", req.llm_api_key); cfg.LLM_API_KEY = req.llm_api_key
    if req.llm_model is not None:
        _set(db, "llm_model", req.llm_model); cfg.LLM_MODEL = req.llm_model
    if req.llm_api_format is not None:
        _set(db, "llm_api_format", req.llm_api_format); cfg.LLM_API_FORMAT = req.llm_api_format
    if req.embedding_model is not None:
        _set(db, "embedding_model", req.embedding_model); cfg.EMBEDDING_MODEL = req.embedding_model
    if req.mcp_enabled is not None:
        _set(db, "mcp_enabled", str(req.mcp_enabled).lower()); cfg.MCP_ENABLED = req.mcp_enabled
    db.add(OperationLog(user_id=user.id, username=user.username, action="修改配置", detail="更新系统配置"))
    db.commit()
    return {"message": "已保存"}


# ============ 用户管理 ============
@router.get("/users")
def list_users(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    us = db.query(User).order_by(User.create_time.desc()).all()
    return {"total": len(us), "users": [
        {"id": u.id, "username": u.username, "nickname": u.nickname or u.username, "role": u.role, "create_time": fmt_time(u.create_time)} for u in us
    ]}


class UserUpdateReq(BaseModel):
    role: Optional[str] = None
    password: Optional[str] = None


@router.put("/users/{uid}")
def update_user(uid: int, req: UserUpdateReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    target = db.query(User).filter(User.id == uid).first()
    if not target:
        raise HTTPException(404)
    if req.role:
        target.role = req.role
    if req.password:
        # 必须与 auth._hash_bcrypt 一致：无盐 SHA256 等于把所有口令放进彩虹表
        from routers.auth import _hash_bcrypt
        target.password = _hash_bcrypt(req.password)
    db.commit()
    return {"message": "已更新"}


@router.delete("/users/{uid}")
def delete_user(uid: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_admin(user)
    if uid == user.id:
        raise HTTPException(400, "不能删除自己")
    target = db.query(User).filter(User.id == uid).first()
    if not target:
        raise HTTPException(404)
    if target.username == "admin":
        raise HTTPException(400, "超级管理员无法删除")
    db.delete(target)
    db.commit()
    return {"message": "已删除"}


# ============ 操作日志 ============
@router.get("/logs")
def get_logs(page: int = 1, page_size: int = 30, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    q = db.query(OperationLog)
    if user.role != "admin":
        q = q.filter(OperationLog.username == user.username)
    total = q.count()
    logs = q.order_by(OperationLog.create_time.desc()).offset((page - 1) * page_size).limit(page_size).all()
    return {"total": total, "page": page, "logs": [
        {"id": l.id, "username": l.username or "", "action": l.action or "", "detail": l.detail or "", "create_time": fmt_time(l.create_time)} for l in logs
    ]}
