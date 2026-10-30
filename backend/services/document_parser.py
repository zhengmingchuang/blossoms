import re
from pathlib import Path
from typing import List, Dict, Any
import os
import docx
import openpyxl

from services.legacy_doc import convert_doc_to_docx_temp, is_ole_doc, is_zip_file

CHUNK_MAX = 800


def parse_document(file_path: str) -> List[Dict[str, Any]]:
    ext = Path(file_path).suffix.lower()
    parsers = {".docx": _parse_docx, ".doc": _parse_doc, ".md": _parse_md, ".xlsx": _parse_xlsx, ".xls": _parse_xls, ".txt": _parse_txt}
    parser = parsers.get(ext)
    if not parser:
        raise ValueError(f"不支持的文件类型: {ext}")
    return parser(file_path)


def _merge_short(chunks: List[Dict], min_len=80) -> List[Dict]:
    """合并过短的段落到上一个chunk，减少碎片"""
    if not chunks:
        return chunks
    merged = [chunks[0]]
    for ch in chunks[1:]:
        prev = merged[-1]
        if prev["chunk_type"] == "paragraph" and ch["chunk_type"] == "paragraph" and len(prev["chunk_text"]) < min_len:
            prev["chunk_text"] += "\n" + ch["chunk_text"]
        elif len(ch["chunk_text"]) < 20 and ch["chunk_type"] == "paragraph":
            prev["chunk_text"] += "\n" + ch["chunk_text"]
        else:
            merged.append(ch)
    for i, c in enumerate(merged):
        c["chunk_index"] = i
    return merged


def _parse_doc(fp: str) -> List[Dict[str, Any]]:
    """老版 .doc（OLE）；python-docx 仅支持 .docx，先转为临时 docx 再解析。"""
    if is_zip_file(fp):
        return _parse_docx(fp)
    if not is_ole_doc(fp):
        raise ValueError("不是有效的 Word 文档（.doc/.docx）")
    tmp = convert_doc_to_docx_temp(fp)
    if not tmp:
        raise ValueError(
            "无法解析老版 .doc：请在 Word/WPS 中「另存为」.docx 后上传，或安装 LibreOffice（可自动转换）；"
            "Windows 已安装 Microsoft Word 时也可自动转换。"
        )
    try:
        return _parse_docx(tmp)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _parse_docx(fp: str) -> List[Dict[str, Any]]:
    try:
        doc = docx.Document(fp)
    except Exception:
        import zipfile
        with zipfile.ZipFile(fp) as z:
            if "word/document.xml" not in z.namelist():
                raise ValueError("无法解析此 docx 文件")
        from lxml import etree
        zf = zipfile.ZipFile(fp)
        tree = etree.parse(zf.open("word/document.xml"))
        ns = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}
        chunks = []
        idx = 0
        for p in tree.findall(".//w:p", ns):
            texts = [t.text for t in p.findall(".//w:t", ns) if t.text]
            t = "".join(texts).strip()
            if t:
                chunks.append({"chunk_index": idx, "chunk_text": t, "chunk_type": "paragraph", "source_position": f"p-{idx}"})
                idx += 1
        for tbl in tree.findall(".//w:tbl", ns):
            rows_data = []
            for tr in tbl.findall(".//w:tr", ns):
                cells = []
                for tc in tr.findall(".//w:tc", ns):
                    cell_texts = [t.text for t in tc.findall(".//w:t", ns) if t.text]
                    cells.append("".join(cell_texts).strip())
                rows_data.append(cells)
            if rows_data:
                header = rows_data[0]
                lines = [" | ".join(header)]
                for r in rows_data[1:]:
                    lines.append(" | ".join(r))
                chunks.append({"chunk_index": idx, "chunk_text": "\n".join(lines), "chunk_type": "table", "source_position": f"table-{idx}"})
                idx += 1
        zf.close()
        return _merge_short(chunks)
    chunks = []
    idx = 0
    for p in doc.paragraphs:
        t = p.text.strip()
        if not t:
            continue
        style = p.style.name if p.style else ""
        ct = "title" if "heading" in style.lower() else "paragraph"
        chunks.append({"chunk_index": idx, "chunk_text": t, "chunk_type": ct, "source_position": f"p-{idx}"})
        idx += 1
    for ti, table in enumerate(doc.tables):
        rows_data = []
        for row in table.rows:
            rows_data.append([c.text.strip() for c in row.cells])
        if not rows_data:
            continue
        header = rows_data[0]
        batch_size = 30
        for start in range(1, len(rows_data), batch_size):
            batch = rows_data[start:start + batch_size]
            lines = [" | ".join(header)]
            for r in batch:
                lines.append(" | ".join(r))
            text = "\n".join(lines)
            if len(text) > CHUNK_MAX * 2:
                text = text[:CHUNK_MAX * 2]
            chunks.append({"chunk_index": idx, "chunk_text": text, "chunk_type": "table", "source_position": f"table-{ti}-r{start}"})
            idx += 1
    return _merge_short(chunks)


