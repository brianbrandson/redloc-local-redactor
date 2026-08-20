import subprocess
import sys
import stat
import os
from pathlib import Path

from redactor.cli import redact_text


def test_redacts_lithuanian_phone_numbers_in_local_and_international_forms():
    text = "Phones: +370 612 34567, +37061234567, 8 612 34567, 861234567"

    out = redact_text(text)

    assert "+370 612 34567" not in out
    assert "+37061234567" not in out
    assert "8 612 34567" not in out
    assert "861234567" not in out
    assert "[PHONE_1]" in out
    assert "[PHONE_4]" in out


def test_redacts_common_international_eu_style_phone_numbers():
    text = "EU phones: +44 20 7946 0958 and +49 30 12345678"

    out = redact_text(text)

    assert "+44 20 7946 0958" not in out
    assert "+49 30 12345678" not in out
    assert "[PHONE_1]" in out
    assert "[PHONE_2]" in out


def test_international_phone_redaction_does_not_swallow_following_public_ip():
    text = "+44 20 7946 0958 203.0.113.44"

    out = redact_text(text)

    assert "+44 20 7946 0958" not in out
    assert "203.0.113.44" not in out
    assert out == "[PHONE_1] [PUBLIC_IP_1]"


def test_term_file_redacts_one_client_term_per_line(tmp_path: Path):
    terms = tmp_path / "terms.txt"
    terms.write_text("ExampleCo\nProject Squirrel\n# ignored comment\n\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--term-file", str(terms)],
        input="ExampleCo owns Project Squirrel\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert "ExampleCo" not in proc.stdout
    assert "Project Squirrel" not in proc.stdout
    assert "[CLIENT_1]" in proc.stdout
    assert "[CLIENT_2]" in proc.stdout
    assert proc.stderr == ""


def test_profile_loads_terms_term_files_and_redacts_public_ips_by_default(tmp_path: Path):
    terms = tmp_path / "terms.txt"
    terms.write_text("Project Squirrel\n", encoding="utf-8")
    config = tmp_path / "profiles.ini"
    config.write_text(
        f"""
[exampleco]
terms =
    ExampleCo
    ExampleCo VPN
term_files =
    {terms}
""".lstrip(),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile", "exampleco"],
        input="ExampleCo VPN at 203.0.113.44 for Project Squirrel\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert "ExampleCo" not in proc.stdout
    assert "Project Squirrel" not in proc.stdout
    assert "203.0.113.44" not in proc.stdout
    assert "[CLIENT_1]" in proc.stdout
    assert "[CLIENT_2]" in proc.stdout
    assert "[PUBLIC_IP_1]" in proc.stdout


def test_default_config_and_data_paths_use_redloc_dirs(tmp_path: Path):
    env = os.environ.copy()
    env["XDG_CONFIG_HOME"] = str(tmp_path / "config")
    env["XDG_DATA_HOME"] = str(tmp_path / "data")

    subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--profile-init", "smoke"],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )
    subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--session-init", "smoke-session"],
        text=True,
        capture_output=True,
        check=True,
        env=env,
    )

    assert (tmp_path / "config" / "redloc" / "profiles.ini").exists()
    assert (tmp_path / "config" / "redloc" / "current-profile").exists()
    assert (tmp_path / "data" / "redloc" / "sessions" / "smoke-session.json").exists()
    assert not (tmp_path / "config" / "local-redactor").exists()
    assert not (tmp_path / "data" / "local-redactor").exists()


def test_profile_term_add_appends_stdin_terms_to_explicit_term_file(tmp_path: Path):
    terms = tmp_path / "terms.txt"
    terms.write_text("ExampleCo\n", encoding="utf-8")

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--profile-term-add", "--term-file", str(terms)],
        input="Project Squirrel\nLithuanian Subsidiary UAB\n\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert terms.read_text(encoding="utf-8") == "ExampleCo\nPROJECT: Project Squirrel\nORG: Lithuanian Subsidiary UAB\n"
    assert proc.stdout == "added 2 term(s) to " + str(terms) + "\n"
    assert proc.stderr == ""


