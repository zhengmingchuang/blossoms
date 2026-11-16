import logging
import mimetypes
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


def _fix_mimetypes():
    """Windows 上 .js/.mjs 可能被注册表映射成 text/plain，导致浏览器拒绝执行 module script（表现为白屏）。

    Python 的 mimetypes 在 Windows 下会读注册表 HKCR\\.js\\Content Type，
    部分机器上该键被写成 text/plain。浏览器对 <script type="module"> 强制校验 MIME，
    于是 index.html 正常返回、JS 全部加载失败，页面只剩空白。
    这里在进程启动时把前端用到的类型显式钉死，不依赖操作系统注册表。
    """
    forced = {
        ".js": "text/javascript",
        ".mjs": "text/javascript",
        ".css": "text/css",
        ".json": "application/json",
        ".map": "application/json",
        ".svg": "image/svg+xml",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
        ".ico": "image/x-icon",
    }
    for ext, mime in forced.items():
        mimetypes.add_type(mime, ext)
    bad = [ext for ext, mime in forced.items() if mimetypes.guess_type("x" + ext)[0] != mime]
    if bad:
        logging.getLogger("main").warning("MIME 类型修正失败: %s", ", ".join(bad))


_fix_mimetypes()


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

# 自动创建 admin 账号（仅当不存在时）
# 安全约束：
#   1. 初始口令只从 ADMIN_INIT_PASSWORD 读取，源码与文档都不留明文默认值；
#   2. 未配置时生成一次性随机口令并落盘到仅本机可读文件，日志只给路径、不打印口令
#      —— 日志会被采集/转发，打印明文等同于公开管理员口令；
#   3. 不在启动时批量改角色，角色变更只走显式管理操作。
try:
    from models import User
    from routers.auth import _hash_bcrypt
    _init_db = SessionLocal()
    if not _init_db.query(User).filter(User.username == "admin").first():
        _init_pwd = os.getenv("ADMIN_INIT_PASSWORD", "").strip()
        _generated = False
        if not _init_pwd:
            import secrets as _secrets
            _init_pwd = _secrets.token_urlsafe(18)
            _generated = True
        _init_db.add(User(username="admin", nickname="admin",
                          password=_hash_bcrypt(_init_pwd), role="admin"))
        _init_db.commit()
        if _generated:
            _pwd_file = cfg.BASE_DIR / ".admin_initial_password"
            try:
                _pwd_file.write_text(_init_pwd, encoding="utf-8")
                try:
                    os.chmod(_pwd_file, 0o600)
                except OSError:
                    pass
                logging.getLogger("main").warning(
                    "已创建 admin 账号，初始口令写入 %s（首次登录后请立即修改并删除该文件）", _pwd_file)
            except Exception:
                logging.getLogger("main").warning(
                    "已创建 admin 账号，但初始口令落盘失败；请设置 ADMIN_INIT_PASSWORD 后重置该账号")
        else:
            logging.getLogger("main").info("已创建 admin 账号，初始口令来自 ADMIN_INIT_PASSWORD")
    _init_db.close()
except Exception as _init_err:
    logging.getLogger("main").error(f"自动创建 admin 账号失败: {_init_err}")

def _recover_stale_tasks():
    """把上次进程遗留的 running/pending 任务标成失败。

    任务状态存在数据库里，但执行它的 asyncio 任务只在内存里。进程一重启（部署、
    崩溃、被 OOM 杀掉），库里那些行就永远停在 running：用户在任务列表看到的是
    一个永远「进行中」的任务，既不能重试也不会报错。

    启动时没有任何任务在跑，所以此时所有 running/pending 都是遗留行——可以直接判定。
    **前提**：单进程部署（本项目的运行方式）。若将来改成多 worker / 多实例，
    这里必须先确认「本实例启动时没有其它实例正在跑任务」，否则会把别人在跑的任务标死。
    """
    from models import TaskInfo
    from datetime import datetime
    db = SessionLocal()
    try:
        rows = db.query(TaskInfo).filter(TaskInfo.status.in_(("running", "pending"))).all()
        if not rows:
            return
        now = datetime.now()
        for t in rows:
            t.status = "error"
            t.end_time = now
            if t.start_time:
                try:
                    t.elapsed = round(max(0.0, (now - t.start_time).total_seconds()), 2)
                except Exception:
                    pass
            t.result_summary = "服务重启导致任务中断，请重新发起"
        db.commit()
        logging.getLogger("main").warning(
            "已把 %d 个上次进程遗留的「进行中」任务标记为失败（服务重启，任务状态无法恢复）",
            len(rows),
        )
    except Exception as e:
        logging.getLogger("main").error(f"回收遗留任务状态失败: {e}")
    finally:
        db.close()


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
    # 必须在开始接受请求之前：否则用户先看到「进行中」，再被改成失败，体验更差
    _recover_stale_tasks()
    yield


app = FastAPI(title="DocFusion - 文档理解与多源数据融合系统", version="1.0.0", lifespan=lifespan)

# CORS：默认只放行本地开发端口；部署时用 CORS_ORIGINS 覆盖（逗号分隔）。
# 不能同时用 allow_origins=["*"] 与 allow_credentials=True —— 浏览器会拒绝带凭据的跨域请求，
# 且等于把接口暴露给任意站点。
_cors_env = os.getenv("CORS_ORIGINS", "").strip()
if _cors_env:
    _cors_origins = [o.strip() for o in _cors_env.split(",") if o.strip()]
else:
    _cors_origins = [
        "http://localhost:3000", "http://127.0.0.1:3000",
        "http://localhost:5173", "http://127.0.0.1:5173",
    ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
