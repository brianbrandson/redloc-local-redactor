from pathlib import Path
import tomllib


ROOT = Path(__file__).resolve().parents[1]


def test_install_docs_cover_operator_install_paths_and_smokes():
    install_doc = ROOT / "INSTALL.md"

    content = install_doc.read_text(encoding="utf-8")

    assert "uv tool install --editable ." in content
    assert "curl -LsSf https://astral.sh/uv/install.sh | sh" in content
    assert "python3 --version" in content
    assert "uv --version" in content
    assert "redloc-benchmark --suite all --tool local" in content
    assert "printf 'Email jane.doe@example.com" in content
    assert "Raw input" in content
    assert "redloc --about" in content
    assert "redloc --in raw-notes.txt --out raw-notes-redacted.txt --summary" in content
    assert "For actual workflows, see `docs/operator/workflows.md`" in content
    assert "uv tool uninstall local-redactor || true" in content
    assert "~/.config/redloc/" in content


def test_readme_points_first_time_users_to_install_docs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "INSTALL.md" in readme


def test_readme_points_operators_to_workflow_examples():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docs/operator/workflows.md" in readme
    assert "docs/operator/benchmark.md" in readme
    assert "docs/operator/tty-list-ui.md" in readme
    assert "docs/testing.md" in readme


def test_operator_benchmark_guide_explains_first_install_smoke_and_report_fields():
    content = (ROOT / "docs" / "operator" / "benchmark.md").read_text(
        encoding="utf-8"
    )

    assert "# Benchmark guide" in content
    assert "redloc-benchmark --suite all --tool local" in content
    assert '"passed": 12' in content
    assert '"failed": 0' in content
    assert "What it proves" in content
    assert "Suites" in content
    assert "Output fields" in content
    assert "Bring your own synthetic corpus" in content
    assert "Tool adapters" in content
    assert "Optional AI checker benchmark" in content
    assert "Passing the benchmark does not prove perfect redaction" in content
    assert "real client text" in content


def test_testing_note_explains_public_test_suite():
    content = (ROOT / "docs" / "testing.md").read_text(encoding="utf-8")

    assert "# Testing redloc" in content
    assert "uv run --with pytest python -m pytest -q" in content
    assert "redloc-benchmark --suite all --tool local" in content
    assert "tests/test_cli.py" in content
    assert "tests/test_detectors.py" in content
    assert "tests/test_benchmark.py" in content
    assert "tests/test_install_docs.py" in content
    assert "synthetic data" in content
    assert "do not prove perfect redaction" in content.lower()


def test_operator_tty_list_ui_guide_is_user_facing():
    content = (ROOT / "docs" / "operator" / "tty-list-ui.md").read_text(
        encoding="utf-8"
    )

    assert "# TTY list UI guide" in content
    assert "Common controls" in content
    assert "Checklist screens" in content
    assert "Single-select screens" in content
    assert "Non-TTY output" in content
    assert "Regression checks" not in content
    assert "PTY smoke" not in content
    assert "maintainer guide" not in content


def test_readme_points_operators_to_current_ai_workflow():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "--ai-suggest" in _command_reference(readme)
    assert "local/private suggestion review workflow" in _command_reference(readme)
    assert "--session-init NAME" in _command_reference(readme)
    assert "--show-secret PLACEHOLDER" in _command_reference(readme)
    assert "--show-secret-all" in _command_reference(readme)
    assert "intentionally prints raw mapped values locally" in _command_reference(readme)
    assert "--profile-term-remove TERM" in _command_reference(readme)
    assert "--ignore-remove TERM" in _command_reference(readme)
    assert "--detector-list" in _command_reference(readme)
    assert "--detector-disable DETECTOR" in _command_reference(readme)
    assert "--manual-detector-add" in _command_reference(readme)
    assert "--ai-config-set" in _command_reference(readme)
    assert "What redloc detects today" in readme
    assert "operator-defined placeholder classes" in _command_reference(readme)
    assert "generic system/tool paths" in readme
    assert "/etc/passwd" in readme
    assert "Operators must inspect output before sharing" in _safety_model(readme)
    assert "No contextual name/org detection yet" not in _known_limitations(readme)


