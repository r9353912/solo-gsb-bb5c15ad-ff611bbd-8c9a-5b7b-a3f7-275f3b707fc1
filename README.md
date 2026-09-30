# 实验室测量可信性追溯服务

基于 **Python 3.11 + FastAPI + SQLite** 的校准证书 / 测量读数可信性追溯 HTTP 服务。

- 校准证书：证书编号、设备编号、UTC **左闭右开**有效期 `[valid_from, valid_to)`、**闭区间**允许值范围 `[min_value, max_value]`。
- 测量读数：读数编号、设备编号、UTC 测量时间、数值。
- 每次成功变更返回**全局单调递增版本号**；所有写请求必须带 `request_id`（请求标识）。
- 证书只能**撤销**或由新证书版本**纠正**：纠正后旧版本在新版本起失效，历史只追加、永不覆盖。
- 读数按测量时间匹配**指定提交版本视角**下的有效证书：
  - 无匹配 → `unverified`（未验证）
  - 多份匹配 → `review`（待复核）
  - 唯一匹配且数值在闭区间内 → `conforming`（合格）
  - 唯一匹配但数值超出闭区间 → `out_of_range`（超限）
- 撤销 / 纠正会改变当前受影响读数的结论，并在响应中给出前后对照；旧版本的结论与所依证书仍可随时查询。
- **人工选证裁决**：处于 `review` 的读数可由人工指定一份候选证书版本作出裁决；成功裁决占用一个全局版本，按所选证书闭区间给出 `conforming` / `out_of_range` 并保留全部候选与裁决依据。日后所选证书版本被纠正 / 撤销，裁决自动失效、读数回到自动结论；旧版本仍可查原裁决。
- **幂等**：相同 `request_id` + 相同内容重复提交不产生新版本（返回首次结果，`idempotent_replay=true`）；`request_id` 复用但内容不同返回 `409`；无效区间 / 未知目标返回 `422` / `404`，单事务保证不留下任何部分变更。

---

## 一、启动

### 方式 A：Docker Compose（推荐，一键启动并初始化数据库）

```bash
docker compose up -d --build
```

服务启动时会自动在数据卷中创建并初始化 SQLite 数据库（表不存在才建，幂等可重复执行）。

查看状态 / 日志 / 停止：

```bash
docker compose ps
docker compose logs -f
docker compose down            # 停止并删除容器，数据卷保留
docker compose down -v         # 同时删除数据库数据卷
```

数据库文件位于容器内 `/data/trace.db`，通过命名卷 `trace-data` 持久化。

### 方式 B：本地 Python 运行

```bash
pip install -r requirements.txt
export TRACE_DB_PATH=./data/trace.db   # 可选，默认即此值
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

> 需要 Python 3.11+。首次启动自动建库（会自动创建 `./data/` 目录）。

---

## 二、地址与端口

| 用途 | 地址 |
| --- | --- |
| 服务根地址 | <http://localhost:8080/> |
| 健康检查 | `GET http://localhost:8080/api/v1/health` |
| API 前缀 | `http://localhost:8080/api/v1` |
| Swagger 交互文档 | <http://localhost:8080/docs> |
| OpenAPI 描述 | <http://localhost:8080/openapi.json> |

---

## 三、配置（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `TRACE_DB_PATH` | `./data/trace.db`（容器内 `/data/trace.db`） | SQLite 数据库文件路径 |
| `TRACE_HOST` | `0.0.0.0` | 监听地址 |
| `TRACE_PORT` | `8080` | 监听端口 |
| `TRACE_BUSY_TIMEOUT_MS` | `5000` | SQLite 写锁等待毫秒数 |
| `TRACE_MAX_PAGE_SIZE` | `200` | 列表接口单页最大条数 |

Compose 中可直接修改 `docker-compose.yml` 的 `environment` 与 `ports` 映射。

---

## 四、数据模型约定

- 所有时间必须**带时区**，服务端统一归一化为 UTC，例：`2026-01-01T00:00:00Z` 或 `2026-01-01T08:00:00+08:00`。
- 有效期：`valid_from <= measured_at < valid_to`（左闭右开，右边界时刻不匹配）。
- 允许值：`min_value <= value <= max_value`（闭区间，边界值合格）。
- 写请求体必须含非空 `request_id`（≤128 字符）。
- 错误响应统一格式：

  ```json
  { "error": { "code": "request_id_conflict", "message": "..." } }
  ```

