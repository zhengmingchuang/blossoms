"""OOXML Strict → Transitional 兼容层。

问题来源：Excel / Word 的「Strict Open XML」保存格式用的是 `purl.oclc.org` 命名空间，
并且根节点带 `conformance="strict"`。openpyxl、python-docx 只认 Transitional 命名空间，
读这种文件不会报错，而是**安静地返回一个零工作表的空工作簿**——上层看到的就是
「文件里没有内容」，用户拿到的是一句毫无信息量的「未解析到任何内容」。

修在入口而不是修在每一个读取方：上传落盘后立刻把 Strict 包就地转成 Transitional，
下游 20 多处 `load_workbook` 一行都不用改。
"""

import os
import re
import tempfile
import zipfile
import logging

from services.archive_guard import assert_archive_safe, ArchiveRejected

logger = logging.getLogger("ooxml_compat")

# Strict 命名空间 -> Transitional 命名空间。按最长前缀优先替换，避免
# `.../spreadsheetml/main` 被 `.../spreadsheetml` 之类的短规则先吃掉。
_NS_PAIRS = [
    ("http://purl.oclc.org/ooxml/spreadsheetml/main",
     "http://schemas.openxmlformats.org/spreadsheetml/2006/main"),
    ("http://purl.oclc.org/ooxml/wordprocessingml/main",
     "http://schemas.openxmlformats.org/wordprocessingml/2006/main"),
    ("http://purl.oclc.org/ooxml/officeDocument/relationships",
     "http://schemas.openxmlformats.org/officeDocument/2006/relationships"),
    ("http://purl.oclc.org/ooxml/officeDocument/docPropsVTypes",
     "http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes"),
    ("http://purl.oclc.org/ooxml/officeDocument/extendedProperties",
     "http://schemas.openxmlformats.org/officeDocument/2006/extendedProperties"),
    ("http://purl.oclc.org/ooxml/officeDocument/customProperties",
     "http://schemas.openxmlformats.org/officeDocument/2006/customProperties"),
    ("http://purl.oclc.org/ooxml/officeDocument/math",
     "http://schemas.openxmlformats.org/officeDocument/2006/math"),
    ("http://purl.oclc.org/ooxml/drawingml/spreadsheetDrawing",
     "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"),
    ("http://purl.oclc.org/ooxml/drawingml/chart",
     "http://schemas.openxmlformats.org/drawingml/2006/chart"),
    ("http://purl.oclc.org/ooxml/drawingml/main",
     "http://schemas.openxmlformats.org/drawingml/2006/main"),
]

_CONFORMANCE_RE = re.compile(rb'\s+conformance\s*=\s*["\']strict["\']')

_OOXML_EXTS = (".xlsx", ".docx", ".pptx", ".xlsm", ".docm")


def _looks_strict(parts: dict) -> bool:
    """判断这个 zip 包是不是 Strict。只看两个主文档，读 8KB 足够。"""
    for name in ("xl/workbook.xml", "word/document.xml"):
        data = parts.get(name)
        if data and (b"purl.oclc.org/ooxml" in data or b'conformance="strict"' in data):
            return True
    return False


def _rewrite_part(data: bytes) -> bytes:
    for strict, transitional in _NS_PAIRS:
        data = data.replace(strict.encode(), transitional.encode())
    return _CONFORMANCE_RE.sub(b"", data)


def is_strict_ooxml(path: str) -> bool:
    if not str(path).lower().endswith(_OOXML_EXTS):
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            probes = {}
            for n in ("xl/workbook.xml", "word/document.xml"):
                if n in names:
                    with zf.open(n) as fp:
                        probes[n] = fp.read(8192)
            return _looks_strict(probes)
    except (zipfile.BadZipFile, OSError):
        return False


def normalize_ooxml_inplace(path: str) -> bool:
    """把 Strict 包就地转成 Transitional。返回是否发生了转换。

    非 Strict / 非法包一律原样返回 False，绝不因为兼容处理而破坏正常文件。
    """
    # 本函数会逐条 read() 整个包，是全仓最容易被打爆的地方之一：
    # 炸弹文件在 is_strict_ooxml 阶段就要被拦下，而不是等到 read() 把内存吃光。
    try:
        assert_archive_safe(path)
    except ArchiveRejected:
        logger.warning("[ooxml_compat] %s 未通过体积校验，跳过 Strict 转换", os.path.basename(path))
        return False
    if not is_strict_ooxml(path):
        return False

    tmp_fd, tmp_path = tempfile.mkstemp(suffix=".tmp", dir=os.path.dirname(path) or None)
    os.close(tmp_fd)
    try:
        with zipfile.ZipFile(path) as src, zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as dst:
            for item in src.infolist():
                data = src.read(item.filename)
                # 只改 xml / rels；图片等二进制原样搬运
                if item.filename.endswith((".xml", ".rels")):
                    data = _rewrite_part(data)
                dst.writestr(item, data)
        os.replace(tmp_path, path)
        return True
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        return False
