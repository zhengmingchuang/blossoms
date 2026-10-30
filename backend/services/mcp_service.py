import json
from typing import List, Dict, Any
from services import vector_store
from services.extractor import hybrid_extract, rule_extract

MCP_TOOLS = [
    {
        "name": "search_documents",
        "description": "语义搜索已上传的文档，返回最相关的文档片段。用于查找特定信息。",
        "input_schema": {"type": "object", "properties": {
            "query": {"type": "string", "description": "搜索关键词或问题"},
            "top_k": {"type": "integer", "default": 5, "description": "返回结果数量"}
        }, "required": ["query"]}
    },
    {
        "name": "list_files",
        "description": "列出所有已上传的文件及其解析状态、字段数量等信息。",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_file_content",
        "description": "获取指定文件的解析内容（文档片段列表）。",
        "input_schema": {"type": "object", "properties": {
            "file_id": {"type": "integer", "description": "文件ID"}
        }, "required": ["file_id"]}
    },
    {
        "name": "extract_fields",
        "description": "从给定文本中提取结构化字段信息。可指定要提取的字段名列表，也可自动提取所有关键信息。",
        "input_schema": {"type": "object", "properties": {
            "text": {"type": "string", "description": "要提取的文本内容"},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "可选，指定要提取的字段名列表"}
        }, "required": ["text"]}
    },
    {
        "name": "extract_from_file",
        "description": "对指定文件执行信息提取（规则提取+LLM提取），将结果存入数据库。返回提取到的字段列表。",
        "input_schema": {"type": "object", "properties": {
            "file_id": {"type": "integer", "description": "文件ID"},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "可选，指定要提取的字段名"}
        }, "required": ["file_id"]}
    },
    {
        "name": "batch_extract",
        "description": "对所有已解析完成的文件批量执行信息提取。返回每个文件的提取结果摘要。",
        "input_schema": {"type": "object", "properties": {
            "fields": {"type": "array", "items": {"type": "string"}, "description": "可选，指定要提取的字段名"}
        }}
    },
    {
        "name": "get_fused_data",
        "description": "获取多源融合后的结构化数据。将所有文件的提取结果进行去重、加权融合，返回最终字段值。",
        "input_schema": {"type": "object", "properties": {}}
    },
    {
        "name": "get_extracted_fields",
        "description": "获取指定文件或所有文件已提取的字段列表。",
        "input_schema": {"type": "object", "properties": {
            "file_id": {"type": "integer", "description": "可选，指定文件ID。不传则返回所有文件的字段。"}
        }}
    },
    {
        "name": "match_template_fields",
        "description": "根据模板字段名列表，从已提取数据和文档中匹配对应值。用于自动填写表格。",
        "input_schema": {"type": "object", "properties": {
            "template_fields": {"type": "array", "items": {"type": "string"}, "description": "模板中需要填写的字段名列表"}
        }, "required": ["template_fields"]}
    },
    {
        "name": "summarize_document",
        "description": "对指定文件生成结构化摘要，包括主题、关键数据、核心结论等。",
        "input_schema": {"type": "object", "properties": {
            "file_id": {"type": "integer", "description": "文件ID"},
            "focus": {"type": "string", "description": "可选，摘要关注的方向（如'经济数据'、'环境指标'）"}
        }, "required": ["file_id"]}
    },
    {
        "name": "compare_documents",
        "description": "对比两个或多个文件中的同名指标/数据，找出差异和变化趋势。",
        "input_schema": {"type": "object", "properties": {
            "file_ids": {"type": "array", "items": {"type": "integer"}, "description": "要对比的文件ID列表"},
            "fields": {"type": "array", "items": {"type": "string"}, "description": "可选，指定要对比的字段名"}
        }, "required": ["file_ids"]}
    },
    {
        "name": "generate_report",
        "description": "根据已提取的数据和文档内容，生成指定主题的分析报告。",
        "input_schema": {"type": "object", "properties": {
            "topic": {"type": "string", "description": "报告主题"},
            "file_ids": {"type": "array", "items": {"type": "integer"}, "description": "可选，指定数据来源文件"}
        }, "required": ["topic"]}
    },
    {
        "name": "fill_template_auto",
        "description": "自动填写指定模板，从已上传文档中匹配数据。返回填写结果和下载链接。",
        "input_schema": {"type": "object", "properties": {
            "template_id": {"type": "integer", "description": "模板ID"},
            "requirement": {"type": "string", "description": "可选，用户的填表要求"}
        }, "required": ["template_id"]}
    },
]


