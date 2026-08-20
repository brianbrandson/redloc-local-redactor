from __future__ import annotations

import argparse
import importlib.resources
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from redactor.cli import check_residual, redact_with_counts, run_ai_check


DEFAULT_CORPUS_PATH = Path("benchmarks/synthetic_corpus.json")
PACKAGE_CORPUS = "benchmarks/synthetic_corpus.json"
CORPUS_TEMPLATE: list[dict[str, Any]] = [
    {
        "id": "my-synthetic-operator-case",
        "suites": ["operator", "project-operator"],
        "input": (
            "ExampleCo portal request: GET "
            "https://app.example.test/api/v1/orgs/Project%20Squirrel/users?email=jane.doe@example.com\n"
            "Authorization: Bearer syntheticBearerToken1234567890\n"
        ),
        "client_terms": ["ExampleCo", "Project Squirrel"],
        "must_redact": [
            "ExampleCo",
            "app.example.test",
            "jane.doe@example.com",
            "syntheticBearerToken1234567890",
        ],
        "must_contain": [
            "https://[HOST_1]/api/v1/orgs/Project%20Squirrel/users?email=[EMAIL_1]",
            "Authorization: Bearer [TOKEN_1]",
        ],
    }
]


@dataclass
class BenchmarkCase:
    case_id: str
    input_text: str
    suites: list[str] = field(default_factory=lambda: ["baseline"])
    must_redact: list[str] = field(default_factory=list)
    must_contain: list[str] = field(default_factory=list)
    client_terms: list[str] = field(default_factory=list)


@dataclass
class AICheckerCase:
    case_id: str
    input_text: str
    client_terms: list[str] = field(default_factory=list)
    expected_warning: bool = False


AI_CHECKER_CASES: list[AICheckerCase] = [
    AICheckerCase(
        case_id="person-and-project-leftover",
        input_text="ExampleCo contact Jane Doe about Project Squirrel at jane.doe@example.com\n",
        client_terms=["ExampleCo"],
        expected_warning=True,
    ),
    AICheckerCase(
        case_id="fully-placeholdered-contact",
        input_text="ExampleCo contact Jane Doe about Project Squirrel at jane.doe@example.com\n",
        client_terms=["ExampleCo", "Jane Doe", "Project Squirrel"],
        expected_warning=False,
    ),
    AICheckerCase(
        case_id="location-and-team-leftover",
        input_text="ExampleCo incident handled by Blue Team Phoenix from Vilnius office\n",
        client_terms=["ExampleCo"],
        expected_warning=True,
    ),
    AICheckerCase(
        case_id="placeholders-only-summary",
        input_text="ExampleCo incident handled by Blue Team Phoenix from Vilnius office\n",
        client_terms=["ExampleCo", "Blue Team Phoenix", "Vilnius office"],
        expected_warning=False,
    ),
    AICheckerCase(
        case_id="product-and-tenant-leftover",
        input_text="ExampleCo tenant migrated from AcmePortal to Northwind Workspace\n",
        client_terms=["ExampleCo"],
        expected_warning=True,
    ),
    AICheckerCase(
        case_id="product-and-tenant-placeholdered",
        input_text="ExampleCo tenant migrated from AcmePortal to Northwind Workspace\n",
        client_terms=["ExampleCo", "AcmePortal", "Northwind Workspace"],
        expected_warning=False,
    ),
    AICheckerCase(
        case_id="report-safe-http-summary",
        input_text="GET https://app.example.test/api/v1/users?email=jane.doe@example.com returned 403\n",
        expected_warning=False,
    ),
    AICheckerCase(
        case_id="office-and-owner-leftover",
        input_text="Escalate to Maria Santos in Kaunas lab after token rotation\n",
        expected_warning=True,
    ),
]


@dataclass(frozen=True)
class BenchmarkTool:
    name: str

    def redact(self, case: BenchmarkCase) -> Any:
        return redact_with_counts(case.input_text, client_terms=case.client_terms)

    def warnings_for(self, output: str, case: BenchmarkCase) -> list[str]:
        return check_residual(output, client_terms=case.client_terms)


