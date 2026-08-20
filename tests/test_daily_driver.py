import os
import subprocess
import sys
from pathlib import Path

from redactor.cli import copy_to_clipboard, main, redact_text


def test_redacts_client_terms_case_insensitively():
    out = redact_text("Baltic Amadeus and Baltic amadeus", client_terms=["Baltic Amadeus"])

    assert "Baltic Amadeus" not in out
    assert "Baltic amadeus" not in out
    assert out == "[CLIENT_1] and [CLIENT_1]"


def test_default_profile_loads_automatically_when_config_has_default(tmp_path: Path):
    terms = tmp_path / "terms.txt"
    terms.write_text("ExampleCo\n", encoding="utf-8")
    config = tmp_path / "profiles.ini"
    config.write_text(
        f"""
[default]
term_files =
    {terms}
""".lstrip(),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config)],
        input="ExampleCo at 203.0.113.44\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "[CLIENT_1] at [PUBLIC_IP_1]\n"
    assert "profile: default" in proc.stderr


def test_select_profile_persists_current_profile_and_redact_uses_it(tmp_path: Path):
    terms = tmp_path / "terms.txt"
    terms.write_text("ExampleCo\n", encoding="utf-8")
    config = tmp_path / "profiles.ini"
    state = tmp_path / "current-profile"
    config.write_text(
        f"""
[exampleco]
term_files =
    {terms}
""".lstrip(),
        encoding="utf-8",
    )

    select = subprocess.run(
        [
            sys.executable,
            "-m",
            "redactor.cli",
            "--config",
            str(config),
            "--state-file",
            str(state),
            "--profile-select",
            "exampleco",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    redact = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--state-file", str(state)],
        input="ExampleCo\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert select.stdout == "selected profile: exampleco\n"
    assert state.read_text(encoding="utf-8") == "exampleco\n"
    assert redact.stdout == "[CLIENT_1]\n"
    assert "profile: exampleco" in redact.stderr


def test_keyboard_interrupt_during_paste_exits_cleanly(monkeypatch, capsys):
    class InterruptingStdin:
        def read(self):
            raise KeyboardInterrupt

    monkeypatch.setattr(sys, "stdin", InterruptingStdin())

    assert main([]) == 130
    captured = capsys.readouterr()
    assert captured.err == "cancelled\n"
    assert "Traceback" not in captured.err


def test_copy_to_clipboard_uses_first_available_clipboard_command(monkeypatch):
    calls = []

    def fake_which(command):
        return f"/usr/bin/{command}" if command == "wl-copy" else None

    def fake_run(command, input=None, text=True, check=True, capture_output=False, **kwargs):
        calls.append((command, input, text, check, capture_output))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("redactor.cli.shutil.which", fake_which)
    monkeypatch.setattr("redactor.cli.subprocess.run", fake_run)

    assert copy_to_clipboard("redacted text") == "wl-copy"
    assert calls == [(["wl-copy", "--type", "text/plain"], "redacted text", True, True, False)]


def test_copy_to_clipboard_verifies_wl_copy_with_wl_paste(monkeypatch):
    calls = []

    def fake_which(command):
        return f"/usr/bin/{command}" if command in {"wl-copy", "wl-paste"} else None

    def fake_run(command, input=None, text=True, check=True, capture_output=False, **kwargs):
        calls.append((command, input, text, check, capture_output))
        if command == ["wl-paste", "--no-newline"]:
            return subprocess.CompletedProcess(command, 0, stdout="redacted text", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("redactor.cli.shutil.which", fake_which)
    monkeypatch.setattr("redactor.cli.subprocess.run", fake_run)

    assert copy_to_clipboard("redacted text") == "wl-copy"
    assert calls == [
        (["wl-copy", "--type", "text/plain"], "redacted text", True, True, False),
        (["wl-paste", "--no-newline"], None, True, True, True),
    ]


def test_copy_to_clipboard_also_updates_x_clipboard_when_available(monkeypatch):
    calls = []

    def fake_which(command):
        return f"/usr/bin/{command}" if command in {"wl-copy", "wl-paste", "xclip"} else None

    def fake_run(command, input=None, text=True, check=True, capture_output=False, **kwargs):
        calls.append((command, input, text, check, capture_output))
        if command == ["wl-paste", "--no-newline"]:
            return subprocess.CompletedProcess(command, 0, stdout="redacted text", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("redactor.cli.shutil.which", fake_which)
    monkeypatch.setattr("redactor.cli.subprocess.run", fake_run)

    assert copy_to_clipboard("redacted text") == "wl-copy+xclip"
    assert (["xclip", "-selection", "clipboard"], "redacted text", True, True, False) in calls


def test_copy_to_clipboard_falls_back_to_xclip_when_wl_copy_fails(monkeypatch):
    calls = []

    def fake_which(command):
        return f"/usr/bin/{command}" if command in {"wl-copy", "xclip"} else None

    def fake_run(command, input=None, text=True, check=True, capture_output=False, **kwargs):
        calls.append((command, input, text, check, capture_output))
        if command == ["wl-copy", "--type", "text/plain"]:
            raise subprocess.CalledProcessError(1, command)
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("redactor.cli.shutil.which", fake_which)
    monkeypatch.setattr("redactor.cli.subprocess.run", fake_run)

    assert copy_to_clipboard("redacted text") == "xclip"
    assert calls == [
        (["wl-copy", "--type", "text/plain"], "redacted text", True, True, False),
        (["xclip", "-selection", "clipboard"], "redacted text", True, True, False),
    ]


def test_copy_to_clipboard_autodetects_wayland_socket_when_env_unset(tmp_path: Path, monkeypatch):
    calls = []
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    (runtime / "wayland-1").touch()

    def fake_which(command):
        return f"/usr/bin/{command}" if command == "wl-copy" else None

    def fake_run(command, input=None, text=True, check=True, capture_output=False, env=None, stderr=None):
        calls.append((command, env.get("WAYLAND_DISPLAY") if env else None, stderr))
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("redactor.cli.shutil.which", fake_which)
    monkeypatch.setattr("redactor.cli.subprocess.run", fake_run)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)

    assert copy_to_clipboard("redacted text") == "wl-copy"
    assert calls == [(["wl-copy", "--type", "text/plain"], "wayland-1", subprocess.DEVNULL)]


def test_copy_to_clipboard_raises_when_wl_paste_verification_mismatches(monkeypatch):
    def fake_which(command):
        return f"/usr/bin/{command}" if command in {"wl-copy", "wl-paste"} else None

    def fake_run(command, input=None, text=True, check=True, capture_output=False, **kwargs):
        if command == ["wl-paste", "--no-newline"]:
            return subprocess.CompletedProcess(command, 0, stdout="raw text", stderr="")
        return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

    monkeypatch.setattr("redactor.cli.shutil.which", fake_which)
    monkeypatch.setattr("redactor.cli.subprocess.run", fake_run)

    try:
        copy_to_clipboard("redacted text")
    except RuntimeError as exc:
        assert "clipboard verification failed" in str(exc)
    else:
        raise AssertionError("expected clipboard verification failure")


def test_profile_copy_setting_copies_output_with_plain_redact(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "clipboard.txt"
    wl_copy = fake_bin / "wl-copy"
    wl_copy.write_text(f"#!/bin/sh\ncat > {capture}\n", encoding="utf-8")
    wl_copy.chmod(0o755)
    terms = tmp_path / "terms.txt"
    terms.write_text("ExampleCo\n", encoding="utf-8")
    config = tmp_path / "profiles.ini"
    config.write_text(
        f"""
[default]
copy = true
term_files =
    {terms}
""".lstrip(),
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config)],
        input="ExampleCo\n",
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    assert proc.stdout == "[CLIENT_1]\n"
    assert capture.read_text(encoding="utf-8") == "[CLIENT_1]\n"
    assert "copied to clipboard via wl-copy" in proc.stderr


def test_copy_flag_copies_redacted_output_with_fake_wl_copy(tmp_path: Path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    capture = tmp_path / "clipboard.txt"
    wl_copy = fake_bin / "wl-copy"
    wl_copy.write_text(f"#!/bin/sh\ncat > {capture}\n", encoding="utf-8")
    wl_copy.chmod(0o755)
    env = os.environ.copy()
    env["PATH"] = str(fake_bin) + os.pathsep + env["PATH"]

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--copy"],
        input="Email jane.doe@example.com\n",
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    assert proc.stdout == "Email [EMAIL_1]\n"
    assert capture.read_text(encoding="utf-8") == "Email [EMAIL_1]\n"
    assert "copied to clipboard via wl-copy" in proc.stderr
