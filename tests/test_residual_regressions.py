from redactor.cli import check_residual, redact_text


def test_residual_checker_ignores_redacted_bearer_and_cookie_placeholders():
    redacted = redact_text(
        "Authorization: Bearer ***"
        "Cookie: sessionid=7f4c6b2d9efab77112233445566778899\n"
    )

    assert check_residual(redacted) == []


def test_residual_checker_ignores_redacted_bearer_placeholder_before_normal_words():
    redacted = "Authorization: Bearer *** and path [PATH_1]"

    assert check_residual(redacted) == []


def test_residual_checker_ignores_common_redaction_markers_in_sensitive_shapes():
    redacted = "\n".join(
        [
            "api_key=[REDACTED]",
            "api_key=[TOKEN_1]",
            "password=<redacted>",
            "client_secret=[TOKEN_2]",
            "token=REDACTED",
            "Authorization: Bearer [REDACTED]",
            "Cookie: sessionid=[REDACTED]; csrftoken=<redacted>",
            "Contact: [EMAIL REDACTED]",
        ]
    )

    assert check_residual(redacted) == []


def test_residual_checker_still_warns_about_unmasked_sensitive_values():
    assert "possible token remains" in check_residual("api_key=synthetic-api-key-value-12345")
    assert "possible cookie remains" in check_residual("Cookie: sessionid=abcdef1234567890")