@dataclass(frozen=True)
class ShareCleanBenchmarkTool(BenchmarkTool):
    def redact(self, case: BenchmarkCase) -> Any:
        command = _shareclean_command()
        if not command:
            return SimpleNamespace(text="", counts={}, warnings=["tool_unavailable:shareclean"])
        try:
            proc = subprocess.run(
                command,
                input=case.input_text,
                text=True,
                capture_output=True,
                check=False,
            )
        except OSError:
            return SimpleNamespace(text="", counts={}, warnings=["tool_unavailable:shareclean"])
        if proc.returncode != 0:
            return SimpleNamespace(text="", counts={}, warnings=["tool_error:shareclean"])
        return SimpleNamespace(text=proc.stdout, counts={})


def _shareclean_command() -> list[str]:
    if shutil.which("shareclean"):
        return ["shareclean"]
    if shutil.which("uvx"):
        return ["uvx", "--from", "shareclean", "shareclean"]
    return []


SUPPORTED_TOOLS = {
    "local": BenchmarkTool("local"),
    "shareclean": ShareCleanBenchmarkTool("shareclean"),
}


def _case_from_dict(raw: dict[str, Any]) -> BenchmarkCase:
    client_terms = [str(value) for value in raw.get("client_terms", [])]
    suites = [str(value) for value in raw.get("suites", [])]
    if not suites:
        suites = ["operator" if client_terms else "baseline"]
    return BenchmarkCase(
        case_id=str(raw["id"]),
        input_text=str(raw["input"]),
        suites=suites,
        must_redact=[str(value) for value in raw.get("must_redact", [])],
        must_contain=[str(value) for value in raw.get("must_contain", [])],
        client_terms=client_terms,
    )


def load_corpus(path: Path) -> list[BenchmarkCase]:
    raw = json.loads(_read_corpus_text(path))
    if not isinstance(raw, list):
        raise ValueError("benchmark corpus must be a JSON list of cases")
    return [_case_from_dict(item) for item in raw]


def write_corpus_template(path: Path, *, force: bool = False) -> None:
    path = path.expanduser()
    if path.exists() and not force:
        raise FileExistsError(f"refusing to overwrite existing corpus template: {path}; pass --force")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(CORPUS_TEMPLATE, indent=2) + "\n", encoding="utf-8")


def _read_corpus_text(path: Path) -> str:
    expanded = path.expanduser()
    if expanded.exists():
        return expanded.read_text(encoding="utf-8")
    if path == DEFAULT_CORPUS_PATH:
        return importlib.resources.files("redactor").joinpath(PACKAGE_CORPUS).read_text(encoding="utf-8")
    return expanded.read_text(encoding="utf-8")


def resolve_tool(name: str) -> BenchmarkTool:
    try:
        return SUPPORTED_TOOLS[name]
    except KeyError as exc:
        supported = ", ".join(sorted(SUPPORTED_TOOLS))
        raise ValueError(f"unsupported benchmark tool: {name}; supported: {supported}") from exc


def select_cases(cases: list[BenchmarkCase], suite_name: str) -> list[BenchmarkCase]:
    if suite_name == "all":
        return cases
    selected = [case for case in cases if suite_name in case.suites]
    if selected:
        return selected
    supported = sorted({suite for case in cases for suite in case.suites} | {"all"})
    raise ValueError(f"unsupported benchmark suite: {suite_name}; supported: {', '.join(supported)}")


def run_benchmark(cases: list[BenchmarkCase], *, tool_name: str = "local", suite_name: str = "all") -> dict[str, Any]:
    selected_cases = select_cases(cases, suite_name)
    if tool_name == "all":
        tool_reports = [run_benchmark(selected_cases, tool_name=name, suite_name="all") for name in sorted(SUPPORTED_TOOLS)]
        return {
            "tool": "all",
            "suite": suite_name,
            "total_tools": len(tool_reports),
            "total": sum(report["total"] for report in tool_reports),
            "passed": sum(report["passed"] for report in tool_reports),
            "failed": sum(report["failed"] for report in tool_reports),
            "tools": tool_reports,
        }
    tool = resolve_tool(tool_name)
    rendered_cases: list[dict[str, Any]] = []
    passed = 0
    for case in selected_cases:
        result = tool.redact(case)
        warnings = list(getattr(result, "warnings", [])) + tool.warnings_for(result.text, case)
        leaked_count = sum(1 for value in case.must_redact if value and value in result.text)
        missing_expected_count = sum(1 for value in case.must_contain if value and value not in result.text)
        failed_checks = []
        if leaked_count:
            failed_checks.append("leaks")
        if missing_expected_count:
            failed_checks.append("missing_expected")
        if warnings:
            failed_checks.append("warnings")
        case_passed = leaked_count == 0 and missing_expected_count == 0 and not warnings
        if case_passed:
            passed += 1
        rendered_cases.append(
            {
                "id": case.case_id,
                "suites": case.suites,
                "tool": tool.name,
                "passed": case_passed,
                "counts": {kind: result.counts[kind] for kind in sorted(result.counts) if result.counts[kind] > 0},
                "failed_checks": failed_checks,
                "leaked_count": leaked_count,
                "missing_expected_count": missing_expected_count,
                "warnings": warnings,
            }
        )
    total = len(rendered_cases)
    failed = total - passed
    return {
        "tool": tool.name,
        "suite": suite_name,
        "total": total,
        "passed": passed,
        "failed": failed,
        "cases": rendered_cases,
    }


