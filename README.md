# 封签链校验 API（Seal Chain Validation）

用于展品运输交接场景的封签链校验服务。客户端提交按时间排列的 **封装 / 交接 / 开封 / 重封** 事件序列，服务按状态机逐事件校验，返回最终保管人、当前封签、逐事件状态及全部违规事件的位置与原因；同时提供**批量封签占用检查**接口，对多条事件链做跨链 `seal_id` 占用冲突检测；提供**离线片段合并重建**接口，将同一 `chain_id` 的多个离线片段按 `event_id` 去重、按 `prev_event_id` 重建唯一序列后再校验；并提供**开封暴露审计**接口，审计各段开封区间落在授权时间窗口之外的未授权暴露时长。

## 校验规则（状态机）

| # | 规则 |
|---|------|
| 1 | 未封装状态下不得交接（handover）或开封（unseal） |
| 2 | 已封装状态下不得重复封装 / 重封，须先开封 |
| 3 | 开封后必须使用**新的、未使用过的**封签重封（reseal） |
| 4 | 已使用过的封签不得复用 |
| 5 | 事件时间戳必须严格递增 |

**违规事件不改变状态，也不阻止后续事件的判定**（时间递增性始终与最近一个*有效*事件比较）。

## 快速启动

单条命令（依赖随镜像构建完成，启动即就绪）：

```bash
docker compose up --build
```

- API 地址：`http://localhost:8000`
- 交互式文档（Swagger UI）：`http://localhost:8000/docs`
- 健康检查：`GET http://localhost:8000/health`

### 配置

| 环境变量 | 默认值 | 说明 |
|----------|--------|------|
| `HOST_PORT` | `8000` | 宿主机映射端口，如 `HOST_PORT=9000 docker compose up --build` |

容器内服务固定监听 `0.0.0.0:8000`。

## API 说明

### `POST /api/v1/seal-chain/validate`

**请求体**

```json
{
  "events": [
    {"type": "seal",     "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
    {"type": "unseal",   "timestamp": "2026-09-18T12:00:00Z"},
    {"type": "reseal",   "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Bob"}
  ]
}
```

- `type`：`seal`（封装）/ `handover`（交接）/ `unseal`（开封）/ `reseal`（重封）
- `timestamp`：ISO 8601 时间，需严格递增
- `seal_id`：`seal` / `reseal` 必填，且不得与历史封签重复
- `custodian`：`seal` / `reseal` / `handover` 必填（交接时为接收方）

**响应示例**（上例中若在第 2、3 步之间插入一次未封装状态下的交接）：

```json
{
  "valid": false,
  "sealed": true,
  "final_custodian": "Bob",
  "current_seal": "S002",
  "events": [
    {"index": 0, "type": "seal", "status": "ok", "reason": null},
    {"index": 1, "type": "handover", "status": "violation", "reason": "未封装状态下不得交接"}
  ],
  "violations": [
    {"index": 1, "reason": "未封装状态下不得交接"}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `valid` | 整条链是否无违规 |
| `sealed` | 处理完所有有效事件后是否处于封装状态 |
| `final_custodian` | 最终保管人 |
| `current_seal` | 当前有效封签，未封装时为 `null` |
| `events[]` | 逐事件状态（`ok` / `violation` + 原因）；`cross_chain_conflict` 在单链接口恒为 `false` |
| `violations[]` | 所有违规事件的位置（`index`，从 0 开始）与原因 |

> 事件结构不合法（如未知 `type`、缺少 `timestamp`）返回 `422`；语义违规（状态机层面）则在响应的 `violations` 中逐条列出。

### 示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/validate \
  -H 'Content-Type: application/json' \
  -d '{"events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
      ]}'
```

### `POST /api/v1/seal-chain/batch-validate`

批量封签占用检查。请求包含多条带 `chain_id` 的事件链，服务处理分两步：

1. **分别按上述状态机规则校验每条链**，得到各链原校验结果；
2. **按输入顺序建立全批次封签占用表**：同一 `seal_id` 被**不同链**的**有效** seal/reseal 事件（单链校验中 `status = ok`）使用时，两条链对应的事件均标记 `cross_chain_conflict: true`。

冲突不改变各链自身状态（链的 `valid` / `sealed` / `violations` 等与单链接口一致），但只要存在冲突，批次的 `valid` 必为 `false`。同一条链内复用封签属于状态机违规（规则 3/4），不计入跨链冲突；单链校验中不合法的事件不构成封签占用。

**请求体**

```json
{
  "chains": [
    {
      "chain_id": "chain-a",
      "events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
      ]
    },
    {
      "chain_id": "chain-b",
      "events": [
        {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Carol"},
        {"type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Dave"}
      ]
    }
  ]
}
```

