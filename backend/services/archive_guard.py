# -*- coding: utf-8 -*-
"""OOXML（zip）解压炸弹防护。

**为什么需要这一层**：上传限额约束的是「压缩后」字节数。一个 600KB 的 xlsx 可以在中央
目录里声明解压出 200MB，openpyxl / python-docx 会照单全收。实测（见
.workbuddy-ai/outputs/bomb_probe.py）：在 1200MB 进程内存上限下，同一个解析入口
解析普通小文件 0.65s 正常返回，解析该炸弹 14.34s 后 MemoryError。
也就是**一个几百 KB 的文件就能把解析进程打死**。

**做法**：在打开任何 OOXML 包之前先读 zip 中央目录（只读目录，不解压数据），
按「条目数 / 解压总量 / 单条最大 / 压缩比」四道闸门判断。全部阈值可用环境变量覆盖。

阈值取值依据（实测本仓库 samples/、docs/demo测试集/、uploads/ 全部 OOXML 文件后定标）：
  - 真实业务文件最大值：解压 19.6MB、压缩比 11.1、最大单条目 19.6MB、条目数 9~10；
  - 旧阈值（解压 300MB / 压缩比 200）**挡不住实测炸弹**：3.0MB 的 xlsx 解压 200MB，
    压缩比只有 66 倍、解压量 200MB 也在 300MB 以内，四道闸门全部放行，
    下游 openpyxl 仍然 MemoryError。说明「只看压缩比」是错的——绝对体积才是主因，
    因为 XML→对象的内存放大约 6 倍（200MB XML → >1.2GB 峰值）。
  - 故以**绝对解压体积**为主闸门：120MB 解压 ≈ 0.7GB 峰值内存，是普通服务器可承受的上限，
    同时比真实文件（19.6MB）宽出 6 倍，不会误伤。压缩比闸门保留作补充信号。
  - 50MB 的上传上限 + 10 倍压缩比 = 500MB 解压，这类文件本身就会 OOM，
    拒绝并给出明确提示，比放进去崩进程更可接受。
"""
from __future__ import annotations

import logging
import os
import zipfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger("archive_guard")


class ArchiveRejected(ValueError):
    """压缩包不满足安全约束。message 面向最终用户，可直接透出。"""


def _int_env(name: str, default: int) -> int:
    try:
        v = int(str(os.getenv(name, "")).strip() or default)
        return v if v > 0 else default
    except (TypeError, ValueError):
        return default


MAX_ENTRIES = _int_env("MAX_ARCHIVE_ENTRIES", 5000)
# 主闸门：绝对解压体积。真实文件上限 19.6MB，取 120MB 留 6 倍余量；
# 单条目上限取 80MB（真实最大单条目 19.6MB），挡住「一个 sheet 撑爆内存」。
MAX_UNCOMPRESSED_MB = _int_env("MAX_ARCHIVE_UNCOMPRESSED_MB", 120)
MAX_ENTRY_MB = _int_env("MAX_ARCHIVE_ENTRY_MB", 80)
MAX_RATIO = _int_env("MAX_ARCHIVE_RATIO", 200)


def is_zip_container(path: str | Path) -> bool:
    """按魔数判断是否 zip（.xlsx/.docx/.zip 都是 zip；.doc/.xls 是 OLE，不是）。"""
    try:
        with open(path, "rb") as f:
            return f.read(4) == b"PK\x03\x04"
    except OSError:
        return False


def inspect_archive(path: str | Path) -> Dict[str, Any]:
    """只读 zip 中央目录，返回体积元数据；不读任何条目内容。"""
    p = Path(path)
    on_disk = p.stat().st_size if p.exists() else 0
    with zipfile.ZipFile(p) as z:
        infos = z.infolist()
        total_uncompressed = sum(i.file_size for i in infos)
        largest = max(infos, key=lambda i: i.file_size) if infos else None
        return {
            "entries": len(infos),
            "on_disk_bytes": on_disk,
            "uncompressed_bytes": total_uncompressed,
            "largest_entry": (largest.filename, largest.file_size) if largest else ("", 0),
            "ratio": round(total_uncompressed / max(on_disk, 1), 1),
        }


def assert_archive_safe(path: str | Path, *, display_name: str = "") -> Optional[Dict[str, Any]]:
    """校验压缩包；超限抛 ArchiveRejected。非 zip 或无法读取时返回 None（交给下游报错）。

    注意：这里**故意**把「不是 zip」当成放行——本函数只负责体积闸门，
    文件类型/格式是否受支持由调用方判断，避免职责混在一起导致错误信息互相掩盖。
    """
    p = Path(path)
    if not p.exists() or not is_zip_container(p):
        return None
    name = display_name or p.name
    try:
        meta = inspect_archive(p)
    except (zipfile.BadZipFile, OSError) as e:
        # 中央目录都读不出来：不是完整 zip，交给下游解析器报「文件损坏」，不必在这里判死
        logger.warning("[archive_guard] 无法读取 %s 的 zip 目录: %s", name, e)
        return None

    if meta["entries"] > MAX_ENTRIES:
        raise ArchiveRejected(
            f"文件「{name}」内部条目过多（{meta['entries']} 个，上限 {MAX_ENTRIES}），"
            f"疑似构造文件，已拒绝解析"
        )

    limit_bytes = MAX_UNCOMPRESSED_MB * 1024 * 1024
    if meta["uncompressed_bytes"] > limit_bytes:
        raise ArchiveRejected(
            f"文件「{name}」解压后约 {meta['uncompressed_bytes'] / 1024 / 1024:.0f}MB，"
            f"超过 {MAX_UNCOMPRESSED_MB}MB 上限（压缩包仅 {meta['on_disk_bytes'] / 1024:.0f}KB，"
            f"压缩比 {meta['ratio']}），已拒绝解析"
        )

    entry_name, entry_size = meta["largest_entry"]
    if entry_size > MAX_ENTRY_MB * 1024 * 1024:
        raise ArchiveRejected(
            f"文件「{name}」内部单个部件过大（{entry_name} 解压后约 "
            f"{entry_size / 1024 / 1024:.0f}MB，上限 {MAX_ENTRY_MB}MB），已拒绝解析"
        )

    if meta["ratio"] > MAX_RATIO:
        raise ArchiveRejected(
            f"文件「{name}」压缩比异常（{meta['ratio']} 倍，上限 {MAX_RATIO} 倍），"
            f"疑似解压炸弹，已拒绝解析"
        )

    logger.info(
        "[archive_guard] %s 通过体积校验: 条目=%s 解压=%.1fMB 压缩比=%s",
        name, meta["entries"], meta["uncompressed_bytes"] / 1024 / 1024, meta["ratio"],
    )
    return meta
