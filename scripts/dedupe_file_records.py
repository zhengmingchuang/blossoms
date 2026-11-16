"""清理 file_info 里「同一磁盘路径多条记录」的历史堆积。

背景：生成类工具（create_spreadsheet / create_document / fill_template_smart）
此前每次都新插一行 FileInfo，磁盘却写到同一个路径。结果是同一份文件在库里
有 8~9 条记录：文档管理页、聊天文件选择器、已修改文档列表都会重复显示；
旧 id 下载到的是最新内容（同路径别名），旧 id 的 doc_chunk 还留着上一版文本。

现在写入侧已改为按 (用户, 路径) 复用记录，这个脚本只处理存量数据。

用法：
    python scripts/dedupe_file_records.py           # 只报告，不改库
    python scripts/dedupe_file_records.py --apply   # 备份后清理
"""
import os
import sys
import sqlite3
import shutil
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.abspath(os.path.join(HERE, "..", "backend", "app.db"))
BACKUP_DIR = os.path.abspath(os.path.join(HERE, "..", "backend", "backups"))


def _groups(cur):
    rows = cur.execute(
        """
        SELECT upload_user, file_path, COUNT(*) AS n, GROUP_CONCAT(id) AS ids
        FROM file_info
        WHERE file_path IS NOT NULL AND file_path <> ''
        GROUP BY upload_user, file_path
        HAVING n > 1
        ORDER BY n DESC
        """
    ).fetchall()
    return rows


def main() -> int:
    apply = "--apply" in sys.argv
    if not os.path.exists(DB):
        print(f"找不到数据库: {DB}")
        return 1

    con = sqlite3.connect(DB)
    cur = con.cursor()
    groups = _groups(cur)
    if not groups:
        print("没有重复记录，无需处理。")
        return 0

    print(f"发现 {len(groups)} 组同路径重复记录：\n")
    plan = []
    for user, path, n, ids in groups:
        id_list = [int(x) for x in ids.split(",")]
        # 保留最新一条：upload_time 最大，时间相同取 id 最大
        rows = cur.execute(
            f"SELECT id, file_name, upload_time FROM file_info WHERE id IN ({','.join('?' * len(id_list))})",
            id_list,
        ).fetchall()
        rows.sort(key=lambda r: (str(r[2] or ""), r[0]))
        keep = rows[-1]
        drop = [r[0] for r in rows[:-1]]
        plan.append((keep, drop, path, n))
        print(f"  {os.path.basename(path)}  x{n}")
        print(f"    保留 id={keep[0]} ({keep[2]})")
        print(f"    清理 id={drop}")

    total_drop = sum(len(p[1]) for p in plan)
    print(f"\n合计待清理 {total_drop} 条记录（及其 doc_chunk / extract_result）。")

    if not apply:
        print("\n这是试运行，未修改数据库。确认无误后加 --apply 执行。")
        return 0

    os.makedirs(BACKUP_DIR, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = os.path.join(BACKUP_DIR, f"app.db.{stamp}.bak")
    shutil.copy2(DB, backup)
    print(f"\n已备份数据库 -> {backup}")

    dropped_chunks = dropped_extracts = 0
    for keep, drop, _path, _n in plan:
        marks = ",".join("?" * len(drop))
        dropped_chunks += cur.execute(
            f"DELETE FROM doc_chunk WHERE file_id IN ({marks})", drop
        ).rowcount
        dropped_extracts += cur.execute(
            f"DELETE FROM extract_result WHERE file_id IN ({marks})", drop
        ).rowcount
        # 解析任务原本挂在被删记录上，改挂到保留记录，避免任务详情指向不存在的文件
        cur.execute(
            f"UPDATE task_info SET related_id = ? WHERE related_id IN ({marks})",
            [keep[0], *drop],
        )
        cur.execute(f"DELETE FROM file_info WHERE id IN ({marks})", drop)
    con.commit()

    remain = cur.execute(
        """
        SELECT COUNT(*) FROM (
            SELECT file_path FROM file_info
            WHERE file_path IS NOT NULL AND file_path <> ''
            GROUP BY upload_user, file_path HAVING COUNT(*) > 1
        )
        """
    ).fetchone()[0]
    print(f"清理完成：删除 {total_drop} 条文件记录、{dropped_chunks} 条索引片段、{dropped_extracts} 条提取结果。")
    print(f"剩余重复组: {remain}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
