"""SQLite 连接与建表。

表结构：
- tests      试验元信息
- samples    原始毫秒采样（rowid 即严格递增的采样序号）
- blocks     多级摘要块，level 1 为最细索引块，level 越大粒度越粗

并发约定（修复要点）：
- 每个请求在“同一个工作线程”内完成 连接 -> 事务 -> 提交/回滚 -> 关闭 的
  完整生命周期。FastAPI 会把同步生成式依赖的 __enter__/__exit__ 分派到
  线程池里的不同线程，而 sqlite3 连接默认禁止跨线程使用，因此连接管理不能
  放在 yield 依赖里，必须放在端点函数内。
- WAL 模式允许一个写事务与多个只读查询同时进行：导入大文件期间，已入库的
  试验仍可正常查询。
- 每个连接设置 busy_timeout：多个导入并发提交时后到者等待写锁，而不是立刻
  抛 “database is locked” 造成 500。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from typing import Iterator

from . import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tests (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL UNIQUE,
    source_filename TEXT    NOT NULL,
    sample_count    INTEGER NOT NULL,
    start_ts        INTEGER NOT NULL,
    end_ts          INTEGER NOT NULL,
    max_level       INTEGER NOT NULL,
    imported_at     TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS samples (
    test_id    INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    pressure   REAL    NOT NULL,
    FOREIGN KEY (test_id) REFERENCES tests(id)
);

CREATE INDEX IF NOT EXISTS idx_samples_test_ts
    ON samples (test_id, ts);

CREATE TABLE IF NOT EXISTS blocks (
    test_id          INTEGER NOT NULL,
    level            INTEGER NOT NULL,
    block_index      INTEGER NOT NULL,
    start_ts         INTEGER NOT NULL,
    end_ts           INTEGER NOT NULL,
    sample_count     INTEGER NOT NULL,
    first_ts         INTEGER NOT NULL,
    last_ts          INTEGER NOT NULL,
    first_pressure   REAL    NOT NULL,
    last_pressure    REAL    NOT NULL,
    max_pressure     REAL    NOT NULL,
    max_pressure_ts  INTEGER NOT NULL,
    min_pressure     REAL    NOT NULL,
    min_pressure_ts  INTEGER NOT NULL,
    first_rowid      INTEGER NOT NULL,
    last_rowid       INTEGER NOT NULL,
    PRIMARY KEY (test_id, level, block_index)
);

CREATE INDEX IF NOT EXISTS idx_blocks_test_level_ts
    ON blocks (test_id, level, start_ts, end_ts);
"""


def connect(db_path=None) -> sqlite3.Connection:
    """打开一个带常用 PRAGMA 的连接（仅限创建它的线程使用）。"""
    path = str(db_path or config.DB_PATH)
    # timeout 与 busy_timeout 双保险：锁竞争时等待而不是立刻报错
    conn = sqlite3.connect(path, timeout=config.BUSY_TIMEOUT_S)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(config.BUSY_TIMEOUT_S * 1000)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA temp_store = MEMORY")
    return conn


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """显式写事务：BEGIN IMMEDIATE 取写锁，正常结束提交，异常一律回滚。

    试验信息、原始采样与各级摘要在同一个事务里写入：只有全部成功并 commit
    后数据才可见；任何一步失败都整体回滚，不留半成品。
    """
    # 先回到干净的 autocommit 状态，避免“事务内开启事务”的歧义
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    """请求级连接上下文：正常结束提交，异常回滚，最终关闭。

    供脚本 / 测试使用；HTTP 端点请在同一线程内显式使用 connect() 与
    transaction()，不要经 FastAPI 的 yield 依赖跨线程移交连接。
    """
    conn = connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    """进程启动时调用，幂等建库建表，并确保数据库工作在 WAL 模式。"""
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect()
    try:
        # WAL 必须在事务之外切换；一次性持久化到数据库文件，之后每个连接
        # 打开都会沿用，无需每条连接重复执行（重复执行也需要短暂写锁）。
        mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
        if str(mode).lower() != "wal":
            raise RuntimeError(
                f"当前文件系统不支持 SQLite WAL（实际 journal_mode={mode!r}），"
                "无法保证导入期间的并发只读查询，请更换数据卷"
            )
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
