# 泵站启泵试验 · 水锤压力峰谷分析服务

面向泵站调试员的启泵试验复盘工具：把记录仪导出的**单次试验 CSV** 导入
FastAPI 服务，采样与多级摘要落盘到 **SQLite**；复盘时给定起止时刻和桶宽，
按桶返回首尾点与压力最高 / 最低点，快速定位水锤造成的峰谷，空桶自动标记缺测。

## 功能与约定

- **CSV 导入**（`POST /tests/import`，Swagger 可直接上传）
  - 首行表头，自动识别时间戳列与压力列，支持中英文表头
    （`timestamp_ms / ts / 时间戳 …`、`pressure_kpa / pressure / 压力(kPa) …`），
    UTF-8 与 GBK 编码均可
  - 毫秒时间戳必须为整数且**严格递增**，重复或倒序整次拒绝（HTTP 400）
  - 压力为有限数值，单位 kPa；**单事务写入**：tests / samples / 各级 blocks
    在一个 `BEGIN IMMEDIATE` 事务里提交，任一步失败整体回滚，不留半成品
  - **提交先于响应**：只有事务 commit 成功才返回 201，回执里的试验编号
    立即可查，不会出现“201 之后查不到试验”
- **并发安全**（修复重点）
  - 数据库工作在 **WAL** 模式：一个导入写事务与多个只读查询互不阻塞，
    正在导入大文件时，其他人仍能查询所有已入库试验
  - 每条连接设置 `busy_timeout`：多个导入并发提交时后到者排队等锁，
    而不是直接报 500；锁等待超限时返回 503 便于客户端重试
  - 连接的 建立→事务→提交/回滚→关闭 全部在同一工作线程内完成
    （不使用 FastAPI 的 yield 依赖持有连接，避免其跨线程退出导致的
    `sqlite3` 线程校验错误）
- **分桶查询**（`GET /tests/{id}/query`）
  - 从 `start_ts` 起按 `bucket_width_ms` 分桶，**含左端、不含右端**，
    末桶截到 `end_ts`
  - 每桶按时序返回首点、尾点、压力最高点、最低点；**同一点只列一次**，
    角色合并到 `roles`；极值并列时取时间戳最早的点
  - 桶内无采样时 `missing=true`、`points=[]`
- **多级摘要加速**（核心）
  - level 1 为最细索引块（默认每 128 个采样一块），其上逐级合并，
    每块保存首尾点、最高 / 最低点（含时间戳）、采样数与原始 rowid 区间
  - 查询时从最粗层级开始：**完整落在桶内的块直接采用摘要**；
    只有桶边界切开 level 1 最细块时，才按 rowid 区间回读原始采样
  - 响应中的 `metrics` 如实回显：`blocks_by_level`（各级摘要命中数）
    与 `raw_rows_read`（回读原始行数，边界不切块时为 0）

## 目录结构

```
app/
  config.py      环境变量配置（库路径、块大小、上传上限、桶数上限、锁等待）
  db.py          SQLite 连接、WAL/建表、显式事务（BEGIN IMMEDIATE）
  ingest.py      CSV 解析、严格校验、单事务导入
  summaries.py   多级摘要构建与区间递归查询
  service.py     分桶编排、点去重与角色合并
  schemas.py     Pydantic 请求 / 响应模型
  main.py        FastAPI 路由（请求内同线程管理连接生命周期）
sample_data/     样例 CSV（由 scripts/generate_sample.py 生成）
scripts/         样例生成脚本与 API 演示脚本
tests/           pytest 测试（24 项，含并发与事务回归）
Dockerfile, docker-compose.yml
```

## Docker Compose 启动

```bash
docker compose up --build
```

- 服务：<http://localhost:8000>
- Swagger UI：<http://localhost:8000/docs>
- 健康检查：`GET /health`
- SQLite 数据保存在命名卷 `pump-data`（容器内 `/data/pump_tests.db`）

