"""
图片填充工具
支持两种方案：
  A. DISPIMG 复制（WPS 专有，直接复制 cellimages.xml + media 文件）
  B. openpyxl 浮动图片插入（Excel/WPS 通用）
"""
import re
import zipfile
import shutil
import os
import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET

logger = logging.getLogger(__name__)

# DISPIMG 公式正则（兼容 =DISPIMG 和 =_xlfn.DISPIMG 两种格式）
_DISPIMG_RE = re.compile(r'=(?:_xlfn\.)?DISPIMG\("([^"]+)"', re.IGNORECASE)


# ─────────────────────────────────────────────
# 公共：解析源文件的 DISPIMG ID → 图片文件 映射
# ─────────────────────────────────────────────

def parse_dispimg_map(src_xlsx: str) -> Dict[str, str]:
    """
    解析源 xlsx 中 cellimages.xml 的 ID → rId 映射，
    再结合 rels 得到 ID → 媒体文件路径（zip 内路径）。
    返回 {dispimg_id: "xl/media/imageXX.png", ...}
    """
    id_to_media: Dict[str, str] = {}
    try:
        with zipfile.ZipFile(src_xlsx, 'r') as z:
            names = set(z.namelist())
            if 'xl/cellimages.xml' not in names:
                return id_to_media

            # 1. 解析 cellimages.xml: name(ID) -> rId
            xml_bytes = z.read('xl/cellimages.xml')
            root = ET.fromstring(xml_bytes)
            ns = {
                'etc': 'http://www.wps.cn/officeDocument/2017/etCustomData',
                'xdr': 'http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing',
                'a':   'http://schemas.openxmlformats.org/drawingml/2006/main',
                'r':   'http://schemas.openxmlformats.org/officeDocument/2006/relationships',
            }
            id_to_rid: Dict[str, str] = {}
            for pic in root.findall('.//xdr:pic', ns):
                cnvpr = pic.find('xdr:nvPicPr/xdr:cNvPr', ns)
                blip  = pic.find('xdr:blipFill/a:blip', ns)
                if cnvpr is not None and blip is not None:
                    img_id = cnvpr.get('name', '')
                    rid    = blip.get('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed', '')
                    if img_id and rid:
                        id_to_rid[img_id] = rid

            # 2. 解析 rels: rId -> 媒体文件
            rels_path = 'xl/_rels/cellimages.xml.rels'
            if rels_path not in names:
                return id_to_media
            rels_bytes = z.read(rels_path)
            rels_root  = ET.fromstring(rels_bytes)
            rid_to_target: Dict[str, str] = {}
            for rel in rels_root:
                rid    = rel.get('Id', '')
                target = rel.get('Target', '')
                if rid and target:
                    # Target 是相对路径，如 "media/image1.png"
                    rid_to_target[rid] = 'xl/' + target if not target.startswith('xl/') else target

            # 3. 合并
            for img_id, rid in id_to_rid.items():
                if rid in rid_to_target:
                    id_to_media[img_id] = rid_to_target[rid]

    except Exception as e:
        logger.warning(f'[image_filler] parse_dispimg_map 失败: {e}')
    return id_to_media


# ─────────────────────────────────────────────
# 方案 A：DISPIMG 复制
# ─────────────────────────────────────────────

