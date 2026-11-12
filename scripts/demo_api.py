#!/usr/bin/env python3
"""可直接调用 API 的演示：边导入边查询 + 收到回执立即查询。

用法（服务已通过 `docker compose up` 启动后）：
    python3 scripts/demo_api.py
    python3 scripts/demo_api.py --base-url http://localhost:8000 \
        --csv sample_data/pump_start_demo.csv

演示内容：
1. 先导入一份“基线试验”，随后启动一组并发查询线程持续读它；
2. 主线程同时导入一份“大文件试验”，打印导入期间查询的成功情况——
   WAL 下写事务不阻塞只读查询，读者全程 200，不会出现 500；
3. 大文件导入拿到 201 回执后，立刻用回执里的试验编号查询，
   数据必然已提交可见（提交发生在响应返回之前）。

只依赖标准库 + requests 不可用时改用 urllib，便于在任意环境直接运行。
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

DEFAULT_BASE_URL = "http://localhost:8000"
SAMPLE_CSV = (
    Path(__file__).resolve().parent.parent / "sample_data" / "large_demo.csv"
)


# ---------------------------------------------------------------- HTTP 小封装

def _request(method: str, url: str, data=None, headers=None, timeout=120):
    req = urllib.request.Request(url, data=data, headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def health(base_url: str) -> None:
    status, body = _request("GET", f"{base_url}/health", timeout=10)
    if status != 200:
        raise SystemExit(f"服务未就绪：GET /health -> {status} {body}")
    print(f"[健康检查] {base_url}/health -> {status} {body}")


def import_csv(base_url: str, name: str, csv_path: Path) -> dict:
    boundary = "----pump-demo-boundary"
    content = csv_path.read_bytes()
    parts = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="name"\r\n\r\n'
        f"{name}\r\n"
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{csv_path.name}"\r\n'
        f"Content-Type: text/csv\r\n\r\n"
    ).encode("utf-8")
    body = parts + content + f"\r\n--{boundary}--\r\n".encode("utf-8")
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
    status, resp = _request("POST", f"{base_url}/tests/import",
                            data=body, headers=headers, timeout=300)
    if status != 201:
        raise SystemExit(f"导入失败：HTTP {status} {resp}")
    return resp


def get_test(base_url: str, test_id: int) -> dict:
    status, resp = _request("GET", f"{base_url}/tests/{test_id}", timeout=30)
    if status != 200:
        raise SystemExit(f"回执编号 {test_id} 查询失败：HTTP {status} {resp}")
    return resp


def query_buckets(base_url: str, test_id: int, start_ts: int, end_ts: int,
                  bucket_width_ms: int) -> dict:
    qs = urllib.parse.urlencode({
        "start_ts": start_ts,
        "end_ts": end_ts,
        "bucket_width_ms": bucket_width_ms,
    })
    status, resp = _request(
        "GET", f"{base_url}/tests/{test_id}/query?{qs}", timeout=60
    )
    if status != 200:
        raise SystemExit(f"分桶查询失败：HTTP {status} {resp}")
    return resp


# ---------------------------------------------------------------- 演示主体

class ConcurrentReaders(threading.Thread):
    """导入进行中持续查询已入库试验，记录每个响应的状态码。"""

    def __init__(self, base_url: str, test_id: int, start_ts: int, end_ts: int):
        super().__init__(daemon=True)
        self.base_url = base_url
        self.test_id = test_id
        # 只查开头 5 秒的小窗口，单次查询足够轻，能在导入窗口内密集发起
        self.start_ts = start_ts
        self.end_ts = min(start_ts + 5_000, end_ts + 1)
        self.stop = threading.Event()
        self.status_counts: dict[int, int] = {}
        self.errors: list[str] = []
        self.queries = 0

    def run(self) -> None:
        qs = urllib.parse.urlencode({
            "start_ts": self.start_ts,
            "end_ts": self.end_ts,
            "bucket_width_ms": 1000,
        })
        url = f"{self.base_url}/tests/{self.test_id}/query?{qs}"
        while not self.stop.is_set():
            status, body = _request("GET", url, timeout=60)
            self.status_counts[status] = self.status_counts.get(status, 0) + 1
            self.queries += 1
            if status != 200:
                self.errors.append(f"HTTP {status}: {body}")


def main() -> int:
    parser = argparse.ArgumentParser(description="泵站分析服务 API 演示")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--csv", type=Path, default=SAMPLE_CSV,
                        help="用于演示的大文件 CSV（默认样例 2 万点）")
    parser.add_argument("--tag", default=str(int(time.time())),
                        help="试验名后缀，保证可重复执行")
    args = parser.parse_args()
    base = args.base_url.rstrip("/")
    csv_path: Path = args.csv
    if not csv_path.exists():
        raise SystemExit(f"找不到 CSV：{csv_path}")

    health(base)

    # ---- 步骤 1：导入基线试验（后续读者在整个导入期间查它）
    print("\n[1/4] 导入基线试验 baseline-{} ...".format(args.tag))
    baseline = import_csv(base, f"baseline-{args.tag}", csv_path)
    print("  回执：", json.dumps(baseline, ensure_ascii=False))
    base_id = baseline["id"]

    # ---- 步骤 2：读者先跑起来，再启动大文件导入（边导入边查询）
    print(f"\n[2/4] 启动 4 个并发读者持续查询试验 #{base_id}，同时导入大文件 ...")
    readers = [
        ConcurrentReaders(base, base_id, baseline["start_ts"], baseline["end_ts"])
        for _ in range(4)
    ]
    for r in readers:
        r.start()
    time.sleep(0.5)  # 确保读者先于写事务进入查询

    t0 = time.time()
    big = import_csv(base, f"big-import-{args.tag}", csv_path)
    elapsed = time.time() - t0
    for r in readers:
        r.stop.set()
    for r in readers:
        r.join(timeout=10)

    total_queries = sum(r.queries for r in readers)
    status_counts: dict[int, int] = {}
    errors: list[str] = []
    for r in readers:
        for code, n in r.status_counts.items():
            status_counts[code] = status_counts.get(code, 0) + n
        errors.extend(r.errors)

    print(f"  大文件导入耗时 {elapsed:.2f}s，期间 4 个读者共发起 {total_queries} 次查询")
    print(f"  读者响应状态码分布：{status_counts}")
    if errors:
        print("  异常响应：", errors[:5])
        return 1
    assert status_counts.get(200, 0) > 0, "导入期间没有任何成功查询"
    print("  -> 导入不阻塞已入库试验的查询，全程无 500")

    # ---- 步骤 3：收到 201 回执后立即用回执编号查询
    new_id = big["id"]
    print(f"\n[3/4] 收到 201 回执（试验 #{new_id}），立即查询 ...")
    detail = get_test(base, new_id)
    print("  元信息：", json.dumps(detail, ensure_ascii=False))

    resp = query_buckets(
        base, new_id,
        start_ts=big["start_ts"],
        end_ts=min(big["start_ts"] + 5_000, big["end_ts"] + 1),
        bucket_width_ms=1000,
    )
    print(
        "  分桶查询：bucket_count={bucket_count}, "
        "missing={missing_bucket_count}, metrics={metrics}".format(**resp)
    )
    first = next(b for b in resp["buckets"] if not b["missing"])
    print("  首个非空桶的峰谷点：", json.dumps(first["points"], ensure_ascii=False))

    # ---- 步骤 4：桶宽与摘要块对齐时，全程只走摘要（raw_rows_read=0）
    print("\n[4/4] 块对齐桶宽查询（128 点 × 10ms = 1280ms），验证摘要加速 ...")
    aligned = query_buckets(
        base, new_id,
        start_ts=big["start_ts"],
        end_ts=big["end_ts"] + 1,
        bucket_width_ms=1280,
    )
    print("  metrics =", json.dumps(aligned["metrics"], ensure_ascii=False))
    assert aligned["metrics"]["raw_rows_read"] == 0

    print("\n演示完成：边导入边查询、回执即查、分桶摘要加速均符合预期。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
