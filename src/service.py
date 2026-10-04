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
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def claim(self, item_id, actor, role, region=None, expected_version=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.ACTION_ROLES.get("claim", set()):
            raise DomainError("forbidden", "当前角色不能领取主办", 403)
        self.repository.get_item(item_id)
        return self.repository.claim_follow_up(item_id, actor, role, expected_version)

    def sync_sources(self, item_id, payload, actor, role, region=None):
        """断网补测回网后按来源合并，逐条独立提交，保留已确认部分并重试未完成项。"""
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        self.repository.get_item(item_id)
        measurements = domain.normalize_sync_sources(payload)
        confirmed = []
        pending = []
        for measurement in measurements:
            index = measurement.pop("_index")
            try:
                source_region = measurement.get("region")
                if region and rules.ENFORCE_REGION and role != "regulator" and source_region and source_region != region:
                    raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
                result = self.repository.upsert_source(
                    item_id,
                    measurement.pop("source_type"),
                    measurement.pop("external_id"),
                    measurement,
                    measurement.pop("observed_at"),
                    actor,
                    role,
                )
                result["index"] = index
                confirmed.append(result)
            except DomainError as exc:
                pending.append({
                    "index": index,
                    "measurement": measurement,
                    "error": exc.code,
                    "message": str(exc),
                })
        return {"confirmed": confirmed, "pending": pending}

    def backfill_follow_up(self, actor="system", role="coordinator"):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role != "coordinator":
            raise DomainError("forbidden", "只有协调员可以执行迁移补录", 403)
        return self.repository.backfill_follow_up(actor, role)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
