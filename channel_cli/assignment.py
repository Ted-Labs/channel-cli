"""Role and capacity based task assignment recommendations."""
import json
from pathlib import Path


POLICY = json.loads(Path(__file__).with_name("assignment.json").read_text())
ROLES = POLICY["roles"]
DIFFICULTIES = ("쉬움", "보통", "어려움")
NAMES = {"cora": "코라", "pixie": "픽시", "gemini": "Gemini"}


def preference(role, difficulty):
    if role not in ROLES:
        raise ValueError(f"unknown role: {role}")
    rule = ROLES[role]
    if role == "구현":
        if difficulty not in rule:
            raise ValueError(f"unknown difficulty: {difficulty}")
        return list(rule[difficulty])
    if difficulty not in DIFFICULTIES:
        raise ValueError(f"unknown difficulty: {difficulty}")
    return list(rule)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0 <= number <= 100 else None


def _used_percent(agent, cora_usage, agent_usage):
    if agent == "cora":
        if not isinstance(cora_usage, dict):
            return None
        values = [_number((cora_usage.get(window) or {}).get("utilization",
                                              (cora_usage.get(window) or {}).get("used_percent")))
                  for window in ("session", "weekly")]
    else:
        meters = agent_usage.get("pixie" if agent == "pixie" else "gemini", [])
        values = [_number(m.get("used")) for m in meters if isinstance(m, dict)]
    valid = [value for value in values if value is not None]
    return max(valid) if valid else None


def _runtime_for(agent, runtimes):
    if isinstance(runtimes, dict):
        value = runtimes.get(agent)
        return value if isinstance(value, dict) else None
    if isinstance(runtimes, list):
        return next((r for r in runtimes if isinstance(r, dict) and r.get("id") == agent), None)
    return None


def recommend(role, difficulty, runtimes, cora_usage, agent_usage, allowed=None):
    """Return the first eligible preference or the first preference as queue fallback."""
    ranked = preference(role, difficulty)
    if allowed is not None:
        ranked = [agent for agent in ranked if agent in allowed]
        if not ranked:
            raise ValueError("no eligible delivery target for role and difficulty")
    threshold = (POLICY["thresholds"]["hard_remaining_percent"]
                 if difficulty == "어려움" else POLICY["thresholds"]["default_remaining_percent"])
    priority = int(POLICY.get("priorities", {}).get(difficulty, 80))
    rejected = []
    considered = []
    for rank, agent in enumerate(ranked, 1):
        runtime = _runtime_for(agent, runtimes)
        reasons = []
        if runtime is None:
            reasons.append("상태 조회 불가")
        elif runtime.get("state") != "idle" or int(runtime.get("queued", 0) or 0) > 0:
            reasons.append("진행 중 또는 대기열 있음")
        used = _used_percent(agent, cora_usage, agent_usage)
        if used is None:
            reasons.append("사용량 조회 불가")
        elif used > 100 - threshold:
            reasons.append(f"잔여 사용량 {100 - used:.0f}%로 기준 {threshold}% 미만")
        considered.append({"agent": agent, "rank": rank, "available": not reasons,
                           "usedPercent": used, "queuedCount": int((runtime or {}).get("queued", 0) or 0), "reasons": reasons})
        if not reasons:
            skipped = "; ".join(f"{item['rank']}지망 {NAMES[item['agent']]}({', '.join(item['reasons'])})"
                                 for item in rejected)
            why = (f"{rank}지망 {NAMES[agent]}가 사용 가능" if not rejected else
                   f"{skipped}를 건너뛰고 {rank}지망 {NAMES[agent]} 배정")
            return {"role": role, "difficulty": difficulty, "target": agent,
                    "preferenceRank": rank, "priority": priority, "tier": "body",
                    "available": True, "queued": False,
                    "reason": why, "considered": considered}
        rejected.append(considered[-1])

    first = ranked[0]
    details = "; ".join(f"{item['rank']}지망 {NAMES[item['agent']]}: {', '.join(item['reasons'])}"
                         for item in considered)
    return {"role": role, "difficulty": difficulty, "target": first,
            "preferenceRank": 1, "priority": priority, "tier": "body",
            "available": False, "queued": True,
            "reason": f"모든 지망이 현재 불가해 1지망 {NAMES[first]} 대기열로 보냄 (기존 대기 {considered[0]['queuedCount']}건, priority {priority}; {details})",
            "considered": considered}
