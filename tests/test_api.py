"""端到端与单元测试：导入校验、分桶语义、极值并列、缺测、摘要命中策略。"""

from __future__ import annotations

import io
import os
import tempfile

import pytest

# 测试数据库与块大小必须在导入 app 之前固定
_tmp = tempfile.TemporaryDirectory()
os.environ["PUMP_DB_PATH"] = os.path.join(_tmp.name, "test.db")
os.environ["PUMP_BLOCK_SIZE"] = "4"
os.environ["PUMP_MAX_BUCKETS"] = "1000"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import connect, init_db  # noqa: E402
from app.ingest import CsvValidationError, parse_csv  # noqa: E402
from app.main import app  # noqa: E402
from app.summaries import build_summaries, range_summary  # noqa: E402

init_db()
client = TestClient(app)


def _csv(rows, header=("timestamp_ms", "pressure_kpa")) -> bytes:
    buf = io.StringIO()
    buf.write(",".join(header) + "\n")
    for ts, p in rows:
        buf.write(f"{int(ts)},{p}\n")
    return buf.getvalue().encode("utf-8")


# ---------------------------------------------------------------- 导入校验

def test_parse_rejects_non_strict_increasing():
    with pytest.raises(CsvValidationError, match="严格递增"):
        parse_csv(_csv([(1, 10.0), (2, 11.0), (2, 12.0)]))


def test_parse_rejects_decreasing():
    with pytest.raises(CsvValidationError):
        parse_csv(_csv([(5, 10.0), (4, 11.0)]))


def test_parse_rejects_float_timestamp():
    with pytest.raises(CsvValidationError, match="整数毫秒"):
        parse_csv(b"timestamp_ms,pressure_kpa\n1.5,10.0\n")


def test_parse_rejects_non_finite_pressure():
    with pytest.raises(CsvValidationError, match="有限值"):
        parse_csv(_csv([(1, "nan")]))


def test_parse_rejects_missing_column():
    with pytest.raises(CsvValidationError, match="表头"):
        parse_csv(b"ts,foo\n1,10\n")


def test_parse_accepts_chinese_header():
    samples = parse_csv("时间戳,压力(kPa)\n1,10.5\n2,11\n".encode())
    assert samples == [(1, 10.5), (2, 11.0)]


# ---------------------------------------------------------------- 端到端 API

