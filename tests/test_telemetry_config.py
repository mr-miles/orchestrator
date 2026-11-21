"""TelemetryConfig: defaults, parsing, validation, and the optional dependencies."""
import tomllib
from pathlib import Path

import pytest

from legion_orch.config import TelemetryConfig, load_config

ROOT = Path(__file__).resolve().parent.parent

BASE = """
project: t
workspaces: [{name: r, base_branch: main}]
decision_owners: [miles]
identity: {dev_spire: true}
"""


def _load(tmp_path, extra=""):
    p = tmp_path / "orchestrator.yaml"
    p.write_text(BASE + extra)
    return load_config(p)


def test_default_telemetry(tmp_path):
    cfg = _load(tmp_path)
    assert cfg.telemetry == TelemetryConfig()
    assert cfg.telemetry.enabled is None


def test_example_yaml_disables_telemetry():
    assert load_config(ROOT / "orchestrator.example.yaml").telemetry.enabled is False


def test_parses_values(tmp_path):
    cfg = _load(tmp_path, "telemetry: {enabled: true, endpoint: 'http://localhost:4318', "
                          "service_name: x, timeout_s: 3}\n")
    assert cfg.telemetry == TelemetryConfig(True, "http://localhost:4318", "x", 3)


@pytest.mark.parametrize("body", ["{endpoint: 'localhost:4318'}", "{timeout_s: 0}"])
def test_rejects_bad_values(tmp_path, body):
    with pytest.raises(ValueError, match="telemetry"):
        _load(tmp_path, f"telemetry: {body}\n")


def test_rejects_unknown_key(tmp_path):
    with pytest.raises(ValueError):
        _load(tmp_path, "telemetry: {bogus: 1}\n")


def test_optional_dependencies_declared():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text())
    extras = meta["project"]["optional-dependencies"]
    for name in ("opentelemetry-sdk>=1.27", "opentelemetry-exporter-otlp-proto-http>=1.27"):
        assert name in extras["telemetry"]
        assert name in extras["test"]
    lock = (ROOT / "uv.lock").read_text()
    assert 'name = "opentelemetry-sdk"' in lock
    assert 'name = "opentelemetry-exporter-otlp-proto-http"' in lock
