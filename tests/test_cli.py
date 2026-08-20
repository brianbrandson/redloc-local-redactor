import io
import configparser
import json
import os
import pty
import select
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError

import redactor.cli as cli_module
from redactor.cli import (
    AISuggestion,
    ListEditorState,
    _read_review_key,
    apply_list_editor_key,
    append_ignored_suggestions,
    apply_ai_suggestions_to_profile,
    init_profile,
    edit_list_items,
    list_ignored_suggestions,
    list_profile_terms,
    load_profile_options,
    remove_ignored_suggestion,
    remove_profile_term,
    render_list_editor_screen,
    render_ai_suggestion_review_screen,
    review_ai_suggestions,
    write_report,
)


class FakeAIHandler(BaseHTTPRequestHandler):
    requests = []
    response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "warnings": [
                                {
                                    "category": "person",
                                    "line": 1,
                                    "confidence": "medium",
                                }
                            ]
                        }
                    )
                }
            }
        ]
    }

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.__class__.requests.append({"path": self.path, "body": body})
        payload = json.dumps(self.__class__.response_payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format, *args):
        return


def run_fake_ai_server():
    FakeAIHandler.requests = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeAIHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


class RedirectAIHandler(BaseHTTPRequestHandler):
    redirect_to = ""
    requests = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.__class__.requests.append({"method": "POST", "path": self.path, "body": body})
        self.send_response(302)
        self.send_header("Location", self.__class__.redirect_to)
        self.end_headers()

    def log_message(self, format, *args):
        return


class RedirectTargetHandler(BaseHTTPRequestHandler):
    requests = []

    def do_GET(self):
        self.__class__.requests.append({"method": "GET", "path": self.path, "body": ""})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        payload = b'{"choices":[{"message":{"content":"{\\"warnings\\":[]}"}}]}'
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length).decode("utf-8")
        self.__class__.requests.append({"method": "POST", "path": self.path, "body": body})
        self.do_GET()

    def log_message(self, format, *args):
        return


def run_redirect_ai_servers():
    RedirectAIHandler.requests = []
    RedirectTargetHandler.requests = []
    target = ThreadingHTTPServer(("127.0.0.1", 0), RedirectTargetHandler)
    RedirectAIHandler.redirect_to = f"http://127.0.0.1:{target.server_port}/v1/chat/completions"
    redirector = ThreadingHTTPServer(("127.0.0.1", 0), RedirectAIHandler)
    threading.Thread(target=target.serve_forever, daemon=True).start()
    threading.Thread(target=redirector.serve_forever, daemon=True).start()
    return redirector, target


def test_cli_reads_stdin_and_writes_redacted_stdout():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli"],
        input="Email jane.doe@example.com twice jane.doe@example.com\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "Email [EMAIL_1] twice [EMAIL_1]\n"
    assert "jane.doe@example.com" not in proc.stdout
    assert proc.stderr == ""


def test_cli_summary_prints_raw_free_counts_to_stderr():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--summary"],
        input="Email jane.doe@example.com api_key=synthetic-api-key-value-12345 https://app.example.test/login\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "Email [EMAIL_1] api_key=[TOKEN_1] https://[HOST_1]/login\n"
    assert proc.stderr == "summary: EMAIL=1 HOST=1 TOKEN=1 warnings=none\n"
    assert "jane.doe@example.com" not in proc.stderr
    assert "synthetic-api-key-value-12345" not in proc.stderr
    assert "https://app.example.test/login" not in proc.stderr


def test_cli_summary_is_visually_separated_after_labeled_redacted_text(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]

    subprocess.run(base + ["--session-init", "ssh-demo"], text=True, capture_output=True, check=True)
    proc = subprocess.run(
        base + ["--summary"],
        input="ssh root@10.10.10.10",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "ssh root@[INTERNAL_IP_1]"
    assert proc.stderr == "session: ssh-demo\n\nRedacted text:\n\n\nsummary: INTERNAL_IP=1 warnings=none\n"


def test_cli_summary_reports_warning_names_without_raw_values():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--summary", "--check-only", "--no-redact"],
        input="Authorization: Bearer abcdef...yz\n",
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert proc.stderr == "summary: warnings=possible token remains\n"
    assert "abcdef" not in proc.stderr


def test_cli_report_writes_raw_free_json_counts_profile_copy_and_warnings(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    settings = tmp_path / "settings.ini"
    report = tmp_path / "report.json"
    terms = tmp_path / "terms.txt"
    terms.write_text("ExampleCo\n", encoding="utf-8")
    config.parent.mkdir(parents=True)
    config.write_text(f"[acme]\nterm_files =\n    {terms}\n", encoding="utf-8")

    subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--copy-enable"],
        text=True,
        capture_output=True,
        check=True,
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--settings",
            str(settings),
            "--profile",
            "acme",
            "--report",
            str(report),
        ],
        input="ExampleCo email jane.doe@example.com api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert proc.stdout == "[CLIENT_1] email [EMAIL_1] api_key=[TOKEN_1]\n"
    assert payload == {
        "copy_enabled": True,
        "counts": {"CLIENT": 1, "EMAIL": 1, "TOKEN": 1},
        "profile": "acme",
        "warnings": [],
    }
    assert "profile: acme" in proc.stderr
    report_text = report.read_text(encoding="utf-8")
    assert "ExampleCo" not in report_text
    assert "jane.doe@example.com" not in report_text
    assert "synthetic-api-key-value-12345" not in report_text


def test_cli_report_writes_warning_names_for_check_only_without_raw_values(tmp_path: Path):
    report = tmp_path / "report.json"

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--check-only", "--no-redact", "--report", str(report)],
        input="Authorization: Bearer abcdef...yz",
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert json.loads(report.read_text(encoding="utf-8")) == {
        "copy_enabled": False,
        "counts": {},
        "profile": "default",
        "warnings": ["possible token remains"],
    }
    report_text = report.read_text(encoding="utf-8")
    assert "Authorization" not in report_text
    assert "abcdef" not in report_text


def test_cli_private_artifacts_are_not_world_readable_under_permissive_umask(tmp_path: Path):
    def mode(path: Path) -> int:
        return os.stat(path).st_mode & 0o777

    config = tmp_path / "config" / "profiles.ini"
    state = tmp_path / "config" / "current-profile"
    settings = tmp_path / "config" / "settings.ini"
    session_dir = tmp_path / "data" / "sessions"
    session_state = tmp_path / "config" / "current-session"
    output = tmp_path / "redacted.txt"
    report = tmp_path / "report.json"
    template = tmp_path / "template.txt"
    profile_dir = tmp_path / "config" / "profiles" / "acme"

    old_umask = os.umask(0o022)
    try:
        subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--copy-enable"],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile-init",
                "acme",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "acme",
                "--profile-term-add",
            ],
            input="ExampleCo\n",
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--session-dir",
                str(session_dir),
                "--session-state-file",
                str(session_state),
                "--session-init",
                "acme-web",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--term-file-template", str(template)],
            text=True,
            capture_output=True,
            check=True,
        )
        subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--settings",
                str(settings),
                "--session-dir",
                str(session_dir),
                "--session-state-file",
                str(session_state),
                "--profile",
                "acme",
                "--session",
                "acme-web",
                "--out",
                str(output),
                "--report",
                str(report),
            ],
            input="ExampleCo email jane.doe@example.com\n",
            text=True,
            capture_output=True,
            check=True,
        )
    finally:
        os.umask(old_umask)

    assert mode(settings) == 0o600
    assert mode(config) == 0o600
    assert mode(state) == 0o600
    assert mode(profile_dir) == 0o700
    assert mode(profile_dir / "terms.txt") == 0o600
    assert mode(profile_dir / "ignored-suggestions.txt") == 0o600
    assert mode(profile_dir / "redacted") == 0o700
    assert mode(session_dir) == 0o700
    assert mode(session_dir / "acme-web.json") == 0o600
    assert mode(session_state) == 0o600
    assert mode(output) == 0o600
    assert mode(report) == 0o600
    assert mode(template) == 0o600


def test_report_ai_suggestions_omit_visible_candidate_terms_by_default(tmp_path: Path):
    report = tmp_path / "report.json"

    write_report(
        report,
        counts={},
        profile="smoke",
        copy_enabled=False,
        warnings=[],
        ai_suggestions=[AISuggestion(term="BigCorp", category="organization", lines=(1,), confidence="high")],
    )

    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["ai_suggestions"] == [{"category": "organization", "lines": [1], "confidence": "high"}]
    assert "BigCorp" not in report.read_text(encoding="utf-8")


def test_cli_ai_check_sends_only_redacted_output_to_local_endpoint(tmp_path: Path):
    server = run_fake_ai_server()
    report = tmp_path / "report.json"
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--ai-check",
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "gpt-oss",
                "--report",
                str(report),
            ],
            input="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            text=True,
            capture_output=True,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert proc.stdout == "ExampleCo contact Jane Doe at [EMAIL_1]\n"
    assert "warning: possible person remains on line 1 (medium)" in proc.stderr
    assert len(FakeAIHandler.requests) == 1
    request = json.loads(FakeAIHandler.requests[0]["body"])
    rendered_request = json.dumps(request)
    assert "jane.doe@example.com" not in rendered_request
    assert "1: ExampleCo contact Jane Doe at [EMAIL_1]" in rendered_request
    payload = json.loads(report.read_text(encoding="utf-8"))
    assert payload["warnings"] == ["possible person remains on line 1 (medium)"]
    assert payload["ai_warnings"] == [
        {"category": "person", "line": 1, "confidence": "medium"}
    ]
    assert "jane.doe@example.com" not in report.read_text(encoding="utf-8")


def test_run_ai_check_refuses_endpoint_redirects_before_following():
    redirector, target = run_redirect_ai_servers()
    endpoint = f"http://127.0.0.1:{redirector.server_port}/v1/chat/completions"
    try:
        try:
            cli_module.run_ai_check(
                "ExampleCo contact Jane Doe\n",
                endpoint=endpoint,
                model="gpt-oss",
            )
        except HTTPError as exc:
            assert exc.code == 302
        else:
            raise AssertionError("AI redirect was followed instead of refused")
    finally:
        redirector.shutdown()
        redirector.server_close()
        target.shutdown()
        target.server_close()

    assert len(RedirectAIHandler.requests) == 1
    assert RedirectTargetHandler.requests == []


def test_run_ai_check_uses_configured_timeout(monkeypatch):
    seen: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return b'{"choices":[{"message":{"content":"{\\"warnings\\":[]}"}}]}'

    def fake_open_ai_request(request, *, timeout):
        seen["timeout"] = timeout
        seen["body"] = json.loads(request.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr(cli_module, "_open_ai_request", fake_open_ai_request)

    warnings = cli_module.run_ai_check(
        "ExampleCo contact Jane Doe\n",
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="gpt-oss",
        timeout_seconds=123.5,
    )

    assert warnings == []
    assert seen["timeout"] == 123.5
    request_body = seen["body"]
    assert isinstance(request_body, dict)
    assert request_body["stream"] is True
    assert request_body["max_tokens"] == 512
    assert request_body["chat_template_kwargs"] == {"reasoning_effort": "low"}


def test_run_ai_check_reads_streaming_chat_completion(monkeypatch):
    class FakeStreamingResponse:
        def __init__(self):
            self.lines = iter(
                [
                    b'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n',
                    b"\n",
                    b'data: {"choices":[{"delta":{"content":"{\\"warnings\\":"}}]}\n',
                    b"\n",
                    b'data: {"choices":[{"delta":{"content":"[{\\"category\\":\\"person\\",\\"line\\":1}]"}}]}\n',
                    b"\n",
                    b'data: {"choices":[{"delta":{"content":"}"}}]}\n',
                    b"\n",
                    b"data: [DONE]\n",
                ]
            )

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def readline(self):
            return next(self.lines, b"")

    def fake_open_ai_request(request, *, timeout):
        return FakeStreamingResponse()

    monkeypatch.setattr(cli_module, "_open_ai_request", fake_open_ai_request)

    warnings = cli_module.run_ai_check(
        "Jane Doe remained\n",
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="gpt-oss",
    )

    assert warnings == [cli_module.AIWarning(category="person", line=1)]


def test_run_ai_check_chunks_large_inputs_and_preserves_global_line_numbers(monkeypatch):
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.payload

    def fake_open_ai_request(request, *, timeout):
        body = json.loads(request.data.decode("utf-8"))
        reviewed_text = body["messages"][1]["content"]
        calls.append(reviewed_text)
        first_line = int(reviewed_text.split(":", 1)[0])
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {"warnings": [{"category": "project", "line": first_line, "confidence": "medium"}]}
                        )
                    }
                }
            ]
        }
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(cli_module, "_open_ai_request", fake_open_ai_request)
    monkeypatch.setattr(cli_module, "DEFAULT_AI_CHUNK_MAX_LINES", 2)
    monkeypatch.setattr(cli_module, "DEFAULT_AI_CHUNK_MAX_CHARS", 10_000)

    warnings = cli_module.run_ai_check(
        "\n".join(f"line {index}" for index in range(1, 6)),
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="gpt-oss",
    )

    assert len(calls) == 3
    assert calls[0].startswith("1: line 1\n2: line 2")
    assert calls[1].startswith("3: line 3\n4: line 4")
    assert calls[2].startswith("5: line 5")
    assert [warning.line for warning in warnings] == [1, 3, 5]