@pytest.fixture(scope="module")
def imported():
    # 10 个点，block_size=4：level1=[4,4,2]，level2=[4,4,2]
    rows = [(1000 + i * 100, float(p)) for i, p in enumerate([10, 12, 8, 15, 9, 14, 7, 20, 11, 6])]
    resp = client.post(
        "/tests/import",
        data={"name": "unit-test-1"},
        files={"file": ("u.csv", _csv(rows), "text/csv")},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def test_import_summary_levels(imported):
    body = imported
    assert body["sample_count"] == 10
    assert body["max_level"] == 2
    assert body["block_counts"] == {"1": 3, "2": 1}
    assert body["start_ts"] == 1000 and body["end_ts"] == 1900


def test_duplicate_name_rejected(imported):
    rows = [(1, 1.0), (2, 2.0)]
    resp = client.post(
        "/tests/import",
        data={"name": "unit-test-1"},
        files={"file": ("u.csv", _csv(rows), "text/csv")},
    )
    assert resp.status_code == 400


def test_bucket_aligned_with_blocks_uses_only_summaries(imported):
    # 桶边界与 level1 块完全对齐：不应读取任何原始采样
    resp = client.get(
        f"/tests/{imported['id']}/query",
        params={"start_ts": 1000, "end_ts": 1800, "bucket_width_ms": 400},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bucket_count"] == 2
    assert body["metrics"]["raw_rows_read"] == 0
    assert body["metrics"]["blocks_used"] >= 1

    b0, b1 = body["buckets"]
    # 桶0 = 点 10,12,8,15
    assert b0["sample_count"] == 4 and b0["missing"] is False
    roles0 = {pt["ts"]: pt["roles"] for pt in b0["points"]}
    assert "first" in roles0[1000] and "last" in roles0[1300]
    assert "max" in roles0[1300] and "min" in roles0[1200]
    # 时间戳 1300 同时是 last 与 max，只列一次、角色合并
    assert roles0[1300].count("max") == 1
    # 点按时序排列
    assert [pt["ts"] for pt in b0["points"]] == sorted(pt["ts"] for pt in b0["points"])
    # 桶1 = 点 9,14,7,20
    max_pt = next(pt for pt in b1["points"] if "max" in pt["roles"])
    min_pt = next(pt for pt in b1["points"] if "min" in pt["roles"])
    assert max_pt["ts"] == 1700 and max_pt["pressure"] == 20.0
    assert min_pt["ts"] == 1600 and min_pt["pressure"] == 7.0


def test_misaligned_bucket_reads_raw_only_at_edges(imported):
    # 桶宽 200ms（2 点/桶），桶边界切开所有 level1 块：仅边界块读原始采样
    resp = client.get(
        f"/tests/{imported['id']}/query",
        params={"start_ts": 1050, "end_ts": 1850, "bucket_width_ms": 200},
    )
    body = resp.json()
    assert resp.status_code == 200
    assert body["bucket_count"] == 4
    # [1050,1850) 含 8 点（1000 与 1900 在半开区间外）
    assert sum(b["sample_count"] for b in body["buckets"]) == 8
    # 所有点都落在被切开的 level1 块中 -> 必然回退原始采样
    assert body["metrics"]["raw_rows_read"] == 8

    first = body["buckets"][0]
    # [1050,1250) 只含 ts=1100(12), 1200(8)
    assert first["sample_count"] == 2
    assert first["points"][0]["ts"] == 1100
    assert "min" in first["points"][1]["roles"]


def test_last_bucket_clipped_to_end(imported):
    # 300ms 桶宽：[1800,2100) 末桶截到 end=1950 -> [1800,1950)，仅含 1800 一点
    # （1900 本在区间内；再用 end=1900 验证右开边界排除末点）
    resp = client.get(
        f"/tests/{imported['id']}/query",
        params={"start_ts": 1800, "end_ts": 1950, "bucket_width_ms": 300},
    )
    body = resp.json()
    assert body["bucket_count"] == 1
    b = body["buckets"][0]
    assert b["end_ts"] == 1950
    assert b["sample_count"] == 2
    assert [pt["ts"] for pt in b["points"]] == [1800, 1900]

    resp2 = client.get(
        f"/tests/{imported['id']}/query",
        params={"start_ts": 1800, "end_ts": 1900, "bucket_width_ms": 300},
    )
    b2 = resp2.json()["buckets"][0]
    assert b2["sample_count"] == 1
    assert [pt["ts"] for pt in b2["points"]] == [1800]


def test_empty_buckets_marked_missing(imported):
    # 数据时间范围 1000..1900，查询前面的空白时段
    resp = client.get(
        f"/tests/{imported['id']}/query",
        params={"start_ts": 0, "end_ts": 600, "bucket_width_ms": 200},
    )
    body = resp.json()
    assert body["bucket_count"] == 3
    assert body["missing_bucket_count"] == 3
    assert all(b["missing"] and b["points"] == [] for b in body["buckets"])


def test_mixed_missing_and_data_buckets(imported):
    resp = client.get(
        f"/tests/{imported['id']}/query",
        params={"start_ts": 800, "end_ts": 1200, "bucket_width_ms": 200},
    )
    body = resp.json()
    # [800,1000) 空，[1000,1200) 含 ts=1000、1100 两点（1200 属右开边界外）
    assert body["buckets"][0]["missing"] is True
    last = body["buckets"][1]
    assert last["missing"] is False and last["sample_count"] == 2
    first_pt = last["points"][0]
    assert first_pt["ts"] == 1000 and "first" in first_pt["roles"]


def test_single_point_bucket_merges_all_roles(imported):
    # [1000,1100) 只含一个点：首/尾/最高/最低同点，只列一次、角色合并
    resp = client.get(
        f"/tests/{imported['id']}/query",
        params={"start_ts": 1000, "end_ts": 1100, "bucket_width_ms": 100},
    )
    body = resp.json()
    bucket = body["buckets"][0]
    assert bucket["sample_count"] == 1 and len(bucket["points"]) == 1
    assert set(bucket["points"][0]["roles"]) == {"first", "last", "max", "min"}


def test_tie_keeps_earliest_extreme():
    rows = [(i, 5.0) for i in range(8)]  # 压力全部相同
    conn = connect()
    rid = conn.execute(
        "INSERT INTO tests (name, source_filename, sample_count, start_ts, end_ts, max_level) "
        "VALUES ('tie', 't.csv', 8, 0, 7, 0)"
    ).lastrowid
    conn.executemany(
        "INSERT INTO samples (test_id, ts, pressure) VALUES (?, ?, ?)",
        [(rid, ts, p) for ts, p in rows],
    )
    build_summaries(conn, rid, 4)
    conn.commit()
    st = range_summary(conn, rid, 0, 8, max_level=2)
    assert st.max_pressure_ts == 0 and st.min_pressure_ts == 0  # 并列取最早
    # 全区间完整覆盖 level1 块，不读原始采样
    st2_metrics = {"blocks_used": 0, "blocks_by_level": {}, "raw_rows_read": 0}
    range_summary(conn, rid, 0, 8, 2, metrics=st2_metrics)
    assert st2_metrics["raw_rows_read"] == 0
    conn.close()


def test_range_summary_matches_raw_bruteforce(imported):
    """随机区间：摘要策略结果必须与直接扫描原始采样一致。"""
    conn = connect()
    test_id = imported["id"]
    max_level = imported["max_level"]
    raw_rows = conn.execute(
        "SELECT ts, pressure FROM samples WHERE test_id = ? ORDER BY ts", (test_id,)
    ).fetchall()

    for q_start, q_end in [(1050, 1450), (999, 1001), (1000, 1900), (1250, 1750), (1500, 1600)]:
        metrics = {"blocks_used": 0, "blocks_by_level": {}, "raw_rows_read": 0}
        st = range_summary(conn, test_id, q_start, q_end, max_level, 4, metrics)
        expected = [
            (r["ts"], r["pressure"])
            for r in raw_rows
            if q_start <= r["ts"] < q_end
        ]
        if not expected:
            assert st is None or st.sample_count == 0
            continue
        assert st.sample_count == len(expected)
        assert (st.first_ts, st.first_pressure) == expected[0]
        assert (st.last_ts, st.last_pressure) == expected[-1]
        mx = max(expected, key=lambda x: (x[1], -x[0]))
        mn = min(expected, key=lambda x: (x[1], x[0]))
        assert (st.max_pressure_ts, st.max_pressure) == mx
        assert (st.min_pressure_ts, st.min_pressure) == mn
    conn.close()


def test_validation_errors():
    assert client.get("/tests/999/query", params={
        "start_ts": 0, "end_ts": 10, "bucket_width_ms": 5
    }).status_code == 404

    rid = client.get("/tests").json()[0]["id"]
    bad = client.get(f"/tests/{rid}/query", params={
        "start_ts": 100, "end_ts": 100, "bucket_width_ms": 5
    })
    assert bad.status_code == 400

    bad_width = client.get(f"/tests/{rid}/query", params={
        "start_ts": 0, "end_ts": 10, "bucket_width_ms": 0
    })
    assert bad_width.status_code == 422


def test_randomized_ranges_match_bruteforce(imported):
    """固定种子随机区间对拍：多级摘要递归结果必须始终等于原始扫描。"""
    import random

    conn = connect()
    test_id = imported["id"]
    max_level = imported["max_level"]
    raw_rows = conn.execute(
        "SELECT ts, pressure FROM samples WHERE test_id = ? ORDER BY ts",
        (test_id,),
    ).fetchall()
    rng = random.Random(20261111)
    lo_ts, hi_ts = raw_rows[0]["ts"], raw_rows[-1]["ts"]

    for _ in range(200):
        q_start = rng.randint(lo_ts - 500, hi_ts + 500)
        q_end = rng.randint(q_start + 1, q_start + 1500)
        metrics = {"blocks_used": 0, "blocks_by_level": {}, "raw_rows_read": 0}
        st = range_summary(conn, test_id, q_start, q_end, max_level, 4, metrics)
        expected = [
            (r["ts"], r["pressure"])
            for r in raw_rows
            if q_start <= r["ts"] < q_end
        ]
        if not expected:
            assert st is None or st.sample_count == 0
            continue
        assert st.sample_count == len(expected)
        assert (st.first_ts, st.first_pressure) == expected[0]
        assert (st.last_ts, st.last_pressure) == expected[-1]
        mx = max(expected, key=lambda x: (x[1], -x[0]))
        mn = min(expected, key=lambda x: (x[1], x[0]))
        assert (st.max_pressure_ts, st.max_pressure) == mx
        assert (st.min_pressure_ts, st.min_pressure) == mn
    conn.close()


def test_bucket_count_cap():
    resp = client.get(
        f"/tests/{client.get('/tests').json()[0]['id']}/query",
        params={"start_ts": 0, "end_ts": 1_000_000, "bucket_width_ms": 1},
    )
    assert resp.status_code == 400
    assert "上限" in resp.json()["detail"]
