import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import redactor.benchmark as benchmark
from redactor.cli import AIWarning

from redactor.benchmark import AICheckerCase, BenchmarkCase, BenchmarkTool, load_corpus, run_ai_checker_benchmark, run_benchmark
from redactor.benchmark import main as benchmark_main


def test_benchmark_scores_synthetic_cases_without_printing_raw_values():
    cases = [
        BenchmarkCase(
            case_id="url-query-email",
            input_text="GET https://app.example.test/api/v1/users?email=jane.doe@example.com&ticket=123\n",
            must_redact=["app.example.test", "jane.doe@example.com"],
            must_contain=["https://[HOST_1]/api/v1/users?email=[EMAIL_1]&ticket=123"],
        ),
        BenchmarkCase(
            case_id="client-term-token",
            input_text="ExampleCo api_key=synthetic-api-key-value-12345\n",
            client_terms=["ExampleCo"],
            must_redact=["ExampleCo", "synthetic-api-key-value-12345"],
            must_contain=["[CLIENT_1]", "api_key=[TOKEN_1]"],
        ),
    ]

    report = run_benchmark(cases)
    rendered = json.dumps(report, sort_keys=True)

    assert report["total"] == 2
    assert report["passed"] == 2
    assert report["failed"] == 0
    assert {case["id"] for case in report["cases"]} == {"url-query-email", "client-term-token"}
    assert "app.example.test" not in rendered
    assert "jane.doe@example.com" not in rendered
    assert "ExampleCo" not in rendered
    assert "synthetic-api-key-value-12345" not in rendered


def test_benchmark_reports_selected_tool_without_raw_values():
    cases = [
        BenchmarkCase(
            case_id="local-tool-email",
            input_text="Email jane.doe@example.com\n",
            must_redact=["jane.doe@example.com"],
            must_contain=["[EMAIL_1]"],
        )
    ]

    report = run_benchmark(cases, tool_name="local")
    rendered = json.dumps(report, sort_keys=True)

    assert report["tool"] == "local"
    assert report["cases"][0]["tool"] == "local"
    assert report["failed"] == 0
    assert "jane.doe@example.com" not in rendered


def test_ai_checker_benchmark_scores_expected_warning_presence_without_raw_values(monkeypatch):
    def fake_run_ai_check(redacted_text: str, *, endpoint: str, model: str):
        assert endpoint == "http://127.0.0.1:11434/v1/chat/completions"
        assert model == "tiny-test-model"
        assert "jane.doe@example.com" not in redacted_text
        if "Jane Doe" in redacted_text:
            return [AIWarning(category="person", line=1, confidence="medium")]
        return []

    monkeypatch.setattr(benchmark, "run_ai_check", fake_run_ai_check)
    cases = [
        AICheckerCase(
            case_id="leftover-person",
            input_text="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            client_terms=["ExampleCo"],
            expected_warning=True,
        ),
        AICheckerCase(
            case_id="clean-placeholders",
            input_text="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            client_terms=["ExampleCo", "Jane Doe"],
            expected_warning=False,
        ),
    ]

    report = run_ai_checker_benchmark(
        cases,
        endpoint="http://127.0.0.1:11434/v1/chat/completions",
        model="tiny-test-model",
    )
    rendered = json.dumps(report, sort_keys=True)

    assert report["tool"] == "ai-checker"
    assert report["model"] == "tiny-test-model"
    assert report["total"] == 2
    assert report["passed"] == 2
    assert report["failed"] == 0
    assert report["cases"][0]["ai_categories"] == ["person"]
    assert "jane.doe@example.com" not in rendered
    assert "ExampleCo" not in rendered


def test_ai_checker_benchmark_reports_false_positive_without_raw_values(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "run_ai_check",
        lambda redacted_text, *, endpoint, model: [AIWarning(category="context", line=1, confidence="low")],
    )
    cases = [
        AICheckerCase(
            case_id="clean-placeholders",
            input_text="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            client_terms=["ExampleCo", "Jane Doe"],
            expected_warning=False,
        )
    ]

    report = run_ai_checker_benchmark(cases, endpoint="http://127.0.0.1:11434/v1/chat/completions", model="noisy")
    rendered = json.dumps(report, sort_keys=True)

    assert report["failed"] == 1
    assert report["cases"][0]["failed_checks"] == ["unexpected_ai_warning"]
    assert "jane.doe@example.com" not in rendered