def inject_dispimg(
    src_xlsx: str,
    dst_xlsx: str,
    used_ids: List[str],
) -> bool:
    """
    把 src_xlsx 中的 cellimages.xml / rels / 媒体文件原封不动复制到 dst_xlsx，
    同时把 sheet1.xml 里 openpyxl 写入的 inlineStr 格式 DISPIMG 替换成
    WPS 需要的 t="str" + <f>_xlfn.DISPIMG 格式。
    """
    if not used_ids:
        return True
    try:
        with zipfile.ZipFile(src_xlsx, 'r') as src_z:
            src_names = set(src_z.namelist())
            if 'xl/cellimages.xml' not in src_names:
                logger.warning('[image_filler] 源文件无 cellimages.xml，跳过方案A')
                return False

            # 收集需要从源文件复制的所有图片相关文件
            files_to_copy: Dict[str, bytes] = {}
            # cellimages 核心文件
            files_to_copy['xl/cellimages.xml'] = src_z.read('xl/cellimages.xml')
            if 'xl/_rels/cellimages.xml.rels' in src_names:
                files_to_copy['xl/_rels/cellimages.xml.rels'] = src_z.read('xl/_rels/cellimages.xml.rels')
            # 所有媒体文件
            for name in src_names:
                if name.startswith('xl/media/') and name != 'xl/media/':
                    files_to_copy[name] = src_z.read(name)
            # drawings 目录（WPS 需要）
            for name in src_names:
                if name.startswith('xl/drawings/'):
                    files_to_copy[name] = src_z.read(name)
            # 注意：不复制 sharedStrings.xml 和 sheet1.xml.rels，
            # 目标文件有自己的字符串表和 rels，直接覆盖会导致 #REF! 错误

        if not files_to_copy:
            return False

        # 重写 dst_xlsx
        tmp_path = dst_xlsx + '.tmp'
        with zipfile.ZipFile(dst_xlsx, 'r') as dst_z, \
             zipfile.ZipFile(tmp_path, 'w', compression=zipfile.ZIP_DEFLATED) as out_z:

            dst_names = set(dst_z.namelist())
            skip = set(files_to_copy.keys())

            ct_path      = '[Content_Types].xml'
            wb_rels_path = 'xl/_rels/workbook.xml.rels'
            new_ct_xml      = None
            new_wb_rels_xml = None

            if ct_path in dst_names:
                ct_xml = dst_z.read(ct_path).decode('utf-8')
                new_ct_xml = _ensure_content_types(ct_xml).encode('utf-8')
                skip.add(ct_path)

            if wb_rels_path in dst_names:
                wb_rels = dst_z.read(wb_rels_path).decode('utf-8')
                new_wb_rels_xml = _ensure_workbook_rels(wb_rels).encode('utf-8')
                skip.add(wb_rels_path)

            # 修复 sheet1.xml.rels：在目标文件已有 rels 基础上追加 drawing 关系
            sheet_rels_path = 'xl/worksheets/_rels/sheet1.xml.rels'
            new_sheet_rels_xml = None
            if sheet_rels_path in dst_names:
                sheet_rels = dst_z.read(sheet_rels_path).decode('utf-8')
                new_sheet_rels_xml = _ensure_drawing_rel(sheet_rels).encode('utf-8')
            else:
                # 目标文件没有 rels，新建一个
                new_sheet_rels_xml = (
                    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                    '<Relationship Id="rId1" '
                    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" '
                    'Target="../drawings/drawing1.xml"/>'
                    '</Relationships>'
                ).encode('utf-8')
            skip.add(sheet_rels_path)

            # 修复 sheet1.xml：
            # 1. 把 openpyxl 写入的 DISPIMG 格式替换成 WPS 需要的格式
            # 2. 确保末尾有 <drawing r:id="rId1"/> 引用
            sheet_path = 'xl/worksheets/sheet1.xml'
            new_sheet_xml = None
            if sheet_path in dst_names:
                sheet_xml = dst_z.read(sheet_path).decode('utf-8')
                sheet_xml = _fix_dispimg_in_sheet(sheet_xml, used_ids)
                # 确保 <drawing r:id="rId1"/> 在 </worksheet> 前
                if '<drawing ' not in sheet_xml:
                    sheet_xml = sheet_xml.replace(
                        '</worksheet>',
                        '<drawing r:id="rId1"/></worksheet>'
                    )
                new_sheet_xml = sheet_xml.encode('utf-8')
                skip.add(sheet_path)

            # 复制 dst 里其余文件
            for item in dst_z.infolist():
                if item.filename in skip:
                    continue
                out_z.writestr(item, dst_z.read(item.filename))

            # 写入修改后的 XML
            if new_ct_xml:
                out_z.writestr(ct_path, new_ct_xml)
            if new_wb_rels_xml:
                out_z.writestr(wb_rels_path, new_wb_rels_xml)
            if new_sheet_xml:
                out_z.writestr(sheet_path, new_sheet_xml)
            if new_sheet_rels_xml:
                out_z.writestr(sheet_rels_path, new_sheet_rels_xml)

            # 原封不动写入源文件的图片资源
            for path, data in files_to_copy.items():
                out_z.writestr(path, data)

        os.replace(tmp_path, dst_xlsx)
        logger.info(f'[image_filler] 方案A成功，复制 {len(files_to_copy)} 个图片资源')
        return True

    except Exception as e:
        logger.error(f'[image_filler] 方案A失败: {e}')
        if os.path.exists(dst_xlsx + '.tmp'):
            os.remove(dst_xlsx + '.tmp')
        return False


