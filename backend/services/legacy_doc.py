"""老版 Word .doc（OLE CFB）转 .docx。python-docx 仅支持 .docx。"""
from __future__ import annotations

import os
import platform
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional

# Microsoft Compound Document 魔数
OLE_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


def _project_root() -> Path:
    """backend/services/legacy_doc.py -> 项目根目录"""
    return Path(__file__).resolve().parents[2]


def _project_vendor_soffice() -> Optional[str]:
    """项目内 vendor/LibreOffice（Windows 便携/解压安装）。"""
    root = _project_root()
    if platform.system() == "Windows":
        p = root / "vendor" / "LibreOffice" / "program" / "soffice.exe"
        return str(p) if p.is_file() else None
    p = root / "vendor" / "LibreOffice" / "program" / "soffice"
    return str(p) if p.is_file() else None


def is_zip_file(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"PK"


def is_ole_doc(path: str) -> bool:
    with open(path, "rb") as f:
        return f.read(8) == OLE_MAGIC


def _soffice_candidates() -> List[str]:
    cands: List[str] = []
    env = (os.environ.get("LIBREOFFICE_PROGRAM") or "").strip()
    if env and os.path.isfile(env):
        cands.append(env)
    pv = _project_vendor_soffice()
    if pv:
        cands.append(pv)
    if platform.system() == "Windows":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        cands.extend(
            [
                os.path.join(pf, "LibreOffice", "program", "soffice.exe"),
                os.path.join(pf86, "LibreOffice", "program", "soffice.exe"),
                "soffice",
            ]
        )
    else:
        cands.extend(["soffice", "/usr/bin/soffice", "/usr/local/bin/soffice", "/snap/bin/libreoffice"])
    # 去重且保持顺序
    seen = set()
    out: List[str] = []
    for x in cands:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


def _run_no_window(args: list, **kwargs) -> subprocess.CompletedProcess:
    if platform.system() == "Windows":
        try:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW  # type: ignore[attr-defined]
        except AttributeError:
            pass
    return subprocess.run(args, **kwargs)


def convert_doc_to_docx_temp(doc_path: str) -> Optional[str]:
    """将 .doc 转为临时 .docx；成功返回绝对路径，调用方负责 os.unlink。失败返回 None。"""
    doc_path = os.path.abspath(doc_path)
    if not os.path.isfile(doc_path):
        return None

    outdir = tempfile.mkdtemp(prefix="doc2docx_")
    try:
        for soffice in _soffice_candidates():
            if soffice != "soffice" and not os.path.isfile(soffice):
                continue
            try:
                r = _run_no_window(
                    [soffice, "--headless", "--convert-to", "docx", "--outdir", outdir, doc_path],
                    capture_output=True,
                    timeout=180,
                    text=True,
                    errors="replace",
                )
                if r.returncode != 0:
                    continue
            except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
                continue
            stem = Path(doc_path).stem
            out = Path(outdir) / f"{stem}.docx"
            if out.is_file() and out.stat().st_size > 0:
                fd, dest = tempfile.mkstemp(suffix=".docx")
                os.close(fd)
                shutil.move(str(out), dest)
                shutil.rmtree(outdir, ignore_errors=True)
                return dest
        shutil.rmtree(outdir, ignore_errors=True)
    except Exception:
        shutil.rmtree(outdir, ignore_errors=True)

    # Windows：本机已安装 Word 时用 COM 另存为 docx
    if platform.system() == "Windows":
        try:
            import win32com.client  # type: ignore
        except ImportError:
            return None
        word = None
        doc = None
        fd, dest = tempfile.mkstemp(suffix=".docx")
        os.close(fd)
        dest_abs = os.path.abspath(dest)
        dest_path: Optional[str] = None
        try:
            word = win32com.client.Dispatch("Word.Application")
            word.Visible = False
            try:
                word.DisplayAlerts = 0
            except Exception:
                pass
            doc = word.Documents.Open(doc_path, ReadOnly=True)
            # 16 = wdFormatXMLDocument (.docx)
            doc.SaveAs2(dest_abs, FileFormat=16)
            dest_path = dest_abs
        except Exception:
            try:
                if os.path.isfile(dest_abs):
                    os.unlink(dest_abs)
            except OSError:
                pass
            return None
        finally:
            try:
                if doc is not None:
                    doc.Close(False)
            except Exception:
                pass
            try:
                if word is not None:
                    word.Quit()
            except Exception:
                pass
        if dest_path and os.path.isfile(dest_path) and os.path.getsize(dest_path) > 0:
            return dest_path
        try:
            if os.path.isfile(dest_abs):
                os.unlink(dest_abs)
        except OSError:
            pass
    return None
