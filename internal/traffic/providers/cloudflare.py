from internal.traffic.base import TrafficProvider


class CloudflareTrafficProvider(TrafficProvider):
    def ensure_record(self, cell_host, primary_targets, secondary_targets, health_checks, policy):
        return {"provider": "cloudflare", "status": "stub", "host": cell_host}

    def switch(self, cell_host, direction, weights=None):
        return {"provider": "cloudflare", "status": "stub", "host": cell_host, "direction": direction}

    def status(self, cell_host):
        return {"provider": "cloudflare", "status": "stub", "host": cell_host}
