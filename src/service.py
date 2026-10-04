from . import domain, rules
from .domain import ConflictError, DomainError


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
        try:
            return self.repository.create_item(
                rules.ENTITY_TYPE, stable_key, rules.INITIAL_STATUS, normalized, actor, role
            )
        except ConflictError:
            existing = self.repository.find_by_stable_key(rules.ENTITY_TYPE, stable_key)
            if existing is None:
                raise
            raise ConflictError(
                "duplicate_item",
                "同一干扰事件已存在（#%s），请基于该记录补充测量来源" % existing["id"],
                details={
                    "item_id": existing["id"],
                    "status": existing["status"],
                    "region": existing["payload"].get("region"),
                    "owner": existing["payload"].get("owner"),
                    "owner_since": existing["payload"].get("owner_since"),
                },
            )

    def _prepare_source(self, payload, role, region):
        normalized = domain.normalize_source(payload)
        if region and rules.ENFORCE_REGION and role != "regulator" and normalized.get("region") and normalized["region"] != region:
            raise DomainError("region_mismatch", "来源记录不属于当前管辖区域", 403)
        return normalized

    @staticmethod
    def _source_body(normalized):
        return {k: v for k, v in normalized.items() if k not in ("source_type", "external_id", "observed_at")}

    def add_source(self, item_id, payload, actor, role, region=None):
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        self.repository.get_item(item_id)
        normalized = self._prepare_source(payload, role, region)
        return self.repository.add_source(
            item_id,
            normalized["source_type"],
            normalized["external_id"],
            self._source_body(normalized),
            normalized["observed_at"],
            actor,
            role,
        )

    def add_sources_batch(self, item_id, sources, actor, role, region=None):
        # 断网期间的补测回网后批量合并：每条独立写入，失败不影响已确认部分，可整体重试
        if not actor or not role:
            raise DomainError("identity_required", "需要用户身份和角色", 401)
        if role not in rules.SOURCE_ROLES:
            raise DomainError("forbidden", "当前角色不能提交来源记录", 403)
        self.repository.get_item(item_id)
        results = []
        created = duplicates = failed = 0
        for index, entry in enumerate(sources):
            external_id = entry.get("external_id") if isinstance(entry, dict) else None
            try:
                if not isinstance(entry, dict):
                    raise DomainError("invalid_source", "来源记录必须是对象")
                normalized = self._prepare_source(entry, role, region)
                outcome = self.repository.merge_source(
                    item_id,
                    normalized["source_type"],
                    normalized["external_id"],
                    self._source_body(normalized),
                    normalized["observed_at"],
                    actor,
                    role,
                )
                if outcome["created"]:
                    created += 1
                    status = "created"
                else:
                    duplicates += 1
                    status = "duplicate"
                results.append({
                    "index": index,
                    "external_id": normalized["external_id"],
                    "status": status,
                    "source_id": outcome["id"],
                })
            except DomainError as exc:
                failed += 1
                results.append({
                    "index": index,
                    "external_id": external_id,
                    "status": "error",
                    "error": exc.code,
                    "message": str(exc),
                })
        return {
            "item_id": item_id,
            "created": created,
            "duplicates": duplicates,
            "failed": failed,
            "results": results,
        }

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
        if action in rules.OWNER_GATED_ACTIONS and role != "regulator":
            owner = item["payload"].get("owner")
            if owner and actor != owner:
                raise DomainError("not_owner", "该事件跟进人为 %s，请先领取主办" % owner, 403)
        if action in rules.ACTION_REQUIRES_VERSION and expected_version is None:
            raise DomainError("expected_version_required", "该操作需要 expected_version", 400)
        new_status, new_payload, event_payload = rules.apply_action(item, action, payload, actor, role)
        self.repository.apply_action(
            item_id, action, actor, role, new_status, new_payload, event_payload, expected_version
        )
        return self.get_item(item_id)

    def get_item(self, item_id):
        item = self.repository.get_item(item_id)
        item["sources"] = self.repository.list_sources(item_id)
        item["audit"] = self.repository.audit_trail(item_id)
        item["assessment"] = rules.assess(item["payload"])
        item["owner"] = item["payload"].get("owner")
        return item

    def list_items(self, status=None):
        return self.repository.list_items(status)

    def state(self):
        return self.repository.state_summary()
