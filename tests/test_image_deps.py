"""The image installs exactly the locked dependencies, once."""
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _lock_pins() -> dict[str, str]:
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    return dict(re.findall(r"^([A-Za-z0-9_.-]+)==([^\s;]+)", text, flags=re.M))


def test_requirements_lock_pins_pyproject_dependencies():
    deps = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["dependencies"]
    pins = {k.lower().replace("_", "-"): v for k, v in _lock_pins().items()}
    for dep in deps:
        name = re.match(r"[A-Za-z0-9_.-]+", dep).group(0).lower().replace("_", "-")
        assert name in pins, f"{name} missing from requirements.lock (re-run uv export)"
        exact = re.search(r"==([^\s;,\]]+)", dep)
        if exact:
            assert pins[name] == exact.group(1), name
    text = (ROOT / "requirements.lock").read_text(encoding="utf-8")
    assert len(re.findall(r"^litellm==", text, flags=re.M)) == 1


def test_dockerfile_installs_lock_then_package_without_deps():
    df = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert "pip install --no-cache-dir -r requirements.lock" in df
    assert "pip install --no-cache-dir --no-deps -e ." in df
    assert "litellm==" not in df


def test_monitor_reuses_the_api_image():
    import yaml

    services = yaml.safe_load((ROOT / "docker-compose.yml").read_text(encoding="utf-8"))["services"]
    assert services["api"]["image"] == "aibroker-api:latest"
    assert services["monitor"]["image"] == "aibroker-api:latest"
    assert "build" not in services["monitor"]
