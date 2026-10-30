import hashlib
import uuid
import bcrypt
from pathlib import Path
from datetime import datetime, timedelta
from fastapi import APIRouter, Depends, HTTPException, UploadFile, File
from fastapi.security import OAuth2PasswordBearer, OAuth2PasswordRequestForm
from sqlalchemy.orm import Session
from pydantic import BaseModel
import jwt
from database import get_db
from models import User
from config import SECRET_KEY, ACCESS_TOKEN_EXPIRE_MINUTES

router = APIRouter(prefix="/api/auth", tags=["认证"])
oauth2 = OAuth2PasswordBearer(tokenUrl="/api/auth/login", auto_error=False)

AVATAR_DIR = Path(__file__).parent.parent / "uploads" / "avatars"
AVATAR_DIR.mkdir(parents=True, exist_ok=True)


def _hash_bcrypt(pw: str) -> str:
    return bcrypt.hashpw(pw.encode(), bcrypt.gensalt()).decode()


def _verify_password(pw: str, hashed: str) -> bool:
    """验证密码，兼容旧 SHA256 和新 bcrypt"""
    if hashed.startswith("$2b$") or hashed.startswith("$2a$"):
        return bcrypt.checkpw(pw.encode(), hashed.encode())
    # 旧 SHA256 兼容
    return hashed == hashlib.sha256(pw.encode()).hexdigest()


def _token(data: dict) -> str:
    data["exp"] = datetime.utcnow() + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    return jwt.encode(data, SECRET_KEY, algorithm="HS256")


def get_current_user(token: str = Depends(oauth2), db: Session = Depends(get_db)):
    if not token:
        raise HTTPException(401, "未登录")
    try:
        p = jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
        u = db.query(User).filter(User.id == p.get("uid")).first()
        if not u:
            raise HTTPException(401, "用户不存在")
        # 内测阶段：取消普通用户/管理员的权限区别，统一视为管理员
        if u.role != "admin":
            u.role = "admin"
            db.commit()
        return u
    except jwt.ExpiredSignatureError:
        raise HTTPException(401, "登录过期")
    except Exception:
        raise HTTPException(401, "认证无效")


class RegReq(BaseModel):
    username: str
    password: str


@router.post("/register")
def register(req: RegReq, db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == req.username).first():
        raise HTTPException(400, "账号已存在")
    # 内测阶段：所有新注册用户均默认为管理员角色
    u = User(username=req.username, nickname=req.username, password=_hash_bcrypt(req.password), role="admin")
    db.add(u)
    db.commit()
    db.refresh(u)
    return {"message": "注册成功", "user_id": u.id}


@router.post("/login")
def login(form: OAuth2PasswordRequestForm = Depends(), db: Session = Depends(get_db)):
    # 支持账号(username)或用户名(nickname)登录
    u = db.query(User).filter(User.username == form.username).first()
    if not u:
        u = db.query(User).filter(User.nickname == form.username).first()
    if not u:
        raise HTTPException(401, "账号/用户名或密码错误")
    if not _verify_password(form.password, u.password):
        raise HTTPException(401, "账号/用户名或密码错误")
    # 旧 SHA256 密码自动迁移为 bcrypt
    if not u.password.startswith("$2b$"):
        u.password = _hash_bcrypt(form.password)
        db.commit()
    # 内测阶段：取消角色区别，统一为管理员
    if u.role != "admin":
        u.role = "admin"
        db.commit()
    t = _token({"uid": u.id, "username": u.username, "role": u.role})
    return {"access_token": t, "token_type": "bearer", "username": u.username, "nickname": u.nickname or u.username, "role": u.role, "avatar": u.avatar or ""}


@router.get("/me")
def me(u: User = Depends(get_current_user)):
    return {"id": u.id, "username": u.username, "nickname": u.nickname or u.username, "role": u.role, "avatar": u.avatar or ""}


@router.post("/avatar")
async def upload_avatar(file: UploadFile = File(...), u: User = Depends(get_current_user), db: Session = Depends(get_db)):
    ext = Path(file.filename).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
        raise HTTPException(400, "仅支持 jpg/png/gif/webp 格式")
    name = f"{u.id}_{uuid.uuid4().hex[:8]}{ext}"
    path = AVATAR_DIR / name
    content = await file.read()
    with open(path, "wb") as f:
        f.write(content)
    u.avatar = f"/api/auth/avatars/{name}"
    db.commit()
    return {"avatar": u.avatar}


from fastapi.responses import FileResponse

@router.get("/avatars/{filename}")
def get_avatar(filename: str):
    path = AVATAR_DIR / filename
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(str(path))


class ChangePwdReq(BaseModel):
    old_password: str
    new_password: str


@router.post("/change-password")
def change_password(req: ChangePwdReq, u: User = Depends(get_current_user), db: Session = Depends(get_db)):
    if not _verify_password(req.old_password, u.password):
        raise HTTPException(400, "原密码错误")
    if len(req.new_password) < 6:
        raise HTTPException(400, "新密码至少 6 位")
    if req.old_password == req.new_password:
        raise HTTPException(400, "新密码不能与原密码相同")
    u.password = _hash_bcrypt(req.new_password)
    db.commit()
    from models import OperationLog
    db.add(OperationLog(user_id=u.id, username=u.username, action="修改密码", detail="用户修改了登录密码"))
    db.commit()
    return {"message": "密码修改成功"}


class NicknameReq(BaseModel):
    nickname: str


@router.post("/change-nickname")
def change_nickname(req: NicknameReq, u: User = Depends(get_current_user), db: Session = Depends(get_db)):
    nick = req.nickname.strip()
    if not nick or len(nick) < 2:
        raise HTTPException(400, "用户名至少 2 个字符")
    if len(nick) > 20:
        raise HTTPException(400, "用户名最多 20 个字符")
    # 检查是否和其他人的 nickname 或 username 重复
    existing = db.query(User).filter(User.id != u.id, (User.nickname == nick) | (User.username == nick)).first()
    if existing:
        raise HTTPException(400, "该用户名已被使用")
    u.nickname = nick
    db.commit()
    from models import OperationLog
    db.add(OperationLog(user_id=u.id, username=u.username, action="修改用户名", detail=f"用户名改为: {nick}"))
    db.commit()
    return {"message": "用户名修改成功", "nickname": nick}