async def execute_tool(name: str, inp: Dict, user_id: int = None) -> str:
    try:
        if name == "search_documents":
            r = vector_store.search(inp.get("query", ""), top_k=inp.get("top_k", 5))
            return json.dumps({"results": [{"file": x.get("file_name", ""), "text": x["chunk_text"][:400], "score": x.get("score", 0)} for x in r]}, ensure_ascii=False)

        elif name == "list_files":
            return _list_files(user_id)

        elif name == "get_file_content":
            chunks = vector_store.get_all(file_id=inp.get("file_id"))
            return json.dumps({"total": len(chunks), "chunks": [{"text": c["chunk_text"][:600], "type": c.get("chunk_type", ""), "position": c.get("source_position", "")} for c in chunks[:50]]}, ensure_ascii=False)

        elif name == "extract_fields":
            r = await hybrid_extract(inp.get("text", ""), fields=inp.get("fields"))
            return json.dumps({"fields": r, "count": len(r)}, ensure_ascii=False, default=str)

        elif name == "extract_from_file":
            return await _extract_from_file(inp.get("file_id"), inp.get("fields"), user_id)

        elif name == "batch_extract":
            return await _batch_extract(inp.get("fields"), user_id)

        elif name == "get_fused_data":
            return _get_fused(user_id)

        elif name == "get_extracted_fields":
            return _get_extracted_fields(inp.get("file_id"), user_id)

        elif name == "match_template_fields":
            return await _match_template(inp.get("template_fields", []), user_id)

        elif name == "summarize_document":
            return await _summarize_document(inp.get("file_id"), inp.get("focus", ""), user_id)

        elif name == "compare_documents":
            return await _compare_documents(inp.get("file_ids", []), inp.get("fields"), user_id)

        elif name == "generate_report":
            return await _generate_report(inp.get("topic", ""), inp.get("file_ids"), user_id)

        elif name == "fill_template_auto":
            return await _fill_template_auto(inp.get("template_id"), inp.get("requirement", ""), user_id)

        return json.dumps({"error": f"unknown tool: {name}"})
    except Exception as e:
        return json.dumps({"error": str(e)}, ensure_ascii=False)


def _list_files(user_id):
    from database import SessionLocal
    from models import FileInfo
    db = SessionLocal()
    try:
        q = db.query(FileInfo)
        if user_id:
            q = q.filter(FileInfo.upload_user == user_id)
        files = q.all()
        return json.dumps({"files": [
            {"id": f.id, "name": f.file_name, "type": f.file_type, "status": f.parse_status,
             "chunks": f.chunk_count or 0, "fields": f.field_count or 0}
            for f in files
        ]}, ensure_ascii=False)
    finally:
        db.close()


async def _extract_from_file(file_id, fields, user_id):
    from database import SessionLocal
    from models import FileInfo, ExtractResult, DocChunk
    db = SessionLocal()
    try:
        fi = db.query(FileInfo).filter(FileInfo.id == file_id).first()
        if not fi:
            return json.dumps({"error": "文件不存在"})
        if fi.parse_status != "done":
            return json.dumps({"error": "文件尚未解析完成"})

        chunks = db.query(DocChunk).filter(DocChunk.file_id == file_id).order_by(DocChunk.chunk_index).all()
        full = "\n\n".join(c.chunk_text for c in chunks)

        rule_fields = rule_extract(full)
        extracted = await hybrid_extract(full[:15000], fields=fields)

        merged = dict(rule_fields)
        for k, v in extracted.items():
            if not k.startswith("_"):
                merged[k] = v

        new_count = 0
        for k, v in merged.items():
            val_str = str(v) if not isinstance(v, list) else str(v[0])
            existing = db.query(ExtractResult).filter(ExtractResult.file_id == file_id, ExtractResult.field_name == k).first()
            best_pos = ""
            for ch in chunks:
                if val_str in ch.chunk_text:
                    best_pos = ch.source_position or ""
                    break
            if existing:
                existing.field_value = val_str
                existing.confidence = 0.90
                existing.extract_method = "mcp"
            else:
                db.add(ExtractResult(file_id=file_id, field_name=k, field_value=val_str, confidence=0.85, extract_method="mcp", source_position=best_pos))
                new_count += 1

        total = db.query(ExtractResult).filter(ExtractResult.file_id == file_id).count()
        fi.field_count = total
        db.commit()

        return json.dumps({
            "file": fi.file_name, "new_fields": new_count, "total_fields": total,
            "sample": {k: str(v)[:100] for k, v in list(merged.items())[:20]}
        }, ensure_ascii=False, default=str)
    finally:
        db.close()