def _fix_dispimg_in_sheet(sheet_xml: str, used_ids: List[str]) -> str:
    """
    把 openpyxl 写入的各种 DISPIMG 格式替换成 WPS 需要的格式：
      <c r="Hx" s="N" t="str"><f>_xlfn.DISPIMG(&quot;ID_xxx&quot;,1)</f><v>=DISPIMG(&quot;ID_xxx&quot;,1)</v></c>

    openpyxl 可能写入的格式：
      1. <c r="Hx" s="N"><f>DISPIMG("ID_xxx",1)</f><v></v></c>   ← 最常见
      2. <c r="Hx" ... t="inlineStr"><is><t>=DISPIMG("ID_xxx",1)</t></is></c>
      3. <c r="Hx" ...><v>=DISPIMG("ID_xxx",1)</v></c>
    """

    def _make_wps_cell(col_row: str, style: str, img_id: str) -> str:
        escaped = f'&quot;{img_id}&quot;'
        f_tag = f'_xlfn.DISPIMG({escaped},1)'
        v_tag = f'=DISPIMG({escaped},1)'
        s_attr = f' s="{style}"' if style else ''
        return f'<c r="{col_row}"{s_attr} t="str"><f>{f_tag}</f><v>{v_tag}</v></c>'

    def _extract_and_replace(m):
        text = m.group(0)
        col_row = re.search(r'r="([^"]+)"', text)
        style   = re.search(r'\bs="([^"]+)"', text)
        img_id  = re.search(r'ID_[A-F0-9]{32}', text, re.IGNORECASE)
        if not col_row or not img_id:
            return text
        return _make_wps_cell(
            col_row.group(1),
            style.group(1) if style else '',
            img_id.group(0)
        )

    # 统一匹配所有含 DISPIMG 的单元格（跨行匹配，兼容 _xlfn.DISPIMG）
    sheet_xml = re.sub(
        r'<c\s[^>]*>(?:(?!</c>).)*(?:_xlfn\.)?DISPIMG(?:(?!</c>).)*</c>',
        _extract_and_replace,
        sheet_xml,
        flags=re.DOTALL
    )
    return sheet_xml


def _build_cellimages_xml(
    needed: Dict[str, str],
    media_data: Dict[str, bytes],
) -> Tuple[str, str, Dict[str, str]]:
    """构建新的 cellimages.xml 和对应 rels，返回 (xml_str, rels_str, {img_id: rId})"""
    rid_map: Dict[str, str] = {}
    pic_xmls = []
    rel_entries = []

    for i, (img_id, media_path) in enumerate(needed.items(), 1):
        rid = f'rId{i}'
        rid_map[img_id] = rid
        ext = Path(media_path).suffix.lower()

        pic_xmls.append(
            f'<etc:cellImage>'
            f'<xdr:pic>'
            f'<xdr:nvPicPr>'
            f'<xdr:cNvPr id="{i}" name="{img_id}"/>'
            f'<xdr:cNvPicPr><a:picLocks noChangeAspect="1"/></xdr:cNvPicPr>'
            f'</xdr:nvPicPr>'
            f'<xdr:blipFill>'
            f'<a:blip r:embed="{rid}"/>'
            f'<a:stretch><a:fillRect/></a:stretch>'
            f'</xdr:blipFill>'
            f'<xdr:spPr>'
            f'<a:xfrm><a:off x="0" y="0"/><a:ext cx="914400" cy="914400"/></a:xfrm>'
            f'<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
            f'</xdr:spPr>'
            f'</xdr:pic>'
            f'</etc:cellImage>'
        )

        mime = {'.png': 'image/png', '.jpg': 'image/jpeg', '.jpeg': 'image/jpeg',
                '.bmp': 'image/bmp', '.gif': 'image/gif', '.webp': 'image/webp'}.get(ext, 'image/png')
        rel_entries.append(
            f'<Relationship Id="{rid}" '
            f'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
            f'Target="{media_path.replace("xl/", "")}" />'
        )

    cellimages_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<etc:cellImages '
        'xmlns:xdr="http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
        'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'xmlns:etc="http://www.wps.cn/officeDocument/2017/etCustomData">'
        + ''.join(pic_xmls) +
        '</etc:cellImages>'
    )

    rels_xml = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        + ''.join(rel_entries) +
        '</Relationships>'
    )

    return cellimages_xml, rels_xml, rid_map


