import hashlib
import json


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def audit_hash(previous_hash, event):
    payload = canonical_json(event)
    return hashlib.sha256((previous_hash + payload).encode("utf-8")).hexdigest()


def verify_chain(events):
    previous = "GENESIS"
    for event in events:
        if event["previous_hash"] != previous:
            return False
        reconstructed = {
            "item_id": event["item_id"],
            "event_type": event["event_type"],
            "actor": event["actor"],
            "role": event["role"],
            "payload": event["payload"],
            "created_at": event["created_at"],
        }
        if audit_hash(previous, reconstructed) != event["event_hash"]:
            return False
        previous = event["event_hash"]
    return True