## 复现样例试验

样例 `sample_data/pump_start_demo.csv` 为 2 万点、10 ms 间隔的合成启泵曲线
（静压 → 启泵爬升 → 衰减振荡，含一次低压深谷）；`large_demo.csv` 为
20 万点大文件，用于制造足够长的导入窗口演示边导入边查询。可重新生成：

```bash
python3 scripts/generate_sample.py
```

在 Swagger（`/docs`）中的操作步骤：

1. `POST /tests/import`：`name` 填 `demo-001`，`file` 选择
   `sample_data/pump_start_demo.csv`，执行。响应返回试验 id、时间范围与各级块数。
2. `GET /tests`：确认试验已入库。
3. `GET /tests/{id}/query`：
   - `start_ts=1731300000000`、`end_ts=1731300019999`、`bucket_width_ms=1000`
     → 200 个 1 秒桶，逐桶查看峰谷；
   - 改成 `bucket_width_ms=1280`（与块边界对齐）再试，
     可见 `metrics.raw_rows_read = 0`，全程只走摘要；
   - 换 `start_ts=1731299990000&end_ts=1731299995000` 可看到全部桶 `missing=true`。

小样本 `sample_data/small_demo.csv`（60 点）适合在 Swagger 里肉眼逐桶核对。

### curl 示例

```bash
curl -X POST http://localhost:8000/tests/import \
  -F "name=demo-001" \
  -F "file=@sample_data/pump_start_demo.csv;type=text/csv"

curl "http://localhost:8000/tests/1/query?start_ts=1731300000000&end_ts=1731300019999&bucket_width_ms=1000"
```

### 一键演示：边导入边查询 + 回执即查

`scripts/demo_api.py` 只用 Python 标准库，直接通过 HTTP 调用服务，依次演示：

1. 导入一份基线试验并启动 **4 个并发读者线程**持续查询它；
2. 读者运行期间再导入 20 万点大文件，打印导入窗口内的查询状态码分布
   （WAL 下应全部为 200，无 500）；
3. 拿到 201 回执后**立刻**用回执里的试验编号查询，验证提交先于响应；
4. 用与摘要块对齐的桶宽（128 点 × 10 ms = 1280 ms）查询，确认
   `metrics.raw_rows_read = 0`，全程只走摘要。

```bash
python3 scripts/demo_api.py                       # 默认 http://localhost:8000
python3 scripts/demo_api.py --base-url http://localhost:8000 \
    --csv sample_data/large_demo.csv --tag run-1
```

预期输出片段：

```
[2/4] 启动 4 个并发读者持续查询试验 #1，同时导入大文件 ...
  大文件导入耗时 46.50s，期间 4 个读者共发起 2409 次查询
  读者响应状态码分布：{200: 2409}
  -> 导入不阻塞已入库试验的查询，全程无 500
[3/4] 收到 201 回执（试验 #2），立即查询 ...
[4/4] 块对齐桶宽查询 ... metrics = {"blocks_used": 1563, "raw_rows_read": 0}
```

## 本地开发（无 Docker）

```bash
python3 -m pip install -r requirements.txt
python3 -m pytest tests/ -q
PUMP_DB_PATH=./data/dev.db python3 -m uvicorn app.main:app --reload --port 8000
```

## 关键环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PUMP_DB_PATH` | `/data/pump_tests.db` | SQLite 文件路径 |
| `PUMP_BLOCK_SIZE` | `128` | 每级摘要块大小（点数 / 子块数） |
| `PUMP_MAX_UPLOAD_BYTES` | `209715200` | 单次上传字节上限（200 MB） |
| `PUMP_MAX_BUCKETS` | `100000` | 单次查询桶数上限，防止桶宽过小 |
| `PUMP_BUSY_TIMEOUT_S` | `30` | 写锁竞争等待上限（秒），超时返回 503 |