def _parse_md(fp: str) -> List[Dict[str, Any]]:
    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    chunks = []
    sections = re.split(r'\n(?=#{1,4}\s)', content)
    idx = 0
    for sec in sections:
        sec = sec.strip()
        if not sec:
            continue
        lines = sec.split("\n", 1)
        heading = lines[0].strip()
        ct = "title" if heading.startswith("#") else "paragraph"
        if len(sec) <= CHUNK_MAX:
            chunks.append({"chunk_index": idx, "chunk_text": sec, "chunk_type": ct, "source_position": f"sec-{idx}"})
            idx += 1
        else:
            paragraphs = re.split(r'\n{2,}', sec)
            for pi, para in enumerate(paragraphs):
                para = para.strip()
                if not para:
                    continue
                pct = "title" if para.startswith("#") else "paragraph"
                if len(para) > CHUNK_MAX:
                    for ci in range(0, len(para), CHUNK_MAX):
                        sub = para[ci:ci + CHUNK_MAX]
                        chunks.append({"chunk_index": idx, "chunk_text": sub, "chunk_type": pct, "source_position": f"sec-{idx}"})
                        idx += 1
                else:
                    chunks.append({"chunk_index": idx, "chunk_text": para, "chunk_type": pct, "source_position": f"sec-{idx}"})
                    idx += 1
    return _merge_short(chunks)


def _sheet_matrix_to_chunks(sn: str, all_rows: list, idx: int) -> tuple:
    """将单个工作表的行矩阵转为 chunks；all_rows 为 values_only 风格的 tuple 行列表。"""
    chunks: List[Dict[str, Any]] = []
    if not all_rows:
        return chunks, idx

    header_row_idx = _find_xlsx_header_row(all_rows)
    header = [str(c).strip().split('\n')[0].strip() if c is not None else "" for c in all_rows[header_row_idx]]
    header_clean = [h for h in header if h and h != "None"]
    if header_clean:
        chunks.append({"chunk_index": idx, "chunk_text": f"[{sn}] 表头: {' | '.join(header_clean)}", "chunk_type": "title", "source_position": f"{sn}-header"})
        idx += 1

    data_rows = all_rows[header_row_idx + 1:]
    total_data = len(data_rows)

    if total_data > 200:
        summary_lines = [f"[{sn}] 数据概览: 共{total_data}行, {len(header_clean)}列"]
        for ci, h in enumerate(header):
            if not h or h == "None":
                continue
            vals = set()
            sample_vals = []
            for row in data_rows[:500]:
                if ci < len(row) and row[ci] is not None:
                    v = str(row[ci]).strip()
                    if v and v != "None":
                        vals.add(v)
                        if len(sample_vals) < 5:
                            sample_vals.append(v)
            if vals:
                unique_count = len(vals)
                sample_str = ", ".join(sample_vals[:5])
                summary_lines.append(f"  {h}: {unique_count}个不同值, 示例=[{sample_str}]")
        chunks.append({"chunk_index": idx, "chunk_text": "\n".join(summary_lines), "chunk_type": "table", "source_position": f"{sn}-summary"})
        idx += 1

        sample_rows = data_rows[:50] + (data_rows[-10:] if total_data > 60 else [])
    else:
        sample_rows = data_rows

    batch_size = 25
    for start in range(0, len(sample_rows), batch_size):
        batch = sample_rows[start:start + batch_size]
        lines = []
        for ri, row in enumerate(batch, start + 1):
            vals = [str(c) if c is not None else "" for c in row]
            if not any(v and v != "None" for v in vals):
                continue
            pairs = []
            for j in range(min(len(header), len(vals))):
                v = vals[j]
                if v and v != "None":
                    h = header[j] if header[j] and header[j] != "None" else f"列{j+1}"
                    pairs.append(f"{h}: {v}")
            if pairs:
                lines.append(f"[行{ri}] " + " | ".join(pairs))
        if lines:
            text = f"[{sn}]\n" + "\n".join(lines)
            if len(text) > CHUNK_MAX * 2:
                text = text[:CHUNK_MAX * 2]
            chunks.append({"chunk_index": idx, "chunk_text": text, "chunk_type": "table", "source_position": f"{sn}-r{start+1}-{start+len(batch)}"})
            idx += 1
    return chunks, idx


