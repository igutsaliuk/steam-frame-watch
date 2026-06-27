# watchers

A generic, rule-based web watcher. Point it at any URLs, pick a rule per target,
and get a phone push (via [ntfy.sh](https://ntfy.sh)) the moment each one fires.
Runs free in the cloud on GitHub Actions, triggered every few minutes by an
external cron. Zero third-party dependencies (Python stdlib only).

## How it works

```
cron-job.org (every 5 min)
      │  workflow_dispatch (honored immediately, unlike GitHub's throttled cron)
      ▼
GitHub Actions  ──►  python3 watch.py run
      │                 ├─ checks each enabled target in watchers.toml
      │                 ├─ pushes to ntfy.sh when a target fires
      │                 └─ writes .watch_state.json (committed back)
      └─ when ALL targets fired ──► disables the workflow + the cron job
```

Each target fires **once**, then is skipped; other targets keep running. The
whole system shuts itself down cleanly once everything has fired.

## Add a target

Edit `watchers.toml`:

```toml
[[target]]
name = "My thing"
url  = "https://example.com/page"
rule = "text_appears:In stock"
sanity = "My Store"            # optional: page must contain this or it's ERROR
notify_title = "It's in stock!"  # optional
enabled = true
```

Commit & push — that's it. No new repo, token, or cron job per target.

### Rules

| Rule | Fires when |
|---|---|
| `text_appears:<substr>` | `<substr>` shows up on the page |
| `text_absent:<substr>` | `<substr>` disappears |
| `regex:<pattern>` | `<pattern>` matches |
| `regex_absent:<pattern>` | `<pattern>` stops matching |
| `content_changed` | the page content changes vs the last snapshot |
| `content_changed:<regex>` | a regex-extracted region changes (less noisy) |
| `steam_commerce` | a Steam hardware page leaves "Notify me" (gains a reservation/purchase widget) |

Add your own in the `RULES` dict in `watch.py`.

### Failure safety

Fetch errors, blocked/rate-limited responses, and pages that fail their `sanity`
check are reported as **ERROR** (and pushed to you once, until they recover) —
*not* silently treated as "nothing happened". This avoids the classic trap where
a scraper goes blind (e.g. the host blocks the CI IP) and you never get alerted.

## Local use

```bash
export NTFY_TOPIC=your-topic
python3 watch.py run            # one pass over all enabled targets
python3 watch.py list           # show targets + state
python3 watch.py test "My thing"  # evaluate one target now (no push)
python3 watch.py watch --interval 300  # continuous local loop
python3 watch.py heartbeat      # push a "still alive" summary
```

Exit codes for `run`: `0` something fired, `3` something errored (none fired),
`2` all quiet, `1` fatal.

## Cloud setup (once)

Shared infrastructure — set up a single time, reused by every target:

- **Repo**: this one (public → free Actions minutes).
- **`NTFY_TOPIC`** secret: your shared ntfy topic. `gh secret set NTFY_TOPIC`
- **External 5-min trigger**: a [cron-job.org](https://cron-job.org) job doing
  `POST .../actions/workflows/watch.yml/dispatches` with a fine-grained GitHub
  PAT (Actions: read/write on this repo) in the `Authorization` header.
- **Clean-stop wiring** (optional): `CRONJOB_API_KEY` secret + `CRONJOB_ID`
  variable so the workflow can disable the cron job when all targets fire.

The daily heartbeat (`0 16 * * *`) reports `waiting / fired / error` counts so
you know it's alive.