def test_numbered_line_chunks_split_single_oversized_line():
    chunks, line_count = cli_module._numbered_line_chunks("A" * 25, max_chars=10, max_lines=80)

    assert line_count == 1
    assert len(chunks) == 5
    assert all(start_line == 1 and end_line == 1 for _chunk, start_line, end_line in chunks)
    assert all(len(chunk) <= 10 for chunk, _start_line, _end_line in chunks)
    assert "".join(chunk.split(": ", 1)[1] for chunk, _start_line, _end_line in chunks) == "A" * 25


def test_run_ai_suggest_chunks_large_inputs_and_dedupes_candidates(monkeypatch):
    calls: list[str] = []

    class FakeResponse:
        def __init__(self, payload: bytes):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return self.payload

    def fake_open_ai_request(request, *, timeout):
        body = json.loads(request.data.decode("utf-8"))
        assert body["max_tokens"] == 512
        assert body["chat_template_kwargs"] == {"reasoning_effort": "low"}
        reviewed_text = body["messages"][1]["content"]
        calls.append(reviewed_text)
        first_line = int(reviewed_text.split(":", 1)[0])
        payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "candidates": [
                                    {
                                        "term": "Project Marigold",
                                        "category": "context" if first_line == 1 else "project",
                                        "lines": [first_line],
                                        "confidence": "medium" if first_line == 1 else "high",
                                    },
                                    {"term": "notes", "category": "context", "lines": [first_line], "confidence": "high"},
                                    {"term": "Administrator", "category": "person", "lines": [first_line], "confidence": "high"},
                                    {"term": "Domain Admins", "category": "context", "lines": [first_line], "confidence": "high"},
                                    {"term": "SYSVOL", "category": "location", "lines": [first_line], "confidence": "high"},
                                    {"term": "C$", "category": "context", "lines": [first_line], "confidence": "medium"},
                                    {"term": "BloodHound.py", "category": "context", "lines": [first_line], "confidence": "high"},
                                ]
                            }
                        )
                    }
                }
            ]
        }
        return FakeResponse(json.dumps(payload).encode("utf-8"))

    monkeypatch.setattr(cli_module, "_open_ai_request", fake_open_ai_request)
    monkeypatch.setattr(cli_module, "DEFAULT_AI_CHUNK_MAX_LINES", 2)
    monkeypatch.setattr(cli_module, "DEFAULT_AI_CHUNK_MAX_CHARS", 10_000)

    suggestions = cli_module.run_ai_suggest(
        "\n".join(f"Project Marigold note {index}" for index in range(1, 5)),
        endpoint="http://127.0.0.1:8080/v1/chat/completions",
        model="gpt-oss",
    )

    assert len(calls) == 2
    assert suggestions == [AISuggestion(term="Project Marigold", category="project", lines=(1, 3), confidence="high")]


def test_cli_ai_config_can_save_timeout_and_ai_check_uses_saved_timeout(tmp_path: Path):
    server = run_fake_ai_server()
    settings = tmp_path / "settings.ini"
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        set_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--settings",
                str(settings),
                "--ai-config-set",
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "gpt-oss",
                "--ai-timeout",
                "75",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        status_proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-config-status"],
            text=True,
            capture_output=True,
            check=True,
        )
        check_proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-check", "--summary"],
            input="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            text=True,
            capture_output=True,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert set_proc.stdout == "AI config saved\n"
    assert "ai_timeout: 75" in status_proc.stdout
    assert "possible person remains on line 1 (medium)" in check_proc.stderr


def test_cli_ai_config_status_shows_defaults_without_unset_spam(tmp_path: Path):
    settings = tmp_path / "settings.ini"

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-config-status"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == (
        "ai_endpoint: unset\n"
        "ai_model: unset\n"
        "ai_timeout: 30 (default)\n"
        "ai_chunk_lines: 80 (default)\n"
        "ai_chunk_chars: 8000 (default)\n"
    )


def test_cli_ai_check_requires_configured_endpoint_and_model(tmp_path: Path):
    settings = tmp_path / "settings.ini"

    missing_both = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-check"],
        input="ExampleCo contact Jane Doe\n",
        text=True,
        capture_output=True,
        check=False,
    )
    missing_model = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--settings",
            str(settings),
            "--ai-check",
            "--ai-endpoint",
            "http://127.0.0.1:11434/v1/chat/completions",
        ],
        input="ExampleCo contact Jane Doe\n",
        text=True,
        capture_output=True,
        check=False,
    )

    assert missing_both.returncode == 2
    assert missing_model.returncode == 2
    assert "--ai-check needs --ai-endpoint and --ai-model" in missing_both.stderr
    assert "--ai-check needs --ai-endpoint and --ai-model" in missing_model.stderr
    assert "Jane Doe" not in missing_both.stderr
    assert "Jane Doe" not in missing_model.stderr


def test_cli_ai_config_can_save_chunk_limits_and_ai_check_uses_them(tmp_path: Path):
    server = run_fake_ai_server()
    settings = tmp_path / "settings.ini"
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        set_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--settings",
                str(settings),
                "--ai-config-set",
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "gpt-oss",
                "--ai-chunk-lines",
                "1",
                "--ai-chunk-chars",
                "10000",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        status_proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-config-status"],
            text=True,
            capture_output=True,
            check=True,
        )
        check_proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-check", "--summary"],
            input="ExampleCo contact Jane Doe\nProject Squirrel contact John Smith\n",
            text=True,
            capture_output=True,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert set_proc.stdout == "AI config saved\n"
    assert f"ai_endpoint: {endpoint} (saved)" in status_proc.stdout
    assert "ai_model: gpt-oss (saved)" in status_proc.stdout
    assert "ai_timeout: 30 (default)" in status_proc.stdout
    assert "ai_chunk_lines: 1 (saved)" in status_proc.stdout
    assert "ai_chunk_chars: 10000 (saved)" in status_proc.stdout
    assert "unset" not in status_proc.stdout
    assert len(FakeAIHandler.requests) == 2
    assert "possible person remains on line 1 (medium)" in check_proc.stderr


def test_cli_ai_config_set_preserves_omitted_optional_ai_settings(tmp_path: Path):
    settings = tmp_path / "settings.ini"
    endpoint_one = "http://127.0.0.1:8080/v1/chat/completions"
    endpoint_two = "http://127.0.0.1:8081/v1/chat/completions"

    first_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--settings",
            str(settings),
            "--ai-config-set",
            "--ai-endpoint",
            endpoint_one,
            "--ai-model",
            "gpt-oss",
            "--ai-timeout",
            "75",
            "--ai-chunk-lines",
            "12",
            "--ai-chunk-chars",
            "3456",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    second_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--settings",
            str(settings),
            "--ai-config-set",
            "--ai-endpoint",
            endpoint_two,
            "--ai-model",
            "qwen",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    status_proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-config-status"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert first_proc.stdout == "AI config saved\n"
    assert second_proc.stdout == "AI config saved\n"
    assert f"ai_endpoint: {endpoint_two}" in status_proc.stdout
    assert "ai_model: qwen" in status_proc.stdout
    assert "ai_timeout: 75" in status_proc.stdout
    assert "ai_chunk_lines: 12" in status_proc.stdout
    assert "ai_chunk_chars: 3456" in status_proc.stdout


def test_cli_ai_config_set_status_clear_and_ai_check_uses_saved_settings(tmp_path: Path):
    server = run_fake_ai_server()
    settings = tmp_path / "settings.ini"
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        set_proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--settings",
                str(settings),
                "--ai-config-set",
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "gpt-oss",
            ],
            text=True,
            capture_output=True,
            check=True,
        )
        status_proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-config-status"],
            text=True,
            capture_output=True,
            check=True,
        )
        check_proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-check", "--summary"],
            input="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            text=True,
            capture_output=True,
            check=True,
        )
        clear_proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--settings", str(settings), "--ai-config-clear"],
            text=True,
            capture_output=True,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()

    assert set_proc.stdout == "AI config saved\n"
    assert f"ai_endpoint: {endpoint}" in status_proc.stdout
    assert "ai_model: gpt-oss" in status_proc.stdout
    assert "possible person remains on line 1 (medium)" in check_proc.stderr
    request = json.loads(FakeAIHandler.requests[0]["body"])
    assert request["model"] == "gpt-oss"
    assert clear_proc.stdout == "AI config cleared\n"


def test_cli_ai_check_drops_out_of_range_model_line_numbers(tmp_path: Path):
    FakeAIHandler.response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "warnings": [
                                {"category": "context", "line": 2, "confidence": "medium"},
                                {"category": "person", "line": 1, "confidence": "medium"},
                            ]
                        }
                    )
                }
            }
        ]
    }
    server = run_fake_ai_server()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--ai-check",
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "gpt-oss",
                "--summary",
            ],
            input="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            text=True,
            capture_output=True,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        FakeAIHandler.response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "warnings": [
                                    {
                                        "category": "person",
                                        "line": 1,
                                        "confidence": "medium",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

    assert "possible person remains on line 1 (medium)" in proc.stderr
    assert "line 2" not in proc.stderr


def test_cli_ai_check_collapses_combined_model_categories_to_context(tmp_path: Path):
    FakeAIHandler.response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "warnings": [
                                {
                                    "category": "person|organization|project|location|context",
                                    "line": 1,
                                    "confidence": "high",
                                }
                            ]
                        }
                    )
                }
            }
        ]
    }
    server = run_fake_ai_server()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--ai-check",
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "gpt-oss",
                "--summary",
            ],
            input="ExampleCo contact Jane Doe at jane.doe@example.com\n",
            text=True,
            capture_output=True,
            check=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        FakeAIHandler.response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "warnings": [
                                    {
                                        "category": "person",
                                        "line": 1,
                                        "confidence": "medium",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

    assert "possible context remains on line 1 (high)" in proc.stderr
    assert "person-organization-project-location-context" not in proc.stderr


def test_cli_ai_check_refuses_public_endpoint_without_sending_raw_input():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--ai-check",
            "--ai-endpoint",
            "https://api.example.test/v1/chat/completions",
            "--ai-model",
            "gpt-oss",
        ],
        input="Email jane.doe@example.com\n",
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "AI checker endpoint must be localhost or a private IP" in proc.stderr
    assert "jane.doe@example.com" not in proc.stderr


def test_cli_ai_config_set_refuses_public_endpoint(tmp_path: Path):
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--settings",
            str(tmp_path / "settings.ini"),
            "--ai-config-set",
            "--ai-endpoint",
            "https://api.example.test/v1/chat/completions",
            "--ai-model",
            "gpt-oss",
        ],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "AI config endpoint must be localhost or a private IP" in proc.stderr


