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
