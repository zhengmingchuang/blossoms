import json
import logging
import os
from datetime import datetime
from typing import List, Optional

from pathlib import Path
import uuid

from fastapi import APIRouter, Depends, HTTPException, UploadFile, File, BackgroundTasks
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, SessionLocal
from models import User, FileInfo, ExtractResult, ChatSession, ChatMessage, TaskInfo, fmt_time
from config import UPLOAD_DIR
from routers.auth import get_current_user
from routers.documents import _fast_parse
from services.llm_service import call_llm, call_llm_text
from services.vector_store import search as vs_search
from services.doc_editor import AGENT_TOOLS, execute_tool

router = APIRouter(prefix="/api/chat", tags=["智能交互"])
logger = logging.getLogger("chat")

MAX_TOOL_ROUNDS = 12


def _title_from_message(msg: str, max_len: int = 40) -> str:
    s = msg.strip().replace("\n", " ")
    if len(s) <= max_len:
        return s
    return s[:max_len] + "..."


class Msg(BaseModel):
    role: str
    content: str


class ChatReq(BaseModel):
    message: str
    history: Optional[List[Msg]] = None
    session_id: Optional[int] = None
    file_id: Optional[int] = None
    file_ids: Optional[List[int]] = None


# =====================  文件列表 & 下载  =====================

