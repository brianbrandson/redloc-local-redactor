from redactor.cli import redact_text


def test_redacts_common_operator_values_with_stable_typed_placeholders():
    aws_key = "AKIA" + "IOSFODNN7EXAMPLE"
    github_token = "ghp_" + "1234567890abcdefABCDEF"
    text = "\n".join(
        [
            "Contact Jane Doe at jane.doe@example.com or jane.doe@example.com",
            "Phone +1 (212) 555-0199 and UUID 4c9f2c6d-4d19-4d07-a34f-8aa2b3d9f111",
            "Internal db-prod-01.corp.example.local 10.42.7.15 public 203.0.113.44",
            "Portal https://vpn.examplecorp.test/login?token=abc123&redirect=/admin",
            "Authorization: Bearer eyJhbG....sig",
            "Cookie: sessionid=7f4c6b2d9efab77112233445566778899; csrftoken=abcdef1234567890abcdef1234567890",
            f"AWS {aws_key} and GitHub {github_token}",
            "Path /home/exampleuser/client/ExampleCo/secrets.env",
        ]
    )

    out = redact_text(text)

    assert "jane.doe@example.com" not in out
    assert out.count("[EMAIL_1]") == 2
    assert "+1 (212) 555-0199" not in out
    assert "[PHONE_1]" in out
    assert "4c9f2c6d-4d19-4d07-a34f-8aa2b3d9f111" not in out
    assert "[UUID_1]" in out
    assert "db-prod-01.corp.example.local" not in out
    assert "[HOST_2]" in out
    assert "10.42.7.15" not in out
    assert "[INTERNAL_IP_1]" in out
    assert "203.0.113.44" not in out
    assert "[PUBLIC_IP_1]" in out
    assert "https://vpn.examplecorp.test/login?token=abc123&redirect=/admin" not in out
    assert "https://[HOST_1]/login?token=[TOKEN_2]&redirect=/admin" in out
    assert "Bearer eyJ" not in out
    assert "[TOKEN_1]" in out
    assert "sessionid=7f4" not in out
    assert "[COOKIE_1]" in out
    assert aws_key not in out
    assert "[TOKEN_3]" in out
    assert github_token not in out
    assert "[TOKEN_4]" in out
    assert "/home/exampleuser/client/ExampleCo/secrets.env" not in out
    assert "[PATH_1]" in out


def test_redacts_public_ip_by_default():
    text = "Public test host 203.0.113.44"

    out = redact_text(text)

    assert "203.0.113.44" not in out
    assert "[PUBLIC_IP_1]" in out


def test_redacts_url_ip_hosts_with_ip_placeholders():
    text = "\n".join(
        [
            "Host: 10.1.103.217:5000",
            "Origin: http://10.1.103.217:5000",
            "Referer: http://10.1.103.217:5000/",
        ]
    )

    out = redact_text(text)

    assert "10.1.103.217" not in out
    assert "Host: [INTERNAL_IP_1]:5000" in out
    assert "Origin: http://[INTERNAL_IP_1]:5000" in out
    assert "Referer: http://[INTERNAL_IP_1]:5000/" in out
    assert "[HOST_" not in out


def test_preserves_browser_version_dotted_quads_in_user_agent():
    text = "User-Agent: Chrome/148.0.0.0 Safari/537.36"

    out = redact_text(text)

    assert out == text
    assert "[PUBLIC_IP_" not in out


def test_preserves_product_version_dotted_quads_without_slash():
    text = "\n".join(
        [
            "Server banner: Apache Tomcat 8.5.51.0",
            "Reverse proxy: nginx 1.24.0.0",
            "Browser note: Chrome 120.0.6099.71",
            "Connection from 203.0.113.44 still redacts",
        ]
    )

    out = redact_text(text)

    assert "Apache Tomcat 8.5.51.0" in out
    assert "nginx 1.24.0.0" in out
    assert "Chrome 120.0.6099.71" in out
    assert "203.0.113.44" not in out
    assert "[PUBLIC_IP_1]" in out


