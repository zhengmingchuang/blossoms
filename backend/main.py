import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from database import engine, Base, SessionLocal
from models import SystemConfig
from routers import auth, documents, extraction, templates, chat, dashboard, settings, mcp, tasks, openapi
import config as cfg

# 配置日志（force 避免被其它库抢先 basicConfig 后本配置不生效）
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    force=True,
)


def _ensure_app_loggers_to_stderr():
    """保证填表相关 logger 打到 stderr；uvicorn 默认配置下部分子 logger 可能不在控制台显示。"""
    fmt = logging.Formatter("%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    for name in ("templates", "data_filter"):
        lg = logging.getLogger(name)
        if lg.handlers:
            continue
        h = logging.StreamHandler()
        h.setFormatter(fmt)
        lg.addHandler(h)
        lg.setLevel(logging.INFO)
        lg.propagate = False

Base.metadata.create_all(bind=engine)

# 自动迁移：给 users 表加 avatar 列（如果不存在）
try:
    with engine.connect() as conn:
        from sqlalchemy import text, inspect
        insp = inspect(engine)
        cols = [c["name"] for c in insp.get_columns("users")]
        if "avatar" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN avatar VARCHAR(500) DEFAULT ''"))
            conn.commit()
        # 给 task_info 表加 output_file 列
        task_cols = [c["name"] for c in insp.get_columns("task_info")]
        if "output_file" not in task_cols:
            conn.execute(text("ALTER TABLE task_info ADD COLUMN output_file VARCHAR(500) DEFAULT ''"))
            conn.commit()
        # 给 users 表加 nickname 列
        if "nickname" not in cols:
            conn.execute(text("ALTER TABLE users ADD COLUMN nickname VARCHAR(50) DEFAULT ''"))
            conn.commit()
            # 把已有用户的 nickname 设为 username
            conn.execute(text("UPDATE users SET nickname = username WHERE nickname = '' OR nickname IS NULL"))
            conn.commit()
except Exception:
    pass

# 自动创建 admin 账号（如果不存在）
try:
    from models import User
    from routers.auth import _hash_bcrypt
    _init_db = SessionLocal()
    if not _init_db.query(User).filter(User.username == "admin").first():
        _init_db.add(User(username="admin", nickname="admin", password=_hash_bcrypt("admin123"), role="admin"))
        _init_db.commit()
        logging.getLogger("main").info("已自动创建 admin 账号 (用户名: admin, 密码: admin123)")
    # 内测阶段：取消普通用户/管理员的权限区别，统一全部用户为管理员
    _upgraded = _init_db.query(User).filter(User.role != "admin").update({"role": "admin"}, synchronize_session=False)
    if _upgraded:
        _init_db.commit()
        logging.getLogger("main").info(f"内测阶段：已将 {_upgraded} 个普通用户统一升级为管理员")
    _init_db.close()
except Exception as _init_err:
    logging.getLogger("main").error(f"自动创建 admin 账号失败: {_init_err}")

def load_saved_config():
    db = SessionLocal()
    overridden = []
    try:
        mapping = {"llm_api_base": "LLM_API_BASE", "llm_api_key": "LLM_API_KEY", "llm_model": "LLM_MODEL", "llm_api_format": "LLM_API_FORMAT", "embedding_model": "EMBEDDING_MODEL"}
        for db_key, cfg_attr in mapping.items():
            row = db.query(SystemConfig).filter(SystemConfig.config_key == db_key).first()
            if row and row.config_value:
                setattr(cfg, cfg_attr, row.config_value)
                overridden.append(db_key)
        mcp_row = db.query(SystemConfig).filter(SystemConfig.config_key == "mcp_enabled").first()
        if mcp_row:
            cfg.MCP_ENABLED = mcp_row.config_value.lower() == "true"
    except Exception:
        pass
    finally:
        db.close()
    # 数据库配置会静默覆盖 .env，容易让人误以为改 .env 就生效了，这里把实际生效值打出来
    logging.getLogger("main").info(
        "LLM 生效配置: format=%s base=%s model=%s key=%s 来源=%s",
        cfg.LLM_API_FORMAT,
        cfg.LLM_API_BASE,
        cfg.LLM_MODEL,
        "已设置" if cfg.LLM_API_KEY else "缺失",
        ("数据库覆盖 " + ",".join(overridden)) if overridden else ".env",
    )
    try:
        import jieba
        jieba.initialize()
    except Exception:
        pass


@asynccontextmanager
async def lifespan(_: FastAPI):
    _ensure_app_loggers_to_stderr()
    load_saved_config()
    yield


app = FastAPI(title="DocFusion - 文档理解与多源数据融合系统", version="1.0.0", lifespan=lifespan)

app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

app.include_router(auth.router)
app.include_router(documents.router)
app.include_router(extraction.router)
app.include_router(templates.router)
app.include_router(chat.router)
app.include_router(dashboard.router)
app.include_router(settings.router)
app.include_router(mcp.router)
app.include_router(tasks.router)
app.include_router(openapi.router)


@app.get("/api/health")
def health():
    return {"status": "ok", "system": "DocFusion 1.0"}


# ===================== 生产模式：后端直接托管前端产物 =====================
# 开发时用 `npm run dev`（3000 端口，Vite 把 /api 代理到本服务）。
# 演示/部署时先 `npm run build`，这里会把 frontend/dist 挂上来，
# 于是单端口（默认 8000）即可访问完整应用，不需要额外的 Nginx。
_DIST_DIR = Path(__file__).resolve().parent.parent / "frontend" / "dist"

if (_DIST_DIR / "index.html").exists():
    if (_DIST_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=_DIST_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    def spa_fallback(full_path: str):
        """静态资源命中则返回文件，其余路径回落到 index.html（兼容 history 路由）。"""
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not Found")
        if full_path:
            candidate = (_DIST_DIR / full_path).resolve()
            # 限制在 dist 目录内，防止 ../ 路径穿越
            if candidate.is_file() and _DIST_DIR.resolve() in candidate.parents:
                return FileResponse(candidate)
        return FileResponse(_DIST_DIR / "index.html")

    logging.getLogger("main").info("已启用前端静态托管: %s（访问 http://127.0.0.1:8000/）", _DIST_DIR)
else:
    logging.getLogger("main").info("未找到 frontend/dist，仅提供 API（开发模式请另起 npm run dev）")


if __name__ == "__main__":
    _ensure_app_loggers_to_stderr()
    # 云托管（如 Sites / 容器）通过 PORT 注入端口，本地默认 8000
    _port = int(os.environ.get("PORT", "8000"))
    uvicorn.run("main:app", host="0.0.0.0", port=_port, reload=False)