---

## 五、调用示例

以下示例可直接用 `curl` 执行。

### 1. 签发校准证书

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-001",
    "certificate_no": "CERT-001",
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to":   "2026-07-01T00:00:00Z",
    "min_value": 0.0,
    "max_value": 100.0
  }'
```

返回：

```json
{
  "version": 1,
  "request_id": "req-001",
  "event_type": "cert_issue",
  "idempotent_replay": false,
  "certificate": {
    "certificate_no": "CERT-001", "version_seq": 1,
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00.000000Z",
    "valid_to":   "2026-07-01T00:00:00.000000Z",
    "min_value": 0.0, "max_value": 100.0,
    "created_at_version": 1, "superseded_at_version": null
  }
}
```

### 2. 提交读数（支持批量，立即返回当前版本视角下的结论）

```bash
curl -s -X POST http://localhost:8080/api/v1/readings \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-002",
    "readings": [
      {"reading_no": "M-1", "device_no": "DEV-A", "measured_at": "2026-03-01T10:00:00Z", "value": 42.0},
      {"reading_no": "M-2", "device_no": "DEV-A", "measured_at": "2026-03-01T11:00:00Z", "value": 140.0},
      {"reading_no": "M-3", "device_no": "DEV-B", "measured_at": "2026-03-01T12:00:00Z", "value": 1.0}
    ]
  }'
```

- `M-1` 唯一匹配且在范围内 → `conforming`
- `M-2` 唯一匹配但超范围 → `out_of_range`
- `M-3` 设备无证书覆盖 → `unverified`

### 3. 待复核：同一测量时刻有多份有效证书

再签发一份覆盖同一时刻的证书后，`M-1` 的当前结论变为 `review`，响应中 `candidates` 列出全部候选证书。

### 4. 人工选证裁决（对 review 读数作出人工结论）

对处于 `review` 的读数提交裁决：携带 `request_id`、读数编号、所选**证书编号 + 版本序号**、裁决所依据的**当前全局版本** `base_version` 与理由。

```bash
# 先取当前全局版本（也可从最近一次写响应的 version 获得）
BASE=$(curl -s http://localhost:8080/api/v1/status | python3 -c 'import sys,json;print(json.load(sys.stdin)["current_version"])')

curl -s -X POST http://localhost:8080/api/v1/readings/M-1/adjudicate \
  -H 'Content-Type: application/json' \
  -d "{
    \"request_id\": \"req-adj-001\",
    \"reading_no\": \"M-1\",
    \"certificate_no\": \"CERT-001\",
    \"version_seq\": 1,
    \"base_version\": $BASE,
    \"reason\": \"复核确认按 CERT-001 判定\"
  }"
```

裁决规则：

- 所选版本必须是该读数**测量时刻**在 `base_version` 视角下的候选证书之一，否则 `422 target_mismatch`。
- `base_version` 必须等于提交时的当前全局版本（乐观并发控制）；已被其他变更推进则 `409 base_version_stale`，请重新读取后再提交。
- 读数当前不是 `review`（未知读数 `404 unknown_reading`；`unverified/conforming/out_of_range` → `409 reading_not_in_review`）。
- 任何拒绝都**不产生新版本**（全局版本号不跳号），单事务回滚不留痕。
- 成功裁决**占用一个全局版本**，事件类型为 `reading_adjudicate`；响应按所选证书闭区间给出 `conforming` / `out_of_range`，`candidates` 保留全部候选，`adjudication` 给出裁决依据（`base_version`、所选编号与版本、候选闭集快照）。

幂等与其他写接口一致：相同 `request_id` + 完全相同内容（含 `reason`）重复提交沿用首次结果（`idempotent_replay=true`，即使此时 `base_version` 已落后）；`request_id` 复用但内容不同返回 `409 request_id_conflict`。

裁决的失效与追溯：

- 所选证书版本日后被**纠正**（新版本使旧版本失效）或**撤销**：依赖它的生效裁决自动**失效**，读数当前结论回到原有自动结论（按当时有效证书重新评估）。读数查询 / 列表的 `adjudication.state` 变为 `invalidated`，并给出 `invalidated_at_version` 与 `invalidated_by_event`；纠正 / 撤销响应的 `affected_readings` 对这类读数标记 `adjudication_invalidated=true`。
- **旧版本仍可查原裁决**：以 `at_version` 查询裁决生效时的版本，仍返回当时的 `conforming` / `out_of_range` 与 `state=effective` 的裁决依据。
- 读数回到 `review` 后可再次人工裁决；历史裁决只追加，永不覆盖。
- 读数查询 / 列表均**按版本呈现**裁决：裁决版本之前看不到裁决（`adjudication=null`、`status=review`），生效期间按裁决呈现，失效后呈现自动结论并标注裁决已失效；列表的 `status` 过滤按呈现口径。

### 5. 纠正证书（新版本使旧版本失效）

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates/CERT-001/correct \
  -H 'Content-Type: application/json' \
  -d '{
    "request_id": "req-003",
    "certificate_no": "CERT-001",
    "device_no": "DEV-A",
    "valid_from": "2026-01-01T00:00:00Z",
    "valid_to":   "2026-07-01T00:00:00Z",
    "min_value": 50.0,
    "max_value": 100.0,
    "reason": "允许范围纠正为 50-100"
  }'
```

