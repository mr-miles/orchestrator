"""Acceptance (out of process): architecture, dependencies, documentation and tests of the
telemetry change (AC-10, AC-11, AC-12). Nothing here imports legion_orch: every check runs a
subprocess or reads a file. See oracle/tests.md for the criterion mapping."""
import json
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
DRIVER = ROOT / "tests" / "acceptance" / "support" / "otel_driver.py"
KEYS = [
    "legion.change.id", "legion.workspace", "legion.ticket.id", "legion.ticket.type",
    "legion.attempt", "legion.status", "legion.waiting", "legion.outcome", "legion.run.id",
    "legion.run.role", "legion.resumed"]
PACKAGES = ("opentelemetry-sdk", "opentelemetry-exporter-otlp-proto-http")


def run(cmd, **kw):
    return subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, timeout=600, **kw)


def run_driver(scenario: dict) -> dict:
    p = subprocess.run([sys.executable, str(DRIVER)], input=json.dumps({"deadline_s": 150, **scenario}),
                       capture_output=True, text=True, timeout=300)
    try:
        return json.loads(p.stdout)
    except ValueError:
        pytest.fail(f"driver crashed ({p.returncode}): {p.stderr[-1500:]}")


# ================================================================================== AC-10
def test_AC_10_lint_imports_passes():
    p = run([str(Path(sys.executable).parent / "lint-imports")])
    assert p.returncode == 0, p.stdout[-2500:] + p.stderr[-1500:]


def test_AC_10_ruff_passes():
    p = run([sys.executable, "-m", "ruff", "check", "legion_orch", "tests", "scripts"])
    assert p.returncode == 0, p.stdout[-2500:] + p.stderr[-1500:]


def test_AC_10_the_ticket_rules_have_no_telemetry_text():
    files = sorted((ROOT / "legion_orch" / "workflow" / "tickets").rglob("*.py"))
    assert files
    for f in files:
        text = f.read_text().lower()
        assert "opentelemetry" not in text and "telemetry" not in text, f.name


def test_AC_10_the_tracing_module_exists_in_one_place():
    assert (ROOT / "legion_orch" / "telemetry.py").is_file()


def test_AC_10_telemetry_and_cli_import_without_opentelemetry():
    code = ("import sys; sys.modules['opentelemetry'] = None; "
            "import legion_orch.telemetry, legion_orch.cli")
    p = run([sys.executable, "-c", code])
    assert p.returncode == 0, p.stderr[-2000:]


# ================================================================================== AC-11
def test_AC_11_pyproject_declares_the_telemetry_extra():
    extras = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["optional-dependencies"]
    for group in ("telemetry", "test"):
        assert group in extras, sorted(extras)
        for pkg in PACKAGES:
            assert any(re.match(rf"{re.escape(pkg)}\s*[>=<~!]", d) for d in extras[group]), (group, pkg)


def test_AC_11_the_lock_lists_the_sdk_and_the_otlp_exporter():
    lock = (ROOT / "uv.lock").read_text()
    for pkg in PACKAGES:
        assert f'name = "{pkg}"' in lock, pkg


def test_AC_11_the_example_config_documents_telemetry_and_passes_check():
    text = (ROOT / "orchestrator.example.yaml").read_text()
    assert re.search(r"^telemetry:", text, re.M), "no telemetry section"
    for word in ("enabled", "endpoint", "service_name", "timeout_s"):
        assert word in text.split("telemetry:", 1)[1], word
    p = run([sys.executable, "-m", "legion_orch", "-c", "orchestrator.example.yaml", "check"])
    assert p.returncode == 0, p.stdout[-1500:] + p.stderr[-1500:]


def test_AC_11_the_docs_name_every_span_attribute_and_the_setup():
    doc = ROOT / "docs" / "telemetry.md"
    assert doc.is_file(), "docs/telemetry.md is missing"
    text = doc.read_text()
    for needle in KEYS + ["change <id>", "<type> <ticket id>", "agent <role>", "localhost:4318"]:
        assert needle in text, needle
    assert "restart" in text.lower()


def test_AC_11_the_readme_links_the_docs():
    assert "docs/telemetry.md" in (ROOT / "README.md").read_text()


def test_AC_11_enabled_without_the_sdk_fails_startup_with_the_install_hint():
    r = run_driver({"scenario": "blocked", "block_sdk": True, "expect_exit": True,
                    "telemetry": {"enabled": True}})
    assert "error" not in r, r.get("error")
    assert r["exit_code"] not in (0, None), r["stderr_tail"]
    assert "uv sync --extra telemetry" in r["stderr_tail"], r["stderr_tail"]


def test_AC_11_auto_mode_without_the_sdk_keeps_serving_and_sends_nothing():
    r = run_driver({"scenario": "blocked", "block_sdk": True,
                    "env": {"OTEL_EXPORTER_OTLP_ENDPOINT": "${COLLECTOR}"}})
    assert "error" not in r, f"{r.get('error')}\n{r.get('stderr_tail', '')[-1500:]}"
    assert r["meta_ok"] is True
    assert r["requests"] == []


# ================================================================================== AC-12
# (the wiring tests were folded into test_telemetry_engine)
@pytest.mark.parametrize("name", ["test_telemetry_config", "test_telemetry", "test_telemetry_engine"])
def test_AC_12_the_implementer_test_files_exist_with_tests(name):
    f = ROOT / "tests" / f"{name}.py"
    assert f.is_file(), f"{f.name} is missing"
    assert re.search(r"^\s*(async )?def test_\w+", f.read_text(), re.M), f"{f.name} has no test function"
