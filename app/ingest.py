"""记录仪 CSV 导入与校验。

要求：
- 首行表头，需能识别出时间戳列与压力列（支持中英文表头，见 config）
- 毫秒时间戳为整数且严格递增（重复或倒序即拒绝整次导入）
- 压力为有限浮点数，单位 kPa
"""

from __future__ import annotations

import csv
import io
import math
import sqlite3

from . import config
from .summaries import build_summaries


class CsvValidationError(ValueError):
    """CSV 内容不满足导入契约。"""


def _resolve_columns(header: list[str]) -> tuple[int, int]:
    ts_idx = p_idx = -1
    for i, name in enumerate(header):
        key = name.strip().lower()
        if ts_idx == -1 and key in config.TIMESTAMP_COLUMNS:
            ts_idx = i
        if p_idx == -1 and key in config.PRESSURE_COLUMNS:
            p_idx = i
    missing = []
    if ts_idx == -1:
        missing.append("时间戳列")
    if p_idx == -1:
        missing.append("压力列")
    if missing:
        raise CsvValidationError(
            "表头缺少 " + "、".join(missing) + f"；实际表头：{header}"
        )
    return ts_idx, p_idx


def _parse_int_ms(raw: str, line_no: int) -> int:
    raw = raw.strip()
    if not raw:
        raise CsvValidationError(f"第 {line_no} 行时间戳为空")
    try:
        # 拒绝 1.5、1.0 这类非整数毫秒时间戳
        value = int(raw)
    except ValueError:
        raise CsvValidationError(
            f"第 {line_no} 行时间戳不是整数毫秒：{raw!r}"
        ) from None
    return value


def _parse_pressure(raw: str, line_no: int) -> float:
    raw = raw.strip()
    if not raw:
        raise CsvValidationError(f"第 {line_no} 行压力为空")
    try:
        value = float(raw)
    except ValueError:
        raise CsvValidationError(f"第 {line_no} 行压力不是数值：{raw!r}") from None
    if not math.isfinite(value):
        raise CsvValidationError(f"第 {line_no} 行压力不是有限值：{raw!r}")
    return value


def parse_csv(content: bytes) -> list[tuple[int, float]]:
    """解析并严格校验 CSV，返回 (ts_ms, pressure_kpa) 序列。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        # 部分记录仪在 Windows 下导出 GBK
        text = content.decode("gbk")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise CsvValidationError("CSV 为空，缺少表头") from None
    if not header or not any(cell.strip() for cell in header):
        raise CsvValidationError("CSV 表头为空")

    ts_idx, p_idx = _resolve_columns(header)
    width = len(header)

    samples: list[tuple[int, float]] = []
    prev_ts: int | None = None
    line_no = 1
    for row in reader:
        line_no += 1
        if not row or not any(cell.strip() for cell in row):
            continue  # 容忍空行
        if len(row) < width:
            raise CsvValidationError(
                f"第 {line_no} 行列数 {len(row)} 少于表头列数 {width}"
            )
        ts = _parse_int_ms(row[ts_idx], line_no)
        pressure = _parse_pressure(row[p_idx], line_no)
        if prev_ts is not None and ts <= prev_ts:
            raise CsvValidationError(
                f"第 {line_no} 行时间戳 {ts} 未严格递增（上一行为 {prev_ts}）"
            )
        samples.append((ts, pressure))
        prev_ts = ts

    if not samples:
        raise CsvValidationError("CSV 没有任何有效采样行")
    return samples


def import_test(
    conn: sqlite3.Connection,
    name: str,
    filename: str,
    content: bytes,
    block_size: int | None = None,
) -> dict:
    """完整导入一次试验：采样入库 + 多级摘要，单事务完成。"""
    block_size = block_size or config.BLOCK_SIZE
    samples = parse_csv(content)

    try:
        cur = conn.execute(
            "INSERT INTO tests (name, source_filename, sample_count, start_ts, end_ts, max_level) "
            "VALUES (?, ?, ?, ?, ?, 0)",
            (name, filename, len(samples), samples[0][0], samples[-1][0]),
        )
        test_id = cur.lastrowid
        conn.executemany(
            "INSERT INTO samples (test_id, ts, pressure) VALUES (?, ?, ?)",
            [(test_id, ts, p) for ts, p in samples],
        )
        max_level = build_summaries(conn, test_id, block_size)
        conn.execute(
            "UPDATE tests SET max_level = ? WHERE id = ?", (max_level, test_id)
        )
    except sqlite3.IntegrityError as exc:
        raise CsvValidationError(f"试验名称 {name!r} 已存在") from exc

    block_counts = dict(
        conn.execute(
            "SELECT level, COUNT(*) FROM blocks WHERE test_id = ? GROUP BY level",
            (test_id,),
        ).fetchall()
    )
    imported_at = conn.execute(
        "SELECT imported_at FROM tests WHERE id = ?", (test_id,)
    ).fetchone()[0]
    return {
        "id": test_id,
        "name": name,
        "source_filename": filename,
        "sample_count": len(samples),
        "start_ts": samples[0][0],
        "end_ts": samples[-1][0],
        "max_level": max_level,
        "imported_at": imported_at,
        "block_counts": {str(k): v for k, v in sorted(block_counts.items())},
    }