def test_ai_checker_benchmark_treats_unparseable_response_as_failure(monkeypatch):
    monkeypatch.setattr(
        benchmark,
        "run_ai_check",
        lambda redacted_text, *, endpoint, model: [
            AIWarning(category="ai-checker-unparseable-response", line=None, confidence="medium")
        ],
    )
    cases = [
        AICheckerCase(
            case_id="leftover-person",
            input_text="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            client_terms=["ExampleCo"],
            expected_warning=True,
        )
    ]

    report = run_ai_checker_benchmark(cases, endpoint="http://127.0.0.1:11434/v1/chat/completions", model="bad-json")
    rendered = json.dumps(report, sort_keys=True)

    assert report["failed"] == 1
    assert report["cases"][0]["failed_checks"] == ["unparseable_ai_response"]
    assert "jane.doe@example.com" not in rendered


def test_benchmark_filters_cases_by_suite_without_raw_values():
    cases = [
        BenchmarkCase(
            case_id="baseline-email",
            input_text="Email jane.doe@example.com\n",
            suites=["baseline"],
            must_redact=["jane.doe@example.com"],
        ),
        BenchmarkCase(
            case_id="operator-client",
            input_text="ExampleCo owns Project Squirrel\n",
            suites=["operator"],
            client_terms=["ExampleCo", "Project Squirrel"],
            must_redact=["ExampleCo", "Project Squirrel"],
        ),
    ]

    report = run_benchmark(cases, suite_name="baseline")
    rendered = json.dumps(report, sort_keys=True)

    assert report["suite"] == "baseline"
    assert report["total"] == 1
    assert report["cases"][0]["id"] == "baseline-email"
    assert report["failed"] == 0
    assert "jane.doe@example.com" not in rendered
    assert "ExampleCo" not in rendered


def test_benchmark_rejects_unknown_suite_without_raw_values():
    cases = [BenchmarkCase(case_id="email", input_text="Email jane.doe@example.com\n", suites=["baseline"])]

    try:
        run_benchmark(cases, suite_name="missing")
    except ValueError as exc:
        message = str(exc)
    else:
        raise AssertionError("expected ValueError")

    assert "unsupported benchmark suite" in message
    assert "baseline" in message
    assert "jane.doe@example.com" not in message


def test_benchmark_all_runs_each_adapter_without_raw_values(monkeypatch):
    class GenericBenchmarkTool(BenchmarkTool):
        def redact(self, case: BenchmarkCase):
            return SimpleNamespace(text="Email [REDACTED]\n", counts={})

    monkeypatch.setattr(
        benchmark,
        "SUPPORTED_TOOLS",
        {
            "local": BenchmarkTool("local"),
            "generic": GenericBenchmarkTool("generic"),
        },
    )
    cases = [
        BenchmarkCase(
            case_id="all-tools-email",
            input_text="Email jane.doe@example.com\n",
            must_redact=["jane.doe@example.com"],
        )
    ]

    report = run_benchmark(cases, tool_name="all")
    rendered = json.dumps(report, sort_keys=True)

    assert report["tool"] == "all"
    assert report["total_tools"] == 2
    assert [tool_report["tool"] for tool_report in report["tools"]] == ["generic", "local"]
    assert report["total"] == 2
    assert report["failed"] == 0
    assert "jane.doe@example.com" not in rendered


def test_benchmark_case_reports_failed_checks_when_no_exact_leaks(monkeypatch):
    class WarningBenchmarkTool(BenchmarkTool):
        def redact(self, case: BenchmarkCase):
            return SimpleNamespace(text="api_key=[REDACTED]\n", counts={}, warnings=["possible token remains"])

    monkeypatch.setattr(benchmark, "SUPPORTED_TOOLS", {"warning-tool": WarningBenchmarkTool("warning-tool")})
    cases = [
        BenchmarkCase(
            case_id="warn-without-exact-leak",
            input_text="api_key=plainSyntheticSecret1234567890\n",
            must_redact=["plainSyntheticSecret1234567890"],
        )
    ]

    report = run_benchmark(cases, tool_name="warning-tool")

    assert report["cases"][0]["passed"] is False
    assert report["cases"][0]["leaked_count"] == 0
    assert report["cases"][0]["failed_checks"] == ["warnings"]


