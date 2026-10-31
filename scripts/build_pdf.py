# -*- coding: utf-8 -*-
"""把 docs/技术文档.md 构建为 PDF。

流程：Markdown -> HTML（含封面样式）-> Chrome 无头打印 PDF -> 统计页数。

用法：
    python scripts/build_pdf.py            # 构建 docs/技术文档.pdf
    python scripts/build_pdf.py --html-only # 只出 HTML，便于调样式

依赖：markdown（pip install markdown）；本机需有 Chrome 或 Edge。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
MD_PATH = DOCS / "技术文档.md"
HTML_PATH = DOCS / ".build" / "技术文档.html"
PDF_PATH = DOCS / "技术文档.pdf"

CSS = """
@page { size: A4; margin: 17mm 15mm 16mm 15mm; }
* { box-sizing: border-box; }
body {
  font-family: "Microsoft YaHei", "PingFang SC", "Source Han Sans SC", sans-serif;
  font-size: 10.4pt; line-height: 1.72; color: #1f2328; margin: 0;
}
h1 {
  font-size: 26pt; line-height: 1.35; text-align: center;
  margin: 46mm 0 4mm; letter-spacing: 1px; color: #12305c;
}
h1 + h2 {
  text-align: center; font-size: 15pt; color: #3d5a80; font-weight: 500;
  margin: 0 0 16mm; border: none; padding: 0;
}
h2 {
  font-size: 14.5pt; color: #12305c; margin: 20pt 0 8pt;
  padding-left: 9px; border-left: 4px solid #2f6fd0;
  page-break-after: avoid; break-after: avoid;
}
h3 {
  font-size: 11.8pt; color: #24405f; margin: 13pt 0 6pt;
  page-break-after: avoid; break-after: avoid;
}
p { margin: 5pt 0; text-align: justify; }
ul, ol { margin: 5pt 0 5pt 0; padding-left: 20px; }
li { margin: 2.5pt 0; }
strong { color: #12305c; }
hr { border: none; border-top: 1px solid #e4e7eb; margin: 14pt 0; }
blockquote {
  margin: 8pt 0; padding: 6pt 10pt; background: #f6f8fa;
  border-left: 3px solid #c9d3e0; color: #4a5361; font-size: 9.6pt;
}
blockquote p { margin: 2pt 0; }
table {
  border-collapse: collapse; width: 100%; margin: 8pt 0 10pt;
  font-size: 9.3pt; page-break-inside: avoid; break-inside: avoid;
}
th, td { border: 1px solid #d5dae0; padding: 4pt 6pt; vertical-align: top; }
th { background: #eef2f7; color: #12305c; font-weight: 600; text-align: left; }
tr:nth-child(even) td { background: #fbfcfd; }
code {
  font-family: Consolas, "Cascadia Mono", monospace; font-size: 9pt;
  background: #f2f4f7; padding: 1px 3px; border-radius: 3px; color: #9a3412;
}
pre {
  background: #f8f9fb; border: 1px solid #e3e6ea; border-radius: 4px;
  padding: 8pt 10pt; margin: 7pt 0 10pt; overflow: hidden;
  page-break-inside: avoid; break-inside: avoid;
}
pre code {
  background: none; padding: 0; color: #24292f; font-size: 8.5pt; line-height: 1.55;
}
img {
  display: block; max-width: 100%; margin: 7pt auto 3pt;
  border: 1px solid #dfe3e8; border-radius: 3px;
  page-break-inside: avoid; break-inside: avoid;
}
p > img { page-break-after: avoid; }
p:has(> img) + p { font-size: 9.2pt; color: #5b6572; text-align: center; margin-top: 0; }
.page-break { page-break-after: always; break-after: page; height: 0; }
"""

COVER_BREAK = '<div class="page-break"></div>\n\n'


def md_to_html(md_text: str) -> str:
    import markdown

    # mermaid 在 PDF 里无法渲染，替换为等价的静态架构图
    def _swap_mermaid(m: re.Match) -> str:
        return '![图 4-1 系统分层架构](figures/architecture.svg)'

    md_text = re.sub(r"```mermaid\n.*?\n```", _swap_mermaid, md_text, flags=re.S)

    # 封面页：材料索引之前强制分页
    md_text = md_text.replace("## 〇、材料索引", COVER_BREAK + "## 〇、材料索引", 1)

    html = markdown.markdown(
        md_text,
        extensions=["tables", "fenced_code", "sane_lists", "nl2br"],
        output_format="html5",
    )
    # HTML 产物在 docs/.build/ 下，用 <base> 把相对路径锚回 docs/，否则图片全部断链
    base_href = DOCS.as_uri().rstrip("/") + "/"
    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<base href="{base_href}">
<title>DocFusion 技术文档</title>
<style>{CSS}</style></head>
<body>{html}</body></html>"""


def find_browser() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    for name in ("chrome", "google-chrome", "chromium", "msedge"):
        p = shutil.which(name)
        if p:
            return p
    return None


def build(html_only: bool = False) -> int:
    if not MD_PATH.exists():
        print(f"[x] 找不到 {MD_PATH}")
        return 1

    HTML_PATH.parent.mkdir(parents=True, exist_ok=True)
    html = md_to_html(MD_PATH.read_text(encoding="utf-8"))
    HTML_PATH.write_text(html, encoding="utf-8")
    print(f"[ok] HTML -> {HTML_PATH.relative_to(ROOT)}")
    if html_only:
        return 0

    browser = find_browser()
    if not browser:
        print("[x] 未找到 Chrome/Edge，无法打印 PDF。可用 CHROME_PATH 环境变量指定。")
        return 2

    PDF_PATH.parent.mkdir(parents=True, exist_ok=True)
    if PDF_PATH.exists():
        PDF_PATH.unlink()

    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-pdf-header-footer",
        "--virtual-time-budget=8000",
        f"--print-to-pdf={PDF_PATH}",
        HTML_PATH.as_uri(),
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=180)
    if not PDF_PATH.exists():
        print("[x] PDF 生成失败")
        print(proc.stderr[-1500:])
        return 3

    size_kb = PDF_PATH.stat().st_size / 1024
    pages = "?"
    try:
        from pypdf import PdfReader

        pages = len(PdfReader(str(PDF_PATH)).pages)
    except Exception:
        pass

    print(f"[ok] PDF  -> {PDF_PATH.relative_to(ROOT)}  ({pages} 页, {size_kb:.0f} KB)")
    if isinstance(pages, int) and pages > 30:
        print(f"[!] 页数 {pages} 超过 30 页上限，需精简内容或调小字号/边距")
        return 4
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--html-only", action="store_true", help="只生成 HTML，不打印 PDF")
    sys.exit(build(ap.parse_args().html_only))
