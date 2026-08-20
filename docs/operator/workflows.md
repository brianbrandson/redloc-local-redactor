# Operator workflows

Synthetic-only examples for using `redloc` in real operator habits: reports, tickets, chat handoff, Caido-style notes, profile terms, local AI suggestion review, and bring-your-own benchmark corpora.

Safety rule: No real client names, real secrets, engagement evidence, raw customer logs, screenshots, tokens, or copied HTTP traffic belong in this file. Translate every example into synthetic values first.

## Workflow: redact a Caido-style request summary for chat or a report note

Use when you have a short request/response note and need a safe snippet to paste into a ticket, report draft, or teammate chat.

Synthetic input:

```text
Caido-style request summary
Target: https://portal.example.test/api/v1/users?email=jane.operator@example.test&ticket=1248
Client: ExampleCo
Project: Project Squirrel
Source: 203.0.113.42
Authorization: Bearer plainSyntheticSecret123
Cookie: sessionid=plainSyntheticSecret456; theme=light
Finding note: user enumeration behaves differently for Jane Operator.
```

One-shot command from a checkout:

```bash
cat synthetic-request-summary.txt | uv run redloc --term ExampleCo --term "Project Squirrel" --summary
```

Installed command shape:

```bash
cat synthetic-request-summary.txt | redloc --term ExampleCo --term "Project Squirrel" --summary
```

Expected operator checks:

- Host is redacted while path/query structure stays useful, for example `https://[HOST_1]/api/v1/users?...`.
- Email, public IP, bearer token, cookie value, and configured client/project terms become placeholders.
- The route, query parameter names, and non-sensitive troubleshooting shape remain readable.
- The summary goes to stderr; redacted text stays on stdout for pipes and copy/paste.

## Workflow: set up a reusable profile for a project

Use when you keep redacting snippets for the same synthetic client or project and do not want to pass `--term` every time.

```bash
redloc --profile-init exampleco
redloc --profile-term-add
```

Paste synthetic terms, then finish stdin:

```text
ExampleCo
Project Squirrel
ExampleCo VPN
```

Then redact normally:

```bash
cat synthetic-request-summary.txt | redloc --summary
```

Inspect or clean profile terms without opening the file directly:

```bash
redloc --profile-term-list
redloc --profile-term-remove "ExampleCo VPN"
```

Add a custom manual detector when a project has a recurring class of terms that should get its own placeholder. Manual detectors are placeholder classes for saved terms, not regex detectors:

```bash
redloc --manual-detector-add
# paste: plate number

redloc --profile-term-add
# paste: PLATE_NUMBER: EV7878
```

After that, `EV7878` redacts as `[PLATE_NUMBER_1]` whenever the `exampleco` profile is used. To inspect or change the available manual detectors:

```bash
redloc --manual-detector-list
redloc --manual-detector-disable "PLATE NUMBER"
redloc --manual-detector-enable "PLATE NUMBER"
redloc --manual-detector-remove "PLATE NUMBER"
```

Built-in detectors are separate from manual detectors. Use `--detector-list` only when you intentionally want to enable/disable deterministic engines such as `PATH` or `PUBLIC_IP` for this profile:

```bash
redloc --detector-list
redloc --detector-list --detector-disable PATH
redloc --detector-list --detector-enable PATH
```

Profile reminders:

- Profile terms are local files under the tool config directory.
- Manual detectors do not auto-discover new shapes; they only name placeholders for explicit profile terms.
- Raw pasted input is not saved by normal profile use.
- Persistent sessions are different: sessions intentionally store raw matched values locally so placeholders remain stable across runs.

## Workflow: set up global always-redact terms

Use this for operator/self/company/default identifiers that should redact in every project, before any profile-specific terms are loaded.

```bash
redloc --global-term-add
```

Paste synthetic terms, then finish stdin:

```text
PERSON: Operator One
ORG: Home Lab UAB
CONTEXT: operator.example.test
```

Then redact normally, with or without an active profile:

```bash
printf 'Operator One from Home Lab UAB reviewed Project Squirrel\n' | redloc --profile exampleco --summary
```

Clean global terms without opening the file directly:

```bash
redloc --global-term-list
redloc --global-term-remove "Operator One"
```

Global reminders:

- Global terms are local raw-sensitive configuration and should describe your own recurring defaults, not client-specific context.
- Profile terms remain the right place for engagement/client/project names.
- Global terms load before active profile terms and use the same typed placeholder format.

## Workflow: keep placeholder labels stable across related snippets

Use a session when several snippets belong to the same thread and you want `[HOST_1]`, `[EMAIL_1]`, and other labels to keep meaning across separate runs.

```bash
redloc --session-init exampleco-web-thread
cat synthetic-request-1.txt | redloc
cat synthetic-request-2.txt | redloc
redloc --session-clear
```

Session warning:

- Sessions store the original matched values locally by design.
- Do not use a persistent session for data you do not want retained on disk.
- Prefer no session for one-off cloud/chat/report-safe redaction.

## Workflow: use local AI suggestions as an operator review layer

Use when deterministic redaction has already run, but visible contextual terms may remain: people, organizations, projects, locations, codenames, or other report-sensitive words.

The endpoint must be local or private by default. This example uses a local OpenAI-compatible endpoint:

```bash
cat synthetic-request-summary.txt | redloc \
  --ai-suggest \
  --ai-endpoint http://127.0.0.1:8080/v1/chat/completions \
  --ai-model gpt-oss
```

What happens:

- Deterministic redaction runs first.
- The model sees already-redacted output by default, not raw detector hits.
- The review screen may show still-visible candidate terms so the operator can accept, ignore, or recategorize them.
- Accepted terms are written to the active profile and redaction reruns on the original input.
- Ignored terms are stored in the profile ignore list and filtered from future suggestion reviews for that profile.

Useful keys in a real terminal:

```text
[↑/↓] move  [Enter/Space] toggle  [i] ignore  [c] change category  [a] all  [n] none  [d] accept  [q] cancel
```

Non-TTY fallback examples:

```text
i 2
c 3 project

```

The blank line accepts the currently checked suggestions in the fallback prompt.

## Workflow: benchmark your own synthetic corpus

Use this when you want to check whether the tool catches your workflow shape without committing private text.

Write a starter template:

```bash
redloc-benchmark --write-template my-synthetic-corpus.json
```

Run it back through the local adapter:

```bash
redloc-benchmark --corpus my-synthetic-corpus.json --suite all --tool local
```

Rules for a useful BYO corpus:

- Rewrite every example with synthetic domains such as `example.test`.
- Use documentation/test IP ranges such as `203.0.113.42`, `198.51.100.23`, or internal lab ranges such as `10.10.10.15`.
- Use fake marker strings such as `plainSyntheticSecret123`, not real provider-shaped tokens.
- Include the safe structure you need preserved, such as route paths, header names, finding context, and non-sensitive status codes.
- Add `must_redact` values for synthetic sensitive strings and `must_contain` values for safe expected placeholders or route context.
- Keep reports raw-free and share only benchmark output after reviewing it.

Tiny synthetic case shape:

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

## Before sharing redacted output

Quick review checklist:

- No real client names remain.
- No personal names, org names, project names, locations, ticket IDs, or codenames remain unless you deliberately kept them.
- No provider-shaped tokens, cookies, private keys, passwords, or session IDs remain.
- URL paths and query names are safe enough for the destination.
- Session use was intentional if stable labels persisted across runs.
- AI suggestions were reviewed by the operator; they are not automatic proof of safety.
