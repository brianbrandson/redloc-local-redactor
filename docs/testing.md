# Testing redloc

The public test suite is here so operators and contributors can verify the tool before trusting changes.

All tests use synthetic data. Do not add real client names, secrets, logs, HTTP traffic, screenshots, profile files, session mappings, or engagement evidence to tests.

## Run the tests

From the repository root:

```bash
uv run --with pytest python -m pytest -q
```

For a faster documentation-only check:

```bash
uv run --with pytest python -m pytest tests/test_install_docs.py -q
```

Run the synthetic benchmark too:

```bash
uv run redloc-benchmark --suite all --tool local
```

Before publishing or sharing a changed tree, also run:

```bash
uvx ruff check .
uvx --from 'bandit[toml]' bandit -c pyproject.toml -r redactor -q
gitleaks detect --source . --no-git --config .gitleaks.toml --redact --verbose --no-banner
git diff --check
```

## What the tests prove

- `tests/test_detectors.py` checks deterministic redaction patterns such as emails, hosts, IPs, paths, tokens, and false-positive boundaries.
- `tests/test_checker.py` checks raw-free residual warnings for likely leftovers.
- `tests/test_cli.py` checks CLI behavior: stdin/stdout/stderr handling, profiles, sessions, output files, reports, list screens, local AI options, and safety guardrails.
- `tests/test_benchmark.py` checks benchmark corpus loading, scoring, reports, tool selection, and generated corpus templates.
- `tests/test_install_docs.py` checks that public docs stay aligned with supported commands, paths, warnings, and public-only documentation boundaries.
- `tests/test_daily_driver.py`, `tests/test_stage1b.py`, and `tests/test_residual_regressions.py` keep earlier regression cases from coming back.

Passing tests do not prove perfect redaction. They prove the known synthetic cases and documented workflows still behave as expected.

## Adding new tests

When a real workflow exposes a miss, translate it into synthetic data first.

Good test data:

- `example.test` domains
- documentation IP ranges such as `203.0.113.42` or `198.51.100.23`
- fake names such as `Jane Operator` or `ExampleCo`
- fake token text such as `plainSyntheticSecret123`

Avoid provider-shaped secrets like real AWS, GitHub, Slack, or cloud API tokens. If a detector needs that shape, build the string from pieces inside the test so scanners do not flag the repository.

## What to trust

Use tests and benchmarks as evidence, not as a guarantee.

Before sharing redacted output, still review it manually. Unknown project names, people, organizations, locations, ticket IDs, and codenames can require profile terms or local AI suggestion review.
