# redloc

`redloc` is a local-first redaction CLI for operator notes.

It helps sanitize logs, tickets, HTTP snippets, paths, tokens, client names, and other sensitive text before you paste or share it.

Raw pasted text is processed locally and is not saved by default.

Status: alpha. Redaction is not a guarantee; review output before sending it to chats, tickets, reports, cloud tools, or client-facing material.

## Quick start

Install from a checkout:

```bash
cd /path/to/redloc-local-redactor
uv tool install --editable .
```

Run a first smoke:

```bash
printf 'Email jane.doe@example.com api_key=synthetic-api-key-value-12345\n' | redloc --summary
```

Expected shape:

```text
Email [EMAIL_1] api_key=[TOKEN_1]
summary: EMAIL=1 TOKEN=1 warnings=none
```

See `INSTALL.md` for requirements, reinstall steps, uninstall, and PATH troubleshooting.

## Common workflows

### Paste and redact

```bash
redloc
# paste text, then finish with Ctrl-D
```

`redloc` prints redacted text to stdout. Prompts, summaries, profile names, and warnings go to stderr so stdout stays pipe-friendly.

### Redact from a file

```bash
redloc --in raw.log
redloc --in raw.log --out redacted.log --summary
```

Input files are never modified. Existing output files are refused unless `--force` is passed.

### Save or copy output

```bash
redloc --copy
redloc --out redacted.txt
redloc --auto-out
```

`--auto-out` writes redacted-only files under the active profile's `redacted/` directory.

### Teach reusable project terms

Profiles are local buckets for client, project, person, organization, location, or other context terms.

```bash
redloc --profile-init exampleco
redloc --profile-term-add
# paste one term per line, then Ctrl-D
```

Example terms:

```text
ExampleCo
PROJECT: Project Squirrel
ORG: ExampleCo Subsidiary UAB
```

In a real terminal, `--profile-term-add` opens a review screen before saving. Use arrows to move, Enter/Space to include/exclude, `c` to change the manual detector/placeholder class, `d` to save, and `q` to cancel.

Future redactions replace saved terms with typed placeholders such as `[CLIENT_1]`, `[PROJECT_1]`, `[ORG_1]`, and `[PERSON_1]`.

### Keep labels stable across related snippets

Default redaction is stateless. Use a session only when several snippets belong together and you want placeholders such as `[HOST_1]` to keep the same meaning across runs.

```bash
redloc --session-init exampleco-web
redloc --summary
redloc --session-clear
```

Session warning: sessions intentionally store raw matched values locally, with files chmodded `0600`, so placeholder mappings can be reused. Do not use persistent sessions for data you do not want retained on disk.

### Stay open for repeated pastes

```bash
redloc --interactive
# paste a chunk, then press Enter on a blank line to redact it
# type q or quit, then Enter, to exit
```

Interactive mode keeps placeholder labels stable in memory for that run. Pair it with a session when labels should persist across future runs.

### Optional local AI suggestion review

`--ai-suggest` can ask a local/private OpenAI-compatible endpoint to review already-redacted output for visible contextual leftovers such as project names, people, organizations, locations, or codenames.

```bash
redloc --ai-config-set \
  --ai-endpoint http://127.0.0.1:8080/v1/chat/completions \
  --ai-model gpt-oss

redloc --ai-suggest --summary
```

The deterministic redactor runs first. The model reviews redacted output, then the operator accepts, ignores, or recategorizes suggestions. Accepted terms are saved to the active profile and redaction reruns.

Public/cloud AI endpoints are refused by default.

## More examples

- `docs/operator/workflows.md` - synthetic Caido-style notes, profiles, sessions, AI suggestions, and bring-your-own benchmark corpora.
- `docs/operator/benchmark.md` - benchmark usage, suites, output fields, and synthetic corpus format.
- `docs/operator/tty-list-ui.md` - terminal list controls for profiles, terms, ignored suggestions, detectors, and sessions.

## What redloc detects today

Built-in deterministic detectors cover:

- emails
- phone numbers
- UUID/GUID values
- URL hosts/domains while preserving useful path/query shape
- HTTP Host headers
- internal hostnames
- RFC1918/private IPv4 addresses
- public IPv4 addresses
- bearer/API/JWT/private-key/credential-like tokens
- selected session/CSRF cookie values
- basic-auth URL userinfo
- local user/workspace paths under `/home`, `/Users`, `/root`, `/workspace`, and `/vault`
- configured profile/client/project/context terms