返回新版本号，`superseded_version_seq` 指向被失效的旧版本，`affected_readings` 给出相关设备上所有读数的前后结论对照（例如 `M-1：conforming -> out_of_range`）。旧版本内容仍保留在历史中。

### 6. 撤销证书

```bash
curl -s -X POST http://localhost:8080/api/v1/certificates/CERT-001/revoke \
  -H 'Content-Type: application/json' \
  -d '{"request_id": "req-004", "certificate_no": "CERT-001", "reason": "证书作废"}'
```

受影响读数结论随之改变（例如 `out_of_range -> unverified`）。撤销后不能再纠正，重复撤销返回 `409`。

### 7. 幂等重放与冲突

```bash
# 与步骤 1 完全相同的 request_id + 内容：不产生新版本，返回首次结果
curl -s -X POST http://localhost:8080/api/v1/certificates \
  -H 'Content-Type: application/json' -d '{ ...同步骤1... }'
# -> 200，"version": 1，"idempotent_replay": true

# 同一 request_id 但内容不同：
# -> 409 {"error":{"code":"request_id_conflict", ...}}，无任何数据落库
```

### 8. 查询

```bash
# 读数当前结论
curl -s http://localhost:8080/api/v1/readings/M-1

# 读数在历史版本视角下的结论（旧结论及所依证书仍可查）
curl -s "http://localhost:8080/api/v1/readings/M-1?at_version=2"
# 按设备 / 结论过滤读数
curl -s "http://localhost:8080/api/v1/readings?device_no=DEV-A&status=out_of_range"

# 证书当前版本
curl -s http://localhost:8080/api/v1/certificates/CERT-001

# 证书在指定版本视角下的内容
curl -s "http://localhost:8080/api/v1/certificates/CERT-001?at_version=1"

# 证书完整版本流水（不可变历史）
curl -s http://localhost:8080/api/v1/certificates/CERT-001/history

# 全局版本流水（每次成功变更一条）
curl -s "http://localhost:8080/api/v1/versions?limit=50"

# 按请求标识追溯原始请求与响应
curl -s http://localhost:8080/api/v1/requests/req-003

# 健康检查 / 当前版本
curl -s http://localhost:8080/api/v1/health
curl -s http://localhost:8080/api/v1/status
```

### 9. 审计取证追溯包

按**设备编号 + 带时区测量时间左闭右开区间 + 已提交版本**生成取证包（只读，不产生新版本）。包内只收录该版本**之前（含该版本）已提交**的读数，每条给出在该版本视角的结论 `status` 与**全部候选证书快照** `candidates`。

```bash
curl -s -G http://localhost:8080/api/v1/audit/packages \
  --data-urlencode 'device_no=DEV-A' \
  --data-urlencode 'measured_from=2026-01-01T00:00:00Z' \
  --data-urlencode 'measured_to=2026-07-01T00:00:00Z' \
  --data-urlencode 'at_version=5' \
  --data-urlencode 'limit=50' \
  --data-urlencode 'offset=0'
```

返回（节选）：

