from internal.handlers.api import ControlPlaneAPI


class DummyK8s:
    def get_claim(self, namespace, name):
        return None


def test_rejects_provider_override():
    api = ControlPlaneAPI(DummyK8s())
    resp = api.create_mysql(
        {
            "namespace": "default",
            "name": "payments-db",
            "cell": "payments",
            "tier": "C1",
            "environment": "prod",
            "size": "small",
            "storageGB": 20,
            "ha": True,
            "provider": "aws",
        }
    )
    assert resp.status == 400
    assert "Developer contract violation" in resp.payload["error"]