def test_profile_term_add_uses_first_profile_term_file(tmp_path: Path):
    terms = tmp_path / "terms.txt"
    terms.write_text("ExampleCo\n", encoding="utf-8")
    config = tmp_path / "profiles.ini"
    config.write_text(
        f"""
[exampleco]
term_files =
    {terms}
""".lstrip(),
        encoding="utf-8",
    )

    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile", "exampleco", "--profile-term-add"],
        input="Jane Doe\nExampleCo\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert terms.read_text(encoding="utf-8") == "ExampleCo\nPERSON: Jane Doe\n"
    assert proc.stdout == "added 1 term(s) to " + str(terms) + "\n"
    assert proc.stderr == ""


def test_profile_term_list_remove_and_old_term_management_flags_are_not_kept_as_aliases(tmp_path: Path):
    terms = tmp_path / "terms.txt"
    terms.write_text("PERSON: Operator One\nORG: Home Lab UAB\n", encoding="utf-8")
    config = tmp_path / "profiles.ini"
    config.write_text(
        f"""
[exampleco]
term_files =
    {terms}
""".lstrip(),
        encoding="utf-8",
    )

    list_before = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile", "exampleco", "--profile-term-list"],
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
            "--profile",
            "exampleco",
            "--profile-term-remove",
            "Operator One",
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    old_flag = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile", "exampleco", "--term-list"],
        text=True,
        capture_output=True,
        check=False,
    )

    assert list_before.stdout == "[x] ORG: Home Lab UAB\n[x] PERSON: Operator One\n"
    assert remove_proc.stdout == "removed 1 profile term(s)\n"
    assert terms.read_text(encoding="utf-8") == "ORG: Home Lab UAB\n"
    assert old_flag.returncode == 2
    assert "unrecognized arguments: --term-list" in old_flag.stderr


def test_global_term_add_redacts_without_explicit_profile_and_uses_private_permissions(tmp_path: Path):
    config = tmp_path / "profiles.ini"
    global_terms = tmp_path / "global-terms.txt"

    add_proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--global-term-add"],
        input="PERSON: Operator One\nORG: Home Lab UAB\n",
        text=True,
        capture_output=True,
        check=True,
    )
    redact_proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config)],
        input="Operator One works with Home Lab UAB\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert add_proc.stdout == f"added 2 global term(s) to {global_terms}\n"
    assert global_terms.read_text(encoding="utf-8") == "PERSON: Operator One\nORG: Home Lab UAB\n"
    assert stat.S_IMODE(global_terms.stat().st_mode) == 0o600
    assert redact_proc.stdout == "[PERSON_1] works with [ORG_1]\n"
    assert redact_proc.stderr == ""


def test_global_terms_merge_before_profile_terms(tmp_path: Path):
    profile_terms = tmp_path / "profile-terms.txt"
    profile_terms.write_text("PROJECT: Project Squirrel\n", encoding="utf-8")
    config = tmp_path / "profiles.ini"
    config.write_text(
        f"""
[exampleco]
term_files =
    {profile_terms}
""".lstrip(),
        encoding="utf-8",
    )

    subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--global-term-add"],
        input="ORG: Home Lab UAB\n",
        text=True,
        capture_output=True,
        check=True,
    )
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--profile", "exampleco"],
        input="Home Lab UAB ships Project Squirrel\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "[ORG_1] ships [PROJECT_1]\n"


def test_global_term_list_and_remove_are_scriptable(tmp_path: Path):
    config = tmp_path / "profiles.ini"

    subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--global-term-add"],
        input="PERSON: Operator One\nORG: Home Lab UAB\n",
        text=True,
        capture_output=True,
        check=True,
    )
    list_before = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--global-term-list"],
        text=True,
        capture_output=True,
        check=True,
    )
    remove_proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--global-term-remove", "Operator One"],
        text=True,
        capture_output=True,
        check=True,
    )
    list_after = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--config", str(config), "--global-term-list"],
        text=True,
        capture_output=True,
        check=True,
    )

    assert list_before.stdout == "[x] ORG: Home Lab UAB\n[x] PERSON: Operator One\n"
    assert remove_proc.stdout == "removed 1 global term(s)\n"
    assert list_after.stdout == "[x] ORG: Home Lab UAB\n"