async def _batch_extract(fields, user_id):
    from database import SessionLocal
    from models import FileInfo
    db = SessionLocal()
    try:
        q = db.query(FileInfo).filter(FileInfo.parse_status == "done")
        if user_id:
            q = q.filter(FileInfo.upload_user == user_id)
        files = q.all()
        results = []
        for f in files:
            r = await _extract_from_file(f.id, fields, user_id)
            results.append(json.loads(r))
        return json.dumps({"total_files": len(files), "results": results}, ensure_ascii=False)
    finally:
        db.close()


def _get_fused(user_id):
    from database import SessionLocal
    from models import FileInfo, ExtractResult
    from services.fusion import fuse
    db = SessionLocal()
    try:
        q = db.query(FileInfo)
        if user_id:
            q = q.filter(FileInfo.upload_user == user_id)
        exts = []
        for f in q.all():
            rs = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).all()
            if rs:
                exts.append({"file_id": f.id, "file_name": f.file_name,
                             "source_type": "table" if f.file_type in ("xlsx", "xls") else "paragraph",
                             "fields": {r.field_name: r.field_value for r in rs}})
        fused = fuse(exts)
        return json.dumps({
            "total": len(fused),
            "fused": [{"field": r["field_name"], "value": r["field_value"], "confidence": r["confidence"], "source": r.get("source_file", "")} for r in fused]
        }, ensure_ascii=False)
    finally:
        db.close()


def _get_extracted_fields(file_id, user_id):
    from database import SessionLocal
    from models import FileInfo, ExtractResult
    db = SessionLocal()
    try:
        if file_id:
            rs = db.query(ExtractResult).filter(ExtractResult.file_id == file_id).all()
            return json.dumps({"file_id": file_id, "total": len(rs),
                               "fields": [{"name": r.field_name, "value": r.field_value[:100], "confidence": r.confidence or 0, "method": r.extract_method or ""} for r in rs]}, ensure_ascii=False)
        else:
            q = db.query(FileInfo)
            if user_id:
                q = q.filter(FileInfo.upload_user == user_id)
            result = []
            for f in q.all():
                cnt = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).count()
                result.append({"file_id": f.id, "file_name": f.file_name, "field_count": cnt})
            return json.dumps({"files": result, "total_fields": sum(r["field_count"] for r in result)}, ensure_ascii=False)
    finally:
        db.close()


async def _match_template(template_fields, user_id):
    from database import SessionLocal
    from models import FileInfo, ExtractResult
    from services.fusion import fuse
    from services.vector_store import search_for_fields
    from services.llm_service import match_fields
    db = SessionLocal()
    try:
        q = db.query(FileInfo)
        if user_id:
            q = q.filter(FileInfo.upload_user == user_id)
        exts = []
        all_extract = {}
        for f in q.all():
            rs = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).all()
            if rs:
                fdict = {r.field_name: r.field_value for r in rs}
                exts.append({"file_id": f.id, "file_name": f.file_name,
                             "source_type": "table" if f.file_type in ("xlsx", "xls") else "paragraph",
                             "fields": fdict})
                all_extract.update(fdict)
        fused = fuse(exts)
        fused_dict = {r["field_name"]: r["field_value"] for r in fused}

        # 直接匹配
        matched = {}
        for fn in template_fields:
            fn_l = fn.strip().lower()
            for k, v in {**fused_dict, **all_extract}.items():
                if fn_l == k.lower().strip() or fn_l in k.lower() or k.lower().strip() in fn_l:
                    matched[fn] = str(v)
                    break

        # 未匹配的用搜索+LLM
        unmatched = [f for f in template_fields if f not in matched or not matched[f]]
        if unmatched:
            relevant = search_for_fields(unmatched, top_k_per_field=3)
            ctx = "\n\n---\n\n".join(list(dict.fromkeys(r["chunk_text"][:400] for r in relevant))[:15])
            llm_matched = await match_fields(unmatched, {**fused_dict, **all_extract}, ctx)
            for fn in unmatched:
                if fn in llm_matched and llm_matched[fn]:
                    matched[fn] = str(llm_matched[fn])

        return json.dumps({
            "total_fields": len(template_fields),
            "matched": len([v for v in matched.values() if v]),
            "results": {fn: matched.get(fn, "") for fn in template_fields}
        }, ensure_ascii=False)
    finally:
        db.close()


