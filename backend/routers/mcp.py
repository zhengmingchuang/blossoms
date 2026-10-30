import json
import asyncio
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse, JSONResponse
from sqlalchemy.orm import Session
from database import get_db
from models import User
from routers.auth import get_current_user
from services.mcp_service import handle_jsonrpc, get_tools_list, MCP_TOOLS

router = APIRouter(prefix="/api/mcp", tags=["MCP"])


@router.post("/jsonrpc")
async def jsonrpc(request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    body = await request.json()
    return JSONResponse(await handle_jsonrpc(body, user_id=user.id))


@router.get("/sse")
async def sse(request: Request):
    async def stream():
        yield f"data: {json.dumps({'type': 'endpoint', 'url': '/api/mcp/message'})}\n\n"
        while not await request.is_disconnected():
            yield ": keepalive\n\n"
            await asyncio.sleep(15)
    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "Connection": "keep-alive"})


@router.post("/message")
async def message(request: Request, db: Session = Depends(get_db)):
    body = await request.json()
    uid = None
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        try:
            import jwt
            from config import SECRET_KEY
            p = jwt.decode(auth[7:], SECRET_KEY, algorithms=["HS256"])
            uid = p.get("uid")
        except Exception:
            pass
    return JSONResponse(await handle_jsonrpc(body, user_id=uid))


@router.get("/tools")
async def tools():
    return {"protocol": "MCP", "version": "2024-11-05", "transport": "HTTP+SSE", "tools_count": len(MCP_TOOLS), "tools": get_tools_list(),
            "endpoints": {"sse": "/api/mcp/sse", "jsonrpc": "/api/mcp/jsonrpc", "message": "/api/mcp/message"}}