```json
{
  "package_id": "cf35d7dd...",
  "filter": {
    "device_no": "DEV-A",
    "measured_from": "2026-01-01T00:00:00.000000Z",
    "measured_to":   "2026-07-01T00:00:00.000000Z",
    "at_version": 5
  },
  "at_version": 5,
  "total_count": 3,
  "segment": {"limit": 50, "offset": 0, "count": 3, "has_more": false,
              "order": ["measured_at ASC", "reading_no ASC"]},
  "items": [
    {
      "reading_no": "M-1", "device_no": "DEV-A",
      "measured_at": "2026-03-01T10:00:00.000000Z", "value": 42.0,
      "submitted_at_version": 2,
      "status": "conforming",
      "reliance": { "certificate_no": "CERT-001", "version_seq": 1, "...": "..." },
      "candidates": [ { "certificate_no": "CERT-001", "version_seq": 1, "...": "..." } ]
    }
  ],
  "digest": {
    "algorithm": "sha256",
    "spec_version": "audit-v1",
    "package_digest": "29773bbf...",
    "package_digest_input": { "canonical_json": "{...规范化后的完整包...}" },
    "segment_digest": "ab12...",
    "segment_digest_input": { "canonical_json": "{...本分段...}" },
    "canonical_rules": { "...": "随包返回的摘要规范化规则，可据其独立复算" }
  }
}
```

要点：

- **无匹配读数返回空包**（`200`，`total_count=0`、`items=[]`，摘要照常给出）；不存在的设备同样返回空包而非 404。
- **版本越界**（`at_version<1` 或 `>当前版本`）、**无效时间区间**（含缺时区、起点 ≥ 终点）一律 `422`，不读取数据、不产生半成品。
- 大量条目用 `limit`（≤200）/`offset` 分段；固定按 `(measured_at ASC, reading_no ASC)` 排序，拼接无遗漏、无重复。各分段的 `package_id` 与 `package_digest` 完全一致（包摘要始终覆盖全部 `total_count` 条），另给 `segment_digest` 校验本分段。
- 包生成在单一只读快照事务内完成；生成期间发生的新提交、证书纠正或撤销对该次取证不可见，且因只收录 `submitted_at_version <= at_version` 的读数、结论按 `at_version` 视角评估，**相同条件 + 版本的内容与摘要在任何时刻重取都一致**。
- **人工裁决按版本呈现**：取证版本视角下生效的裁决，其 `status` 按所选证书给出、`reliance` 指向所选证书版本，`candidates` 保留裁决依据的候选闭集；裁决失效（所选版本被纠正 / 撤销）后，当前版本包回到自动结论。包条目字段集合与 **audit-v1 摘要规则保持不变**；裁决只改变既有 `status` / `reliance` / `candidates` 的取值，旧版本包的内容与摘要不因裁决的日后失效而改变。

独立复算摘要（任何语言均可）：

```bash
# package_digest = lower(hex(sha256(package_digest_input.canonical_json)))
python3 - <<'PY'
import hashlib, json, urllib.parse, urllib.request
q = urllib.parse.urlencode({
    "device_no": "DEV-A", "measured_from": "2026-01-01T00:00:00Z",
    "measured_to": "2026-07-01T00:00:00Z", "at_version": 5})
b = json.load(urllib.request.urlopen("http://localhost:8080/api/v1/audit/packages?" + q))
s = b["digest"]["package_digest_input"]["canonical_json"]
print("recomputed:", hashlib.sha256(s.encode("utf-8")).hexdigest())
print("matches:   ", hashlib.sha256(s.encode("utf-8")).hexdigest() == b["digest"]["package_digest"])
PY
```

---

