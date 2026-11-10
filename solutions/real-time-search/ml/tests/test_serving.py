from types import SimpleNamespace

from rts_ml.serving import ensure_endpoint


class FakeEndpoints:
    def __init__(self, serving=None):
        self.serving, self.calls = serving, []  # serving: (entity_name, entity_version[, scale_to_zero]) or None

    def list(self):
        return [SimpleNamespace(name="ep")] if self.serving else []

    def get(self, name):
        name, version, *scale = self.serving
        entity = SimpleNamespace(entity_name=name, entity_version=version,
                                 scale_to_zero_enabled=scale[0] if scale else False)
        return SimpleNamespace(config=SimpleNamespace(served_entities=[entity]))

    def create(self, name, config, route_optimized):
        self.calls.append(("create", name, route_optimized))

    def update_config(self, name, served_entities):
        self.calls.append(("update", name))


def entity(version):
    return SimpleNamespace(entity_name="main.s.model", entity_version=version, scale_to_zero_enabled=False)


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


def test_restores_scale_to_zero_someone_else_turned_on():
    # e2-demo-fe's cleanup bot turns scale-to-zero on overnight; cold starts make L2 time out to the L1 order.
    w = SimpleNamespace(serving_endpoints=FakeEndpoints(serving=("main.s.model", "2", True)))
    assert ensure_endpoint(w, "ep", entity("2"), route_optimized=True) == "updated"
    assert w.serving_endpoints.calls == [("update", "ep")]
