from sqlalchemy import Column, Integer, String, Text, DateTime, Float, ForeignKey
from sqlalchemy.sql import func
from datetime import datetime
from database import Base


def _now():
    return datetime.now()


def fmt_time(t):
    """格式化时间，去掉微秒"""
    if not t:
        return ""
    if isinstance(t, datetime):
        return t.strftime("%Y-%m-%d %H:%M:%S")
    return str(t).split(".")[0][:19]


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)  # 账号，不可修改
    nickname = Column(String(50), default="")  # 用户名/昵称，可修改，默认=账号
    password = Column(String(255), nullable=False)
    role = Column(String(20), default="user")
    avatar = Column(String(500), default="")
    create_time = Column(DateTime, default=_now)


class FileInfo(Base):
    __tablename__ = "file_info"
    id = Column(Integer, primary_key=True, autoincrement=True)
    file_name = Column(String(255), nullable=False)
    file_type = Column(String(20), nullable=False)
    file_path = Column(String(500), nullable=False)
    file_size = Column(Integer, default=0)
    upload_user = Column(Integer, ForeignKey("users.id"))
    parse_status = Column(String(20), default="pending")
    parse_time = Column(Float, default=0.0)
    chunk_count = Column(Integer, default=0)
    field_count = Column(Integer, default=0)
    upload_time = Column(DateTime, default=_now)


class DocChunk(Base):
    __tablename__ = "doc_chunk"
    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(Integer, ForeignKey("file_info.id", ondelete="CASCADE"))
    chunk_index = Column(Integer, default=0)
    chunk_text = Column(Text)
    chunk_type = Column(String(50))
    source_position = Column(String(255))


class ExtractResult(Base):
    __tablename__ = "extract_result"
    id = Column(Integer, primary_key=True, autoincrement=True)
    file_id = Column(Integer, ForeignKey("file_info.id", ondelete="CASCADE"))
    field_name = Column(String(255))
    field_value = Column(Text)
    confidence = Column(Float, default=0.0)
    extract_method = Column(String(50))
    source_chunk_id = Column(Integer, nullable=True)
    source_position = Column(String(255), nullable=True)
    create_time = Column(DateTime, default=_now)


class TemplateInfo(Base):
    __tablename__ = "template_info"
    id = Column(Integer, primary_key=True, autoincrement=True)
    template_name = Column(String(255))
    template_type = Column(String(20))
    template_path = Column(String(500))
    field_count = Column(Integer, default=0)
    template_mode = Column(String(20), default="field_fill")  # table_fill / field_fill
    user_requirement = Column(Text, default="")
    upload_user = Column(Integer, ForeignKey("users.id"), nullable=True)
    upload_time = Column(DateTime, default=_now)


class TemplateMapping(Base):
    __tablename__ = "template_mapping"
    id = Column(Integer, primary_key=True, autoincrement=True)
    template_id = Column(Integer, ForeignKey("template_info.id", ondelete="CASCADE"))
    field_name = Column(String(255))
    matched_value = Column(Text)
    confidence = Column(Float, default=0.0)
    source_file = Column(String(255))
    source_position = Column(String(255), nullable=True)
    need_confirm = Column(Integer, default=0)


class TaskInfo(Base):
    __tablename__ = "task_info"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_type = Column(String(50))
    task_name = Column(String(255))
    status = Column(String(20), default="pending")
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    related_id = Column(Integer, nullable=True)
    result_summary = Column(Text, nullable=True)
    output_file = Column(String(500), nullable=True)
    accuracy = Column(Float, nullable=True)
    elapsed = Column(Float, nullable=True)
    start_time = Column(DateTime, default=_now)
    end_time = Column(DateTime, nullable=True)


class OperationLog(Base):
    __tablename__ = "operation_log"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    username = Column(String(50))
    action = Column(String(100))
    detail = Column(Text, nullable=True)
    ip = Column(String(50), nullable=True)
    create_time = Column(DateTime, default=_now)


class FieldDict(Base):
    __tablename__ = "field_dict"
    id = Column(Integer, primary_key=True, autoincrement=True)
    standard_name = Column(String(100), nullable=False)
    aliases = Column(Text)
    field_type = Column(String(50))
    description = Column(Text, nullable=True)


class SystemConfig(Base):
    __tablename__ = "system_config"
    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_value = Column(Text)


class ChatSession(Base):
    __tablename__ = "chat_session"
    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(200), default="")
    create_time = Column(DateTime, default=_now)
    updated_at = Column(DateTime, default=_now, onupdate=_now)


class ChatMessage(Base):
    __tablename__ = "chat_message"
    id = Column(Integer, primary_key=True, autoincrement=True)
    session_id = Column(Integer, ForeignKey("chat_session.id", ondelete="CASCADE"), nullable=False)
    role = Column(String(20), nullable=False)
    content = Column(Text)
    create_time = Column(DateTime, default=_now)