## 六、HTTP 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/api/v1/certificates` | 签发证书 |
| POST | `/api/v1/certificates/{cert_no}/correct` | 纠正证书（追加新版本，旧版本失效） |
| POST | `/api/v1/certificates/{cert_no}/revoke` | 撤销证书 |
| GET  | `/api/v1/certificates` | 证书列表（`at_version` / `device_no` / 分页） |
| GET  | `/api/v1/certificates/{cert_no}` | 证书当前/指定版本视角内容 |
| GET  | `/api/v1/certificates/{cert_no}/history` | 证书版本历史 |
| POST | `/api/v1/readings` | 批量提交读数（读数不可变） |
| POST | `/api/v1/readings/{reading_no}/adjudicate` | 对 review 读数人工选证裁决（占用一个全局版本） |
| GET  | `/api/v1/readings` | 读数列表（`at_version` / `device_no` / `status` / 分页，按版本呈现裁决与失效状态） |
| GET  | `/api/v1/readings/{reading_no}` | 读数结论（默认当前版本，`at_version` 查历史） |
| GET  | `/api/v1/versions` | 全局单调版本流水 |
| GET  | `/api/v1/requests/{request_id}` | 按请求标识追溯 |
| GET  | `/api/v1/audit/packages` | 审计取证追溯包（设备 + 左闭右开时间区间 + 版本） |
| GET  | `/api/v1/health` | 健康检查 |

主要状态码：`200` 成功；`404` 未知目标（证书 / 读数 / 请求）；`409` 请求标识冲突、证书已存在 / 已撤销、读数已存在、裁决依据版本过期（`base_version_stale`）、读数非 review（`reading_not_in_review`）；`422` 参数校验失败（含无效时间区间、范围倒置、缺时区、所选版本非候选 `target_mismatch` 等）。

---

## 七、设计要点

- **单调整局版本**：`versions` 表自增主键，五类事件 `cert_issue / cert_correct / cert_revoke / reading_submit / reading_adjudicate` 全部追加，记录请求体、指纹与响应快照。
- **历史不可覆盖**：证书内容存为只追加的 `certificate_versions`，纠正 / 撤销仅在旧行登记 `superseded_at_version`；读数只追加；人工裁决存于只追加的 `reading_adjudications`，所选版本失效时仅登记 `invalidated_at_version`，裁决行本身不改不删。
- **版本视角查询**：证书在版本 V 有效 ⇔ 其内容版本 `created_at_version <= V` 且（`superseded_at_version` 为空或 `> V`），且测量时刻落在 `[valid_from, valid_to)`。
- **人工裁决口径**：读数在版本 V 的呈现结论 = 自动结论（候选证书匹配）叠加最近一条 `decided_at_version <= V` 的裁决：裁决所选版本在 V 仍有效则按所选证书闭区间呈现 `conforming` / `out_of_range`（候选保留裁决时的候选闭集），已失效或尚无裁决则呈现自动结论并把裁决标为 `invalidated`（含 `invalidated_at_version` / `invalidated_by_event`）。裁决依据按其 `base_version` 视角冻结，纠正 / 撤销不会改写历史版本所见。
- **事务与并发**：所有写操作在单条 `BEGIN IMMEDIATE` 事务内完成「幂等判定 → 业务校验 → 落库」，任何错误整体回滚；裁决先在写锁内做幂等判定，再校验 `base_version==当前版本` 与候选资格，拒绝不占版本；WAL 模式支持读写并发。
- **幂等指纹**：对归一化（UTC 化）后的请求体做排序 JSON 指纹；相同 `request_id` + 指纹返回首次响应，`request_id` 复用且指纹不同则拒绝。
- **审计取证包**：`GET /audit/packages` 在单一只读快照事务（WAL `BEGIN` 读快照）内完成筛选与评估，事务开始后的提交一律不可见；逻辑上再叠加 `submitted_at_version <= at_version` 与测量时间 `[from, to)` 双重过滤。候选证书快照做版本视角投影——`superseded_at_version` 晚于取证版本时投影为 `null`，使历史版本包不携带"未来"事件。包摘要 `sha256(canonical_json(...))` 覆盖固定键集合下的全部条目（与分页无关），`package_id` 为筛选条件 + 版本规范化哈希的前 32 位；规范化规则（键排序、无空白、UTC 定长时间、binary64 最短往返数字、条目排序）随包返回，支持独立复算。

---

## 八、测试

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

测试通过 TestClient 覆盖：四种结论、左闭右开边界、纠正 / 撤销的结论联动、旧版本可追溯、幂等重放、标识冲突、无效区间、未知目标、批次部分变更防护、多时区归一化，以及人工选证裁决的成功裁决与依据返回、依据版本过期 / 目标不符拒绝且不占版本、幂等重放与冲突、纠正 / 撤销导致裁决失效并回退自动结论、旧版本原裁决可查、读数查询 / 列表按版本呈现、取证包按版本呈现裁决且旧版本摘要恒定。
