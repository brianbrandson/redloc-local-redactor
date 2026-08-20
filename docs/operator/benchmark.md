# Benchmark guide

`redloc-benchmark` runs synthetic redaction checks against `redloc`.

Run it after installation to make sure the installed command can find the packaged benchmark corpus and the local redaction engine behaves as expected.

## Quick check

After installing `redloc`, run:

```bash
redloc-benchmark --suite all --tool local
```

A healthy run exits `0` and ends with a summary like:

```json
{
  "failed": 0,
  "passed": 12,
  "suite": "all",
  "tool": "local",
  "total": 12
}
```

The full output is JSON. It is meant to be safe to paste into issues or review notes after you skim it.

## What it proves

The benchmark checks known synthetic cases for:

- emails
- phone numbers
- private and public IP addresses
- URL hosts while preserving useful route/query shape
- HTTP headers and cookies
- bearer/basic-auth/API-token-like values
- UUIDs
- local filesystem paths
- configured client/project terms
- Caido-style request summaries

Each case checks two things:

- `must_redact` values do not appear in the final output.
- `must_contain` values do appear, proving useful structure was preserved.

A case fails if a required redaction leaks, an expected placeholder/shape is missing, or the residual checker reports warnings.

Passing the benchmark does not prove perfect redaction. It proves the bundled synthetic workflows still behave as expected.

## Suites

Use suites to run smaller groups:

```bash
redloc-benchmark --suite baseline --tool local
redloc-benchmark --suite operator --tool local
redloc-benchmark --suite project-operator --tool local
redloc-benchmark --suite all --tool local
```

Suite meanings:

- `baseline` - plain obvious patterns such as emails, phone numbers, and tokens.
- `operator` - realistic operator snippets such as HTTP summaries, headers, paths, IPs, and tokens.
- `project-operator` - examples that include configured project/client terms.
- `all` - every bundled case.

## Output fields

Top-level fields:

- `tool` - benchmark adapter used.
- `suite` - suite requested.
- `total` - cases run.
- `passed` - cases that passed.
- `failed` - cases that failed.
- `cases` - per-case results.

Per-case fields:

- `id` - synthetic case name.
- `suites` - suites the case belongs to.
- `passed` - whether that case passed.
- `counts` - placeholder counts returned by the redactor.
- `failed_checks` - reasons for failure: `leaks`, `missing_expected`, or `warnings`.
- `leaked_count` - number of `must_redact` values still visible.
- `missing_expected_count` - number of expected output shapes missing.
- `warnings` - raw-free residual warning names.

The report intentionally does not include raw input text.

## Bring your own synthetic corpus

Create a starter file:

```bash
redloc-benchmark --write-template my-synthetic-corpus.json
```

Run it:

```bash
redloc-benchmark --corpus my-synthetic-corpus.json --suite all --tool local
```

A corpus is a JSON list of cases:

```json
[
  {
    "id": "synthetic-caido-summary",
    "suites": ["operator"],
    "input": "GET https://portal.example.test/api/v1/users?email=jane.operator@example.test Authorization: Bearer plainSyntheticSecret123",
    "must_redact": ["portal.example.test", "jane.operator@example.test", "plainSyntheticSecret123"],
    "must_contain": ["https://[HOST_1]/api/v1/users?email=[EMAIL_1]", "Bearer [TOKEN_1]"]
  }
]
```

Rules:

- Use synthetic values only.
- Use domains such as `example.test`.
- Use documentation IP ranges such as `203.0.113.42` or `198.51.100.23`.
- Use fake names such as `Jane Operator` and `ExampleCo`.
- Use fake token text such as `plainSyntheticSecret123`.
- Do not add real client text, real secrets, request dumps, logs, screenshots, profile files, or session mappings.

## Tool adapters

Default path:

```bash
redloc-benchmark --tool local
```

The benchmark also has adapter plumbing for comparisons, such as:

```bash
redloc-benchmark --tool shareclean
redloc-benchmark --tool all
```

These are comparison paths only. Third-party tools are not bundled runtime dependencies unless project metadata says so.

For first-install confidence, use `--tool local`.

## Optional AI checker benchmark

If you run a local/private OpenAI-compatible endpoint, you can smoke the AI checker behavior:

```bash
redloc-benchmark \
  --ai-checker \
  --ai-endpoint http://127.0.0.1:11434/v1/chat/completions \
  --ai-model local-redaction-checker
```

This checks whether the model warns on synthetic contextual leftovers and stays quiet on already-placeholdered controls.

Use this as a model smoke, not as proof that AI review is safe or complete.
