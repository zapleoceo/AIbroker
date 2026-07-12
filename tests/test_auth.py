"""Auth primitives — project key gen/hash/verify."""
from aibroker.auth import generate_project_key, hash_project_key, verify_project_key


def test_project_key_unique():
    k1 = generate_project_key()
    k2 = generate_project_key()
    assert k1 != k2
    assert k1.startswith("aib_prj_")


def test_hash_deterministic():
    k = generate_project_key()
    assert hash_project_key(k) == hash_project_key(k)


def test_verify_match():
    k = generate_project_key()
    h = hash_project_key(k)
    assert verify_project_key(k, h)


def test_verify_no_match():
    k = generate_project_key()
    h = hash_project_key(k)
    assert not verify_project_key("aib_prj_bogus", h)


# ─── client_ip — honest client address behind Cloudflare+nginx ───────────────


def _req(headers: dict[str, str] | None = None, host: str | None = "10.0.0.9"):
    """Duck-typed Request: .headers mapping + .client.host."""
    from types import SimpleNamespace
    client = SimpleNamespace(host=host) if host is not None else None
    return SimpleNamespace(headers=headers or {}, client=client)


def test_client_ip_takes_first_xff_entry():
    """Behind CF+nginx the ORIGINAL client is the first X-Forwarded-For entry;
    later entries are the proxies that appended themselves."""
    from aibroker.auth import client_ip
    r = _req({"x-forwarded-for": "203.0.113.7, 172.68.1.1, 10.0.0.1"})
    assert client_ip(r) == "203.0.113.7"


def test_client_ip_falls_back_to_client_host_without_header():
    from aibroker.auth import client_ip
    assert client_ip(_req()) == "10.0.0.9"


def test_client_ip_empty_when_no_client_at_all():
    from aibroker.auth import client_ip
    assert client_ip(_req(host=None)) == ""