def test_benchmark_residual_checker_accepts_common_masked_secret_markers(monkeypatch):
    class MaskingBenchmarkTool(BenchmarkTool):
        def redact(self, case: BenchmarkCase):
            return SimpleNamespace(
                text="api_key=[REDACTED]\nAuthorization: Bearer [REDACTED]\nCookie: sessionid=<redacted>\n",
                counts={},
            )

    monkeypatch.setattr(benchmark, "SUPPORTED_TOOLS", {"masking-tool": MaskingBenchmarkTool("masking-tool")})
    cases = [
        BenchmarkCase(
            case_id="masked-secret-markers",
            input_text="api_key=plainSyntheticSecret1234567890\n",
            must_redact=["plainSyntheticSecret1234567890"],
        )
    ]

    report = run_benchmark(cases, tool_name="masking-tool")

    assert report["cases"][0]["passed"] is True
    assert report["cases"][0]["warnings"] == []
    assert report["cases"][0]["failed_checks"] == []


def test_benchmark_cli_all_outputs_aggregate_report(monkeypatch, capsys):
    monkeypatch.setattr(benchmark, "SUPPORTED_TOOLS", {"local": BenchmarkTool("local")})

    exit_code = benchmark_main(["--tool", "all"])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["tool"] == "all"
    assert payload["total_tools"] == 1
    assert payload["tools"][0]["tool"] == "local"
    assert captured.err == ""


def test_benchmark_cli_accepts_explicit_local_tool():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.benchmark", "--tool", "local"],
        text=True,
        capture_output=True,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["tool"] == "local"
    assert payload["total"] >= 8
    assert proc.stderr == ""


def test_benchmark_cli_accepts_explicit_suite():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.benchmark", "--suite", "project-operator", "--tool", "local"],
        text=True,
        capture_output=True,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 0
    assert payload["suite"] == "project-operator"
    assert payload["total"] >= 1
    assert proc.stderr == ""


def test_benchmark_cli_writes_synthetic_corpus_template(tmp_path: Path):
    template = tmp_path / "my-synthetic-corpus.json"

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.benchmark", "--write-template", str(template)],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 0
    assert proc.stdout == ""
    assert proc.stderr == f"wrote synthetic corpus template to {template}\n"
    payload = json.loads(template.read_text(encoding="utf-8"))
    assert payload[0]["id"] == "my-synthetic-operator-case"
    assert "ExampleCo" in payload[0]["input"]
    assert payload[0]["client_terms"] == ["ExampleCo", "Project Squirrel"]
    assert "project-operator" in payload[0]["suites"]


