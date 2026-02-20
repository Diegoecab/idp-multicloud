from internal.policy.data import policy_store
from internal.traffic.factory import get_traffic_provider

_REPLICATION_LAG = {}
_FAILOVER_EVENTS = []


def register_replication(cell_env: str, expected_rpo_seconds: int):
    _REPLICATION_LAG[cell_env] = {"lagSeconds": 0, "expectedRPO": expected_rpo_seconds}
    return _REPLICATION_LAG[cell_env]


def failover(cell: str, env: str, tier: str, dr_profile: str, host: str):
    policy = policy_store.load()
    expected_rpo = policy["drProfiles"][dr_profile]["rpoSeconds"]
    key = f"{cell}:{env}"
    lag = _REPLICATION_LAG.get(key, {"lagSeconds": 0, "expectedRPO": expected_rpo})
    if lag["lagSeconds"] > expected_rpo:
        raise ValueError(f"Replication lag {lag['lagSeconds']} exceeds target RPO {expected_rpo}")

    traffic = get_traffic_provider()
    traffic_state = traffic.switch(host, "to_secondary", {"primary": 0, "secondary": 100})
    event = {
        "cell": cell,
        "env": env,
        "tier": tier,
        "drProfile": dr_profile,
        "writeFence": "enabled-read-only",
        "traffic": traffic_state,
        "status": "completed",
    }
    _FAILOVER_EVENTS.append(event)
    return event


def failover_events():
    return list(_FAILOVER_EVENTS)
