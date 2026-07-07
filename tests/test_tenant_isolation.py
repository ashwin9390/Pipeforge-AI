# PipeForge -- tenant isolation tests
# Exercises the real BlackboardClient against an in-memory Redis (fakeredis),
# so these fail if session state is not keyed per tenant.

import pytest

fakeredis = pytest.importorskip("fakeredis")
import redis


@pytest.fixture
def bb_factory(monkeypatch):
    """Return a factory that builds BlackboardClients sharing one fake Redis."""
    server = fakeredis.FakeServer()
    monkeypatch.setattr(
        redis, "Redis",
        lambda *a, **k: fakeredis.FakeStrictRedis(
            server=server, decode_responses=k.get("decode_responses", False)
        ),
    )
    from shared.redis_utils import BlackboardClient

    def make(tenant):
        return BlackboardClient(tenant=tenant)

    return make


def test_same_sid_different_tenants_do_not_collide(bb_factory):
    a = bb_factory("tenant_a")
    b = bb_factory("tenant_b")
    sid = "pf_shared"

    a.set_state(sid, {"goal": "A-secret", "memory": []})
    b.set_state(sid, {"goal": "B-secret", "memory": []})

    assert a.get_state(sid)["goal"] == "A-secret"
    assert b.get_state(sid)["goal"] == "B-secret"


def test_all_session_ids_scoped_to_tenant(bb_factory):
    a = bb_factory("tenant_a")
    b = bb_factory("tenant_b")

    a.set_state("pf_1", {"goal": "x", "memory": []})
    a.set_state("pf_2", {"goal": "y", "memory": []})
    b.set_state("pf_3", {"goal": "z", "memory": []})

    assert sorted(a.all_session_ids()) == ["pf_1", "pf_2"]
    assert b.all_session_ids() == ["pf_3"]


def test_delete_removes_namespaced_key(bb_factory):
    a = bb_factory("tenant_a")
    a.set_state("pf_1", {"goal": "x", "memory": []})
    a.delete("pf_1")
    assert a.get_state("pf_1") is None
    assert a.all_session_ids() == []
