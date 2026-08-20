# TTY list UI guide

Some `redloc` commands open a terminal list screen when stdin and stderr are attached to a real TTY.

These screens are used when you review profile terms, ignored AI suggestions, manual detectors, profiles, or sessions.

If the command is run through a pipe or from automation, `redloc` keeps output plain and script-friendly instead.

## Common controls

Most list screens use the same rhythm:

```text
[↑/↓] move  [Enter/Space] select or toggle  [f] filter  [Esc] clear  [d] accept  [q] cancel
```

Use:

- `↑` / `↓` to move between rows.
- `Enter` or `Space` to select or toggle the highlighted row.
- `f` or `/` to filter a long list.
- `Esc` to clear the filter or leave filter typing.
- `d` to accept/apply the pending changes.
- `q` to cancel without saving changes.

While typing a filter, normal action keys are not active. Press `Enter` to finish typing the filter, then use list controls again.

## Checklist screens

Checklist screens are used by commands such as:

```bash
redloc --profile-term-list
redloc --global-term-list
redloc --ignore-list
redloc --manual-detector-list
```

Markers:

- `[x]` means enabled or active.
- `[ ]` means disabled or inactive after you accept.
- `[r]` means remove/delete after you accept.
- `[m]` means move to the paired list when available.

Pending changes are only written when you press `d` to accept. Press `q` to leave files unchanged.

Some rows support category changes:

```text
[c] change detector
```

For example, a saved term can be changed from `CLIENT: ExampleCo` to `ORG: ExampleCo` before accepting.

## Single-select screens

Single-select screens are used by commands such as:

```bash
redloc --profile-list
redloc --session-list
```

Use `Enter` or `Space` to choose the highlighted row, then press `d` to make it active.

Single-select screens do not use `[a] all` or `[n] none` controls.

## Non-TTY output

When output is captured or piped, list commands avoid full-screen controls and print plain text instead. This keeps scripts and logs predictable.

Example:

```bash
redloc --profile-list > profiles.txt
```

That path is meant for inspection and automation, not interactive editing.
