---
name: web-watcher
description: Watch any web page(s) for a condition (text appears/disappears, regex match, content change, or a custom detector) and get a phone push when it fires, running free in the cloud on a schedule with clean self-shutdown. Use when the user wants to monitor a website for availability/restock/price/status changes, "tell me when X is in stock / on sale / available", set up an uptime or change watcher, or build a "notify me when this page changes" automation.
---

# Web Watcher

A zero-dependency, rule-based pattern for monitoring URLs and getting a phone
push when each one fires. One shared cloud setup serves unlimited targets — no
new repo/token/cron per task. Templates are in `templates/`.

## Architecture (one-time shared infra, reused by every target)

```
cron-job.org (every N min) --workflow_dispatch--> GitHub Actions --> watch.py
   reliable timing (GitHub's own `schedule` is throttled for short intervals)
```

- **One repo** holds `watch.py` + `watchers.toml` + `.github/workflows/watch.yml`.
- **One GitHub fine-grained PAT** (Actions: read/write on that repo) lives in the
  cron-job.org request header — triggers the workflow on time.
- **One ntfy topic** (`NTFY_TOPIC` secret) receives all pushes. ntfy needs no
  token; the topic name is the address. Install the ntfy phone app, subscribe.
- **One cron-job.org account key** (`CRONJOB_API_KEY` secret + `CRONJOB_ID` var)
  lets the workflow turn the external trigger off when everything's done.

Adding target #2..N = appending a `[[target]]` block to `watchers.toml`. Nothing
else. See [REFERENCE.md](REFERENCE.md) for full provisioning steps.

## Add a target

```toml
[[target]]
name = "My thing"
url  = "https://example.com/page"
rule = "text_appears:In stock"
sanity = "My Store"              # page must contain this or it's ERROR
notify_title = "It's in stock!"  # optional
enabled = true
```

### Rules (extend in the `RULES` dict of `watch.py`)

| Rule | Fires when |
|---|---|
| `text_appears:<s>` / `text_absent:<s>` | substring present / gone |
| `regex:<p>` / `regex_absent:<p>` | pattern matches / stops matching |
| `content_changed[:<extract-regex>]` | page (or extracted region) changes vs snapshot |
| `steam_commerce` | a Steam hardware page leaves "Notify me" |

## Non-negotiable: failure safety

A naive watcher silently treats a blocked/changed/errored fetch as "nothing
happened" and goes blind. This pattern reports those as **ERROR** (pushed once,
until recovery) via:

- a `sanity` token that must appear in a healthy page, and
- custom detectors returning ERROR (not WAITING) on unexpected pages.

Always set a `sanity` token for real targets.

## Lifecycle

Each target fires **once** then is skipped (state in `.watch_state.json`, committed
by CI). When ALL enabled targets have fired, the workflow disables itself **and**
the cron-job.org job. A daily heartbeat pushes `waiting/fired/error` counts.

## Local commands

```bash
export NTFY_TOPIC=your-topic
python3 watch.py run                  # one pass
python3 watch.py test "My thing"      # evaluate one target, no push
python3 watch.py list
python3 watch.py watch --interval 300 # local loop
```

Reference implementation: https://github.com/igutsaliuk/steam-frame-watch
