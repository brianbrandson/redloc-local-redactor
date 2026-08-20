import subprocess
import sys

from redactor.cli import check_residual, redact_text


def test_configured_client_terms_redact_with_stable_client_placeholder():
    text = "ExampleCo portal and ExampleCo report"

    out = redact_text(text, client_terms=["ExampleCo"])

    assert out == "[CLIENT_1] portal and [CLIENT_1] report"


def test_residual_checker_warns_about_configured_client_terms():
    warnings = check_residual("ExampleCo portal", client_terms=["ExampleCo"])

    assert "possible client term remains" in warnings


def test_residual_checker_warns_about_expanded_secret_tokens():
    private_key_block = "\n".join(
        [
            "-----BEGIN " + "PRIVATE KEY-----",
            "synthetic-private-key-body",
            "-----END " + "PRIVATE KEY-----",
        ]
    )
    secret_examples = [
        "jwt eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJzeW50aGV0aWMifQ.synthetic-signature",
        "slack " + "xoxb-" + "111111111111-222222222222-syntheticTokenValue123456",
        "discord MTExMTExMTExMTExMTExMTEx.syntheticpart.syntheticSignaturePart",
        "api_key=synthetic-api-key-value-12345",
        "https://demo-user:demo-pass@app.example.test/private",
        private_key_block,
    ]

    for secret_example in secret_examples:
        assert "possible token remains" in check_residual(secret_example)


def test_residual_checker_warns_about_local_paths():
    examples = [
        "/home/exampleuser/client/ExampleCo/secrets.env",
        "C:\\Users\\exampleuser\\Documents\\ExampleCo\\raw.txt",
    ]

    for example in examples:
        assert "possible local path remains" in check_residual(example)


def test_residual_checker_preserves_generic_system_and_tool_paths():
    text = "SSTI read /etc/passwd, hosts fix in /etc/hosts, crack with /usr/share/wordlists/rockyou.txt"

    assert check_residual(text) == []


def test_residual_checker_warns_about_public_ips():
    assert "possible public ip remains" in check_residual("Public host 203.0.113.44")


def test_residual_checker_ignores_browser_version_dotted_quads():
    assert check_residual("User-Agent: Chrome/148.0.0.0 Safari/537.36") == []


def test_residual_checker_ignores_path_placeholders():
    assert check_residual("Saved at [PATH_1]\n") == []


def test_cli_term_option_redacts_client_term_from_stdin():
    proc = subprocess.run(
        [sys.executable, "-m", "redactor.cli", "--term", "ExampleCo"],
        input="ExampleCo Jane Doe jane.doe@example.com\n",
        text=True,
        capture_output=True,
        check=True,
    )

    assert proc.stdout == "[CLIENT_1] Jane Doe [EMAIL_1]\n"
    assert "ExampleCo" not in proc.stdout