def test_cli_ai_suggest_requires_tty_review_in_noninteractive_runs(tmp_path: Path):
    FakeAIHandler.response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "candidates": [
                                {
                                    "term": "BigCorp",
                                    "category": "organization",
                                    "lines": [1],
                                    "confidence": "high",
                                },
                                {
                                    "term": "[EMAIL_1]",
                                    "category": "person",
                                    "lines": [1],
                                    "confidence": "high",
                                },
                            ]
                        }
                    )
                }
            }
        ]
    }
    server = run_fake_ai_server()
    report = tmp_path / "report.json"
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--ai-suggest",
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "gpt-oss",
                "--report",
                str(report),
            ],
            input="[CLIENT_1] is CEO of BigCorp and his email is jane.doe@example.com\n",
            text=True,
            capture_output=True,
        )
    finally:
        server.shutdown()
        server.server_close()
        FakeAIHandler.response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "warnings": [
                                    {
                                        "category": "person",
                                        "line": 1,
                                        "confidence": "medium",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "--ai-suggest needs an interactive terminal" in proc.stderr
    assert "[EMAIL_1]" not in proc.stderr
    request = json.loads(FakeAIHandler.requests[0]["body"])
    rendered_request = json.dumps(request)
    assert "jane.doe@example.com" not in rendered_request
    assert "1: [CLIENT_1] is CEO of BigCorp and his email is [EMAIL_1]" in rendered_request
    assert not report.exists()


def test_cli_ai_suggest_refuses_public_endpoint_without_sending_raw_input():
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--ai-suggest",
            "--ai-endpoint",
            "https://api.example.test/v1/chat/completions",
            "--ai-model",
            "gpt-oss",
        ],
        input="Email jane.doe@example.com\n",
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "AI suggestion endpoint must be localhost or a private IP" in proc.stderr
    assert "jane.doe@example.com" not in proc.stderr


def test_review_ai_suggestions_accepts_checked_candidates_after_toggle():
    suggestions = [
        AISuggestion(term="BigCorp", category="organization", lines=(1,), confidence="high"),
        AISuggestion(term="London office", category="location", lines=(2,), confidence="medium"),
    ]
    output = io.StringIO()

    accepted = review_ai_suggestions(suggestions, input_stream=io.StringIO("2\n\n"), output_stream=output)

    assert accepted == [suggestions[0]]
    rendered = output.getvalue()
    assert "1. [x] BigCorp  organization  lines 1  high" in rendered
    assert "2. [ ] London office  location  lines 2  medium" in rendered
    assert "Commands: Enter=accept checked" in rendered
    assert "i NUMBER=ignore" in rendered
    assert "c NUMBER CATEGORY=category" in rendered


def test_review_ai_suggestions_can_ignore_candidate():
    suggestions = [
        AISuggestion(term="BigCorp", category="organization", lines=(1,), confidence="high"),
        AISuggestion(term="Tuesday room", category="location", lines=(2,), confidence="medium"),
    ]
    ignored: list[AISuggestion] = []
    output = io.StringIO()

    accepted = review_ai_suggestions(
        suggestions,
        input_stream=io.StringIO("i 2\n\n"),
        output_stream=output,
        ignored_suggestions=ignored,
    )

    assert accepted == [suggestions[0]]
    assert ignored == [suggestions[1]]
    rendered = output.getvalue()
    assert "Tuesday room" in rendered
    assert "2. [i] Tuesday room  location  lines 2  medium" in rendered


def test_review_ai_suggestions_does_not_persist_ignored_on_cancel():
    suggestions = [AISuggestion(term="Tuesday room", category="location")]
    ignored: list[AISuggestion] = []

    accepted = review_ai_suggestions(
        suggestions,
        input_stream=io.StringIO("i 1\nq\n"),
        output_stream=io.StringIO(),
        ignored_suggestions=ignored,
    )

    assert accepted is None
    assert ignored == []


def test_review_ai_suggestions_can_override_category():
    suggestions = [AISuggestion(term="Apollo", category="project", lines=(1,), confidence="medium")]
    output = io.StringIO()

    accepted = review_ai_suggestions(
        suggestions,
        input_stream=io.StringIO("c 1 context\n\n"),
        output_stream=output,
    )

    assert accepted == [AISuggestion(term="Apollo", category="context", lines=(1,), confidence="medium")]
    assert "1. [x] Apollo  context  lines 1  medium" in output.getvalue()


def test_review_ai_suggestions_can_cancel():
    output = io.StringIO()

    accepted = review_ai_suggestions(
        [AISuggestion(term="BigCorp", category="organization")],
        input_stream=io.StringIO("q\n"),
        output_stream=output,
    )

    assert accepted is None


def test_render_ai_suggestion_review_screen_scrolls_large_candidate_lists():
    suggestions = [AISuggestion(term=f"Term {index}", category="context") for index in range(1, 41)]

    rendered = render_ai_suggestion_review_screen(suggestions, selected={1, 40}, ignored={21}, cursor=20, height=5)

    assert "AI suggestions review" in rendered
    assert "[↑/↓] move  [Enter/Space] toggle  [i] ignore  [c] change category  [a] all  [n] none  [d] accept  [q] cancel" in rendered
    assert "... 18 earlier candidate(s) ..." in rendered
    assert "> 21. [i] Term 21  context  lines -  -" in rendered
    assert "... 17 later candidate(s) ..." in rendered
    assert "checked: 2/40  ignored: 1" in rendered


def test_cli_review_apply_profile_does_not_replay_suggestions_after_output(tmp_path: Path):
    FakeAIHandler.response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "candidates": [
                                {
                                    "term": "BigCorp",
                                    "category": "organization",
                                    "lines": [1],
                                    "confidence": "high",
                                }
                            ]
                        }
                    )
                }
            }
        ]
    }
    server = run_fake_ai_server()
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    settings = tmp_path / "settings.ini"
    input_file = tmp_path / "input.txt"
    input_file.write_text("[CLIENT_1] is CEO of BigCorp\n", encoding="utf-8")
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state), "--profile-init", "smoke"],
            check=True,
            text=True,
            capture_output=True,
        )
        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "redactor.cli",
                    "--config",
                    str(config),
                    "--state-file",
                    str(state),
                    "--settings",
                    str(settings),
                    "--profile",
                    "smoke",
                    "--in",
                    str(input_file),
                    "--ai-suggest",
                    "--ai-endpoint",
                    endpoint,
                    "--ai-model",
                    "gpt-oss",
                ],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(slave_fd)
            os.write(master_fd, b"\n")
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            os.close(master_fd)
    finally:
        server.shutdown()
        server.server_close()
        FakeAIHandler.response_payload = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps(
                            {
                                "warnings": [
                                    {
                                        "category": "person",
                                        "line": 1,
                                        "confidence": "medium",
                                    }
                                ]
                            }
                        )
                    }
                }
            ]
        }

    assert proc.returncode == 0
    assert stdout == "[CLIENT_1] is CEO of [ORG_1]\n"
    assert "AI suggestions:" in stderr
    assert "1. [x] BigCorp  organization  lines 1  high" in stderr
    assert "profile: smoke" in stderr
    assert "\nRedacted text:\n" in stderr
    assert stderr.index("profile: smoke") < stderr.index("Redacted text:")
    assert "suggestion: possible organization term: BigCorp" not in stderr


def test_cli_review_can_ignore_suggestion_and_filters_it_next_run(tmp_path: Path):
    FakeAIHandler.response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "candidates": [
                                {"term": "BigCorp", "category": "organization", "lines": [1], "confidence": "high"},
                                {"term": "Tuesday room", "category": "location", "lines": [1], "confidence": "medium"},
                            ]
                        }
                    )
                }
            }
        ]
    }
    server = run_fake_ai_server()
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    settings = tmp_path / "settings.ini"
    input_file = tmp_path / "input.txt"
    input_file.write_text("BigCorp met in Tuesday room\n", encoding="utf-8")
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state), "--profile-init", "smoke"],
            check=True,
            text=True,
            capture_output=True,
        )
        master_fd, slave_fd = pty.openpty()
        try:
            first_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "redactor.cli",
                    "--config",
                    str(config),
                    "--state-file",
                    str(state),
                    "--settings",
                    str(settings),
                    "--profile",
                    "smoke",
                    "--in",
                    str(input_file),
                    "--ai-suggest",
                    "--ai-endpoint",
                    endpoint,
                    "--ai-model",
                    "gpt-oss",
                ],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(slave_fd)
            os.write(master_fd, b"i 2\n\n")
            first_stdout, first_stderr = first_proc.communicate(timeout=10)
        finally:
            os.close(master_fd)
        master_fd, slave_fd = pty.openpty()
        try:
            second_proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "redactor.cli",
                    "--config",
                    str(config),
                    "--state-file",
                    str(state),
                    "--settings",
                    str(settings),
                    "--profile",
                    "smoke",
                    "--in",
                    str(input_file),
                    "--ai-suggest",
                    "--ai-endpoint",
                    endpoint,
                    "--ai-model",
                    "gpt-oss",
                ],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(slave_fd)
            os.write(master_fd, b"\n")
            second_stdout, second_stderr = second_proc.communicate(timeout=10)
        finally:
            os.close(master_fd)
    finally:
        server.shutdown()
        server.server_close()

    ignored_file = tmp_path / "profiles" / "smoke" / "ignored-suggestions.txt"
    assert first_proc.returncode == 0
    assert first_stdout == "[ORG_1] met in Tuesday room\n"
    assert "ignored 1 AI suggestion term(s)" in first_stderr
    assert ignored_file.read_text(encoding="utf-8") == "LOCATION: Tuesday room\n"
    assert second_proc.returncode == 0
    assert second_stdout == "[ORG_1] met in Tuesday room\n"
    assert "BigCorp" in second_stderr
    assert "Tuesday room" not in second_stderr


def test_cli_review_category_override_applies_overridden_category(tmp_path: Path):
    FakeAIHandler.response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "candidates": [
                                {"term": "Apollo", "category": "organization", "lines": [1], "confidence": "medium"},
                            ]
                        }
                    )
                }
            }
        ]
    }
    server = run_fake_ai_server()
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    settings = tmp_path / "settings.ini"
    input_file = tmp_path / "input.txt"
    input_file.write_text("Apollo should stay quiet\n", encoding="utf-8")
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    try:
        subprocess.run(
            [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state), "--profile-init", "smoke"],
            check=True,
            text=True,
            capture_output=True,
        )
        master_fd, slave_fd = pty.openpty()
        try:
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "redactor.cli",
                    "--config",
                    str(config),
                    "--state-file",
                    str(state),
                    "--settings",
                    str(settings),
                    "--profile",
                    "smoke",
                    "--in",
                    str(input_file),
                    "--ai-suggest",
                    "--ai-endpoint",
                    endpoint,
                    "--ai-model",
                    "gpt-oss",
                ],
                stdin=slave_fd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            os.close(slave_fd)
            os.write(master_fd, b"c 1 project\n\n")
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            os.close(master_fd)
    finally:
        server.shutdown()
        server.server_close()

    terms_file = tmp_path / "profiles" / "smoke" / "terms.txt"
    assert proc.returncode == 0
    assert stdout == "[PROJECT_1] should stay quiet\n"
    assert terms_file.read_text(encoding="utf-8") == "PROJECT: Apollo\n"
    assert "Apollo  project" in stderr


def test_cli_review_requires_ai_suggest():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--review"],
        input="Email jane.doe@example.com\n",
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "unrecognized arguments: --review" in proc.stderr
    assert "jane.doe@example.com" not in proc.stderr


def test_apply_ai_suggestions_to_profile_appends_terms_and_reruns_redaction(tmp_path: Path):
    term_file = tmp_path / "terms.txt"
    terms: list[str] = []
    suggestions = [AISuggestion(term="BigCorp", category="organization", lines=(1,), confidence="high")]

    result, added_count = apply_ai_suggestions_to_profile(
        "[CLIENT_1] is CEO of BigCorp and his email is jane.doe@example.com\n",
        term_file=term_file,
        terms=terms,
        suggestions=suggestions,
        vault=None,
    )

    assert added_count == 1
    assert terms == ["ORG: BigCorp"]
    assert term_file.read_text(encoding="utf-8") == "ORG: BigCorp\n"
    assert result is not None
    assert result.text == "[CLIENT_1] is CEO of [ORG_1] and his email is [EMAIL_1]\n"
    assert result.counts == {"EMAIL": 1, "ORG": 1}


def test_apply_ai_suggestions_to_profile_preserves_location_category(tmp_path: Path):
    term_file = tmp_path / "terms.txt"
    terms: list[str] = []

    result, added_count = apply_ai_suggestions_to_profile(
        "Project is handled from the Vilnius office\n",
        term_file=term_file,
        terms=terms,
        suggestions=[AISuggestion(term="Vilnius", category="location", lines=(1,), confidence="high")],
        vault=None,
    )

    assert added_count == 1
    assert terms == ["LOCATION: Vilnius"]
    assert term_file.read_text(encoding="utf-8") == "LOCATION: Vilnius\n"
    assert result is not None
    assert result.text == "Project is handled from the [LOCATION_1] office\n"
    assert result.counts == {"LOCATION": 1}


def test_apply_ai_suggestions_to_profile_handles_placeholderized_url_hosts(tmp_path: Path):
    term_file = tmp_path / "terms.txt"
    terms: list[str] = []

    result, added_count = apply_ai_suggestions_to_profile(
        "Open https://dc01.dry.martini.bars/admin\n",
        term_file=term_file,
        terms=terms,
        suggestions=[AISuggestion(term="dc01.dry.martini.bars", category="context", lines=(1,), confidence="high")],
        vault=None,
    )

    assert added_count == 1
    assert result is not None
    assert result.text == "Open https://[CONTEXT_1]/admin\n"