Some generic system/tool paths such as `/etc/passwd`, `/etc/hosts`, and `/usr/share/wordlists/rockyou.txt` are intentionally preserved for evidence readability.

## Benchmark

Run the bundled synthetic corpus:

```bash
redloc-benchmark --suite all --tool local
```

The benchmark emits raw-free JSON and exits non-zero on failed cases. Fixtures are synthetic only; do not add real client text, real secrets, or engagement evidence.

See `docs/operator/benchmark.md` for what the benchmark proves, how to read the JSON report, and how to write your own synthetic corpus.

Create a starter corpus for your own synthetic workflow checks:

```bash
redloc-benchmark --write-template my-synthetic-corpus.json
redloc-benchmark --corpus my-synthetic-corpus.json --suite all --tool local
```

## Testing

Run the public test suite from the repository root:

```bash
uv run --with pytest python -m pytest -q
```

See `docs/testing.md` for what each test group proves and the extra checks to run before sharing a changed tree.

## Command reference

Run `redloc --help` for the full flag list.

Common flags:

- `--in FILE` reads input from a file.
- `--out FILE_OR_DIR` writes redacted output instead of printing to stdout.
- `--auto-out` writes to the active profile's `redacted/` directory.
- `--summary` prints raw-free counts and warning names to stderr.
- `--report FILE` writes a raw-free JSON report.
- `--copy` copies output for this run.
- `--interactive` keeps the process open for repeated paste chunks.
- `--profile-init NAME`, `--profile-select NAME`, and `--profile NAME` manage local profiles.
- `--profile-term-add`, `--profile-term-list`, and `--profile-term-remove TERM` manage saved profile terms.
- `--global-term-add`, `--global-term-list`, and `--global-term-remove TERM` manage always-redact terms in `~/.config/redloc/global-terms.txt` for operator/company/default identifiers that should redact in every profile.
- `--ignore-list` and `--ignore-remove TERM` manage ignored AI suggestions.
- `--detector-list` with `--detector-disable DETECTOR` / `--detector-enable DETECTOR` controls built-in detectors for a profile.
- `--manual-detector-add`, `--manual-detector-list`, `--manual-detector-disable`, `--manual-detector-enable`, and `--manual-detector-remove` manage operator-defined placeholder classes for explicit terms.
- `--session-init NAME`, `--session-select NAME`, `--session NAME`, `--session-clear`, `--session-status`, `--session-list`, and `--session-delete NAME` manage persistent placeholder sessions.
- `--show-secret PLACEHOLDER` reveals one local session mapping.
- `--show-secret-all` reveals all mappings for the active/current session and intentionally prints raw mapped values locally.
- `--ai-check` asks a local/private model for contextual leak warnings after deterministic redaction.
- `--ai-suggest` opens the local/private suggestion review workflow.
- `--ai-config-set`, `--ai-config-status`, and `--ai-config-clear` manage saved local AI settings.

## Known limitations

- Detector coverage is still growing.
- Unknown contextual names, organizations, projects, locations, and codenames require operator review, profile terms, or optional local AI suggestions.
- Clipboard support is best-effort and depends on the local terminal, desktop, VM, or SSH environment.
- The currently supported install path is Python/uv.

## Support

If this project has been useful to you, you can support continued development through GitHub Sponsors or Ko-fi.

Bug reports, false-positive examples, and missed-redaction reports are also useful. Please keep examples synthetic or already-redacted; do not paste real secrets, client data, or engagement evidence into issues.

## Safety model

- Local-first: normal pasted/file input is processed locally and is not saved.
- Deterministic-first: regex, structured detectors, and explicit terms are the main redaction path.
- Review-required: redaction is not a guarantee. Operators must inspect output before sharing.
- Sessions retain raw mappings locally by design. Use them only when stable labels are worth the retention.
- AI review is optional and local/private by default. It is a review layer, not proof of safety.
- Tests, examples, benchmarks, and public docs must use synthetic data only.

## Local data paths

Default local data lives under:

```text
~/.config/redloc/
~/.local/share/redloc/
```

## License and attribution

`redloc` is licensed under Apache-2.0. See `LICENSE` and `NOTICE`.

Third-party tools mentioned in documentation or benchmarks remain under their own licenses and are not bundled as runtime dependencies unless listed in project metadata.