- `chains[]`：按批次输入顺序排列；`chain_id` 在批次内不可重复（重复返回 `422`），允许空批次。

**响应示例**（对应上例）

```json
{
  "valid": false,
  "chains": [
    {
      "chain_id": "chain-a",
      "valid": true,
      "sealed": true,
      "final_custodian": "Alice",
      "current_seal": "S001",
      "events": [
        {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": true}
      ],
      "violations": []
    },
    {
      "chain_id": "chain-b",
      "valid": true,
      "sealed": true,
      "final_custodian": "Dave",
      "current_seal": "S001",
      "events": [
        {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": true},
        {"index": 1, "type": "handover", "status": "ok", "reason": null, "cross_chain_conflict": false}
      ],
      "violations": []
    }
  ],
  "conflicts": [
    {"seal_id": "S001", "chain_id": "chain-a", "event_index": 0, "first_chain_id": "chain-a"},
    {"seal_id": "S001", "chain_id": "chain-b", "event_index": 0, "first_chain_id": "chain-a"}
  ],
  "summary": [
    {"chain_id": "chain-a", "valid": true, "conflicts": 1},
    {"chain_id": "chain-b", "valid": true, "conflicts": 1}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `valid` | 批次是否通过：各链自身均无违规**且**不存在任何跨链冲突 |
| `chains[]` | 每链的原校验结果（结构同单链接口，附 `chain_id`），按**输入顺序**返回；冲突事件的 `events[].cross_chain_conflict` 为 `true` |
| `conflicts[]` | 冲突项，每个被标记的事件一条：`seal_id`、所属 `chain_id`、链内 `event_index`（从 0 开始）、按输入顺序首次占用该封签的 `first_chain_id`；首次占用事件自身的锚点项 `chain_id` 与 `first_chain_id` 相同 |
| `summary[]` | 按 `chain_id` 字典序稳定排序的汇总：每链自身 `valid` 与冲突事件数 `conflicts` |

### 批量接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/batch-validate \
  -H 'Content-Type: application/json' \
  -d '{"chains": [
        {"chain_id": "chain-a", "events": [
          {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
        ]},
        {"chain_id": "chain-b", "events": [
          {"type": "seal", "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Carol"}
        ]}
      ]}'
```

### `POST /api/v1/seal-chain/reconcile-validate`

离线片段合并重建。同一条链（同一 `chain_id`）在离线状态下被拆成多个片段分别记录，本接口将其合并为唯一序列后再校验。处理分四步：

1. **按 `event_id` 去重**：同一 `event_id` 的多个拷贝业务内容（`type` / `timestamp` / `seal_id` / `custodian` / `prev_event_id`）完全相同时仅保留一份；内容存在分歧时记入 `conflicts` 冲突明细，冲突事件不参与后续重建。
2. **按 `prev_event_id` 重建唯一序列**：仅基于无冲突事件，显式检测**多头**（`prev_event_id` 为 `null` 的链头必须恰好一个）、**缺失前驱**、**分叉**（多个事件共用同一前驱）、**环**；**绝不按时间戳猜测顺序**。
3. **仅重建成功时**（无冲突、恰好一个链头、无缺失前驱、无分叉、无环）按重建序列调用现有单链校验器，时间戳严格递增等规则仍由该校验器判定；否则 `sequence` 与 `validation` 为 `null`。
4. 无论是否重建成功，都返回每个 `event_id` 的**来源片段编号列表**。

**请求体**

```json
{
  "chain_id": "chain-a",
  "fragments": [
    {
      "events": [
        {"event_id": "e1", "prev_event_id": null, "type": "seal",     "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"event_id": "e2", "prev_event_id": "e1", "type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
      ]
    },
    {
      "events": [
        {"event_id": "e2", "prev_event_id": "e1", "type": "handover", "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"},
        {"event_id": "e3", "prev_event_id": "e2", "type": "unseal",   "timestamp": "2026-09-18T12:00:00Z"}
      ]
    }
  ]
}
```

- `chain_id`：链标识，所有片段必须属于同一条链
- `fragments[]`：离线片段，**片段编号即其在请求中的下标（从 0 开始）**
- 事件字段在单链事件基础上增加：`event_id`（必填，判重依据）、`prev_event_id`（前驱事件标识，`null` 表示链头）

**响应示例**（对应上例，两个片段的 `e2` 完全一致，被合并）

```json
{
  "chain_id": "chain-a",
  "reconciled": true,
  "conflicts": [],
  "structure": {"heads": ["e1"], "missing_predecessors": [], "forks": [], "cycles": []},
  "sequence": ["e1", "e2", "e3"],
  "validation": {
    "valid": true,
    "sealed": false,
    "final_custodian": "Bob",
    "current_seal": null,
    "events": [
      {"index": 0, "type": "seal", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 1, "type": "handover", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 2, "type": "unseal", "status": "ok", "reason": null, "cross_chain_conflict": false}
    ],
    "violations": []
  },
  "event_sources": [
    {"event_id": "e1", "fragments": [0]},
    {"event_id": "e2", "fragments": [0, 1]},
    {"event_id": "e3", "fragments": [1]}
  ]
}
```

