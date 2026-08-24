# Changelog

Redloc is public alpha. Release notes are intentionally short and operator-focused: what changed, what security/privacy boundary moved, and what still needs review.

## Unreleased

### Added

- `redloc --restore` restores exact placeholders such as `[HOST_1]` and `[TOKEN_3]` from the active or explicit local session mapping.
- Restore supports pasted stdin, `--in`, `--out`, `--force`, and `--summary`.

### Fixed

- `--restore` now rejects incompatible action flags before any other dispatch can mutate session, profile, settings, copy, AI, or reveal state.
- Restored `--out` files are written with private file permissions.

### Security / privacy

- `--restore`, `--show-secret`, and `--show-secret-all` are intentional local raw-reveal actions. They can expose raw values in terminal scrollback, shell captures, recordings, copy/paste buffers, and restored output files.
- Unknown restore placeholders remain unchanged and warnings stay raw-free.

### Known limits

- Restore only works for values already captured in the selected local session. It cannot reverse stateless redaction or infer AI-invented placeholders.
- Redaction is not a guarantee; review output before sharing.

## 0.1.0 - Public alpha

### Added

- Local-first `redloc` CLI for redacting operator notes, request snippets, logs, and similar text before sharing.
- Deterministic detectors for common sensitive values such as email addresses, phone numbers, hosts, IPs, tokens, cookies, UUIDs, paths, and configured terms.
- Profile terms, global always-redact terms, ignored AI suggestions, detector controls, and manual detector categories.
- Stable placeholder sessions for local multi-paste workflows.
- Optional local/private OpenAI-compatible AI checker and suggestion workflow.
- Synthetic benchmark corpus and `redloc-benchmark` command.
- Public alpha install docs, operator workflow examples, and release gate.

### Security / privacy

- Raw input stays local by default.
- Persistent sessions are the explicit exception: they intentionally store raw placeholder mappings locally with private permissions.
- Public/cloud AI endpoints are refused by default.
- Fixtures, examples, benchmarks, and release evidence are synthetic-only.

### Known limits

- Redaction is not a guarantee; operators must review output before sharing.
- Contextual names, project code words, and business-specific identifiers may need profile terms or AI-assisted review.
- Windows `.exe` packaging is not supported yet.
