"""分桶查询业务逻辑。"""

from __future__ import annotations

import sqlite3

from . import config
from .schemas import BucketOut, PointOut, QueryMetrics, QueryResponse
from .summaries import Stats, range_summary

_ROLE_ORDER = ("first", "last", "max", "min")
# 角色 -> Stats 中对应的 (ts 字段, pressure 字段)
_ROLE_FIELDS = {
    "first": ("first_ts", "first_pressure"),
    "last": ("last_ts", "last_pressure"),
    "max": ("max_pressure_ts", "max_pressure"),
    "min": ("min_pressure_ts", "min_pressure"),
}


def _points_from_stats(stats: Stats) -> list[PointOut]:
    """把四点统计展开为按时序、去重后的点列表；同一点合并多个角色。"""
    by_ts: dict[int, dict] = {}
    for role in _ROLE_ORDER:
        ts_field, p_field = _ROLE_FIELDS[role]
        ts = getattr(stats, ts_field)
        pressure = getattr(stats, p_field)
        if ts is None:
            continue
        entry = by_ts.setdefault(ts, {"ts": ts, "pressure": pressure, "roles": []})
        entry["roles"].append(role)
    return [
        PointOut(**by_ts[ts]) for ts in sorted(by_ts)
    ]


def bucket_query(
    conn: sqlite3.Connection,
    test_id: int,
    start_ts: int,
    end_ts: int,
    bucket_width_ms: int,
) -> QueryResponse:
    """从 start_ts 起按固定桶宽分桶（左含右不含），末桶截到 end_ts。"""
    test = conn.execute("SELECT * FROM tests WHERE id = ?", (test_id,)).fetchone()
    if test is None:
        raise KeyError(f"试验 {test_id} 不存在")

    span = end_ts - start_ts
    bucket_count = (span + bucket_width_ms - 1) // bucket_width_ms
    if bucket_count > config.MAX_BUCKETS:
        raise ValueError(
            f"桶数 {bucket_count} 超过上限 {config.MAX_BUCKETS}，请加大桶宽或缩小时间范围"
        )

    metrics = {"blocks_used": 0, "blocks_by_level": {}, "raw_rows_read": 0}
    buckets: list[BucketOut] = []
    missing_count = 0

    for i in range(bucket_count):
        b_start = start_ts + i * bucket_width_ms
        b_end = min(b_start + bucket_width_ms, end_ts)  # 末桶截到终点
        stats = range_summary(
            conn,
            test_id,
            b_start,
            b_end,
            test["max_level"],
            config.BLOCK_SIZE,
            metrics,
        )
        if stats is None or stats.sample_count == 0:
            missing_count += 1
            buckets.append(
                BucketOut(
                    bucket_index=i,
                    start_ts=b_start,
                    end_ts=b_end,
                    sample_count=0,
                    missing=True,
                    points=[],
                )
            )
        else:
            buckets.append(
                BucketOut(
                    bucket_index=i,
                    start_ts=b_start,
                    end_ts=b_end,
                    sample_count=stats.sample_count,
                    missing=False,
                    points=_points_from_stats(stats),
                )
            )

    return QueryResponse(
        test_id=test_id,
        test_name=test["name"],
        start_ts=start_ts,
        end_ts=end_ts,
        bucket_width_ms=bucket_width_ms,
        bucket_count=bucket_count,
        missing_bucket_count=missing_count,
        metrics=QueryMetrics(**metrics),
        buckets=buckets,
    )
