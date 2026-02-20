import os
import yaml


class PolicyStore:
    def __init__(self, path: str | None = None):
        self.path = path or os.environ.get("IDP_POLICY_PATH", "config/policy.yaml")
        self._cache = None

    def load(self) -> dict:
        if self._cache is None:
            with open(self.path, "r", encoding="utf-8") as f:
                self._cache = yaml.safe_load(f) or {}
        return self._cache

    def reload(self) -> dict:
        self._cache = None
        return self.load()


policy_store = PolicyStore()
