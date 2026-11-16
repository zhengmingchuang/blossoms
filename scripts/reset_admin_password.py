# -*- coding: utf-8 -*-
"""重置 admin 口令，并保证「库内哈希」与「凭据文件」严格一致。

背景：`.workbuddy-ai/tmp/fix_p0_creds.py` 那类加固脚本会轮换口令，
但它是「先生成密码 → UPDATE → 再写文件」，一旦中途异常或库被锁，
就会出现「密码文件写好了、库里还是旧哈希」的错位，表现为拿着文件里的
密码登录却一直 401。本脚本把顺序反过来，并在最后做一次真实校验。

    python scripts/reset_admin_password.py [新口令]

不传参数则随机生成 15 位强口令。
"""
import io
import os
import secrets
import sqlite3
import sys

SCRIPTS_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(SCRIPTS_DIR)
BACKEND = os.path.join(ROOT, "backend")
DB = os.path.join(BACKEND, "app.db")
PWD_FILE = os.path.join(BACKEND, ".admin_initial_password")

sys.path.insert(0, BACKEND)


def main() -> int:
    import bcrypt  # 与 routers/auth.py 用同一套实现，避免算法不一致

    if not os.path.exists(DB):
        print(f"[FAIL] 找不到数据库: {DB}")
        return 2

    pwd = sys.argv[1] if len(sys.argv) > 1 else secrets.token_urlsafe(15)
    hashed = bcrypt.hashpw(pwd.encode(), bcrypt.gensalt()).decode()

    conn = sqlite3.connect(DB)
    try:
        row = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()
        if not row:
            print("[FAIL] users 表里没有 admin 账号")
            return 2
        conn.execute("UPDATE users SET password=? WHERE id=?", (hashed, row[0]))
        conn.commit()
    finally:
        conn.close()

    # 写完库立刻回读校验，确认 UPDATE 真的落盘了，再写凭据文件
    conn = sqlite3.connect(DB)
    try:
        stored = conn.execute("SELECT password FROM users WHERE username='admin'").fetchone()[0]
    finally:
        conn.close()
    if not bcrypt.checkpw(pwd.encode(), stored.encode()):
        print("[FAIL] 库内哈希与新口令不匹配，UPDATE 未生效（可能有其它进程占用数据库）")
        return 1

    with io.open(PWD_FILE, "w", encoding="utf-8") as f:
        f.write(pwd + "\n")
    try:
        os.chmod(PWD_FILE, 0o600)
    except OSError:
        pass

    print("[OK] admin 口令已重置并校验通过")
    print(f"     凭据文件: {PWD_FILE}")
    print(f"     新口令  : {pwd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