def test_public_readme_does_not_link_internal_maintainer_docs():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "docs/branding-notes.md" not in readme
    assert "docs/dogfooding-plan.md" not in readme
    assert "docs/release-checklist.md" not in readme


def test_readme_known_limitations_match_current_ai_and_benchmark_features():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "No ShareClean integration yet" not in readme
    assert "No local LLM pass yet" not in readme
    assert "ShareClean" not in _known_limitations(readme)
    assert "AI review is optional and local/private by default" in readme


def test_operator_workflow_examples_cover_safe_realistic_paths():
    content = (ROOT / "docs" / "operator" / "workflows.md").read_text(
        encoding="utf-8"
    )

    assert "# Operator workflows" in content
    assert "Synthetic-only" in content
    assert "Caido-style request summary" in content
    assert "redloc --profile-init exampleco" in content
    assert "cat synthetic-request-summary.txt | redloc --summary" in content
    assert "redloc --session-init exampleco-web-thread" in content
    assert "redloc-benchmark --write-template my-synthetic-corpus.json" in content
    assert "redloc-benchmark --corpus my-synthetic-corpus.json --suite all --tool local" in content
    assert "http://127.0.0.1:8080/v1/chat/completions" in content
    assert "No real client names" in content
    assert "example.test" in content
    assert "plainSyntheticSecret" in content
    assert "plainS...t123" not in content
    assert "AKIA" not in content
    assert "ghp_" not in content
    assert "xox" not in content


def test_pyproject_configures_scoped_ruff_quality_gate():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["ruff"]["target-version"] == "py310"
    assert config["tool"]["ruff"]["lint"]["select"] == ["E4", "E7", "E9", "F"]


def test_pyproject_configures_scoped_bandit_security_gate():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["bandit"]["exclude_dirs"] == ["tests", "spikes", "dist"]
    assert config["tool"]["bandit"]["skips"] == ["B404", "B603", "B607"]


def test_pyproject_uses_locked_redloc_package_and_command_names():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["project"]["name"] == "redloc"
    assert config["project"]["scripts"] == {
        "redloc": "redactor.cli:main",
        "redloc-benchmark": "redactor.benchmark:main",
    }


def test_source_distribution_excludes_local_context_dist_and_prior_art_spikes():
    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert config["tool"]["hatch"]["build"]["targets"]["sdist"]["exclude"] == [
        "/.hermes.md",
        "/.hermes",
        "/AGENTS.md",
        "/BACKLOG.md",
        "/INSTALL.md",
        "/docs",
        "/dist",
        "/spikes",
    ]


def test_gitleaks_config_scopes_generated_files_and_synthetic_examples():
    config = tomllib.loads((ROOT / ".gitleaks.toml").read_text(encoding="utf-8"))

    assert config["extend"]["useDefault"] is True
    allowlist = config["allowlist"]
    assert r'''(?:^|/)__pycache__(?:/.*)?$''' in allowlist["paths"]
    assert r'''(?:^|/)spikes(?:/.*)?$''' in allowlist["paths"]
    assert r'''(?i)synthetic[-_a-z0-9]*''' in allowlist["regexes"]
    assert r'''\*\*\*''' in allowlist["regexes"]


def _known_limitations(readme: str) -> str:
    marker = "## Known limitations"
    _, _, tail = readme.partition(marker)
    body, _, _ = tail.partition("## Safety model")
    return body


def _safety_model(readme: str) -> str:
    marker = "## Safety model"
    _, _, tail = readme.partition(marker)
    body, _, _ = tail.partition("## Local data paths")
    return body


def _command_reference(readme: str) -> str:
    marker = "## Command reference"
    _, _, tail = readme.partition(marker)
    body, _, _ = tail.partition("## Safety model")
    return body