| 字段 | 说明 |
|------|------|
| `reconciled` | 是否成功重建唯一序列：无内容冲突、恰好一个链头、无缺失前驱、无分叉、无环 |
| `conflicts[]` | 同一 `event_id` 内容分歧的明细：`differing_fields` 列出分歧字段，`variants[]` 给出每种内容变体及其来源片段编号 |
| `structure.heads[]` | `prev_event_id` 为 `null` 的链头事件，必须恰好一个才能重建 |
| `structure.missing_predecessors[]` | 前驱不在无冲突事件集中的事件（前驱未出现，或因冲突被排除） |
| `structure.forks[]` | 分叉：`prev_event_id` 及其全部后继事件 |
| `structure.cycles[]` | 环：环内每个事件的 `prev_event_id` 依次指向列表下一事件，末尾指回开头 |
| `sequence` | 重建成功时的 `event_id` 序列，否则为 `null` |
| `validation` | 仅重建成功时调用现有单链校验器的结果（结构同单链接口，`index` 对应 `sequence` 中的位置），否则为 `null` |
| `event_sources[]` | 每个 `event_id` 的来源片段编号列表（升序去重），按首次出现顺序排列；冲突事件也包含在内 |

> 结构诊断（`structure`）只基于无冲突事件；时间戳只交给单链校验器按规则 5 判定，重建阶段不按时间戳排序。事件结构不合法（如缺少 `event_id`）返回 `422`。

### 合并重建接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/reconcile-validate \
  -H 'Content-Type: application/json' \
  -d '{"chain_id": "chain-a", "fragments": [
        {"events": [
          {"event_id": "e1", "prev_event_id": null, "type": "seal",
           "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"}
        ]},
        {"events": [
          {"event_id": "e2", "prev_event_id": "e1", "type": "handover",
           "timestamp": "2026-09-18T11:00:00Z", "custodian": "Bob"}
        ]}
      ]}'
