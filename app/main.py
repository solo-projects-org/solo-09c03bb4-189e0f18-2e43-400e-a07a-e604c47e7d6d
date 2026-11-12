"""FastAPI 入口：试验导入、列表、分桶查询。

启动后访问 /docs 即可在 Swagger UI 上传样例 CSV 并调整桶宽查询。

并发模型：每个请求在端点函数（同一工作线程）内完成
连接 -> 事务 -> 提交/回滚 -> 关闭。不要把 sqlite3 连接放进
yield 依赖——FastAPI 会把同步上下文管理器的 __enter__ 与 __exit__
分派到线程池中的不同线程，而 sqlite3 连接默认不允许跨线程使用，
会导致提交失败（201 之后查不到试验）和 500。
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile

from . import config
from .db import connect, init_db
from .ingest import CsvValidationError, import_test
from .schemas import ImportResponse, QueryResponse, TestOut
from .service import bucket_query


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(
    title="泵站启泵试验水锤压力分析",
    description=(
        "导入记录仪单次试验 CSV（毫秒时间戳、kPa 压力），"
        "在 SQLite 中保存原始采样与多级摘要；按起止时刻与桶宽分桶，"
        "返回每桶首/尾/最高/最低点，空桶标记为缺测。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _locked(exc: sqlite3.OperationalError) -> bool:
    msg = str(exc).lower()
    return "locked" in msg or "busy" in msg


@app.get("/health", tags=["系统"])
def health():
    return {"status": "ok"}


@app.post(
    "/tests/import",
    response_model=ImportResponse,
    status_code=201,
    tags=["试验导入"],
    summary="上传单次试验 CSV",
)
def upload_test(
    file: UploadFile = File(..., description="记录仪导出的 CSV，首行表头"),
    name: str = Form(..., description="试验名称，库内唯一"),
):
    # 读文件在事务之外，避免持锁等待慢客户端
    content = file.file.read(config.MAX_UPLOAD_BYTES + 1)
    if len(content) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"文件超过 {config.MAX_UPLOAD_BYTES} 字节上限",
        )
    if not content:
        raise HTTPException(status_code=400, detail="上传文件为空")

    conn = connect()
    try:
        try:
            # import_test 内部以单事务写入 tests/samples/blocks，
            # 只有 commit 成功才会返回；失败已整体回滚，无半成品。
            result = import_test(
                conn, name=name, filename=file.filename or "", content=content
            )
        except CsvValidationError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except sqlite3.OperationalError as exc:
            if _locked(exc):
                raise HTTPException(
                    status_code=503,
                    detail="数据库写锁繁忙，请稍后重试",
                ) from None
            raise
    finally:
        conn.close()
    return result


@app.get("/tests", response_model=list[TestOut], tags=["试验查询"], summary="列出全部试验")
def list_tests():
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT id, name, source_filename, sample_count, start_ts, end_ts, "
            "max_level, imported_at FROM tests ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


@app.get(
    "/tests/{test_id}",
    response_model=TestOut,
    tags=["试验查询"],
    summary="查看单个试验元信息",
)
def get_test(test_id: int):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT id, name, source_filename, sample_count, start_ts, end_ts, "
            "max_level, imported_at FROM tests WHERE id = ?",
            (test_id,),
        ).fetchone()
    finally:
        conn.close()
    if row is None:
        raise HTTPException(status_code=404, detail=f"试验 {test_id} 不存在")
    return dict(row)


@app.get(
    "/tests/{test_id}/query",
    response_model=QueryResponse,
    tags=["试验查询"],
    summary="按起止时刻与桶宽分桶，定位每桶水锤峰谷",
)
def query_buckets(
    test_id: int,
    start_ts: int = Query(..., description="查询起点毫秒时间戳（含），桶从该时刻起划分"),
    end_ts: int = Query(..., description="查询终点毫秒时间戳（不含），末桶截到该时刻"),
    bucket_width_ms: int = Query(..., gt=0, description="桶宽，毫秒，必须为正整数"),
):
    if end_ts <= start_ts:
        raise HTTPException(
            status_code=400, detail="end_ts 必须大于 start_ts（半开区间 [start, end)）"
        )
    # WAL 下只读连接不会被正在进行的导入阻塞：已提交的试验随时可查。
    conn = connect()
    try:
        try:
            return bucket_query(conn, test_id, start_ts, end_ts, bucket_width_ms)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from None
        except sqlite3.OperationalError as exc:
            if _locked(exc):
                raise HTTPException(
                    status_code=503,
                    detail="数据库繁忙，请稍后重试",
                ) from None
            raise
    finally:
        conn.close()