def test_apply_ai_suggestions_to_profile_dedupes_existing_terms_without_rerun(tmp_path: Path):
    term_file = tmp_path / "terms.txt"
    term_file.write_text("BigCorp\n", encoding="utf-8")
    terms = ["BigCorp"]

    result, added_count = apply_ai_suggestions_to_profile(
        "BigCorp\n",
        term_file=term_file,
        terms=terms,
        suggestions=[AISuggestion(term="BigCorp", category="organization")],
        vault=None,
    )

    assert added_count == 0
    assert result is None
    assert terms == ["BigCorp"]
    assert term_file.read_text(encoding="utf-8") == "BigCorp\n"


def test_profile_ignored_suggestions_file_is_created_and_loaded(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    profile_path = init_profile("smoke", config_path=config)
    ignored_file = profile_path / "ignored-suggestions.txt"

    added_count = append_ignored_suggestions(
        ignored_file,
        [AISuggestion(term="Tuesday room", category="location")],
    )
    options = load_profile_options("smoke", config)

    assert added_count == 1
    assert ignored_file.read_text(encoding="utf-8") == "LOCATION: Tuesday room\n"
    assert options.ignored_suggestion_files == [ignored_file]


def test_list_and_remove_ignored_suggestions(tmp_path: Path):
    ignored_file = tmp_path / "ignored-suggestions.txt"
    ignored_file.write_text("Tuesday room\n# note\nBlue Team Room\ntuesday room\n", encoding="utf-8")

    assert list_ignored_suggestions([ignored_file]) == ["UNASSIGNED: Blue Team Room", "UNASSIGNED: Tuesday room"]
    assert remove_ignored_suggestion(ignored_file, "tuesday room") == 2
    assert ignored_file.read_text(encoding="utf-8") == "# note\nBlue Team Room\n"
    assert remove_ignored_suggestion(ignored_file, "missing") == 0


def test_cli_ignore_add_stores_unassigned_terms(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    profile_path = init_profile("smoke", config_path=config)
    ignored_file = profile_path / "ignored-suggestions.txt"

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile", "smoke", "--ignore-add"],
        input="Blue Team Room\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == f"added 1 ignored AI suggestion term(s) to {ignored_file}\n"
    assert ignored_file.read_text(encoding="utf-8") == "UNASSIGNED: Blue Team Room\n"


def test_cli_term_add_accepts_single_ctrl_d_without_extra_eof(tmp_path: Path):
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "redactor.cli", "--profile-term-add", "--term-file", str(terms_file)],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, b"Project Squirrel\x04")
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate(timeout=5)
            raise AssertionError("--profile-term-add waited for a second Ctrl-D") from exc
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert stdout == f"added 1 term(s) to {terms_file}\n"
    assert "Finish with Ctrl-D." in stderr
    assert terms_file.read_text(encoding="utf-8") == "PROJECT: Project Squirrel\n"


def test_cli_ignore_add_accepts_single_ctrl_d_without_extra_eof(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    profile_path = init_profile("smoke", config_path=config)
    ignored_file = profile_path / "ignored-suggestions.txt"
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile", "smoke", "--ignore-add"],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, b"Blue Team Room\x04")
        try:
            stdout, stderr = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired as exc:
            proc.kill()
            proc.communicate(timeout=5)
            raise AssertionError("--ignore-add waited for a second Ctrl-D") from exc
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert stdout == f"added 1 ignored AI suggestion term(s) to {ignored_file}\n"
    assert "Finish with Ctrl-D." in stderr
    assert ignored_file.read_text(encoding="utf-8") == "UNASSIGNED: Blue Team Room\n"


def test_list_and_remove_profile_terms(tmp_path: Path):
    terms_file = tmp_path / "terms.txt"
    terms_file.write_text("ORG: BigCorp\n# note\nPROJECT: Apollo\nbigcorp\n", encoding="utf-8")

    assert list_profile_terms([terms_file]) == ["PROJECT: Apollo", "ORG: BigCorp"]
    assert remove_profile_term(terms_file, "BigCorp") == 2
    assert terms_file.read_text(encoding="utf-8") == "# note\nPROJECT: Apollo\n"
    assert remove_profile_term(terms_file, "missing") == 0


def test_list_sessions_marks_active_session(tmp_path: Path):
    session_dir = tmp_path / "sessions"
    state_file = tmp_path / "current-session"
    cli_module.init_session("first", session_dir=session_dir, state_file=state_file)
    cli_module.init_session("second", session_dir=session_dir, state_file=state_file)
    cli_module.select_session("first", session_dir=session_dir, state_file=state_file)

    assert cli_module.list_sessions(session_dir, state_file=state_file) == ["* first:", "  second:"]


def test_render_single_select_screen_matches_list_editor_chrome():
    rendered = cli_module.render_single_select_screen(
        "Sessions",
        ["lab-maritiniad: HOST=1", "lab-odyssey: TOKEN=1"],
        selected=1,
        cursor=0,
    )

    assert "Sessions" in rendered
    assert "[↑/↓] move  [Enter/Space] select" in rendered
    assert "[f] filter" in rendered
    assert "[d] accept" in rendered
    assert "filter: -" in rendered
    assert ">  1. [x] lab-maritiniad: HOST=1" in rendered
    assert "   2. [ ] lab-odyssey: TOKEN=1" in rendered


def test_render_single_select_screen_shows_filter_mode_when_active():
    rendered = cli_module.render_single_select_screen(
        "Profiles",
        ["default"],
        selected=1,
        cursor=0,
        filter_text="d",
        filter_mode=True,
    )

    assert "filter: d (typing)" in rendered
    assert "typing filter" in rendered
    assert "[Enter] confirm filter" in rendered
    assert "[d] accept" not in rendered
    assert "[q] cancel" not in rendered


def test_edit_list_items_unchecked_items_are_disabled():
    output = io.StringIO()

    selected = edit_list_items(
        ["Blue Team Room", "Tuesday room"],
        title="Ignored AI suggestions",
        input_stream=io.StringIO("2\nd\n"),
        output_stream=output,
    )

    assert selected == ["Tuesday room"]
    assert "1. [x] Blue Team Room" in output.getvalue()
    assert "2. [ ] Tuesday room" in output.getvalue()


def test_render_list_editor_screen_scrolls_large_lists():
    items = [f"Term {index}" for index in range(1, 41)]

    rendered = render_list_editor_screen("Profile terms", items, kept=set(range(1, 41)) - {21}, cursor=20, height=5)

    assert "Profile terms" in rendered
    assert "[↑/↓] move  [Enter/Space] toggle enabled" in rendered
    assert "... 18 earlier item(s) ..." in rendered
    assert "> 21. [ ] Term 21" in rendered
    assert "enabled: 39/40" in rendered


def test_render_list_editor_screen_filters_large_lists():
    items = ["BigCorp", "Project Apollo", "Tuesday room"]

    rendered = render_list_editor_screen("Profile terms", items, kept={1, 2, 3}, cursor=0, filter_text="apollo")

    assert "filter: apollo" in rendered
    assert "Project Apollo" in rendered
    assert "BigCorp" not in rendered
    assert "Tuesday room" not in rendered


def test_render_list_editor_screen_uses_explicit_filter_mode_hint():
    rendered = render_list_editor_screen("Profile terms", ["BigCorp"], kept={1}, cursor=0)

    assert "[f] filter" in rendered
    assert "[Enter/Space] toggle enabled" in rendered
    assert "[d] accept" in rendered
    assert "[type] filter" not in rendered


def test_render_builtin_detector_list_does_not_advertise_inactive_category_change_control():
    rendered = render_list_editor_screen(
        "Built-in detectors for profile: smoke",
        ["EMAIL       email addresses", "PATH        local paths"],
        kept={1, 2},
        cursor=0,
        change_label=None,
    )

    assert "[Enter/Space] toggle enabled" in rendered
    assert "[c] change detector" not in rendered


def test_render_list_editor_screen_shows_move_action_and_marker():
    rendered = render_list_editor_screen(
        "Profile terms",
        ["ORG: BigCorp", "CONTEXT: Linux Mint"],
        kept={1},
        moved={2},
        move_key="m",
        move_label="move to ignore list",
        cursor=1,
    )

    assert "[m] move to ignore list" in rendered
    assert "   1. [x] ORG: BigCorp" in rendered
    assert ">  2. [m] CONTEXT: Linux Mint" in rendered


def test_render_list_editor_screen_shows_filter_mode_when_active():
    rendered = render_list_editor_screen("Profile terms", ["BigCorp"], kept={1}, cursor=0, filter_text="d", filter_mode=True)

    assert "filter: d (typing)" in rendered
    assert "typing filter" in rendered
    assert "[Enter] confirm filter" in rendered
    assert "[Backspace] edit filter" not in rendered
    assert "[d] accept" not in rendered
    assert "[q] cancel" not in rendered


def test_list_editor_state_treats_action_keys_as_filter_text_while_typing():
    state = ListEditorState(kept={1}, filter_text="", cursor=0, filter_mode=True)

    next_state, outcome = apply_list_editor_key("d", items=["BigCorp"], state=state)

    assert outcome is None
    assert next_state == ListEditorState(kept={1}, filter_text="d", cursor=0, filter_mode=True)


def test_list_editor_state_move_key_moves_item_to_other_list():
    state = ListEditorState(kept={1, 2}, cursor=1)

    moved, outcome = cli_module.apply_list_editor_key("i", items=["BigCorp", "Linux Mint"], state=state, move_key="i")
    restored, restore_outcome = cli_module.apply_list_editor_key("i", items=["BigCorp", "Linux Mint"], state=moved, move_key="i")

    assert moved == ListEditorState(kept={1}, moved={2}, cursor=1)
    assert outcome is None
    assert restored == ListEditorState(kept={1, 2}, moved=set(), cursor=1)
    assert restore_outcome is None


def test_transferable_list_line_mode_returns_removed_and_moved_items():
    output = io.StringIO()

    result = cli_module.edit_transferable_list_items(
        ["ORG: BigCorp", "CONTEXT: Linux Mint"],
        title="Profile terms",
        move_key="m",
        move_label="move to ignore list",
        input_stream=io.StringIO("m 2\nr 1\nd\n"),
        output_stream=output,
    )

    assert result == cli_module.ListEditResult(removed=["ORG: BigCorp"], disabled=[], moved=["CONTEXT: Linux Mint"])
    assert "[m] CONTEXT: Linux Mint" in output.getvalue()


def test_list_editor_state_left_and_right_arrows_are_safe_noops():
    state = ListEditorState(kept={1, 2}, filter_text="", cursor=1, filter_mode=False)

    left_state, left_outcome = apply_list_editor_key("\x1b[D", items=["BigCorp", "Apollo"], state=state)
    right_state, right_outcome = apply_list_editor_key("\x1b[C", items=["BigCorp", "Apollo"], state=state)

    assert left_state == state
    assert right_state == state
    assert left_outcome is None
    assert right_outcome is None


def test_list_editor_state_filter_enter_and_escape_mode_contract():
    state = ListEditorState(kept={1, 2}, filter_text="ap", cursor=0, filter_mode=True)

    confirmed, confirm_outcome = apply_list_editor_key("\r", items=["BigCorp", "Apollo"], state=state)
    cleared, clear_outcome = apply_list_editor_key("\x1b", items=["BigCorp", "Apollo"], state=state)

    assert confirmed == ListEditorState(kept={1, 2}, filter_text="ap", cursor=0, filter_mode=False)
    assert cleared == ListEditorState(kept={1, 2}, filter_text="", cursor=0, filter_mode=False)
    assert confirm_outcome is None
    assert clear_outcome is None


def test_list_editor_state_escape_without_filter_does_not_jump_cursor():
    state = ListEditorState(kept={1, 2, 3}, filter_text="", cursor=2, filter_mode=False)

    next_state, outcome = apply_list_editor_key("\x1b", items=["BigCorp", "Apollo", "Zephyr"], state=state)

    assert next_state == state
    assert outcome is None


def test_list_editor_state_backspace_without_filter_does_not_jump_cursor():
    state = ListEditorState(kept={1, 2, 3}, filter_text="", cursor=2, filter_mode=False)

    next_state, outcome = apply_list_editor_key("\x7f", items=["BigCorp", "Apollo", "Zephyr"], state=state)

    assert next_state == state
    assert outcome is None


def test_list_editor_state_filter_backspace_without_text_does_not_jump_cursor():
    state = ListEditorState(kept={1, 2, 3}, filter_text="", cursor=2, filter_mode=True)

    next_state, outcome = apply_list_editor_key("\x7f", items=["BigCorp", "Apollo", "Zephyr"], state=state)

    assert next_state == state
    assert outcome is None


def test_list_editor_state_moves_cursor_and_toggles_visible_item():
    state = ListEditorState(kept={1, 2, 3})

    moved, move_outcome = apply_list_editor_key("\x1b[B", items=["BigCorp", "Apollo", "Zephyr"], state=state)
    toggled, toggle_outcome = apply_list_editor_key("\r", items=["BigCorp", "Apollo", "Zephyr"], state=moved)

    assert moved == ListEditorState(kept={1, 2, 3}, cursor=1)
    assert move_outcome is None
    assert toggled == ListEditorState(kept={1, 3}, cursor=1)
    assert toggle_outcome is None


def test_list_editor_state_filtered_toggle_uses_underlying_item_number():
    state = ListEditorState(kept={1, 2, 3}, filter_text="apollo", cursor=0, filter_mode=False)

    toggled, outcome = apply_list_editor_key("\r", items=["BigCorp", "Apollo", "Zephyr"], state=state)

    assert toggled == ListEditorState(kept={1, 3}, filter_text="apollo", cursor=0, filter_mode=False)
    assert outcome is None


def test_list_editor_state_accept_and_cancel_outcomes_do_not_mutate_state():
    state = ListEditorState(kept={1}, cursor=0)

    accepted, accept_outcome = apply_list_editor_key("d", items=["BigCorp"], state=state)
    cancelled, cancel_outcome = apply_list_editor_key("q", items=["BigCorp"], state=state)

    assert accepted == state
    assert cancelled == state
    assert accept_outcome == "accept"
    assert cancel_outcome == "cancel"


def test_read_review_key_accepts_single_escape_without_waiting_for_sequence():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x1b")
        os.close(write_fd)
        write_fd = -1
        with os.fdopen(read_fd, "r", encoding="utf-8") as input_stream:
            assert _read_review_key(input_stream) == "\x1b"
    finally:
        if write_fd != -1:
            os.close(write_fd)


def test_read_review_key_keeps_arrow_escape_sequence_together():
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, b"\x1b[A")
        os.close(write_fd)
        write_fd = -1
        with os.fdopen(read_fd, "r", encoding="utf-8") as input_stream:
            assert _read_review_key(input_stream) == "\x1b[A"
    finally:
        if write_fd != -1:
            os.close(write_fd)