async def _summarize_document(file_id, focus, user_id):
    from database import SessionLocal
    from models import FileInfo, DocChunk
    from services.llm_service import call_llm_text
    db = SessionLocal()
    try:
        fi = db.query(FileInfo).filter(FileInfo.id == file_id).first()
        if not fi:
            return json.dumps({"error": "文件不存在"})
        chunks = db.query(DocChunk).filter(DocChunk.file_id == file_id).order_by(DocChunk.chunk_index).all()
        text = "\n\n".join(c.chunk_text for c in chunks)[:12000]
        focus_hint = f"，重点关注：{focus}" if focus else ""
        sys = f"你是文档分析助手。请对以下文档生成结构化摘要{focus_hint}。包括：1)文档主题 2)关键数据/指标 3)核心结论 4)重要细节。用中文回答。"
        r = await call_llm_text([{"role": "user", "content": text}], system=sys, temperature=0.0, max_tokens=2000)
        return json.dumps({"file": fi.file_name, "summary": r}, ensure_ascii=False)
    finally:
        db.close()


async def _compare_documents(file_ids, fields, user_id):
    from database import SessionLocal
    from models import FileInfo, ExtractResult
    from services.llm_service import call_llm_text
    db = SessionLocal()
    try:
        file_data = {}
        for fid in file_ids:
            fi = db.query(FileInfo).filter(FileInfo.id == fid).first()
            if fi:
                rs = db.query(ExtractResult).filter(ExtractResult.file_id == fid).all()
                file_data[fi.file_name] = {r.field_name: r.field_value for r in rs}
        if len(file_data) < 2:
            return json.dumps({"error": "需要至少2个有效文件"})
        data_str = json.dumps(file_data, ensure_ascii=False)[:6000]
        fields_hint = f"重点对比字段：{json.dumps(fields, ensure_ascii=False)}" if fields else "对比所有同名字段"
        sys = f"你是数据对比分析助手。{fields_hint}。找出差异、变化趋势和异常值。用中文回答。"
        r = await call_llm_text([{"role": "user", "content": data_str}], system=sys, temperature=0.0, max_tokens=2000)
        return json.dumps({"files": list(file_data.keys()), "analysis": r}, ensure_ascii=False)
    finally:
        db.close()


async def _generate_report(topic, file_ids, user_id):
    from database import SessionLocal
    from models import FileInfo, ExtractResult
    from services.llm_service import call_llm_text
    db = SessionLocal()
    try:
        q = db.query(FileInfo)
        if user_id:
            q = q.filter(FileInfo.upload_user == user_id)
        if file_ids:
            q = q.filter(FileInfo.id.in_(file_ids))
        files = q.all()
        all_data = {}
        for f in files:
            rs = db.query(ExtractResult).filter(ExtractResult.file_id == f.id).all()
            if rs:
                all_data[f.file_name] = {r.field_name: r.field_value for r in rs[:50]}
        # 补充搜索
        search_results = vector_store.search(topic, top_k=10)
        ctx = "\n\n".join(r["chunk_text"][:400] for r in search_results)
        data_str = json.dumps(all_data, ensure_ascii=False)[:4000]
        sys = f"你是报告生成助手。根据提供的数据和文档内容，生成关于「{topic}」的分析报告。要求：结构清晰、数据准确、有分析结论。用中文回答。"
        r = await call_llm_text([{"role": "user", "content": f"数据：\n{data_str}\n\n文档片段：\n{ctx}"}], system=sys, temperature=0.1, max_tokens=3000)
        return json.dumps({"topic": topic, "report": r, "sources": list(all_data.keys())}, ensure_ascii=False)
    finally:
        db.close()


async def _fill_template_auto(template_id, requirement, user_id):
    import requests
    from database import SessionLocal
    from models import TemplateInfo
    db = SessionLocal()
    try:
        t = db.query(TemplateInfo).filter(TemplateInfo.id == template_id).first()
        if not t:
            return json.dumps({"error": "模板不存在"})
        if requirement and not t.user_requirement:
            t.user_requirement = requirement
            db.commit()
        return json.dumps({
            "message": f"请在模板填报页面点击「自动填写」按钮执行填充",
            "template_id": template_id,
            "template_name": t.template_name,
            "mode": t.template_mode or "field_fill",
            "user_requirement": t.user_requirement or requirement or ""
        }, ensure_ascii=False)
    finally:
        db.close()


def get_tools_list():
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"]} for t in MCP_TOOLS]


async def handle_jsonrpc(req: Dict, user_id: int = None) -> Dict:
    method, rid, params = req.get("method", ""), req.get("id"), req.get("params", {})
    if method == "initialize":
        return {"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": "2024-11-05", "capabilities": {"tools": {}}, "serverInfo": {"name": "DocFusion MCP", "version": "2.0.0"}}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": rid, "result": {"tools": get_tools_list()}}
    if method == "tools/call":
        r = await execute_tool(params.get("name", ""), params.get("arguments", {}), user_id)
        return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": r}]}}
    if method in ("initialized", "ping"):
        return {"jsonrpc": "2.0", "id": rid, "result": {}}
    return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Unknown: {method}"}}