def _ensure_drawing_rel(rels_xml: str) -> str:
    """确保 sheet1.xml.rels 包含 drawing 关系（rId1），不存在则追加"""
    if 'relationships/drawing' in rels_xml:
        return rels_xml
    insert = (
        '<Relationship Id="rId1" '
        'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/drawing" '
        'Target="../drawings/drawing1.xml"/>'
    )
    return rels_xml.replace('</Relationships>', insert + '</Relationships>')


def _ensure_content_types(ct_xml: str) -> str:
    """确保 [Content_Types].xml 包含 cellimages 的 Override 条目和图片 Default 类型"""
    # 添加 cellimages Override
    if 'cellimages.xml' not in ct_xml:
        insert = (
            '<Override PartName="/xl/cellimages.xml" '
            'ContentType="application/vnd.wps-officedocument.cellimage+xml"/>'
        )
        ct_xml = ct_xml.replace('</Types>', insert + '</Types>')
    # 添加图片 Default 类型（WPS 需要）
    for ext, mime in [('png', 'image/png'), ('bmp', 'image/bmp'), ('jpeg', 'image/jpeg')]:
        if f'Extension="{ext}"' not in ct_xml:
            insert = f'<Default Extension="{ext}" ContentType="{mime}"/>'
            ct_xml = ct_xml.replace('</Types>', insert + '</Types>')
    return ct_xml


def _ensure_workbook_rels(rels_xml: str) -> str:
    """确保 workbook.xml.rels 包含 cellimages 关系"""
    if 'cellimages' in rels_xml:
        return rels_xml
    insert = (
        '<Relationship Id="rIdCellImages" '
        'Type="http://www.wps.cn/officeDocument/2017/relationships/cellImage" '
        'Target="cellimages.xml"/>'
    )
    return rels_xml.replace('</Relationships>', insert + '</Relationships>')


# ─────────────────────────────────────────────
# 方案 B：openpyxl 浮动图片插入
# ─────────────────────────────────────────────

def insert_images_openpyxl(
    dst_xlsx: str,
    src_xlsx: str,
    cell_images: List[Dict],
    col_letter: str,
    start_row: int,
    row_height_pt: float = 60.0,
) -> bool:
    """
    用 openpyxl 把图片插入到 dst_xlsx 的指定列。
    cell_images: [{"row_offset": 0, "img_id": "ID_xxx"}, ...]  (row_offset 从 0 开始)
    col_letter: 目标列字母，如 "H"
    start_row: 数据起始行（1-based）
    row_height_pt: 行高（磅），用于计算图片大小
    """
    try:
        from openpyxl import load_workbook
        from openpyxl.drawing.image import Image as XLImage
        from openpyxl.utils import column_index_from_string
        import tempfile

        id_to_media = parse_dispimg_map(src_xlsx)
        if not id_to_media:
            return False

        wb = load_workbook(dst_xlsx)
        ws = wb.active

        col_idx = column_index_from_string(col_letter)  # 1-based

        with zipfile.ZipFile(src_xlsx, 'r') as src_z:
            for item in cell_images:
                row_offset = item.get('row_offset', 0)
                img_id     = item.get('img_id', '')
                if not img_id or img_id not in id_to_media:
                    continue

                media_path = id_to_media[img_id]
                if media_path not in src_z.namelist():
                    continue

                img_data = src_z.read(media_path)
                ext = Path(media_path).suffix.lower()

                # 写到临时文件（openpyxl 需要文件路径）
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(img_data)
                    tmp_path = tmp.name

                try:
                    xl_img = XLImage(tmp_path)
                    # 缩放到行高
                    target_px = int(row_height_pt * 1.33)  # pt -> px 近似
                    if xl_img.height and xl_img.height > 0:
                        scale = target_px / xl_img.height
                        xl_img.width  = int(xl_img.width  * scale)
                        xl_img.height = target_px

                    target_row = start_row + row_offset
                    # anchor: 列字母 + 行号
                    xl_img.anchor = f'{col_letter}{target_row}'
                    ws.add_image(xl_img)
                    # 设置行高
                    ws.row_dimensions[target_row].height = row_height_pt
                finally:
                    os.unlink(tmp_path)

        wb.save(dst_xlsx)
        logger.info(f'[image_filler] 方案B成功，插入 {len(cell_images)} 张图片')
        return True

    except Exception as e:
        logger.error(f'[image_filler] 方案B失败: {e}')
        return False


