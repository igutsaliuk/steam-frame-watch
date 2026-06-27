# Web Watcher — full setup reference

This is the one-time shared infrastructure. After this, every new watch is just a
`[[target]]` block in `watchers.toml`.

## 1. Repo

Copy `templates/watch.py`, `templates/watchers.toml`, and
`templates/watch.yml` (to `.github/workflows/watch.yml`) into a repo. Create an
initial state file and commit:

```bash
echo '{ "targets": {} }' > .watch_state.json
git add . && git commit -m "web watcher" && gh repo create my-watchers --public --source=. --push
```

Use a **public** repo: every-5-min triggering on a private repo blows past the
2,000 free Actions minutes/month; public repos get unlimited minutes. Configs
hold no secrets (the ntfy topic is a GitHub secret), so public is safe.

## 2. Phone notifications (ntfy — free, no signup)

- Install the **ntfy** app (iOS/Android), subscribe to a random topic name,
  e.g. `my-watch-9f3kq7x2` (the topic name is effectively a password).
- Store it as a secret: `gh secret set NTFY_TOPIC` (paste, then Ctrl-D).

iOS: enable Time Sensitive notifications for ntfy and exclude it from Focus so
`urgent` pushes break through a locked phone. Android: disable battery
optimization for ntfy.

## 3. Reliable 5-minute trigger (external cron)

GitHub's own `schedule` is throttled/dropped for short intervals (`*/5` often
runs every ~30 min). `workflow_dispatch` is honored immediately, so drive it
externally.

1. **Fine-grained GitHub PAT**: github.com → Settings → Developer settings →
   Fine-grained tokens. Repository access: only this repo. Permissions:
   **Actions: Read and write** (Metadata: Read auto-added). Set an expiry.

2. **cron-job.org job** ([cron-job.org](https://cron-job.org), free):
   - URL: `https://api.github.com/repos/<owner>/<repo>/actions/workflows/watch.yml/dispatches`
   - Method: `POST`; Body: `{"ref":"main"}`
   - Headers:
     ```
     Accept: application/vnd.github+json
     Authorization: Bearer github_pat_...
     X-GitHub-Api-Version: 2022-11-28
     Content-Type: application/json
     ```
   - Schedule: every 5 minutes. A `204` response = success.

   You can also create it via the cron-job.org API:
   `PUT https://api.cron-job.org/jobs` with `Authorization: Bearer <account-api-key>`
   and a body describing the job (see their docs). Note the returned `jobId`.

## 4. Clean self-shutdown (optional but recommended)

So the workflow can disable the external trigger when all targets fire:

```bash
printf '%s' '<cron-job.org account API key>' | gh secret set CRONJOB_API_KEY
gh variable set CRONJOB_ID --body '<jobId>'
```

The `Stop everything` step in `watch.yml` then disables both the workflow and
the cron-job.org job once `all_done == true`.

## 5. Verify

```bash
gh workflow run watch.yml && gh run watch        # manual trigger
python3 watch.py test "<target name>"            # local rule check
```

## Updating / rotating

- New ntfy topic or rotated cron-job.org key: `gh secret set NTFY_TOPIC` /
  `gh secret set CRONJOB_API_KEY` (takes effect next run).
- Rotated GitHub PAT: update the `Authorization` header in the cron-job.org job
  (the PAT lives there, not in GitHub secrets).

## Re-arming after everything fired

```bash
gh workflow enable watch.yml
# re-enable the cron-job.org job in their dashboard or via API
```

To re-arm a single target, remove its entry from `.watch_state.json` (or set its
`status` away from `fired`) and commit.

## Gotchas

- Each third-party action bundles a Node version; keep `actions/checkout` current
  to avoid Node deprecation warnings. The watcher itself uses system `python3`
  (3.11+ for `tomllib`) — no `pip install`, no `setup-python` needed.
- GitHub auto-disables scheduled workflows after 60 days without repo commits;
  the daily state commits / heartbeat keep it alive.
- `content_changed` on a full page can be noisy (CSRF tokens, timestamps); narrow
  it with `content_changed:<extract-regex>` or prefer a precise text/regex rule.