def run_ai_checker_benchmark(
    cases: list[AICheckerCase], *, endpoint: str, model: str
) -> dict[str, Any]:
    rendered_cases: list[dict[str, Any]] = []
    passed = 0
    for case in cases:
        result = redact_with_counts(case.input_text, client_terms=case.client_terms)
        deterministic_warnings = check_residual(result.text, client_terms=case.client_terms)
        ai_warnings = run_ai_check(result.text, endpoint=endpoint, model=model)
        ai_warning_messages = [warning.message() for warning in ai_warnings]
        categories = sorted({warning.category for warning in ai_warnings})
        has_unparseable_response = any(warning.category == "ai-checker-unparseable-response" for warning in ai_warnings)
        has_ai_warning = bool(ai_warnings)
        case_passed = not deterministic_warnings and not has_unparseable_response and has_ai_warning == case.expected_warning
        if case_passed:
            passed += 1
        rendered_cases.append(
            {
                "id": case.case_id,
                "passed": case_passed,
                "expected_warning": case.expected_warning,
                "ai_warning_count": len(ai_warnings),
                "ai_categories": categories,
                "counts": {kind: result.counts[kind] for kind in sorted(result.counts) if result.counts[kind] > 0},
                "failed_checks": [
                    check
                    for check, failed in (
                        ("deterministic_warnings", bool(deterministic_warnings)),
                        ("unparseable_ai_response", has_unparseable_response),
                        ("missing_ai_warning", case.expected_warning and not has_ai_warning),
                        ("unexpected_ai_warning", not case.expected_warning and has_ai_warning),
                    )
                    if failed
                ],
                "warnings": deterministic_warnings + ai_warning_messages,
            }
        )
    total = len(rendered_cases)
    failed = total - passed
    return {
        "tool": "ai-checker",
        "model": model,
        "total": total,
        "passed": passed,
        "failed": failed,
        "cases": rendered_cases,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the synthetic redloc benchmark corpus.")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH, help="JSON benchmark corpus path")
    parser.add_argument(
        "--suite",
        default="all",
        help="Benchmark suite to run; built-in suites: all, baseline, operator, project-operator",
    )
    parser.add_argument(
        "--tool",
        default="local",
        help="Benchmark tool adapter to run; currently supported: local, shareclean, all",
    )
    parser.add_argument(
        "--write-template",
        type=Path,
        metavar="FILE",
        help="write a starter synthetic corpus JSON file for your own local/private benchmark cases",
    )
    parser.add_argument("--force", action="store_true", help="overwrite FILE with --write-template")
    parser.add_argument("--ai-checker", action="store_true", help="run the built-in AI checker model smoke benchmark")
    parser.add_argument(
        "--ai-endpoint",
        default="http://127.0.0.1:11434/v1/chat/completions",
        help="local/private OpenAI-compatible chat completions endpoint for --ai-checker",
    )
    parser.add_argument("--ai-model", default="local-redaction-checker", help="model name sent to --ai-endpoint")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.write_template:
            write_corpus_template(args.write_template, force=args.force)
            print(f"wrote synthetic corpus template to {args.write_template}", file=sys.stderr)
            return 0
        if args.ai_checker:
            report = run_ai_checker_benchmark(AI_CHECKER_CASES, endpoint=args.ai_endpoint, model=args.ai_model)
        else:
            cases = load_corpus(args.corpus)
            report = run_benchmark(cases, tool_name=args.tool, suite_name=args.suite)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(f"error: {exc}")
        return 2
    print(json.dumps(report, indent=2, sort_keys=True))
    return 1 if report["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
