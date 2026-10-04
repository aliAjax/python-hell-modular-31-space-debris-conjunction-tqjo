# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本和审计链。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions`、`POST /api/items/<id>/receipts` 和 `GET /api/reconciliation`。身份使用 `X-User-Id`、`X-Role` 请求头。

## 规避指令对账

接近事件进入 `coordinating` 后，规避指令通过动作接口发起，`avoidance_commands` 与 `command_receipts` 两张表构成可对账记录：

- `execute`（payload 带 `command_ref`）：指令发出即记为 `pending`（待确认），事件进入 `executing`。
- `POST /api/items/<id>/receipts`（`command_ref` + `coordination_ref`）：带协调编号的首次回执把指令置为 `confirmed`，才算执行完成；同一协调编号重复到达只记一次（`disposition=duplicate`），旧版本指令的晚到回执记为 `late`，不改变当前状态；查无指令的回执记为 `unmatched`。
- `reissue_command`：确认前操作员可重新发起，旧指令置为 `superseded`，新指令成为唯一待确认记录。同一接近事件同时只能挂一条 `pending` 指令（部分唯一索引 `idx_command_single_open` 保证，重复发起返回 `pending_command_exists`）。
- `cancel_command`：撤销待确认指令，事件退回 `coordinating`。发起与撤销并发时，乐观版本让先到者生效，后来者收到 `version_conflict`，重新读取最新版本后再试。
- `report_revision`：待确认期间观测修订若改变风险等级，当前指令置为 `voided`、事件退回 `assessed` 重新评估；审计事件（`command_issued`/`command_superseded`/`command_cancelled`/`command_voided`/`command_confirmed`）全部追加保留。
- 所有状态只存于 SQLite（WAL，事务内写入），进程崩溃重启后对账状态可直接恢复；`GET /api/reconciliation` 汇总各状态指令数、重复/未匹配回执。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、执行、解决、重复告警、权限、版本冲突、过期轨道、运营方意见冲突，以及指令待确认/回执确认、重复回执幂等、单条未确认指令、重新发起取代、修订作废、并发先到先得和崩溃恢复对账。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
