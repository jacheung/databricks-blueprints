from types import SimpleNamespace

from rts_ml.serving import ensure_endpoint


class FakeEndpoints:
    def __init__(self, serving=None):
        self.serving, self.calls = serving, []  # serving: (entity_name, entity_version) or None if missing

    def list(self):
        return [SimpleNamespace(name="ep")] if self.serving else []

    def get(self, name):
        entity = SimpleNamespace(entity_name=self.serving[0], entity_version=self.serving[1])
        return SimpleNamespace(config=SimpleNamespace(served_entities=[entity]))

    def create(self, name, config, route_optimized):
        self.calls.append(("create", name, route_optimized))

    def update_config(self, name, served_entities):
        self.calls.append(("update", name))


def entity(version):
    return SimpleNamespace(entity_name="main.s.model", entity_version=version)


def test_creates_a_missing_endpoint():
    w = SimpleNamespace(serving_endpoints=FakeEndpoints())
    assert ensure_endpoint(w, "ep", entity("2"), route_optimized=True) == "created"
    assert w.serving_endpoints.calls == [("create", "ep", True)]


def test_updates_an_endpoint_serving_another_version():
    w = SimpleNamespace(serving_endpoints=FakeEndpoints(serving=("main.s.model", "1")))
    assert ensure_endpoint(w, "ep", entity("2"), route_optimized=True) == "updated"
    assert w.serving_endpoints.calls == [("update", "ep")]


def test_leaves_an_endpoint_already_serving_the_version():
    w = SimpleNamespace(serving_endpoints=FakeEndpoints(serving=("main.s.model", "2")))
    assert ensure_endpoint(w, "ep", entity("2"), route_optimized=True) == "unchanged"
    assert w.serving_endpoints.calls == []
