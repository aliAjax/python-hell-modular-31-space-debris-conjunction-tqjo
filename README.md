# 太空碎片接近预警与规避协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8331`。

## 模块

- `app.py`：参数解析、依赖组装和 HTTP 生命周期。
- `src/domain.py`：领域类型、校验和错误定义。
- `src/rules.py`：风险评估、意见冲突和状态机。
- `src/repository.py`：SQLite、事务、乐观版本、审计链、规避指令与外部回执对账。
- `src/service.py`：身份、权限、用例编排。
- `src/http_api.py`：JSON API 和静态首页。
- `src/audit.py`：哈希审计事件。

## 运行

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8331
```

服务提供 `GET /health`、`GET /api/state`、`GET /api/items`、`POST /api/items`、`POST /api/items/<id>/sources` 和 `POST /api/items/<id>/actions`。身份使用 `X-User-Id`、`X-Role` 请求头。

动作包括 `assess`、`record_opinion`、`approve`、`initiate_command`、`record_receipt`、`resolve`、`cancel`、`report_revision`。其中 `approve`、`initiate_command`、`record_receipt`、`resolve`、`cancel` 需要携带 `expected_version`。

## 规避指令与外部回执对账

指令下发后网络可能断网，回执会重复或晚到，值班员需要一份能对账的记录：把接近事件、规避指令和外部回执串起来，明确每条指令是否已执行。

- **待确认**：`initiate_command` 把指令记为 `pending_confirm`，此时尚未收到外部回执。
- **执行完成**：`record_receipt` 登记带协调编号（`coordination_number`）的回执后，指令转为 `executed`。
- **重复只认一次**：同一协调编号重复到达时，回执幂等处理，状态不变，仅追加一条 `duplicate_receipt` 审计事件。
- **重新发起**：确认前操作员可以重新发起；同一接近事件同时最多挂一条未确认指令，旧的待确认指令置为 `superseded`。已执行完成的指令不能重新发起。
- **风险变动作废**：待确认期间若 `report_revision` 改动了风险等级，指令作废（`voided`，原因 `risk_changed`），事件退回评估（`assessed`），审计事件全部保留。
- **崩溃恢复**：指令与回执都落盘到 SQLite，服务重启后待确认指令、已执行状态和去重编号都能恢复，可继续对账。
- **乐观并发**：两名值班员同时提交发起/撤销时，先到的一方生效，另一方收到 `version_conflict` 后重新读取最新版本再重试。

对账视图：`GET /api/items/<id>/reconciliation` 返回该事件的指令、回执与待确认/已执行/作废清单；`GET /api/reconciliation` 返回全局视图，`outstanding` 列出所有仍挂着未确认指令的事件，供值班员追回执。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖评估、批准、指令发起与回执对账、重复回执幂等、重新发起、风险变动作废、崩溃恢复、乐观并发、解决、重复告警、权限、版本冲突、过期轨道和运营方意见冲突。数据使用 SQLite 持久化；规则是可运行的演示模型，不替代真实轨道力学、碰撞概率和空间交通协调服务。
