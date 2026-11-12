"""多级摘要的构建与查询。

摘要块层级：
- level 1：每 BLOCK_SIZE 个原始采样聚合出的最细索引块
- level k>1：由 level k-1 中每 BLOCK_SIZE 个子块合并而成的更粗块

每个块保存：首尾点、压力最高/最低点（并列取最早）、采样点数、
覆盖的原始 rowid 区间与时间区间（end_ts 为最后一点的时间戳）。

查询时优先使用“完整落在查询区间内的最粗块”，只有区间边界切开
level 1 块时，才回退读取该块内的原始采样。
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Optional

from . import config

# blocks 表的列 -> Stats 字段
_BLOCK_FIELDS = (
    "sample_count",
    "first_ts",
    "last_ts",
    "first_pressure",
    "last_pressure",
    "max_pressure",
    "max_pressure_ts",
    "min_pressure",
    "min_pressure_ts",
)


@dataclass
class Stats:
    """一段采样（原始行或摘要块）的四点统计结果。"""

    sample_count: int = 0
    first_ts: Optional[int] = None
    last_ts: Optional[int] = None
    first_pressure: Optional[float] = None
    last_pressure: Optional[float] = None
    max_pressure: Optional[float] = None
    max_pressure_ts: Optional[int] = None
    min_pressure: Optional[float] = None
    min_pressure_ts: Optional[int] = None

    @classmethod
    def from_row(cls, row) -> "Stats":
        return cls(**{f: row[f] for f in _BLOCK_FIELDS})

    def to_tuple(self) -> tuple:
        return tuple(getattr(self, f) for f in _BLOCK_FIELDS)


def stats_from_sample(ts: int, pressure: float) -> Stats:
    """单点统计。"""
    return Stats(
        sample_count=1,
        first_ts=ts,
        last_ts=ts,
        first_pressure=pressure,
        last_pressure=pressure,
        max_pressure=pressure,
        max_pressure_ts=ts,
        min_pressure=pressure,
        min_pressure_ts=ts,
    )


def merge_stats(a: Stats, b: Stats) -> Stats:
    """按时序合并两段统计。极值并列时保留时间戳更早的一个。"""
    if a.sample_count == 0:
        return b
    if b.sample_count == 0:
        return a
    if b.max_pressure > a.max_pressure or (
        b.max_pressure == a.max_pressure and b.max_pressure_ts < a.max_pressure_ts
    ):
        max_p, max_ts = b.max_pressure, b.max_pressure_ts
    else:
        max_p, max_ts = a.max_pressure, a.max_pressure_ts
    if b.min_pressure < a.min_pressure or (
        b.min_pressure == a.min_pressure and b.min_pressure_ts < a.min_pressure_ts
    ):
        min_p, min_ts = b.min_pressure, b.min_pressure_ts
    else:
        min_p, min_ts = a.min_pressure, a.min_pressure_ts
    return Stats(
        sample_count=a.sample_count + b.sample_count,
        first_ts=a.first_ts,
        last_ts=b.last_ts,
        first_pressure=a.first_pressure,
        last_pressure=b.last_pressure,
        max_pressure=max_p,
        max_pressure_ts=max_ts,
        min_pressure=min_p,
        min_pressure_ts=min_ts,
    )


def build_summaries(conn: sqlite3.Connection, test_id: int, block_size: int) -> int:
    """为已写入 samples 的试验构建全部摘要层级，返回最高 level。

    调用方负责事务管理；level 1 块直接按 rowid 序号分块聚合，
    之后逐层合并子块，直到只剩一个块。
    """
    cur = conn.execute(
        "SELECT rowid AS rid, ts, pressure FROM samples WHERE test_id = ? ORDER BY rowid",
        (test_id,),
    )
    rows = cur.fetchall()
    if not rows:
        return 0

    level_rows: list[tuple] = []  # (stats, first_rowid, last_rowid)

    def flush(level: int, entries: list[tuple]) -> None:
        for idx, (st, first_rid, last_rid) in enumerate(entries):
            conn.execute(
                f"""INSERT INTO blocks (
                        test_id, level, block_index,
                        start_ts, end_ts,
                        {", ".join(_BLOCK_FIELDS)},
                        first_rowid, last_rowid
                    ) VALUES (
                        ?, ?, ?, ?, ?,
                        {", ".join("?" for _ in _BLOCK_FIELDS)},
                        ?, ?
                    )""",
                (
                    test_id,
                    level,
                    idx,
                    st.first_ts,
                    st.last_ts,
                    *st.to_tuple(),
                    first_rid,
                    last_rid,
                ),
            )

    # ---- level 1：原始采样分块 ----
    entries: list[tuple] = []
    acc = Stats()
    block_first_rid = rows[0]["rid"]
    for i, row in enumerate(rows):
        st = stats_from_sample(row["ts"], row["pressure"])
        acc = merge_stats(acc, st)
        if (i + 1) % block_size == 0:
            entries.append((acc, block_first_rid, row["rid"]))
            acc = Stats()
            block_first_rid = rows[i + 1]["rid"] if i + 1 < len(rows) else row["rid"]
    if acc.sample_count:
        entries.append((acc, block_first_rid, rows[-1]["rid"]))
    flush(1, entries)
    max_level = 1

    # ---- level 2+：逐层合并上一级块 ----
    prev = entries
    while len(prev) > 1:
        level = max_level + 1
        cur_entries: list[tuple] = []
        for i in range(0, len(prev), block_size):
            group = prev[i : i + block_size]
            st = Stats()
            for child_st, _, _ in group:
                st = merge_stats(st, child_st)
            cur_entries.append((st, group[0][1], group[-1][2]))
        flush(level, cur_entries)
        prev = cur_entries
        max_level = level

    return max_level


def range_summary(
    conn: sqlite3.Connection,
    test_id: int,
    q_start: int,
    q_end: int,
    max_level: int,
    block_size: int | None = None,
    metrics: Optional[dict] = None,
) -> Optional[Stats]:
    """计算半开区间 [q_start, q_end) 内采样的四点统计。

    递归策略：
    1. 取当前最粗层级与区间相交的块；
    2. 块完整落在区间内 -> 直接采用块摘要（命中缓存，不读采样）；
    3. 块被区间边界切开 -> 降到下一层递归；到 level 1 仍被切开
       时，按 rowid 范围读取该块内的原始采样。
    """
    block_size = block_size or config.BLOCK_SIZE
    if metrics is None:
        metrics = {"blocks_used": 0, "blocks_by_level": {}, "raw_rows_read": 0}

    if q_start >= q_end:
        return None

    level = max_level
    overall = Stats()

    while level >= 1:
        rows = conn.execute(
            """SELECT * FROM blocks
               WHERE test_id = ? AND level = ?
                 AND start_ts < ? AND end_ts >= ?
               ORDER BY block_index""",
            (test_id, level, q_end, q_start),
        ).fetchall()

        if not rows:
            # 本层没有相交块（例如查询区间只压在尾部半块之外），继续向下找
            level -= 1
            continue

        for row in rows:
            fully_inside = row["start_ts"] >= q_start and row["end_ts"] < q_end
            if fully_inside:
                overall = merge_stats(overall, Stats.from_row(row))
                metrics["blocks_used"] += 1
                metrics["blocks_by_level"][str(level)] = (
                    metrics["blocks_by_level"].get(str(level), 0) + 1
                )
                continue

            # 块被区间边界切开
            if level == 1:
                # 最细索引块也被切开 -> 唯一需要读原始采样的情形
                raw = conn.execute(
                    """SELECT ts, pressure FROM samples
                       WHERE test_id = ? AND rowid BETWEEN ? AND ?
                         AND ts >= ? AND ts < ?
                       ORDER BY rowid""",
                    (
                        test_id,
                        row["first_rowid"],
                        row["last_rowid"],
                        q_start,
                        q_end,
                    ),
                ).fetchall()
                metrics["raw_rows_read"] += len(raw)
                for r in raw:
                    overall = merge_stats(
                        overall, stats_from_sample(r["ts"], r["pressure"])
                    )
            else:
                # 递归处理该父块在更细一层中的子块，区间裁剪到父块覆盖范围。
                # 右端取 end_ts+1：当父块末点本就落在桶内时，保证最末子块
                # 仍能命中“完整落入”分支；右开排除由 level 1 的 ts < q_end 兜底，
                # 不会把桶外采样带进来。
                sub = range_summary(
                    conn,
                    test_id,
                    max(q_start, row["start_ts"]),
                    min(q_end, row["end_ts"] + 1),
                    level - 1,
                    block_size,
                    metrics,
                )
                if sub is not None:
                    overall = merge_stats(overall, sub)
        return overall if overall.sample_count else None

    return overall if overall.sample_count else None