def _parse_xls(fp: str) -> List[Dict[str, Any]]:
    """Excel 97-2003 .xls（openpyxl 不支持，需 xlrd）。"""
    import xlrd
    book = xlrd.open_workbook(fp, formatting_info=False)
    chunks: List[Dict[str, Any]] = []
    idx = 0
    for si in range(book.nsheets):
        sheet = book.sheet_by_index(si)
        sn = sheet.name
        all_rows = []
        for rowx in range(sheet.nrows):
            all_rows.append(tuple(sheet.row_values(rowx)))
            if len(all_rows) > 20000:
                break
        part, idx = _sheet_matrix_to_chunks(sn, all_rows, idx)
        chunks.extend(part)
    for i, c in enumerate(chunks):
        c["chunk_index"] = i
    return chunks


def _parse_xlsx(fp: str) -> List[Dict[str, Any]]:
    wb = openpyxl.load_workbook(fp, data_only=True, read_only=True)
    chunks = []
    idx = 0
    fallback_needed = False
    for sn in wb.sheetnames:
        ws = wb[sn]
        first_row = None
        for row in ws.iter_rows(max_row=1, values_only=True):
            first_row = list(row)
            break
        if first_row and sum(1 for v in first_row if v is not None) <= 1:
            fallback_needed = True
            break
    wb.close()

    # 用非 read_only 模式重新打开，以便正确处理合并单元格
    wb = openpyxl.load_workbook(fp, data_only=True)

    for sn in wb.sheetnames:
        ws = wb[sn]
        merged_map = {}
        for rng in ws.merged_cells.ranges:
            top_val = ws.cell(row=rng.min_row, column=rng.min_col).value
            for r in range(rng.min_row, rng.max_row + 1):
                for c in range(rng.min_col, rng.max_col + 1):
                    if r != rng.min_row or c != rng.min_col:
                        merged_map[(r, c)] = top_val

        all_rows = []
        for row in ws.iter_rows(values_only=False):
            vals = []
            for cell in row:
                key = (cell.row, cell.column)
                if key in merged_map:
                    vals.append(merged_map[key])
                else:
                    vals.append(cell.value)
            all_rows.append(tuple(vals))
            if len(all_rows) > 20000:
                break
        if not all_rows:
            continue
        part, idx = _sheet_matrix_to_chunks(sn, all_rows, idx)
        chunks.extend(part)
    wb.close()
    for i, c in enumerate(chunks):
        c["chunk_index"] = i
    return chunks


def _find_xlsx_header_row(all_rows: list) -> int:
    """从已加载的行数据中找到真实表头行的索引（0-based）。
    
    评分标准：短文本列最多、非纯数字、无Excel公式。
    扫描前15行，返回得分最高的行索引。
    """
    best_idx = 0
    best_score = -1
    for ri, row in enumerate(all_rows[:15]):
        cells = []
        for v in row:
            if v is None:
                continue
            text = str(v).strip()
            # 跳过含大量换行的单元格（公司信息/产品描述特征）
            if not text or text.count('\n') > 1:
                continue
            if len(text) > 200 and '\n' in text:
                continue
            cells.append(text)
        if len(cells) < 3:
            continue
        score = 0
        for c in cells:
            if c.startswith('='):
                continue  # Excel公式不是表头
            if c.replace('.', '').replace('-', '').replace(':', '').isdigit():
                score += 0.2  # 纯数字，可能是数据行
            elif len(c) <= 50:
                score += 1.0  # 短文本，表头特征
            else:
                score += 0.5
        if score > best_score:
            best_score = score
            best_idx = ri
    return best_idx


def _parse_txt(fp: str) -> List[Dict[str, Any]]:
    with open(fp, "r", encoding="utf-8", errors="ignore") as f:
        content = f.read()
    chunks = []
    idx = 0
    parts = re.split(r'\n(?=[一二三四五六七八九十（(]\s*[、.）)]|\d+[、.]\s)', content)
    for part in parts:
        part = part.strip()
        if not part:
            continue
        ct = "title" if re.match(r'^[一二三四五六七八九十（(]\s*[、.）)]|\d+[、.]\s', part) else "paragraph"
        if len(part) <= CHUNK_MAX:
            chunks.append({"chunk_index": idx, "chunk_text": part, "chunk_type": ct, "source_position": f"sec-{idx}"})
            idx += 1
        else:
            sub_parts = re.split(r'\n{2,}', part)
            for sp in sub_parts:
                sp = sp.strip()
                if not sp:
                    continue
                if len(sp) > CHUNK_MAX:
                    for ci in range(0, len(sp), CHUNK_MAX):
                        sub = sp[ci:ci + CHUNK_MAX]
                        chunks.append({"chunk_index": idx, "chunk_text": sub, "chunk_type": "paragraph", "source_position": f"sec-{idx}"})
                        idx += 1
                else:
                    chunks.append({"chunk_index": idx, "chunk_text": sp, "chunk_type": "paragraph", "source_position": f"sec-{idx}"})
                    idx += 1
    return _merge_short(chunks)