def test_redacts_cookie_values_without_consuming_shell_quotes():
    text = "--cookie='PHPSESSID=synthetic-session-123; csrftoken=synthetic-csrf-123'"

    out = redact_text(text)

    assert out == "--cookie='PHPSESSID=[COOKIE_1]; csrftoken=[COOKIE_2]'"


def test_redacts_local_paths_without_consuming_quotes():
    text = '  File "/vault/client/ExampleCo/app.py", line 42, in <module>'

    out = redact_text(text)

    assert out == '  File "[PATH_1]", line 42, in <module>'


def test_redacts_json_and_url_token_assignments():
    text = (
        '{"password":"synthetic-password-123","apiToken":"camel-token-123",'
        '"callback":"/login?access_token=url-token-123&code=keep-code-readable"}'
    )

    out = redact_text(text)

    assert "synthetic-password-123" not in out
    assert "camel-token-123" not in out
    assert "url-token-123" not in out
    assert '"password":"[TOKEN_1]"' in out
    assert '"apiToken":"[TOKEN_2]"' in out
    assert "access_token=[TOKEN_3]&code=keep-code-readable" in out


def test_redacts_host_header_windows_paths_and_basic_auth_url_userinfo():
    text = "\n".join(
        [
            "Host: portal.example.test",
            "Path C:\\Users\\exampleuser\\Documents\\ExampleCo\\raw.txt",
            "URL https://demo-user:demo-pass@app.example.test/private",
        ]
    )

    out = redact_text(text, client_terms=["ExampleCo"])

    assert "portal.example.test" not in out
    assert "C:\\Users\\exampleuser\\Documents\\ExampleCo\\raw.txt" not in out
    assert "demo-user:demo-pass" not in out
    assert "app.example.test" not in out
    assert "Host: [HOST_2]" in out
    assert "Path [PATH_1]" in out
    assert "URL https://" + "[TOKEN_1]" + "@[HOST_1]/private" in out


def test_preserves_placeholder_url_hosts_in_angle_brackets():
    text = 'curl "http://<IP_ADDRESS>:8080/cgi-bin/magicBox.cgi?action=getSystemInfo"'

    out = redact_text(text)

    assert out == text
    assert "[HOST_" not in out


def test_preserves_generic_system_and_tool_paths_for_evidence_readability():
    text = "SSTI read /etc/passwd, hosts fix in /etc/hosts, crack with /usr/share/wordlists/rockyou.txt"

    out = redact_text(text)

    assert out == text
    assert "[PATH_" not in out


def test_redacts_expanded_secret_token_pack():
    jwt = "eyJ" + "hbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.syntheticpayload.syntheticSignaturePart"
    slack_token = "xoxb-" + "123456789012-123456789012-abcdefghijklmnopqrstuvwx"
    private_key_block = "\n".join(
        [
            "-----BEGIN " + "PRIVATE KEY-----",
            "synthetic-private-key-body",
            "-----END " + "PRIVATE KEY-----",
        ]
    )
    text = "\n".join(
        [
            f"JWT {jwt}",
            f"Slack bot {slack_token}",
            "Discord bot MTExMTExMTExMTExMTExMTEx.syntheticpart.syntheticSignaturePart",
            "api_key=synthetic-api-key-value-12345",
            "client_secret: synthetic-client-secret-value-12345",
            "password = synthetic-password-value-12345",
            "Basic auth https://demo-user:demo-pass@app.example.test/private",
            private_key_block,
        ]
    )

    out = redact_text(text)

    assert jwt not in out
    assert slack_token not in out
    assert "MTExMTExMTExMTExMTExMTEx.syntheticpart" not in out
    assert "synthetic-api-key-value-12345" not in out
    assert "synthetic-client-secret-value-12345" not in out
    assert "synthetic-password-value-12345" not in out
    assert "demo-user:demo-pass" not in out
    assert "synthetic-private-key-body" not in out
    assert out.count("[TOKEN_") == 8
    assert "api_key=[TOKEN_" in out
    assert "client_secret: [TOKEN_" in out
    assert "password = [TOKEN_" in out