@router.get("/files")
def chat_file_list(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """返回当前用户所有可操作文档：已上传解析完成的 + 模板填写导出的文件"""
    result = []
    files = (
        db.query(FileInfo)
        .filter(FileInfo.upload_user == user.id, FileInfo.parse_status == "done")
        .order_by(FileInfo.upload_time.desc())
        .all()
    )
    for f in files:
        result.append({"id": f.id, "name": f.file_name, "type": f.file_type, "source": "upload"})

    tasks = (
        db.query(TaskInfo)
        .filter(TaskInfo.user_id == user.id, TaskInfo.task_type == "fill", TaskInfo.status == "done")
        .filter(TaskInfo.output_file.isnot(None), TaskInfo.output_file != "")
        .order_by(TaskInfo.end_time.desc())
        .limit(30)
        .all()
    )
    for t in tasks:
        if t.output_file and os.path.exists(t.output_file):
            fname = os.path.basename(t.output_file)
            ext = fname.rsplit(".", 1)[-1] if "." in fname else ""
            result.append({"id": f"task_{t.id}", "name": f"[填写] {fname}", "type": ext, "source": "filled", "task_id": t.id, "path": t.output_file})

    return result


@router.get("/files/{fid}/download")
def download_file(fid: str, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """下载指定文件（支持上传文档和填写导出文档）"""
    if isinstance(fid, str) and fid.startswith("task_"):
        tid = int(fid.replace("task_", ""))
        task = db.query(TaskInfo).filter(TaskInfo.id == tid, TaskInfo.user_id == user.id).first()
        if not task or not task.output_file or not os.path.exists(task.output_file):
            raise HTTPException(404, "文件不存在")
        return FileResponse(task.output_file, filename=os.path.basename(task.output_file), media_type="application/octet-stream")
    else:
        try:
            file_id = int(fid)
        except ValueError:
            raise HTTPException(400, "无效的文件ID")
        fi = db.query(FileInfo).filter(FileInfo.id == file_id, FileInfo.upload_user == user.id).first()
        if not fi:
            raise HTTPException(404, "文件不存在")
        if not os.path.exists(fi.file_path):
            raise HTTPException(404, "文件已被删除")
        return FileResponse(fi.file_path, filename=fi.file_name, media_type="application/octet-stream")


_DOC_EXTS = {".docx", ".doc", ".md", ".xlsx", ".xls", ".txt"}
_IMG_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_ALL_EXTS = _DOC_EXTS | _IMG_EXTS


@router.post("/upload")
async def chat_upload(file: UploadFile = File(...), bg: BackgroundTasks = BackgroundTasks(), db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """在聊天界面直接上传文档或图片"""
    ext = Path(file.filename).suffix.lower()
    if ext not in _ALL_EXTS:
        raise HTTPException(400, f"不支持的文件格式: {ext}，支持文档和图片")
    disk_name = f"{uuid.uuid4().hex[:8]}_{file.filename}"
    path = UPLOAD_DIR / disk_name
    content = await file.read()
    with open(path, "wb") as fp:
        fp.write(content)

    is_image = ext in _IMG_EXTS
    file_type = ext.lstrip(".")
    fi = FileInfo(file_name=file.filename, file_type=file_type, file_path=str(path), file_size=len(content), upload_user=user.id)
    if is_image:
        fi.parse_status = "done"
    db.add(fi)
    db.commit()
    db.refresh(fi)

    if not is_image:
        task = TaskInfo(task_type="parse", task_name=f"解析: {file.filename}", status="pending", user_id=user.id, related_id=fi.id)
        db.add(task)
        db.commit()
        db.refresh(task)
        import time
        bg.add_task(_fast_parse, fi.id, str(path), task.id, time.time())

    return {"id": fi.id, "name": fi.file_name, "type": fi.file_type, "source": "upload" if not is_image else "image", "status": "done" if is_image else "parsing"}


# =====================  会话管理  =====================

@router.get("/sessions")
def list_sessions(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rows = (
        db.query(ChatSession)
        .filter(ChatSession.user_id == user.id)
        .order_by(ChatSession.updated_at.desc())
        .limit(50)
        .all()
    )
    return [
        {"id": r.id, "title": (r.title or "新对话").strip() or "新对话", "updated_at": fmt_time(r.updated_at)}
        for r in rows
    ]


@router.get("/sessions/{session_id}/messages")
def get_session_messages(
    session_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    chat_sess = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not chat_sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    msgs = (
        db.query(ChatMessage)
        .filter(ChatMessage.session_id == session_id)
        .order_by(ChatMessage.create_time.asc(), ChatMessage.id.asc())
        .all()
    )
    return [
        {"role": m.role, "content": m.content or "", "create_time": fmt_time(m.create_time)}
        for m in msgs
    ]


@router.delete("/sessions/{session_id}")
def delete_session(
    session_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)
):
    chat_sess = (
        db.query(ChatSession)
        .filter(ChatSession.id == session_id, ChatSession.user_id == user.id)
        .first()
    )
    if not chat_sess:
        raise HTTPException(status_code=404, detail="会话不存在")
    db.delete(chat_sess)
    db.commit()
    return {"ok": True}


# =====================  核心对话 + Agent 循环  =====================

def _build_system_prompt(files, extracts_summary: str, has_tools: bool) -> str:
    parts = [
        "你是 DocFusion 智能文档助手，拥有以下能力：",
        "1. 在用户已上传的文档中搜索内容并回答问题",
        "2. 提取文档中的关键信息和实体数据",
        "3. 编辑文档内容（替换/删除/追加文本）",
        "4. 调整文档格式（字体、字号、加粗、对齐、行距、颜色等）",
        "5. 查看已提取的字段结果",
        "6. 分析文档结构（标题层级、正文段落）",
        "",
        "回答规则：",
        "- 优先使用工具获取准确信息，再组织回答",
        "- 基于文档内容回答，引用具体数据",
        "- 如果文档中没有相关信息，诚实告知用户",
        "- 回答简洁、准确、有条理，使用 Markdown 格式",
        "- 当用户要求编辑或格式化文档时，主动调用 edit_document 或 format_document 工具",
        "- 当用户询问文件列表或文件状态时，调用 list_files 工具",
        "- 当用户要求提取信息时，调用 extract_info 工具",
        "",
        "【核心】文档格式化工作流程（必须严格遵守）：",
        "第一步：先调用 analyze_document 分析文档结构，确认标题段落数量和正文段落数量。",
        "第二步：根据分析结果和用户需求，精准使用 format_document 的 style_target 参数。",
        "",
        "【重要】format_document 文档格式化规则：",
        "- 必须使用 style_target 参数精确区分段落类型，不要用 target='all'！",
        "- 用户说「标题」时 → style_target='heading'，只会影响被识别为标题的段落",
        "- 用户说「正文」时 → style_target='body'，只影响非标题段落",
        "- 用户说「全文」或「所有文字」时 → style_target='all'",
        "- 标题和正文格式不同时，必须分两次调用：",
        "  第一次：style_target='heading' 设置标题格式（如黑体、加粗、居中、较大字号）",
        "  第二次：style_target='body' 设置正文格式（如宋体、不加粗、两端对齐、首行缩进2字符、较小字号）",
        "- 如果只需要调整标题，就只调用一次 style_target='heading'",
        "- 如果只需要调整正文，就只调用一次 style_target='body'",
        "- 绝对不要在一次调用中把标题格式应用到正文，反之亦然",
        "- 首行缩进用 first_line_indent 参数，2 表示缩进 2 字符",
        "",
        "【关键】理解用户意图：",
        "- 用户说「标题字号设为红色」→ 只改标题颜色，style_target='heading', color='FF0000'",
        "- 用户说「全文字号12磅宋体」→ 改所有段落字号字体，style_target='all', font_size=12, font_name='宋体'",
        "- 用户说「标题黑体加粗，正文宋体」→ 分两次调用",
        "- 当用户要求只改某种段落的某个属性时，不要附带修改其他属性！",
        "",
        "【Excel 表格操作】：",
        "- create_spreadsheet：从零创建 Excel 文件，传入二维数组（表头+数据行）",
        "- format_spreadsheet：格式化 Excel 单元格（字体/颜色/边框/底纹/数字格式/对齐/列宽行高）",
        "- add_chart：在 Excel 中创建图表（饼图pie/折线图line/柱状图column/条形图bar）",
        "- validate_spreadsheet：添加数据有效性（整数范围、下拉菜单、禁止重复、自定义公式）",
        "- 创建表格后会返回 file_id，后续格式化、图表、验证都使用该 file_id",
        "- 需要对不同区域设置不同格式时，分多次调用 format_spreadsheet",
        "- 常用数字格式代码：货币 '¥#,##0.00'  日期 'yyyy/mm/dd'  百分比 '0.00%'  整数 '#,##0'",
        "",
        "【图表创建要点】：",
        "- add_chart 必须在数据已存在的 Excel 文件上操作，先用 create_spreadsheet 创建好数据",
        "- data_range 和 categories_range 都必须包含表头行（第1行），如 'A1:A6' 而非 'A2:A6'",
        "- 饼图：data_range 为数值列（如 'B1:B4'），categories_range 为标签列（如 'A1:A4'）",
        "- 折线图/柱状图：可用 multi_series 展示多条线/多组柱",
        "- position 指定图表左上角放置位置（如 'E2'），避免覆盖数据区域",
        "",
        "【数据分析任务处理流程】：",
        "当用户要求整理数据、统计汇总、绘制图表时，按以下步骤执行：",
        "1. 用 create_spreadsheet 创建结构化数据表（原始数据 + 统计汇总行/列）",
        "2. 用 format_spreadsheet 格式化表头、数据区、汇总区",
        "3. 用 add_chart 创建所需图表（饼图、折线图等），图表数据来自步骤1的表格",
        "4. 如需文字总结，直接在回复文本中撰写",
        "- 不要只用文字描述图表，必须实际调用 add_chart 创建图表文件！",
        "- 统计计算（求和、平均值等）在创建表格时直接算好填入数据，不要用 Excel 公式",
        "- 常用填充色：浅灰 'D9D9D9'  浅蓝 'DAEEF3'  浅黄 'FFF2CC'  浅绿 'E2EFDA'",
        "",
        "【多步骤任务处理】：",
        "- 用户可能一次给出多个步骤要求（如建表+格式化+验证），按顺序逐步执行所有工具调用",
        "- 每步完成后继续执行下一步，不要中途停止",
        "",
        "【智能模板填表】fill_template_smart：",
        "- 触发场景：用户同时上传一个数据源 Excel + 一个模板 Excel，要求按模板格式把数据填进去",
        "- 关键词：模板填充、按模板填表、智能填表、填到模板、把数据填到、根据模板生成",
        "- 自动能力：识别合并表头、跳过英文/空行/省份分组行、模糊匹配列名、清洗脏数据(8659! 6il3 1337())、自动算总和/平均值",
        "- 调用方法：fill_template_smart({template_file_id, data_file_id})",
        "- 哪个是模板？通常文件名含「模板」或体积较小，仅有表头无数据；数据源文件较大、含真实数值",
        "- 调用前请先用 list_files 确认两个文件的 file_id，再调用 fill_template_smart",
        "- 调用成功后向用户说明：填了多少行、识别到多少列、是否计算了总和/平均值，并提示在「已修改的文档」中下载",
    ]

    if files:
        file_lines = []
        for f in files[:20]:
            file_lines.append(f"  - [ID={f.id}] {f.file_name} ({f.file_type}, {f.chunk_count or 0}片段, {f.field_count or 0}字段)")
        parts.append("")
        parts.append("用户已上传的文档：")
        parts.extend(file_lines)

    if extracts_summary:
        parts.append("")
        parts.append(extracts_summary)

    return "\n".join(parts)


@router.post("/ask")
async def ask(req: ChatReq, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    query = req.message.strip()
    if not query:
        return {"answer": "请输入你的问题。", "session_id": req.session_id}

    # ---- 会话管理 ----
    chat_sess: Optional[ChatSession] = None
    if req.session_id is not None:
        chat_sess = (
            db.query(ChatSession)
            .filter(ChatSession.id == req.session_id, ChatSession.user_id == user.id)
            .first()
        )
        if not chat_sess:
            raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    else:
        chat_sess = ChatSession(user_id=user.id, title=_title_from_message(query))
        db.add(chat_sess)
        db.commit()
        db.refresh(chat_sess)

    # ---- 检索文档上下文 ----
    chunks = []
    try:
        chunks = vs_search(query, 10, user_id=user.id)
    except Exception as e:
        logger.warning(f"向量搜索失败: {e}")

    files = db.query(FileInfo).filter(
        FileInfo.upload_user == user.id, FileInfo.parse_status == "done"
    ).all()

    extract_summary = ""
    if files:
        extracts = (
            db.query(ExtractResult)
            .filter(ExtractResult.file_id.in_([f.id for f in files[:10]]))
            .limit(50)
            .all()
        )
        if extracts:
            ext_lines = []
            for e in extracts[:30]:
                val = str(e.field_value or "")[:60]
                ext_lines.append(f"  {e.field_name}: {val}")
            extract_summary = "已提取的关键字段：\n" + "\n".join(ext_lines)

    doc_context = ""
    if chunks:
        ctx_parts = []
        for c in chunks:
            text = c.get("chunk_text", "")[:800]
            fname = c.get("file_name", "")
            if text.strip():
                ctx_parts.append(f"[来源: {fname}]\n{text}")
        if ctx_parts:
            doc_context = "\n\n---\n\n".join(ctx_parts)

    system = _build_system_prompt(files, extract_summary, has_tools=True)

    # ---- 构建消息 ----
    messages = []
    if req.history:
        for h in req.history[-8:]:
            messages.append({"role": h.role, "content": h.content})

    # ---- 收集所有选中的文件 ID（兼容单选 file_id 和多选 file_ids）----
    all_file_ids = []
    if req.file_ids:
        all_file_ids = list(req.file_ids)
    elif req.file_id:
        all_file_ids = [req.file_id]

    target_file_hint = ""
    image_paths = []
    if all_file_ids:
        doc_hints = []
        for fid in all_file_ids:
            fi = db.query(FileInfo).filter(FileInfo.id == fid, FileInfo.upload_user == user.id).first()
            if not fi:
                continue
            if fi.file_type in ("png", "jpg", "jpeg", "gif", "webp", "bmp"):
                image_paths.append(fi.file_path)
            else:
                doc_hints.append(f"ID={fi.id}, 文件名={fi.file_name}, 类型={fi.file_type}")
        if doc_hints:
            hint_lines = "\n".join(f"  - {h}" for h in doc_hints)
            target_file_hint = f"\n\n【用户指定操作的目标文件】\n{hint_lines}\n请在调用工具时使用对应的 file_id。"
        if image_paths:
            target_file_hint += f"\n\n【用户附带了 {len(image_paths)} 张图片，请仔细查看图片内容并据此完成用户的要求】"

    user_msg = query
    if doc_context:
        user_msg = f"用户问题：{query}\n\n以下是从文档中检索到的相关内容：\n\n{doc_context}"
    if target_file_hint:
        user_msg += target_file_hint

    # 如果有图片，构建多模态消息（替换 history 中最后一条 user 消息）
    if image_paths:
        from services.llm_service import build_image_content_blocks
        multimodal_content = build_image_content_blocks(image_paths, user_msg)
        replaced = False
        for idx in range(len(messages) - 1, -1, -1):
            if messages[idx].get("role") == "user" and isinstance(messages[idx].get("content"), str):
                messages[idx]["content"] = multimodal_content
                replaced = True
                break
        if not replaced:
            messages.append({"role": "user", "content": multimodal_content})
    else:
        if not messages or messages[-1].get("content") != query:
            messages.append({"role": "user", "content": user_msg})
        else:
            messages[-1]["content"] = user_msg

    # ---- Agent 循环 (tool-use) ----
    tool_actions = []
    final_answer = ""

    try:
        for _round in range(MAX_TOOL_ROUNDS):
            resp = await call_llm(
                messages, system=system, temperature=0.3,
                max_tokens=3000, tools=AGENT_TOOLS,
            )

            text_part = resp.get("text", "")
            tool_calls = resp.get("tool_calls", [])
            stop_reason = resp.get("stop_reason", "")

            if not tool_calls or stop_reason != "tool_use":
                final_answer = text_part
                break

            assistant_content = []
            if text_part:
                assistant_content.append({"type": "text", "text": text_part})
            for tc in tool_calls:
                assistant_content.append({
                    "type": "tool_use", "id": tc["id"],
                    "name": tc["name"], "input": tc["input"],
                })
            messages.append({"role": "assistant", "content": assistant_content})

            tool_results_content = []
            for tc in tool_calls:
                t_name = tc["name"]
                t_input = tc["input"]
                logger.info(f"Agent 执行工具: {t_name}({json.dumps(t_input, ensure_ascii=False)[:200]})")
                try:
                    result_str = await execute_tool(t_name, t_input, user.id)
                except Exception as te:
                    logger.error(f"工具执行异常 {t_name}: {te}", exc_info=True)
                    result_str = json.dumps({"error": str(te)[:200]})

                tool_actions.append({
                    "tool": t_name, "input": t_input,
                    "result": result_str[:500],
                })
                tool_results_content.append({
                    "type": "tool_result", "tool_use_id": tc["id"],
                    "content": result_str[:4000],
                })
            messages.append({"role": "user", "content": tool_results_content})
        else:
            final_answer = final_answer or "操作已完成（达到最大轮次）。"

    except Exception as e:
        logger.error(f"Agent 循环异常: {e}", exc_info=True)
        final_answer = f"抱歉，处理请求时出现错误：{str(e)[:200]}"

    if not final_answer:
        final_answer = "操作已完成。"

    # ---- 持久化 ----
    db.add(ChatMessage(session_id=chat_sess.id, role="user", content=query))

    assistant_content_to_save = final_answer
    if tool_actions:
        _friendly = {
            "search_documents": "检索文档内容",
            "extract_info": "提取关键信息",
            "list_files": "查询文件列表",
            "edit_document": "编辑文档内容",
            "analyze_document": "分析文档结构",
            "format_document": "调整文档格式",
            "create_spreadsheet": "创建Excel表格",
            "format_spreadsheet": "格式化Excel表格",
            "add_chart": "创建图表",
            "validate_spreadsheet": "设置数据有效性",
            "get_extract_results": "获取提取结果",
            "fill_template_smart": "智能模板填表",
        }
        action_lines = []
        for a in tool_actions:
            label = _friendly.get(a["tool"], a["tool"])
            inp = a.get("input", {})
            detail = ""
            if a["tool"] == "format_document":
                parts = []
                if inp.get("style_target"):
                    st_map = {"heading": "标题", "heading_1": "一级标题", "heading_2": "二级标题", "heading_3": "三级标题", "body": "正文", "all": "全文"}
                    parts.append(f"筛选: {st_map.get(inp['style_target'], inp['style_target'])}")
                if inp.get("font_name"):
                    parts.append(f"字体: {inp['font_name']}")
                if inp.get("font_size"):
                    parts.append(f"字号: {inp['font_size']}磅")
                if inp.get("bold") is not None:
                    parts.append("加粗" if inp["bold"] else "取消加粗")
                if inp.get("alignment"):
                    al_map = {"center": "居中", "left": "左对齐", "right": "右对齐", "justify": "两端对齐"}
                    parts.append(al_map.get(inp["alignment"], inp["alignment"]))
                if inp.get("color"):
                    color_map = {"FF0000": "红色", "0000FF": "蓝色", "000000": "黑色", "008000": "绿色"}
                    c = inp["color"].lstrip("#").upper()
                    parts.append(f"颜色: {color_map.get(c, '#' + c)}")
                if parts:
                    detail = "（" + "、".join(parts) + "）"
            elif a["tool"] == "analyze_document":
                detail = ""
            elif a["tool"] == "edit_document":
                ops = inp.get("operations", [])
                detail = f"（{len(ops)}项修改）" if ops else ""
            elif a["tool"] == "search_documents":
                q = inp.get("query", "")
                detail = f"（关键词：{q}）" if q else ""
            action_lines.append(f"- {label}{detail}")
        action_summary = "\n".join(action_lines)
        assistant_content_to_save = f"{final_answer}\n\n---\n**已完成的处理：**\n{action_summary}"

    db.add(ChatMessage(session_id=chat_sess.id, role="assistant", content=assistant_content_to_save))
    chat_sess.updated_at = datetime.now()
    db.commit()

    edited_files = []
    seen_fids = set()
    for a in tool_actions:
        if a["tool"] in ("edit_document", "format_document", "create_spreadsheet", "format_spreadsheet", "add_chart", "validate_spreadsheet", "fill_template_smart"):
            fid = a.get("input", {}).get("file_id")
            if not fid and a["tool"] in ("create_spreadsheet", "fill_template_smart"):
                try:
                    res = json.loads(a.get("result", "{}"))
                    fid = res.get("file_id")
                except Exception:
                    pass
            if fid and fid not in seen_fids:
                seen_fids.add(fid)
                fi = db.query(FileInfo).filter(FileInfo.id == fid).first()
                if fi:
                    edited_files.append({"id": fi.id, "name": fi.file_name, "type": fi.file_type})

    return {
        "answer": final_answer,
        "session_id": chat_sess.id,
        "tool_actions": tool_actions if tool_actions else None,
        "edited_files": edited_files if edited_files else None,
    }
