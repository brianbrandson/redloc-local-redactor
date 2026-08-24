# Install redloc

`redloc` installs two commands:

- `redloc` - redact pasted text or files
- `redloc-benchmark` - run the bundled synthetic benchmark

Status: alpha. Redaction is not a guarantee; review output before sharing it.

Raw input stays local by default. Normal paste/file input is not saved. Persistent sessions are the explicit exception: they store raw matched values locally so placeholder labels can stay stable across runs.

Release notes live in `CHANGELOG.md`.

## Requirements

- Python 3.10+
- `uv`
- Linux or macOS terminal

Check what you already have:

```bash
python3 --version
uv --version
```

If Python is missing, install it with your OS package manager:

```bash
# Debian/Ubuntu/Kali
sudo apt install python3

# Fedora
sudo dnf install python3

# macOS with Homebrew
brew install python
```

If `uv` is missing, install it from Astral:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Open a fresh shell after installing `uv`.

## Install from the repo checkout

From the repository root:

```bash
cd /path/to/redloc-local-redactor
uv tool install --editable .
```

If `redloc` is not found after install, open a fresh shell or make sure uv's tool bin directory is on `PATH`.

## Verify the install

Run:

```bash
redloc --help
redloc --about
redloc-benchmark --suite all --tool local
printf 'Email jane.doe@example.com api_key=synthetic-api-key-value-12345\n' | redloc --summary
```

Expected redaction shape:

```text
Email [EMAIL_1] api_key=[TOKEN_1]
summary: EMAIL=1 TOKEN=1 warnings=none
```

Redacted text goes to stdout. Status, profile names, summaries, and warnings go to stderr.

## Upgrade or reinstall

During alpha, reinstall editable after pulling changes:

```bash
cd /path/to/redloc-local-redactor
uv tool uninstall local-redactor || true  # old package name
uv tool uninstall redloc || true
uv tool install --editable .
```

Then verify again:

```bash
redloc --help
redloc-benchmark --suite all --tool local
```

## Basic smoke use

Paste text, then finish with Ctrl-D:

```bash
redloc --summary
```

Read from a file without modifying the original:

```bash
redloc --in raw-notes.txt --summary
```

Write a redacted file:

```bash
redloc --in raw-notes.txt --out raw-notes-redacted.txt --summary
```

For actual workflows, see `docs/operator/workflows.md`.

## Uninstall

```bash
uv tool uninstall redloc
```

This removes the installed commands. It does not delete local profiles, terms, ignored suggestions, redacted outputs, settings, or session mappings.

Raw-reveal warning: `--restore`, `--show-secret`, and `--show-secret-all` intentionally expose stored raw values. Treat terminal scrollback, recordings, shell captures/redirection, copied terminal text, and restored output files as raw-sensitive. A restored file is not redacted output.

Typical local data paths:

```text
~/.config/redloc/
~/.local/share/redloc/
```

Delete those manually only if you intentionally want to remove local tool data.
