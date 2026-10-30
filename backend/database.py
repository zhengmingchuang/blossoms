from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
from config import DATABASE_URL

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


@event.listens_for(engine, "connect")
def _enable_sqlite_fk(dbapi_conn, _record):
    """SQLite 默认不开启外键约束，导致 ondelete="CASCADE" 形同虚设。

    不开的话，删除会话后 chat_message 会残留孤儿行；而 SQLite 的
    INTEGER PRIMARY KEY 在删掉最大 id 后会复用该 id，新会话就会"继承"
    上一个已删除会话的历史消息。
    """
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()
    except Exception:
        pass


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
