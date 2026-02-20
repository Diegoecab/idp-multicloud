from internal.traffic.base import TrafficProvider


class Route53TrafficProvider(TrafficProvider):
    def ensure_record(self, cell_host, primary_targets, secondary_targets, health_checks, policy):
        return {"provider": "route53", "status": "stub", "host": cell_host}

    def switch(self, cell_host, direction, weights=None):
        return {"provider": "route53", "status": "stub", "host": cell_host, "direction": direction}

    def status(self, cell_host):
        return {"provider": "route53", "status": "stub", "host": cell_host}
