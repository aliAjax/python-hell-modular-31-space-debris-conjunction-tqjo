from . import domain, rules
from .domain import DomainError


class Service:
    def __init__(self, repository):
        self.repository = repository

    def create_item(self, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.CREATE_ROLES:
            raise DomainError("forbidden", "当前角色不能创建此类业务记录", 403)
        normalized = domain.normalize_create(payload)
        stable_key = normalized.pop("_stable_key")
        return self.repository.create_item(
            rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
        )

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        item = self.repository.get_item(item_id)
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        result = self.repository.add_source(
            item_id,
            normalized.pop("source_type"),
            normalized.pop("external_id"),
            normalized,
            normalized.pop("observed_at"),
            actor,
            role,
        )
        return result

    def act(self, item_id, action, payload, actor, role, expected_version=None, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        item = self.repository.get_item(item_id)
        allowed = rules.ACTION_ROLES.get(action, set())
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能执行该操作", 403)
        if rules.ENFORCE_REGION and action in rules.REGION_SENSITIVE_ACTIONS and region and role != "regulator":
            if item["payload"].get("region") != region:
                raise DomainError("region_mismatch", "不能处理其他区域的记录", 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)

        if action == "execute":
            command = domain.normalize_command(payload)
            self.repository.issue_command(
                item_id, command["command_ref"], command["instruction"], actor, role, expected_version
            )
            return self.get_item(item_id)
        if action == "reissue_command":
            command = domain.normalize_command(payload)
            self.repository.reissue_command(
                item_id, command["command_ref"], command["instruction"], actor, role, expected_version
            )
            return self.get_item(item_id)
        if action == "cancel_command":
            reason = payload.get("reason")
            reason = str(reason).strip() if reason is not None else None
            if not reason:
                raise DomainError("field_required", "reason 不能为空")
            self.repository.cancel_command(item_id, actor, role, expected_version, reason)
            return self.get_item(item_id)
        if action == "report_revision":
            return self._report_revision(item_id, payload, actor, role, expected_version)

        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def _report_revision(self, item_id, payload, actor, role, expected_version):
        revision = rules.build_revision(payload)
        self.repository.apply_revision(
            item_id, revision, actor, role, expected_version,
            lambda current: rules.assess(current),
        )
        return self.get_item(item_id)

    def ingest_receipt(self, item_id, payload, actor, role):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        allowed = rules.ACTION_ROLES["ingest_receipt"]
        if role not in allowed:
            raise DomainError("forbidden", "当前角色不能登记外部回执", 403)
        receipt = domain.normalize_receipt(payload)
        return self.repository.ingest_receipt(item_id, receipt, actor, role)

    def reconciliation(self):
        return self.repository.reconciliation_summary()

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["commands"] = self.repository.list_commands(item_id)
        item["receipts"] = self.repository.list_receipts(item_id)
        item["open_command"] = next(
            (command for command in item["commands"] if command["status"] == rules.COMMAND_PENDING),
            None,
        )
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
