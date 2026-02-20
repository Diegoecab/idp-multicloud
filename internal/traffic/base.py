from abc import ABC, abstractmethod


class TrafficProvider(ABC):
    @abstractmethod
    def ensure_record(self, cell_host, primary_targets, secondary_targets, health_checks, policy):
        raise NotImplementedError

    @abstractmethod
    def switch(self, cell_host, direction, weights=None):
        raise NotImplementedError

    @abstractmethod
    def status(self, cell_host):
        raise NotImplementedError