```

### `POST /api/v1/seal-chain/exposure-audit`

开封暴露审计。服务先复用现有单链校验器得到完整校验结果，再仅取其中 `status = ok` 的 **unseal / reseal** 事件推导半开开封区间 `[start, end)`：

- 每个有效 unseal 开启一段开封，其后第一个有效 reseal 关闭该段；违规事件不参与推导；
- **末次开封未重封**时，区间终点延伸到 `audit_until`；
- 先把 `allowed_windows` 中**相交或首尾相接**（`end == start`）的窗口合并为有序、互不相交的窗口（输入乱序 / 嵌套时结果稳定）；
- 再对每段开封区间做**区间差**：落在合并后窗口内的分段记为 `covered`，其余记为 `uncovered`，并累计未授权总秒数；一段开封跨越多个窗口时按窗口逐段切分。

每个分段都带有所属开封区间的 `unseal_index` / `reseal_index`（请求 `events` 中的下标，从 0 开始；末次未重封时 `reseal_index` 为 `null`）。

**请求体**

```json
{
  "events": [
    {"type": "seal",   "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
    {"type": "unseal", "timestamp": "2026-09-18T12:00:00Z"},
    {"type": "reseal", "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Alice"},
    {"type": "unseal", "timestamp": "2026-09-18T16:00:00Z"}
  ],
  "audit_until": "2026-09-18T18:00:00Z",
  "allowed_windows": [
    {"start": "2026-09-18T12:10:00Z", "end": "2026-09-18T13:20:00Z"},
    {"start": "2026-09-18T17:00:00Z", "end": "2026-09-18T17:30:00Z"}
  ]
}
```

- `events`：与单链校验接口完全相同的事件模型。
- `audit_until`：审计截止时间；末次开封未重封时开封区间延伸到该时刻。**早于最后一个*有效*事件的时间戳返回 `422`**（违规事件不参与比较）。
- `allowed_windows[]`：经授权的开封允许窗口（半开 `[start, end)`）；`start` 必须严格早于 `end`（否则返回 `422`），可乱序、相交、嵌套或首尾相接，服务会先合并。
- 所有时间戳必须同为带时区或同为不带时区（混用返回 `422`）。

**响应示例**（对应上例：第一段开封 12:00–13:00，第二段 16:00–18:00 未重封）

```json
{
  "validation": {
    "valid": true,
    "sealed": false,
    "final_custodian": "Alice",
    "current_seal": null,
    "events": [
      {"index": 0, "type": "seal",   "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 1, "type": "unseal", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 2, "type": "reseal", "status": "ok", "reason": null, "cross_chain_conflict": false},
      {"index": 3, "type": "unseal", "status": "ok", "reason": null, "cross_chain_conflict": false}
    ],
    "violations": []
  },
  "audit_until": "2026-09-18T18:00:00Z",
  "exposure_intervals": [
    {"start": "2026-09-18T12:00:00Z", "end": "2026-09-18T13:00:00Z", "unseal_index": 1, "reseal_index": 2},
    {"start": "2026-09-18T16:00:00Z", "end": "2026-09-18T18:00:00Z", "unseal_index": 3, "reseal_index": null}
  ],
  "merged_windows": [
    {"start": "2026-09-18T12:10:00Z", "end": "2026-09-18T13:20:00Z"},
    {"start": "2026-09-18T17:00:00Z", "end": "2026-09-18T17:30:00Z"}
  ],
  "covered": [
    {"start": "2026-09-18T12:10:00Z", "end": "2026-09-18T13:00:00Z", "seconds": 3000,
     "unseal_index": 1, "reseal_index": 2, "window_index": 0},
    {"start": "2026-09-18T17:00:00Z", "end": "2026-09-18T17:30:00Z", "seconds": 1800,
     "unseal_index": 3, "reseal_index": null, "window_index": 1}
  ],
  "uncovered": [
    {"start": "2026-09-18T12:00:00Z", "end": "2026-09-18T12:10:00Z", "seconds": 600,
     "unseal_index": 1, "reseal_index": 2, "window_index": null},
    {"start": "2026-09-18T16:00:00Z", "end": "2026-09-18T17:00:00Z", "seconds": 3600,
     "unseal_index": 3, "reseal_index": null, "window_index": null},
    {"start": "2026-09-18T17:30:00Z", "end": "2026-09-18T18:00:00Z", "seconds": 1800,
     "unseal_index": 3, "reseal_index": null, "window_index": null}
  ],
  "uncovered_seconds": 6000
}
```

| 字段 | 说明 |
|------|------|
| `validation` | 现有单链校验器的完整结果，结构与 `/validate` 完全一致（含 `cross_chain_conflict`，本接口恒为 `false`） |
| `audit_until` | 回显审计截止时间 |
| `exposure_intervals[]` | 仅由有效 unseal/reseal 推导出的半开开封区间，按时间升序；`reseal_index` 为 `null` 表示末次未重封、终点取 `audit_until` |
| `merged_windows[]` | 合并相交 / 首尾相接窗口后的授权窗口，按 `start` 升序、互不相交 |
| `covered[]` | 落在授权窗口内的分段（窗口伸出开封区间的部分会被裁掉），按时间升序；`window_index` 指向 `merged_windows` 下标 |
| `uncovered[]` | 未授权开封分段，按时间升序；`window_index` 恒为 `null` |
| `uncovered_seconds` | 未授权开封总秒数（各 `uncovered` 分段秒数之和，非整数秒向下取整） |

> 窗口与开封区间均为半开区间：`[start, end)`，窗口终点恰为开封终点时该点不计暴露。无有效开封区间（或无事件）时各分段列表为空、`uncovered_seconds` 为 `0`。

### 开封暴露审计接口示例调用

```bash
curl -s http://localhost:8000/api/v1/seal-chain/exposure-audit \
  -H 'Content-Type: application/json' \
  -d '{"events": [
        {"type": "seal",   "timestamp": "2026-09-18T10:00:00Z", "seal_id": "S001", "custodian": "Alice"},
        {"type": "unseal", "timestamp": "2026-09-18T12:00:00Z"},
        {"type": "reseal", "timestamp": "2026-09-18T13:00:00Z", "seal_id": "S002", "custodian": "Alice"}
      ],
      "audit_until": "2026-09-18T14:00:00Z",
      "allowed_windows": [
        {"start": "2026-09-18T12:00:00Z", "end": "2026-09-18T12:30:00Z"}
      ]}'
```

## 本地开发与测试

```bash
pip install -r requirements-dev.txt
pytest -v
uvicorn app.main:app --reload --port 8000
```

## 项目结构

```
app/
  main.py        # FastAPI 入口与路由
  models.py      # 请求 / 响应模型（单链、批量、合并重建与开封暴露审计）
  validator.py   # 单链状态机校验核心
  batch.py       # 批量封签占用检查（跨链冲突）
  reconcile.py   # 离线片段合并重建（去重 / 结构诊断 / 调用单链校验）
  exposure.py    # 开封暴露审计（窗口合并 / 区间差 / 未授权时长）
tests/
  test_validator.py
  test_batch.py
  test_reconcile.py
  test_exposure.py
Dockerfile
docker-compose.yml
requirements.txt
```
