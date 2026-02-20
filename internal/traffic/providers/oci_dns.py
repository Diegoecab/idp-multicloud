from internal.traffic.base import TrafficProvider


class OciDnsTrafficProvider(TrafficProvider):
    def __init__(self):
        self._state = {}

    def ensure_record(self, cell_host, primary_targets, secondary_targets, health_checks, policy):
        self._state[cell_host] = {
            "active": "primary",
            "primary": primary_targets,
            "secondary": secondary_targets,
            "healthChecks": health_checks,
            "provider": "oci-dns",
            "policy": policy,
            "host": cell_host,
        }
        return self._state[cell_host]

    def switch(self, cell_host, direction, weights=None):
        state = self._state.setdefault(cell_host, {"provider": "oci-dns"})
        state["active"] = "secondary" if direction == "to_secondary" else "primary"
        state["weights"] = weights or {}
        return state

    def status(self, cell_host):
        return self._state.get(cell_host, {"active": "unknown", "provider": "oci-dns"})
