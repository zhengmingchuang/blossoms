# -*- coding: utf-8 -*-
"""把 docs/作品介绍文档.md 构建为 PDF。

复用 build_pdf.py 的排版样式与浏览器打印逻辑，只替换源文件与标题，
保证两份 PDF 的字体、页边距、表格样式一致。

用法：
    python scripts/build_intro_pdf.py
"""
from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MD_PATH = DOCS / "作品介绍文档.md"
HTML_PATH = DOCS / ".build" / "作品介绍文档.html"
PDF_PATH = DOCS / "作品介绍文档.pdf"

# 在 build_pdf 的样式之上追加两条：
#   1. 限制图片高度，否则 16:9 截图按宽度铺满后一张图就占掉一整页
#   2. 图注段落居中灰字（图片后紧跟的一段文字即图注）
CSS_EXTRA = """
img { max-height: 104mm; width: auto; }
p:has(> img) { margin: 6pt auto 2pt; text-align: center; }
p:has(> img) + p { margin-bottom: 10pt; }
"""


def _load_build_pdf():
    spec = importlib.util.spec_from_file_location("build_pdf", ROOT / "scripts" / "build_pdf.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def md_to_html(md_text: str, css: str) -> str:
    import markdown

    html = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format="html5",
    )
    base_href = DOCS.as_uri().rstrip("/") + "/"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<base href="{base_href}">
<title>DocFusion 作品介绍文档</title>
<style>{css}</style></head>
<body>{html}</body></html>"""


def main() -> int:
    if not MD_PATH.exists():
        print(f"[x] 找不到 {MD_PATH}")
        return 1

    bp = _load_build_pdf()
    browser = bp.find_browser()
    if not browser:
        print("[x] 未找到 Chrome/Edge，无法打印 PDF")
        return 1

    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    html = md_to_html(MD_PATH.read_text(encoding="utf-8"), bp.CSS + CSS_EXTRA)
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"[ok] HTML -> {HTML_PATH.relative_to(ROOT)}")

    if PDF_PATH.exists():
        PDF_PATH.unlink()

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-pdf-header-footer",
        f"--print-to-pdf={PDF_PATH}",
        HTML_PATH.as_uri(),
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not PDF_PATH.exists():
        print("[x] PDF 生成失败")
        print((r.stderr or "")[-800:])
        return 1

    size_kb = PDF_PATH.stat().st_size / 1024
    pages = "?"
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(str(PDF_PATH)).pages)
    except Exception:
        pass

    print(f"[ok] PDF  -> {PDF_PATH.relative_to(ROOT)}  ({pages} 页, {size_kb:.0f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