def test_benchmark_cli_refuses_to_overwrite_template_without_force(tmp_path: Path):
    template = tmp_path / "my-synthetic-corpus.json"
    template.write_text("[]", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.benchmark", "--write-template", str(template)],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert "refusing to overwrite existing corpus template" in proc.stdout
    assert "pass --force" in proc.stdout
    assert proc.stderr == ""
    assert template.read_text(encoding="utf-8") == "[]"


def test_benchmark_cli_rejects_unknown_tool_without_raw_output():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.benchmark", "--tool", "not-a-real-tool"],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert "unsupported benchmark tool" in proc.stdout
    assert "local" in proc.stdout
    assert "jane.doe@example.com" not in proc.stdout
    assert proc.stderr == ""


def test_benchmark_supports_shareclean_adapter_without_raw_values(monkeypatch):
    def fake_run(command, *, input, text, capture_output, check):
        assert "shareclean" in command[-1]
        assert input == "Email jane.doe@example.com\n"
        assert text is True
        assert capture_output is True
        assert check is False
        return SimpleNamespace(returncode=0, stdout="Email [REDACTED]\n", stderr="")

    monkeypatch.setattr(benchmark, "_shareclean_command", lambda: ["uvx", "--from", "shareclean", "shareclean"])
    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    cases = [
        BenchmarkCase(
            case_id="shareclean-email",
            input_text="Email jane.doe@example.com\n",
            must_redact=["jane.doe@example.com"],
        )
    ]

    report = run_benchmark(cases, tool_name="shareclean")
    rendered = json.dumps(report, sort_keys=True)

    assert report["tool"] == "shareclean"
    assert report["cases"][0]["tool"] == "shareclean"
    assert report["failed"] == 0
    assert "jane.doe@example.com" not in rendered


def test_shareclean_adapter_failures_are_raw_free(monkeypatch):
    def fake_run(command, *, input, text, capture_output, check):
        return SimpleNamespace(returncode=2, stdout="", stderr="boom jane.doe@example.com")

    monkeypatch.setattr(benchmark, "_shareclean_command", lambda: ["shareclean"])
    monkeypatch.setattr(benchmark.subprocess, "run", fake_run)
    cases = [BenchmarkCase(case_id="shareclean-error", input_text="Email jane.doe@example.com\n")]

    report = run_benchmark(cases, tool_name="shareclean")
    rendered = json.dumps(report, sort_keys=True)

    assert report["failed"] == 1
    assert report["cases"][0]["warnings"] == ["tool_error:shareclean"]
    assert "jane.doe@example.com" not in rendered


def test_benchmark_cli_runs_json_corpus_and_exits_nonzero_on_failed_case(tmp_path: Path):
    corpus = tmp_path / "corpus.json"
    corpus.write_text(
        json.dumps(
            [
                {
                    "id": "passing-email",
                    "input": "Email jane.doe@example.com\n",
                    "must_redact": ["jane.doe@example.com"],
                    "must_contain": ["[EMAIL_1]"],
                },
                {
                    "id": "failing-missing-placeholder",
                    "input": "No sensitive data\n",
                    "must_contain": ["[EMAIL_1]"],
                },
            ]
        ),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.benchmark", "--corpus", str(corpus)],
        text=True,
        capture_output=True,
    )

    payload = json.loads(proc.stdout)
    assert proc.returncode == 1
    assert payload["total"] == 2
    assert payload["passed"] == 1
    assert payload["failed"] == 1
    assert payload["cases"][1]["id"] == "failing-missing-placeholder"
    assert payload["cases"][1]["missing_expected_count"] == 1
    assert "jane.doe@example.com" not in proc.stdout
    assert proc.stderr == ""


def test_builtin_synthetic_corpus_covers_day_to_day_operator_paste_shapes():
    corpus = load_corpus(Path("benchmarks/synthetic_corpus.json"))
    report = run_benchmark(corpus)

    case_ids = {case.case_id for case in corpus}
    suites = {suite for case in corpus for suite in case.suites}
    assert len(corpus) >= 8
    assert {"baseline", "operator", "project-operator"}.issubset(suites)
    assert {
        "http-request-headers-and-cookies",
        "caido-style-request-summary",
        "local-workspace-paths",
        "operator-contact-and-phone-notes",
        "bearer-and-basic-auth",
    }.issubset(case_ids)
    assert report["failed"] == 0


def test_builtin_baseline_is_soft_plain_tool_suite():
    corpus = load_corpus(Path("benchmarks/synthetic_corpus.json"))
    baseline_cases = [case for case in corpus if "baseline" in case.suites]
    report = run_benchmark(corpus, suite_name="baseline")

    assert len(baseline_cases) >= 4
    assert report["failed"] == 0
    assert all(not case.client_terms for case in baseline_cases)
    assert {case.case_id for case in baseline_cases}.issuperset(
        {
            "plain-email",
            "plain-phone",
            "plain-api-key",
            "plain-bearer-token",
        }
    )


def test_builtin_project_operator_shows_terms_without_profiles_on_disk():
    corpus = load_corpus(Path("benchmarks/synthetic_corpus.json"))
    configured_cases = [case for case in corpus if "project-operator" in case.suites]
    report = run_benchmark(corpus, suite_name="project-operator")

    assert configured_cases
    assert report["failed"] == 0
    assert any(case.client_terms for case in configured_cases)


def test_benchmark_default_corpus_loads_outside_repo_cwd(tmp_path: Path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    exit_code = benchmark_main([])

    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert exit_code == 0
    assert payload["total"] >= 8
    assert payload["failed"] == 0
    assert captured.err == ""


def test_root_and_packaged_builtin_corpora_stay_in_sync():
    root_corpus = json.loads(Path("benchmarks/synthetic_corpus.json").read_text(encoding="utf-8"))
    packaged_corpus = json.loads(Path("redactor/benchmarks/synthetic_corpus.json").read_text(encoding="utf-8"))

    assert root_corpus == packaged_corpus