def insert_images_by_cells(
    dst_xlsx: str,
    src_xlsx: str,
    cell_formulas: List[Dict[str, str]],
    row_height_pt: float = 60.0,
) -> bool:
    try:
        from openpyxl import load_workbook
        from openpyxl.drawing.image import Image as XLImage
        import tempfile

        if not cell_formulas:
            return False

        id_to_media = parse_dispimg_map(src_xlsx)
        if not id_to_media:
            return False

        wb = load_workbook(dst_xlsx)
        ws_cache: Dict[str, object] = {}
        inserted = 0

        with zipfile.ZipFile(src_xlsx, 'r') as src_z:
            for item in cell_formulas:
                formula = str(item.get('value', '') or '')
                m = _DISPIMG_RE.search(formula)
                if not m:
                    continue

                img_id = m.group(1)
                media_path = id_to_media.get(img_id, '')
                if not media_path or media_path not in src_z.namelist():
                    continue

                target_sheet = item.get('sheet') or wb.sheetnames[0]
                target_cell = item.get('cell', '')
                if not target_cell:
                    continue
                if target_sheet not in wb.sheetnames:
                    target_sheet = wb.sheetnames[0]

                ws = ws_cache.get(target_sheet)
                if ws is None:
                    ws = wb[target_sheet]
                    ws_cache[target_sheet] = ws

                img_data = src_z.read(media_path)
                ext = Path(media_path).suffix.lower() or '.png'
                with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
                    tmp.write(img_data)
                    tmp_path = tmp.name

                try:
                    xl_img = XLImage(tmp_path)
                    target_px = int(row_height_pt * 1.33)
                    if xl_img.height and xl_img.height > 0:
                        scale = target_px / xl_img.height
                        xl_img.width = int(xl_img.width * scale)
                        xl_img.height = target_px
                    xl_img.anchor = target_cell
                    ws.add_image(xl_img)
                    row_no = re.sub(r'^[A-Z]+', '', target_cell.upper())
                    if row_no.isdigit():
                        ws.row_dimensions[int(row_no)].height = row_height_pt
                    inserted += 1
                finally:
                    os.unlink(tmp_path)

        if inserted == 0:
            return False
        wb.save(dst_xlsx)
        logger.info(f'[image_filler] 按单元格插入图片成功，数量={inserted}')
        return True
    except Exception as e:
        logger.error(f'[image_filler] 按单元格插入图片失败: {e}')
        return False


# ─────────────────────────────────────────────
# 统一入口：优先方案A，fallback 方案B
# ─────────────────────────────────────────────

def fill_images(
    src_xlsx: str,
    dst_xlsx: str,
    rows_data: List[Dict],
    pic_col_name: str,
    col_letter: str,
    start_row: int,
) -> bool:
    """
    从 rows_data 中提取图片列的 DISPIMG 值，
    优先用方案A（DISPIMG复制），失败则用方案B（openpyxl浮动图片）。

    src_xlsx: 数据源文件（含图片）
    dst_xlsx: 已填好文字数据的目标文件（将被修改）
    rows_data: 行数据列表
    pic_col_name: 图片列的列名，如 "Picture" 或 "图片"
    col_letter: 图片列在模板中的列字母，如 "H"
    start_row: 数据起始行（1-based）
    """
    if not rows_data or not src_xlsx or not os.path.exists(src_xlsx):
        return False

    # 提取所有 DISPIMG ID
    used_ids: List[str] = []
    cell_images: List[Dict] = []
    for i, row in enumerate(rows_data):
        val = str(row.get(pic_col_name, '') or '')
        m = _DISPIMG_RE.search(val)
        if m:
            img_id = m.group(1)
            used_ids.append(img_id)
            cell_images.append({'row_offset': i, 'img_id': img_id})

    if not used_ids:
        logger.info('[image_filler] 无 DISPIMG 图片，跳过')
        return False

    logger.info(f'[image_filler] 发现 {len(used_ids)} 张图片，尝试方案A')

    # 方案A
    if inject_dispimg(src_xlsx, dst_xlsx, used_ids):
        return True

    # 方案B fallback
    logger.info('[image_filler] 方案A失败，尝试方案B')
    return insert_images_openpyxl(dst_xlsx, src_xlsx, cell_images, col_letter, start_row)
