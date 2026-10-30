import os
import secrets
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
UPLOAD_DIR = BASE_DIR / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)
EXPORT_DIR = BASE_DIR / "exports"
EXPORT_DIR.mkdir(exist_ok=True)

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{BASE_DIR / 'app.db'}")

LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.deepseek.com/anthropic")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "deepseek-flash")
LLM_API_FORMAT = os.getenv("LLM_API_FORMAT", "anthropic")

# 关闭模型思考模式（DeepSeek V4 默认开启，会带来三个问题）：
#   1. 思考模式下 temperature 被忽略，本项目大量 temperature=0.0 的确定性抽取会失效；
#   2. 延迟与 token 成本上升数倍，而填表/抽取属于机械任务，思考无收益；
#   3. 请求带 tools 时后续每轮必须回传 reasoning_content，否则 API 返回 400，
#      本项目 chat.py 的 agent 循环未回传，多轮工具链会直接失败。
# 注意：该参数为 DeepSeek 专用。若回滚到 Anthropic 官方 API，需置为 false，
#       因为官方 API 的 thinking.type 只接受 enabled，不接受 disabled。
LLM_DISABLE_THINKING = os.getenv("LLM_DISABLE_THINKING", "true").lower() == "true"

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "all-MiniLM-L6-v2")
MCP_ENABLED = os.getenv("MCP_ENABLED", "true").lower() == "true"

SECRET_KEY = os.getenv("SECRET_KEY", secrets.token_hex(32))
ACCESS_TOKEN_EXPIRE_MINUTES = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", "1440"))

VECTOR_STORE_DIR = BASE_DIR / "vector_store"
VECTOR_STORE_DIR.mkdir(exist_ok=True)
