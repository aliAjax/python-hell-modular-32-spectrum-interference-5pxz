import math
from datetime import datetime, timezone

from .domain import DomainError

ENTITY_TYPE = "spectrum_interference"
INITIAL_STATUS = "pending"
CREATE_ROLES = {"analyst", "monitor"}
SOURCE_ROLES = {"analyst", "monitor", "field_operator"}
ACTION_ROLES = {
    "assess": {"analyst", "monitor"},
    "locate": {"field_operator", "analyst"},
    "suspend": {"coordinator", "regulator"},
    "confirm_suspend": {"coordinator", "regulator"},
    "coordinate": {"coordinator"},
    "claim": {"coordinator", "regulator"},
    "resolve": {"coordinator", "regulator"},
    "correct_measurement": {"analyst", "monitor"},
    "cancel": {"coordinator"},
}
ENFORCE_REGION = True
REGION_SENSITIVE_ACTIONS = {"suspend", "confirm_suspend", "coordinate", "claim", "resolve", "cancel"}
ACTION_REQUIRES_VERSION = {"suspend", "confirm_suspend", "coordinate", "claim", "resolve", "cancel"}
# 已有主办人时，处置类动作只能由跟进人（或监管角色）执行，保证唯一跟进人
OWNER_GATED_ACTIONS = {"suspend", "confirm_suspend", "coordinate", "resolve", "cancel"}


def assess(payload):
    strength = float(payload.get("strength_dbm", -120))
    bandwidth = max(float(payload.get("bandwidth_mhz", 0.1)), 0.001)
    impact = strength + 10.0 * math.log10(bandwidth * 1000.0)
    if impact >= -37:
        level = "critical"
    elif impact >= -50:
        level = "high"
    elif impact >= -65:
        level = "medium"
    else:
        level = "low"
    score = round(max(0.0, min(100.0, 100.0 + impact)), 2)
    return {"score": score, "level": level, "impact_value": round(impact, 2)}


def _need_status(item, allowed):
    if item["status"] not in allowed:
        raise DomainError("invalid_state", "当前状态 %s 不允许执行该操作" % item["status"])


def _need_authorization_fresh(payload):
    if payload.get("suspend_authorization_status") == "pending_reconfirm":
        raise DomainError("authorization_stale", "测量已更正，停用授权需重新确认后才能继续处置", 409)


def _text(payload, name):
    value = payload.get(name)
    if not isinstance(value, str) or not value.strip():
        raise DomainError("field_required", "%s 不能为空" % name)
    return value.strip()


def apply_action(item, action, payload, actor, role):
    status = item["status"]
    current = dict(item["payload"])

    if action == "claim":
        _need_status(item, {"pending", "assessed", "located", "suspended", "coordinating"})
        previous_owner = current.get("owner")
        current["owner"] = actor
        current["owner_since"] = datetime.now(timezone.utc).isoformat()
        current.pop("owner_backfilled", None)
        return status, current, {"owner": actor, "previous_owner": previous_owner}

    if action == "assess":
        _need_status(item, {"pending", "assessed"})
        current["assessment"] = assess(current)
        return "assessed", current, {"assessment": current["assessment"]}

    if action == "correct_measurement":
        _need_status(item, {"pending", "assessed", "located", "suspended", "coordinating"})
        try:
            strength = float(payload["strength_dbm"])
        except (KeyError, TypeError, ValueError):
            raise DomainError("field_required", "strength_dbm 不能为空")
        revision = {
            "old_strength_dbm": current.get("strength_dbm"),
            "new_strength_dbm": strength,
            "old_assessment": current.get("assessment"),
            "reason": _text(payload, "reason"),
            "actor": actor,
        }
        current.setdefault("measurement_revisions", []).append(revision)
        current["strength_dbm"] = strength
        current["assessment"] = assess(current)
        revision["new_assessment"] = current["assessment"]
        event = {"revision": revision}
        if current.get("suspend_authorization") and status in {"suspended", "coordinating"}:
            current["suspend_authorization_status"] = "pending_reconfirm"
            event["suspend_authorization_status"] = "pending_reconfirm"
        return status, current, event

    if action == "locate":
        _need_status(item, {"assessed", "located"})
        location = _text(payload, "location")
        confidence = float(payload.get("confidence", 0))
        if confidence < 0.6:
            raise DomainError("low_location_confidence", "定位置信度低于0.6，不能进入处置", 409)
        current["location"] = {"label": location, "confidence": confidence}
        return "located", current, {"location": current["location"]}

    if action == "suspend":
        _need_status(item, {"located", "suspended"})
        authorization = _text(payload, "authorization_code")
        if not authorization.startswith("REG-"):
            raise DomainError("invalid_authorization", "停用授权编号无效", 403)
        current["suspend_authorization"] = authorization
        current["suspend_authorization_status"] = "confirmed"
        return "suspended", current, {"authorization_code": authorization}

    if action == "confirm_suspend":
        _need_status(item, {"suspended", "coordinating"})
        if current.get("suspend_authorization_status") != "pending_reconfirm":
            raise DomainError("invalid_state", "停用授权无需重新确认")
        authorization = payload.get("authorization_code")
        if authorization is not None:
            authorization = _text(payload, "authorization_code")
            if not authorization.startswith("REG-"):
                raise DomainError("invalid_authorization", "停用授权编号无效", 403)
            current["suspend_authorization"] = authorization
        current["suspend_authorization_status"] = "confirmed"
        return status, current, {"authorization_code": current["suspend_authorization"], "reconfirmed": True}

    if action == "coordinate":
        _need_status(item, {"suspended"})
        _need_authorization_fresh(current)
        agreement = _text(payload, "coordination_agreement")
        current["coordination_agreement"] = agreement
        current["coordination_note"] = payload.get("note", "")
        return "coordinating", current, {"coordination_agreement": agreement}

    if action == "resolve":
        _need_status(item, {"coordinating"})
        _need_authorization_fresh(current)
        if not payload.get("measurement_cleared"):
            raise DomainError("interference_present", "干扰尚未消除，不能结案", 409)
        current["resolution"] = {"evidence": _text(payload, "evidence"), "cleared": True}
        return "resolved", current, {"evidence": current["resolution"]["evidence"]}

    if action == "cancel":
        _need_status(item, {"pending", "assessed"})
        reason = _text(payload, "reason")
        current["cancellation"] = {"reason": reason, "actor": actor}
        return "cancelled", current, {"reason": reason}

    raise DomainError("unknown_action", "不支持的操作")
