# -*- coding: utf-8 -*-
"""
Database setup for the License Server
"""

import logging

from sqlalchemy import create_engine, event
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import config

logger = logging.getLogger(__name__)

#: SQLite 写锁等待时长（秒）。多个 worker/线程同时写时，SQLite 默认几乎立即
#: 返回 "database is locked"；等待一段时间让写操作排队，而不是直接把请求打回。
SQLITE_BUSY_TIMEOUT_SECONDS = 30.0


def is_sqlite_url(url: str) -> bool:
    """URL 是否指向 SQLite。

    保持既有判断标准（子串包含），以免改变非 SQLite 后端的现有行为。
    """
    return "sqlite" in url


def sqlite_connect_args(url: str) -> dict:
    """SQLite 专用的 DBAPI 连接参数；非 SQLite 返回空 dict（行为不变）。

    ``timeout`` 是 sqlite3.connect 的 busy timeout（秒），等价于
    ``PRAGMA busy_timeout``。``check_same_thread`` 保持既有取值。
    """
    if not is_sqlite_url(url):
        return {}
    return {
        "check_same_thread": False,
        "timeout": SQLITE_BUSY_TIMEOUT_SECONDS,
    }


def apply_sqlite_pragmas(dbapi_connection, connection_record):
    """每条 SQLite 连接建立时启用 WAL。

    ``journal_mode=WAL`` 是数据库文件的持久属性，因此对已有数据库重复执行是幂等
    的，既不重建也不迁移数据，也不会改变事务语义（仍然满足 ACID；同步级别保持
    默认的 FULL 不变）。

    设置失败不抛出：只读挂载或部分网络文件系统不支持 WAL，此时连接本身仍应可用，
    只留一条日志。
    """
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.fetchone()  # PRAGMA 会返回一行，取走它
    except Exception:
        logger.warning(
            "无法为 SQLite 连接启用 WAL，继续按原日志模式运行", exc_info=True
        )
    finally:
        cursor.close()


# Create database engine
engine = create_engine(
    config.settings.DATABASE_URL,
    connect_args=sqlite_connect_args(config.settings.DATABASE_URL),
)

# WAL 只对 SQLite 注册，其它后端不挂任何监听器。
if is_sqlite_url(config.settings.DATABASE_URL):
    event.listen(engine, "connect", apply_sqlite_pragmas)

# Create SessionLocal class
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Create Base class for models
Base = declarative_base()

# Import all models for registration
from models import Authorization, DeviceDailyActive, License  # noqa: F401


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()