def _drain_pty(master_fd: int, *, timeout: float = 0.2) -> str:
    chunks: list[bytes] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ready, _, _ = select.select([master_fd], [], [], 0.02)
        if not ready:
            continue
        try:
            data = os.read(master_fd, 4096)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    return b"".join(chunks).decode("utf-8", errors="replace")


def test_session_list_screen_uses_list_editor_chrome_and_selects_with_arrows(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]
    subprocess.run(base + ["--session-select", "lab-maritiniad"], text=True, capture_output=True, check=True)
    subprocess.run(
        base,
        input="Open https://martini.example.test/login\n",
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(base + ["--session-select", "lab-odyssey"], text=True, capture_output=True, check=True)
    subprocess.run(
        base,
        input="Open https://odyssey.example.test/login and api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(base + ["--session-select", "lab-maritiniad"], text=True, capture_output=True, check=True)

    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            base + ["--session-list"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\x1b[B")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\r")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert "[↑/↓] move  [Enter/Space] select" in output
    assert "filter: -" in output
    assert ">  1. [x] lab-maritiniad: HOST=1" in output
    assert "   2. [ ] lab-odyssey: HOST=1 TOKEN=1" in output
    assert "active session set: lab-odyssey" in output
    assert state_file.read_text(encoding="utf-8") == "lab-odyssey\n"


def test_profile_list_screen_uses_list_editor_chrome_and_selects_with_arrows(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state_file = tmp_path / "current-profile"
    init_profile("default", config_path=config)
    init_profile("lab-maritiniad", config_path=config)
    cli_module.select_profile("default", config_path=config, state_file=state_file)
    base = [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state_file)]

    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            base + ["--profile-list"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\x1b[B")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\r")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert "[↑/↓] move  [Enter/Space] select" in output
    assert ">  1. [x] default" in output
    assert "   2. [ ] lab-maritiniad" in output
    assert "selected profile: lab-maritiniad" in output
    assert state_file.read_text(encoding="utf-8") == "lab-maritiniad\n"


def test_terms_list_screen_fragmented_left_arrow_does_not_accept(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    terms_file.write_text("ORG: BigCorp\nPROJECT: Apollo\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--profile-term-list",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)

        os.write(master_fd, b"\r")  # uncheck the highlighted first item
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\x1b")
        time.sleep(0.05)
        os.write(master_fd, b"[D")  # delayed Left Arrow tail must not become [d] accept
        output += _drain_pty(master_fd)

        if proc.poll() is None:
            os.write(master_fd, b"q")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 130
    assert "removed" not in output
    assert terms_file.read_text(encoding="utf-8") == "ORG: BigCorp\nPROJECT: Apollo\n"


def test_terms_list_screen_down_arrow_moves_before_toggle(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    terms_file.write_text("ORG: BigCorp\nPROJECT: Apollo\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--profile-term-list",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)

        os.write(master_fd, b"\x1b[B")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\r")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert "removed 0 profile term(s)" in output
    assert "disabled 1 profile term(s)" in output
    assert terms_file.read_text(encoding="utf-8") == "# disabled: ORG: BigCorp\nPROJECT: Apollo\n"


def test_terms_list_screen_can_move_term_to_ignore_list(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    ignored_file = profile_path / "ignored-suggestions.txt"
    terms_file.write_text("ORG: AlphaCo\nCONTEXT: BetaBox\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--profile-term-list",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\x1b[B")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"m")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert "[m] move to ignore list" in output
    assert ">  2. [m] CONTEXT: BetaBox" in output
    assert "moved 1 profile term(s) to ignored AI suggestions (1 new)" in output
    assert terms_file.read_text(encoding="utf-8") == "ORG: AlphaCo\n"
    assert ignored_file.read_text(encoding="utf-8") == "CONTEXT: BetaBox\n"


def test_terms_list_screen_can_change_existing_term_detector(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    terms_file.write_text("ORG: AlphaCo\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--profile-term-list",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)
        os.write(master_fd, b"c")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert "[c] change detector" in output
    assert ">  1. [x] PROJECT: AlphaCo" in output
    assert "updated 1 profile term detector(s)" in output
    assert terms_file.read_text(encoding="utf-8") == "PROJECT: AlphaCo\n"


def test_terms_list_screen_cancel_does_not_move_term(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    ignored_file = profile_path / "ignored-suggestions.txt"
    terms_file.write_text("ORG: AlphaCo\nCONTEXT: BetaBox\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--profile-term-list",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        _drain_pty(master_fd)
        os.write(master_fd, b"\x1b[B")
        _drain_pty(master_fd)
        os.write(master_fd, b"m")
        _drain_pty(master_fd)
        os.write(master_fd, b"q")
        _drain_pty(master_fd)
        proc.wait(timeout=10)
        _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 130
    assert terms_file.read_text(encoding="utf-8") == "ORG: AlphaCo\nCONTEXT: BetaBox\n"
    assert ignored_file.read_text(encoding="utf-8") == ""


def test_cli_can_list_and_remove_ignore_list_terms(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    ignored_file = profile_path / "ignored-suggestions.txt"
    ignored_file.write_text("PERSON: Tuesday room\nCONTEXT: Blue Team Room\n", encoding="utf-8")

    list_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--ignore-list",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    remove_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--ignore-remove",
            "Tuesday room",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert list_proc.stdout == "[x] CONTEXT: Blue Team Room\n[x] PERSON: Tuesday room\n"
    assert remove_proc.stdout == "removed 1 ignored AI suggestion term(s)\n"
    assert ignored_file.read_text(encoding="utf-8") == "CONTEXT: Blue Team Room\n"


def test_cli_can_interactively_edit_ignore_list(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    ignored_file = profile_path / "ignored-suggestions.txt"
    ignored_file.write_text("CONTEXT: Blue Team Room\nPERSON: Tuesday room\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--ignore-list",
            ],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave_fd)
        os.write(master_fd, b"/Tues\nr 2\nd\n")
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        os.close(master_fd)

    assert proc.returncode == 0
    assert stdout == "removed 1 ignored AI suggestion term(s)\n"
    assert "Tuesday room" in stderr
    assert ignored_file.read_text(encoding="utf-8") == "CONTEXT: Blue Team Room\n"


def test_ignore_list_screen_can_move_ignored_term_to_terms_list(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    ignored_file = profile_path / "ignored-suggestions.txt"
    ignored_file.write_text("CONTEXT: Domain root DN\nORG: Linux Mint\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--ignore-list",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)
        os.write(master_fd, b"\x1b[B")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"m")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert "[m] move to terms list" in output
    assert ">  2. [m] ORG: Linux Mint" in output
    assert "moved 1 ignored AI suggestion term(s) to profile terms (1 new)" in output
    assert terms_file.read_text(encoding="utf-8") == "ORG: Linux Mint\n"
    assert ignored_file.read_text(encoding="utf-8") == "CONTEXT: Domain root DN\n"


def test_ignore_list_screen_can_change_detector_before_moving_to_terms_list(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    ignored_file = profile_path / "ignored-suggestions.txt"
    ignored_file.write_text("ORG: Linux Mint\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--ignore-list",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)
        os.write(master_fd, b"c")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"m")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        output += _drain_pty(master_fd)
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    assert proc.returncode == 0
    assert ">  1. [m] PROJECT: Linux Mint" in output
    assert terms_file.read_text(encoding="utf-8") == "PROJECT: Linux Mint\n"
    assert ignored_file.read_text(encoding="utf-8") == ""


def test_cli_can_remove_profile_terms_with_terms_list(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    terms_file.write_text("ORG: BigCorp\nPROJECT: Apollo\n", encoding="utf-8")

    list_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--profile-term-list",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    remove_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--profile-term-remove",
            "BigCorp",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    assert list_proc.stdout == "[x] PROJECT: Apollo\n[x] ORG: BigCorp\n"
    assert remove_proc.stdout == "removed 1 profile term(s)\n"
    assert terms_file.read_text(encoding="utf-8") == "PROJECT: Apollo\n"


def test_cli_category_list_can_disable_profile_category_and_redaction_reports_it(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    init_profile("smoke", config_path=config)

    list_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--detector-list",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    disable_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--detector-list",
            "--detector-disable",
            "PATH",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    redact_proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--summary",
        ],
        input="Email jane.doe@example.com path /home/exampleuser/client/raw.txt\n",
        text=True,
        capture_output=True,
        check=True,
    )

    parser = configparser.ConfigParser()
    parser.read(config, encoding="utf-8")
    assert "Built-in detectors for profile: smoke" in list_proc.stdout
    assert "[x] EMAIL" in list_proc.stdout
    assert "email addresses" in list_proc.stdout
    assert "[x] PATH" in list_proc.stdout
    assert disable_proc.stdout == "disabled detector PATH for profile: smoke\n"
    assert parser["smoke"]["disabled_categories"] == "PATH"
    assert redact_proc.stdout == "Email [EMAIL_1] path /home/exampleuser/client/raw.txt\n"
    assert "summary: EMAIL=1 warnings=possible local path remains disabled_categories=PATH" in redact_proc.stderr


def test_cli_category_list_requires_known_category(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    init_profile("smoke", config_path=config)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--detector-list",
            "--detector-disable",
            "BANANA",
        ],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "unknown detector: BANANA" in proc.stderr


def test_cli_manual_detector_add_list_enable_disable_and_remove(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    init_profile("smoke", config_path=config)
    base = [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state), "--profile", "smoke"]

    add_proc = subprocess.run(
        base + ["--manual-detector-add"],
        input="plate number\nORG\ninvalid!!!\n",
        text=True,
        capture_output=True,
        check=True,
    )
    list_proc = subprocess.run(base + ["--manual-detector-list"], text=True, capture_output=True, check=True)
    disable_proc = subprocess.run(
        base + ["--manual-detector-disable", "plate-number"], text=True, capture_output=True, check=True
    )
    disabled_list_proc = subprocess.run(base + ["--manual-detector-list"], text=True, capture_output=True, check=True)
    enable_proc = subprocess.run(
        base + ["--manual-detector-enable", "plate number"], text=True, capture_output=True, check=True
    )
    remove_proc = subprocess.run(
        base + ["--manual-detector-remove", "PLATE_NUMBER"], text=True, capture_output=True, check=True
    )
    removed_list_proc = subprocess.run(base + ["--manual-detector-list"], text=True, capture_output=True, check=True)

    parser = configparser.ConfigParser()
    parser.read(config, encoding="utf-8")
    assert add_proc.stdout == "added 1 manual detector(s) to profile: smoke\n"
    assert "[x] CLIENT: [CLIENT_N]" in list_proc.stdout
    assert "[x] PLATE NUMBER: [PLATE_NUMBER_N]" in list_proc.stdout
    assert disable_proc.stdout == "disabled manual detector PLATE NUMBER for profile: smoke\n"
    assert "[ ] PLATE NUMBER: [PLATE_NUMBER_N]" in disabled_list_proc.stdout
    assert enable_proc.stdout == "enabled manual detector PLATE NUMBER for profile: smoke\n"
    assert remove_proc.stdout == "removed 1 manual detector(s) from profile: smoke\n"
    assert "PLATE NUMBER" not in removed_list_proc.stdout
    assert parser["smoke"].get("manual_detectors", "") == ""


def test_cli_manual_detector_remove_rejects_default_detector(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    init_profile("smoke", config_path=config)

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--manual-detector-remove",
            "CLIENT",
        ],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "default manual detector cannot be removed: CLIENT" in proc.stderr


def test_cli_term_add_tty_can_assign_custom_manual_detector(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_path = profile_path / "terms.txt"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--manual-detector-add",
        ],
        input="plate number\n",
        text=True,
        capture_output=True,
        check=True,
    )
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--profile-term-add",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, b"EV7878\n\x04")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"cccccc")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    redact_proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state), "--profile", "smoke"],
        input="Plate EV7878 seen in the lab\n",
        text=True,
        capture_output=True,
        check=True,
    )
    assert proc.returncode == 0
    assert "Manual detectors: CLIENT, PERSON, ORG, PROJECT, LOCATION, CONTEXT, PLATE NUMBER" in output
    assert ">  1. [x] PLATE_NUMBER: EV7878" in output
    assert terms_path.read_text(encoding="utf-8") == "PLATE_NUMBER: EV7878\n"
    assert redact_proc.stdout == "Plate [PLATE_NUMBER_1] seen in the lab\n"


def test_cli_can_interactively_edit_profile_terms(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    terms_file = profile_path / "terms.txt"
    terms_file.write_text("ORG: BigCorp\nPROJECT: Apollo\n", encoding="utf-8")
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
                "--profile",
                "smoke",
                "--profile-term-list",
            ],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        os.close(slave_fd)
        os.write(master_fd, b"r 1\nd\n")
        stdout, stderr = proc.communicate(timeout=10)
    finally:
        os.close(master_fd)

    assert proc.returncode == 0
    assert stdout == "removed 1 profile term(s)\n"
    assert "PROJECT: Apollo" in stderr
    assert terms_file.read_text(encoding="utf-8") == "ORG: BigCorp\n"


def test_cli_apply_profile_requires_ai_suggest_review():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--apply-profile"],
        input="Email jane.doe@example.com\n",
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "unrecognized arguments: --apply-profile" in proc.stderr
    assert "jane.doe@example.com" not in proc.stderr


def test_cli_check_only_returns_nonzero_when_residual_sensitive_text_remains():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--check-only", "--no-redact"],
        input="Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n",
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 1
    assert proc.stdout == ""
    assert "possible token" in proc.stderr.lower()


def test_cli_redacts_url_host_but_preserves_path_and_sanitized_query():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli"],
        input="GET https://app.example.test/api/v1/users?email=jane.doe@example.com&ticket=123\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "GET https://[HOST_1]/api/v1/users?email=[EMAIL_1]&ticket=123\n"


def test_url_redaction_preserves_markdown_code_backticks():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli"],
        input="Base URL: `https://api.example.test/me`\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "Base URL: `https://[HOST_1]/me`\n"
    assert "api.example.test" not in proc.stdout


def test_token_and_path_redaction_preserves_markdown_code_backticks():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli"],
        input=(
            "Header: `Authorization: Bearer synthetic-token-value-12345`\n"
            "Secret: `api_key=synthetic-api-key-value-12345`\n"
            "Cookie: `sessionid=synthetic-session-value-12345`\n"
            "Path: `/home/alex/client/report.txt`\n"
        ),
        text=True,
        capture_output=True,
        check=True,
    )

    assert "Header: `Authorization: Bearer [TOKEN_1]`" in proc.stdout
    assert "Secret: `api_key=[TOKEN_2]`" in proc.stdout
    assert "Cookie: `sessionid=[COOKIE_1]`" in proc.stdout
    assert "Path: `[PATH_1]`" in proc.stdout
    assert "synthetic-token-value" not in proc.stdout
    assert "synthetic-api-key-value" not in proc.stdout
    assert "synthetic-session-value" not in proc.stdout
    assert "/home/alex/client/report.txt" not in proc.stdout


def test_cli_redacts_expanded_secret_token_pack_from_stdin():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli"],
        input="api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "api_key=[TOKEN_1]\n"
    assert "synthetic-api-key-value-12345" not in proc.stdout
    assert proc.stderr == ""


def test_cli_preserves_python_dunder_attribute_chain_that_looks_like_discord_token():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli"],
        input='**{{ get_flashed_messages.__globals__.__builtins__.open("/etc/passwd").read() }}**\n',
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == '**{{ get_flashed_messages.__globals__.__builtins__.open("/etc/passwd").read() }}**\n'
    assert "[TOKEN_" not in proc.stdout
    assert proc.stderr == ""


def test_cli_still_redacts_discord_shaped_tokens_after_dunder_false_positive_fix():
    token = f"{'A' * 24}.{'b' * 6}.{'C' * 27}"
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli"],
        input=f"discord token {token}\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "discord token [TOKEN_1]\n"
    assert token not in proc.stdout
    assert proc.stderr == ""


def test_cli_select_session_makes_plain_redact_reuse_stable_mapping(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"

    select = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--session-state-file",
            str(state_file),
            "--session-dir",
            str(session_dir),
            "--session-select",
            "acme-webapp",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    first = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)],
        input="Open https://app.example.test/login and api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )
    second = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)],
        input="Open https://app.example.test/login then https://app.example.test/admin\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert select.stdout == "active session set: acme-webapp\n"
    assert state_file.read_text(encoding="utf-8") == "acme-webapp\n"
    assert first.stdout == "Open https://[HOST_1]/login and api_key=[TOKEN_1]\n"
    assert second.stdout == "Open https://[HOST_1]/login then https://[HOST_1]/admin\n"
    assert "session: acme-webapp" in first.stderr
    assert "session: acme-webapp" in second.stderr


def test_cli_init_session_creates_and_selects_session_for_future_runs(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]

    init = subprocess.run(base + ["--session-init", "acme-webapp"], text=True, capture_output=True, check=True)
    redacted = subprocess.run(
        base,
        input="Open https://app.example.test/login\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert init.stdout == f"initialized session: acme-webapp ({session_dir / 'acme-webapp.json'})\nactive session set: acme-webapp\n"
    assert state_file.read_text(encoding="utf-8") == "acme-webapp\n"
    assert oct((session_dir / "acme-webapp.json").stat().st_mode & 0o777) == "0o600"
    assert redacted.stdout == "Open https://[HOST_1]/login\n"
    assert "session: acme-webapp" in redacted.stderr


def test_cli_set_and_unset_session_aliases_control_plain_redact_session(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]

    set_session = subprocess.run(base + ["--session-select", "acme-webapp"], text=True, capture_output=True, check=True)
    assert set_session.stdout == "active session set: acme-webapp\n"
    assert state_file.read_text(encoding="utf-8") == "acme-webapp\n"

    first = subprocess.run(
        base,
        input="Open https://app.example.test/login\n",
        text=True,
        capture_output=True,
        check=True,
    )
    unset_session = subprocess.run(base + ["--session-clear"], text=True, capture_output=True, check=True)
    after_unset = subprocess.run(
        base,
        input="Open https://app.example.test/admin\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert first.stdout == "Open https://[HOST_1]/login\n"
    assert "session: acme-webapp" in first.stderr
    assert unset_session.stdout == "active session cleared\n"
    assert not state_file.exists()
    assert after_unset.stdout == "Open https://[HOST_1]/admin\n"
    assert after_unset.stderr == ""


def test_cli_session_tools_status_show_list_clear_and_forget(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]

    subprocess.run(base + ["--session-select", "acme-webapp"], text=True, capture_output=True, check=True)
    subprocess.run(
        base,
        input="Open https://app.example.test/login and api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )
    status = subprocess.run(base + ["--session-status"], text=True, capture_output=True, check=True)
    show_host = subprocess.run(base + ["--show-secret", "HOST_1"], text=True, capture_output=True, check=True)
    list_sessions = subprocess.run(base + ["--session-list"], text=True, capture_output=True, check=True)
    clear = subprocess.run(base + ["--session-clear"], text=True, capture_output=True, check=True)
    after_clear = subprocess.run(
        base,
        input="Open https://app.example.test/login\n",
        text=True,
        capture_output=True,
        check=True,
    )
    forget = subprocess.run(base + ["--session-delete", "acme-webapp"], text=True, capture_output=True, check=True)

    assert status.stdout == "HOST=1 TOKEN=1\n"
    assert status.stderr == "session: acme-webapp\n"
    assert show_host.stdout == "[HOST_1] = app.example.test\n"
    assert "synthetic-api-key-value-12345" not in status.stdout
    assert "acme-webapp: HOST=1 TOKEN=1" in list_sessions.stdout
    assert clear.stdout == "active session cleared\n"
    assert after_clear.stdout == "Open https://[HOST_1]/login\n"
    assert after_clear.stderr == ""
    assert forget.stdout == "deleted session: acme-webapp\n"


def test_cli_help_groups_everyday_workflows_and_start_here_examples():
    proc = subprocess.run([sys.executable, "-m", "redactor.cli", "--help"], text=True, capture_output=True, check=True)

    assert "$ redloc" in proc.stdout
    assert "local-first redaction for operator notes" in proc.stdout
    assert "Redact sensitive text before you paste or share it." in proc.stdout
    assert "  --about" in proc.stdout.split("Everyday redaction:", 1)[0]
    assert "START HERE" in proc.stdout
    assert "Status: alpha. Redaction is not a guarantee" in proc.stdout
    assert "redloc --summary" in proc.stdout
    assert "Paste text, then finish input" in proc.stdout
    assert "Redacted text goes to stdout; counts and warnings go to stderr" in proc.stdout
    assert "Everyday redaction" in proc.stdout
    assert "Profiles and reusable terms" in proc.stdout
    assert "Stable labels / sessions" in proc.stdout
    assert "Review and automation" in proc.stdout
    assert "Advanced paths/settings" in proc.stdout
    assert "--session-select" in proc.stdout
    assert "--session-init" in proc.stdout
    assert "--session-clear" in proc.stdout
    assert "--profile-term-add" in proc.stdout
    assert "--profile-term-list" in proc.stdout
    assert "--profile-term-remove" in proc.stdout
    assert "--ignore-add" in proc.stdout
    assert "--ignore-remove" in proc.stdout
    assert "--ignore-file" in proc.stdout
    assert "--term-file-template" in proc.stdout
    assert "--add-term" not in proc.stdout
    assert "--terms-list" not in proc.stdout
    assert "--remove-term" not in proc.stdout
    assert "--detector-list" in proc.stdout
    assert "--ai-config-set" in proc.stdout
    assert "--ai-config-status" in proc.stdout
    assert "matched values" in proc.stdout
    assert "are saved locally" in proc.stdout
    assert "send about CHARS characters" in proc.stdout
    assert "default 8000" in proc.stdout


def test_cli_about_prints_redloc_wordmark_without_stdin_work():
    proc = subprocess.run([sys.executable, "-m", "redactor.cli", "--about"], text=True, capture_output=True, check=True)

    assert " _ __ ___  __| | | ___   ___" in proc.stdout
    assert "local-first redaction for operator notes" in proc.stdout
    assert "Local-first CLI for sanitizing logs, notes, HTTP snippets," in proc.stdout
    assert "paths, tokens, client names, and other sensitive text before sharing." in proc.stdout
    assert "Author: Brian Brandson" in proc.stdout
    assert "License: Apache-2.0" in proc.stdout
    assert proc.stderr == ""


def test_cli_help_uses_operator_facing_metavars_instead_of_dest_names():
    proc = subprocess.run([sys.executable, "-m", "redactor.cli", "--help"], text=True, capture_output=True, check=True)

    assert "--profile NAME" in proc.stdout
    assert "--profile-init NAME" in proc.stdout
    assert "--profile-select NAME" in proc.stdout
    assert "--profile-term-remove TERM" in proc.stdout
    assert "--ignore-remove TERM" in proc.stdout
    assert "--detector-disable DETECTOR" in proc.stdout
    assert "--manual-detector-remove DETECTOR" in proc.stdout
    assert "--ai-endpoint URL" in proc.stdout
    assert "--session-init NAME" in proc.stdout
    assert "--session-select NAME" in proc.stdout
    assert "--show-secret PLACEHOLDER" in proc.stdout
    assert "--show-secret-all" in proc.stdout
    assert "--session-show PLACEHOLDER" not in proc.stdout
    assert "INIT_PROFILE" not in proc.stdout
    assert "SELECT_PROFILE" not in proc.stdout
    assert "TERM_REMOVE" not in proc.stdout
    assert "MANUAL_DETECTOR_REMOVE" not in proc.stdout
    assert "AI_ENDPOINT" not in proc.stdout
    assert "INIT_SESSION" not in proc.stdout
    assert "SELECT_SESSION" not in proc.stdout
    assert "[NAME]" not in proc.stdout
    assert "[FILE]" not in proc.stdout


def test_cli_help_keeps_common_long_options_on_same_line_as_help_text():
    proc = subprocess.run([sys.executable, "-m", "redactor.cli", "--help"], text=True, capture_output=True, check=True)

    for option, description_start in [
        ("--term-file-template FILE", "print a categorized-term template"),
        ("--profile-select NAME", "make NAME the default profile"),
        ("--show-secret PLACEHOLDER", "reveal the local value behind"),
        ("--ai-chunk-lines LINES", "send at most LINES lines"),
        ("--session-state-file FILE", "store the default session in FILE"),
    ]:
        assert f"  {option}\n" not in proc.stdout
        assert any(option in line and description_start in line for line in proc.stdout.splitlines())


def test_cli_help_orders_related_flag_families_together():
    proc = subprocess.run([sys.executable, "-m", "redactor.cli", "--help"], text=True, capture_output=True, check=True)
    help_text = proc.stdout

    assert help_text.index("Profiles and reusable terms:") < help_text.index("Stable labels / sessions:")
    assert help_text.index("Stable labels / sessions:") < help_text.index("Reveal local session secrets:")
    assert help_text.index("Reveal local session secrets:") < help_text.index("Review and automation:")

    assert help_text.index("--profile-term-add") < help_text.index("--profile-term-list")
    assert help_text.index("--profile-term-list") < help_text.index("--profile-term-remove TERM")
    assert help_text.index("--profile-term-remove TERM") < help_text.index("--global-term-add")
    assert help_text.index("--global-term-add") < help_text.index("--global-term-list")
    assert help_text.index("--global-term-list") < help_text.index("--global-term-remove TERM")

    assert help_text.index("--session-status") < help_text.index("--session-delete NAME")
    assert help_text.index("--session-delete NAME") < help_text.index("--show-secret PLACEHOLDER")
    assert help_text.index("--show-secret PLACEHOLDER") < help_text.index("--show-secret-all")



def test_cli_tty_prompts_for_paste_input_without_polluting_stdout():
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "redactor.cli"],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, b"Email jane.doe@example.com\n\x04")
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)

    assert proc.returncode == 0
    assert stdout == "Email [EMAIL_1]\n"
    assert "Paste text to redact" in stderr
    assert "Redacted text prints to stdout" in stderr


def test_cli_tty_single_ctrl_d_after_partial_line_redacts_and_separates_status(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    init_profile("lab-odyssey", config_path=config)
    cli_module.select_profile("lab-odyssey", config_path=config, state_file=state)
    master_fd, slave_fd = pty.openpty()
    output = ""
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        prompt_deadline = time.time() + 5
        while time.time() < prompt_deadline and "Redacted text prints to stdout" not in output:
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                output += os.read(master_fd, 4096).decode("utf-8", errors="replace")
        os.write(master_fd, b"10.10.10.25 is IP of my PC\x04")
        deadline = time.time() + 5
        while time.time() < deadline and proc.poll() is None:
            ready, _, _ = select.select([master_fd], [], [], 0.1)
            if ready:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk.decode("utf-8", errors="replace")
        proc.wait(timeout=1)
        while True:
            ready, _, _ = select.select([master_fd], [], [], 0.05)
            if not ready:
                break
            try:
                chunk = os.read(master_fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk.decode("utf-8", errors="replace")
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)
        if proc is not None and proc.poll() is None:
            proc.kill()

    normalized = output.replace("\r\n", "\n")
    assert proc is not None
    assert proc.returncode == 0
    assert "10.10.10.25 is IP of my PCprofile:" not in normalized
    assert "10.10.10.25 is IP of my PC\nprofile: lab-odyssey" in normalized
    assert "[INTERNAL_IP_1] is IP of my PC" in normalized
    assert normalized.endswith("[INTERNAL_IP_1] is IP of my PC\n")


def test_cli_tty_newline_does_not_submit_bare_paste_before_ctrl_d(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    init_profile("lab-odyssey", config_path=config)
    cli_module.select_profile("lab-odyssey", config_path=config, state_file=state)
    master_fd, slave_fd = pty.openpty()
    output = ""
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--config",
                str(config),
                "--state-file",
                str(state),
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        output += _drain_pty(master_fd)
        os.write(master_fd, b"Scanning 10.1.103.217 [4 ports]\n")
        output += _drain_pty(master_fd, timeout=0.2)
        assert proc.poll() is None
        assert "Redacted text:" not in output

        os.write(master_fd, b"Completed SYN Stealth Scan at 15:16, 0.29s elapsed (2 total ports)\x04")
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)
        if proc is not None and proc.poll() is None:
            proc.kill()

    normalized = output.replace("\r\n", "\n")
    assert proc is not None
    assert proc.returncode == 0
    assert "Scanning [INTERNAL_IP_1] [4 ports]" in normalized
    assert "Completed SYN Stealth Scan at 15:16, 0.29s elapsed (2 total ports)" in normalized


def test_cli_tty_prompts_for_add_term_destination(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    master_fd, slave_fd = pty.openpty()
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile-term-add"],
            stdin=slave_fd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            close_fds=True,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, b"ExampleCo\n\x04")
        stdout, stderr = proc.communicate(timeout=5)
    finally:
        os.close(master_fd)
        if slave_fd != -1:
            os.close(slave_fd)

    terms_path = config.parent / "profiles" / "default" / "terms.txt"
    assert proc.returncode == 0
    assert stdout == f"added 1 term(s) to {terms_path}\n"
    assert "Add one manual term per line" in stderr
    assert f"Terms will be saved to: {terms_path}" in stderr
    assert terms_path.read_text(encoding="utf-8") == "CLIENT: ExampleCo\n"


def test_cli_term_add_tty_can_change_category_before_accepting(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile-term-add"],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, b"Boobies\n10.0.0.5\n\x04")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"c")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)

    terms_path = config.parent / "profiles" / "default" / "terms.txt"
    assert proc.returncode == 0
    assert "Profile term review" in output
    assert "[c] change manual detector" in output
    assert "skipped 10.0.0.5: IP addresses are already redacted by deterministic IP detectors" in output
    assert ">  1. [x] PERSON: Boobies" in output
    assert terms_path.read_text(encoding="utf-8") == "PERSON: Boobies\n"


def test_cli_ignore_add_appends_to_profile_ignore_list(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    profile_path = init_profile("smoke", config_path=config)
    ignored_file = profile_path / "ignored-suggestions.txt"
    ignored_file.write_text("Tuesday room\n", encoding="utf-8")

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile",
            "smoke",
            "--ignore-add",
        ],
        input="Linux Mint\nTuesday room\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == f"added 1 ignored AI suggestion term(s) to {ignored_file}\n"
    assert ignored_file.read_text(encoding="utf-8") == "Tuesday room\nUNASSIGNED: Linux Mint\n"


def test_cli_ignore_file_filters_one_shot_ai_suggestions(tmp_path: Path):
    ignored_file = tmp_path / "ignored.txt"
    ignored_file.write_text("Linux Mint\n", encoding="utf-8")
    FakeAIHandler.response_payload = {
        "choices": [
            {
                "message": {
                    "content": json.dumps(
                        {
                            "candidates": [
                                {"term": "Linux Mint", "category": "context", "lines": [1], "confidence": "high"},
                                {"term": "Project Apollo", "category": "project", "lines": [1], "confidence": "high"},
                            ]
                        }
                    )
                }
            }
        ]
    }
    server = run_fake_ai_server()
    endpoint = f"http://127.0.0.1:{server.server_port}/v1/chat/completions"
    master_fd, slave_fd = pty.openpty()
    output = ""
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "redactor.cli",
                "--ai-suggest",
                "--ignore-file",
                str(ignored_file),
                "--ai-endpoint",
                endpoint,
                "--ai-model",
                "fake-model",
            ],
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            text=False,
        )
        os.close(slave_fd)
        slave_fd = -1
        os.write(master_fd, b"[CLIENT_1] uses Linux Mint for Project Apollo\n\x04")
        output += _drain_pty(master_fd)
        os.write(master_fd, b"d")
        proc.wait(timeout=10)
        output += _drain_pty(master_fd)
    finally:
        if slave_fd != -1:
            os.close(slave_fd)
        os.close(master_fd)
        server.shutdown()

    assert proc.returncode == 0
    assert "Project Apollo" in output
    assert "Project Apollo  project  lines 1  high" in output
    assert "Linux Mint  context  lines 1  high" not in output


def test_cli_term_file_template_prints_and_writes(tmp_path: Path):
    template_path = tmp_path / "terms-template.txt"

    printed = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--term-file-template"],
        text=True,
        capture_output=True,
        check=True,
    )
    written = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--term-file-template", str(template_path)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert "# Format: CATEGORY: term" in printed.stdout
    assert "ORG: ExampleCo" in printed.stdout
    assert written.stdout == f"wrote term-file template to {template_path}\n"
    assert template_path.read_text(encoding="utf-8") == printed.stdout


def test_cli_rejects_removed_term_flag_aliases():
    for flag in ("--add-term", "--terms-list", "--remove-term"):
        proc = subprocess.run([sys.executable, "-m", "redactor.cli", flag], text=True, capture_output=True)
        assert proc.returncode == 2
        assert "unrecognized arguments" in proc.stderr


def test_cli_interactive_redacts_multiple_blank_line_submitted_chunks_with_stable_in_memory_labels():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--interactive"],
        input=(
            "Open https://app.example.test/login\n"
            "\n"
            "Open https://app.example.test/login then https://app.example.test/admin\n"
            "\n"
            "quit\n"
        ),
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == (
        "--- redacted 1 ---\n"
        "Open https://[HOST_1]/login\n"
        "--- redacted 2 ---\n"
        "Open https://[HOST_1]/login then https://[HOST_1]/admin\n"
    )
    assert "interactive mode" in proc.stderr
    assert "Paste text, then press Enter on a blank line to redact" in proc.stderr
    assert "Type q or quit, then Enter, to exit" in proc.stderr
    assert "https://app.example.test" not in proc.stdout


def test_cli_stay_open_alias_and_auto_out_save_each_interactive_chunk(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--interactive", "--auto-out"],
        input="Email jane.doe@example.com\n\nq\n",
        text=True,
        capture_output=True,
        check=True,
    )

    outputs = list((config.parent / "profiles" / "default" / "redacted").glob("paste-redacted-*.txt"))
    assert len(outputs) == 1
    assert proc.stdout == ""
    assert proc.stderr.endswith(f"wrote redacted output to {outputs[0]}\n")
    assert "interactive mode" in proc.stderr
    assert outputs[0].read_text(encoding="utf-8") == "Email [EMAIL_1]\n"
    assert "jane.doe@example.com" not in outputs[0].read_text(encoding="utf-8")


def test_cli_show_secret_all_lists_every_session_mapping_with_values(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]

    subprocess.run(base + ["--session-select", "acme-webapp"], text=True, capture_output=True, check=True)
    subprocess.run(
        base,
        input="Open https://app.example.test/login as jane.doe@example.com with api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )
    shown = subprocess.run(base + ["--show-secret-all"], text=True, capture_output=True, check=True)

    assert shown.stderr == "session: acme-webapp\n"
    assert "[HOST_1] = app.example.test" in shown.stdout
    assert "[EMAIL_1] = jane.doe@example.com" in shown.stdout
    assert "[TOKEN_1] = synthetic-api-key-value-12345" in shown.stdout


def test_cli_rejects_removed_session_show_aliases(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]

    subprocess.run(base + ["--session-select", "acme-webapp"], text=True, capture_output=True, check=True)
    subprocess.run(
        base,
        input="api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )
    old_single = subprocess.run(base + ["--session-show", "TOKEN_1"], text=True, capture_output=True)
    old_bulk = subprocess.run(base + ["--session-show-secrets"], text=True, capture_output=True)

    assert old_single.returncode == 2
    assert old_bulk.returncode == 2
    assert "unrecognized arguments: --session-show" in old_single.stderr
    assert "unrecognized arguments: --session-show-secrets" in old_bulk.stderr
    assert "synthetic-api-key-value-12345" not in old_single.stdout + old_single.stderr
    assert "synthetic-api-key-value-12345" not in old_bulk.stdout + old_bulk.stderr


def test_cli_refuses_single_and_bulk_secret_reveal_together(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--session-state-file",
            str(state_file),
            "--session-dir",
            str(session_dir),
            "--show-secret",
            "HOST_1",
            "--show-secret-all",
        ],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "use either --show-secret or --show-secret-all" in proc.stderr


def test_cli_session_mapping_file_is_user_only(tmp_path: Path):
    state_file = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"
    base = [sys.executable, "-m", "redactor.cli", "--session-state-file", str(state_file), "--session-dir", str(session_dir)]

    subprocess.run(base + ["--session-select", "acme-webapp"], text=True, capture_output=True, check=True)

    assert oct((session_dir / "acme-webapp.json").stat().st_mode & 0o777) == "0o600"


def test_cli_file_mode_reads_input_file_and_writes_output_file(tmp_path: Path):
    raw = tmp_path / "raw.log"
    redacted = tmp_path / "redacted.log"
    raw.write_text("Email jane.doe@example.com api_key=synthetic-api-key-value-12345\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--in", str(raw), "--out", str(redacted), "--summary"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == ""
    assert redacted.read_text(encoding="utf-8") == "Email [EMAIL_1] api_key=[TOKEN_1]\n"
    assert proc.stderr == "summary: EMAIL=1 TOKEN=1 warnings=none\n"
    assert raw.read_text(encoding="utf-8") == "Email jane.doe@example.com api_key=synthetic-api-key-value-12345\n"


def test_cli_file_mode_out_directory_derives_redacted_filename(tmp_path: Path):
    raw = tmp_path / "Techniques discussion for web app greybox.txt"
    raw.write_text("Email jane.doe@example.com\n", encoding="utf-8")
    output_dir = tmp_path / "out"
    output_dir.mkdir()

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--in", str(raw), "--out", str(output_dir), "--summary"],
        text=True,
        capture_output=True,
        check=True,
    )

    output = output_dir / "Techniques discussion for web app greybox-redacted.txt"
    assert proc.stdout == ""
    assert output.read_text(encoding="utf-8") == "Email [EMAIL_1]\n"
    assert proc.stderr == f"wrote redacted output to {output}\nsummary: EMAIL=1 warnings=none\n"
    assert raw.read_text(encoding="utf-8") == "Email jane.doe@example.com\n"


def test_cli_paste_mode_writes_stdin_to_output_file(tmp_path: Path):
    redacted = tmp_path / "redacted.log"

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--out", str(redacted), "--summary"],
        input="Email jane.doe@example.com api_key=synthetic-api-key-value-12345\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == ""
    assert redacted.read_text(encoding="utf-8") == "Email [EMAIL_1] api_key=[TOKEN_1]\n"
    assert proc.stderr == "summary: EMAIL=1 TOKEN=1 warnings=none\n"
    assert "jane.doe@example.com" not in redacted.read_text(encoding="utf-8")
    assert "synthetic-api-key-value-12345" not in redacted.read_text(encoding="utf-8")


def test_cli_file_mode_reads_file_and_prints_to_stdout_when_no_output(tmp_path: Path):
    raw = tmp_path / "raw.log"
    raw.write_text("Open https://app.example.test/login\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--in", str(raw)],
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "Open https://[HOST_1]/login\n"
    assert proc.stderr == ""


def test_cli_file_mode_refuses_to_overwrite_without_force(tmp_path: Path):
    raw = tmp_path / "raw.log"
    redacted = tmp_path / "redacted.log"
    raw.write_text("Email jane.doe@example.com\n", encoding="utf-8")
    redacted.write_text("keep me\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--in", str(raw), "--out", str(redacted)],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "refusing to overwrite" in proc.stderr
    assert redacted.read_text(encoding="utf-8") == "keep me\n"


def test_cli_file_mode_force_overwrites_existing_output(tmp_path: Path):
    raw = tmp_path / "raw.log"
    redacted = tmp_path / "redacted.log"
    raw.write_text("Email jane.doe@example.com\n", encoding="utf-8")
    redacted.write_text("replace me\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--in", str(raw), "--out", str(redacted), "--force"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == ""
    assert proc.stderr == ""
    assert redacted.read_text(encoding="utf-8") == "Email [EMAIL_1]\n"


def test_cli_auto_out_writes_redacted_subdir_file(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    source_dir = tmp_path / "webapp"
    source_dir.mkdir()
    raw = source_dir / "some-log.txt"
    raw.write_text("Email jane.doe@example.com\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--in", str(raw), "--auto-out"],
        text=True,
        capture_output=True,
        check=True,
    )

    output = config.parent / "profiles" / "default" / "redacted" / "some-log-redacted.txt"
    assert proc.stdout == ""
    assert proc.stderr == f"wrote redacted output to {output}\n"
    assert output.read_text(encoding="utf-8") == "Email [EMAIL_1]\n"
    assert raw.read_text(encoding="utf-8") == "Email jane.doe@example.com\n"
    assert (config.parent / "profiles" / "default" / "terms.txt").exists()


def test_cli_auto_out_without_input_file_writes_timestamped_paste_file(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--auto-out"],
        input="Email jane.doe@example.com\n",
        text=True,
        capture_output=True,
        check=True,
    )

    outputs = list((config.parent / "profiles" / "default" / "redacted").glob("paste-redacted-*.txt"))
    assert len(outputs) == 1
    assert proc.stdout == ""
    assert proc.stderr == f"wrote redacted output to {outputs[0]}\n"
    assert outputs[0].read_text(encoding="utf-8") == "Email [EMAIL_1]\n"
    assert outputs[0].stem.startswith("paste-redacted-")
    assert "jane.doe@example.com" not in outputs[0].read_text(encoding="utf-8")


def test_cli_auto_out_refuses_existing_file_without_force(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    raw = tmp_path / "some-log.txt"
    output = config.parent / "profiles" / "default" / "redacted" / "some-log-redacted.txt"
    raw.write_text("Email jane.doe@example.com\n", encoding="utf-8")
    output.parent.mkdir(parents=True)
    output.write_text("keep me\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--in", str(raw), "--auto-out"],
        text=True,
        capture_output=True,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "refusing to overwrite" in proc.stderr
    assert output.read_text(encoding="utf-8") == "keep me\n"


def test_cli_auto_out_timestamp_adds_timestamp_to_filename(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    raw = tmp_path / "some-log.txt"
    raw.write_text("Email jane.doe@example.com\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--in", str(raw), "--auto-out", "--timestamp"],
        text=True,
        capture_output=True,
        check=True,
    )

    outputs = list((config.parent / "profiles" / "default" / "redacted").glob("some-log-redacted-*.txt"))
    assert len(outputs) == 1
    assert proc.stdout == ""
    assert proc.stderr == f"wrote redacted output to {outputs[0]}\n"
    assert outputs[0].read_text(encoding="utf-8") == "Email [EMAIL_1]\n"
    assert outputs[0].stem.startswith("some-log-redacted-")


def test_cli_init_profile_creates_profile_terms_and_redacted_dir(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    state_file = tmp_path / "current-profile"

    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state_file),
            "--profile-init",
            "acme",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    profile_dir = config.parent / "profiles" / "acme"
    assert proc.stdout == f"initialized profile: acme ({profile_dir})\nselected profile: acme\n"
    assert state_file.read_text(encoding="utf-8") == "acme\n"
    assert (profile_dir / "terms.txt").exists()
    assert (profile_dir / "redacted").is_dir()
    config_text = config.read_text(encoding="utf-8")
    assert "[acme]" in config_text
    assert str(profile_dir / "terms.txt") in config_text


def test_cli_auto_out_uses_selected_profile_term_directory(tmp_path: Path):
    config = tmp_path / "config" / "profiles.ini"
    state_file = tmp_path / "current-profile"
    raw = tmp_path / "scattered" / "some-log.txt"
    raw.parent.mkdir()
    raw.write_text("Email jane.doe@example.com\n", encoding="utf-8")

    subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile-init", "acme"],
        text=True,
        capture_output=True,
        check=True,
    )
    subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state_file), "--profile-select", "acme"],
        text=True,
        capture_output=True,
        check=True,
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state_file),
            "--in",
            str(raw),
            "--auto-out",
        ],
        text=True,
        capture_output=True,
        check=True,
    )

    output = config.parent / "profiles" / "acme" / "redacted" / "some-log-redacted.txt"
    assert proc.stdout == ""
    assert proc.stderr == f"wrote redacted output to {output}\nprofile: acme\n"
    assert output.read_text(encoding="utf-8") == "Email [EMAIL_1]\n"


def test_cli_global_copy_enable_disable_and_status(tmp_path: Path):
    settings_file = tmp_path / "settings.ini"

    initial = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings_file), "--copy-status"],
        text=True,
        capture_output=True,
        check=True,
    )
    enabled = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings_file), "--copy-enable"],
        text=True,
        capture_output=True,
        check=True,
    )
    after_enable = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings_file), "--copy-status"],
        text=True,
        capture_output=True,
        check=True,
    )
    disabled = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings_file), "--copy-disable"],
        text=True,
        capture_output=True,
        check=True,
    )
    after_disable = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--settings", str(settings_file), "--copy-status"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert initial.stdout == "copy: disabled\n"
    assert enabled.stdout == "copy enabled\n"
    assert after_enable.stdout == "copy: enabled\n"
    assert disabled.stdout == "copy disabled\n"
    assert after_disable.stdout == "copy: disabled\n"
    assert "copy = false" in settings_file.read_text(encoding="utf-8")


