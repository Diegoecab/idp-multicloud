from internal.policy.data import policy_store
from internal.traffic.providers.cloudflare import CloudflareTrafficProvider
from internal.traffic.providers.oci_dns import OciDnsTrafficProvider
from internal.traffic.providers.route53 import Route53TrafficProvider

_oci_singleton = OciDnsTrafficProvider()


def get_traffic_provider():
    p = policy_store.load().get("traffic", {}).get("defaultProvider", "oci-dns")
    if p == "oci-dns":
        return _oci_singleton
    if p == "cloudflare":
        return CloudflareTrafficProvider()
    if p == "route53":
        return Route53TrafficProvider()
    return _oci_singleton