def test_cli_uses_topic_first_profile_and_session_flags(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    profile_state = tmp_path / "current-profile"
    session_state = tmp_path / "current-session"
    session_dir = tmp_path / "sessions"

    init = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(profile_state), "--profile-init", "acme"],
        text=True,
        capture_output=True,
        check=True,
    )
    profile_select = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(profile_state), "--profile-select", "acme"],
        text=True,
        capture_output=True,
        check=True,
    )
    profile_list = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(profile_state), "--profile-list"],
        text=True,
        capture_output=True,
        check=True,
    )
    session_select = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--session-state-file", str(session_state), "--session-dir", str(session_dir), "--session-select", "acme-webapp"],
        text=True,
        capture_output=True,
        check=True,
    )
    session_delete = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--session-state-file", str(session_state), "--session-dir", str(session_dir), "--session-delete", "acme-webapp"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert init.stdout.endswith("selected profile: acme\n")
    assert profile_select.stdout == "selected profile: acme\n"
    assert profile_list.stdout == "* acme\n"
    assert session_select.stdout == "active session set: acme-webapp\n"
    assert session_delete.stdout == "deleted session: acme-webapp\n"
    assert not session_state.exists()
    assert not (session_dir / "acme-webapp.json").exists()


def test_cli_removed_legacy_flags_are_rejected_and_long_flags_do_not_abbreviate(tmp_path: Path):
    rejected_flags = [
        "--init-profile",
        "--select-profile",
        "--list-terms",
        "--list-ignored-suggestions",
        "--set-session",
        "--unset-session",
        "--select-session",
        "--clear-session",
        "--forget-session",
        "--review",
        "--apply-profile",
        "--ter",
    ]

    for flag in rejected_flags:
        proc = subprocess.run(
            [sys.executable, "-m", "redactor.cli", flag],
            input="Email jane.doe@example.com\n",
            text=True,
            capture_output=True,
        )

        assert proc.returncode == 2, flag
        assert proc.stdout == "", flag
