#!/usr/bin/env python3
"""
watch.py — a generic, rule-based web watcher.

Watch any number of URLs for a condition (text appears, regex matches, content
changes, or a custom detector) and get a phone push via ntfy.sh when each one
fires. Designed to run on a schedule in the cloud (GitHub Actions, triggered by
an external cron) with zero third-party dependencies.

Key properties
--------------
* Zero dependencies — stdlib only (config via `tomllib`, state via `json`).
* Pluggable rules — pick a rule per target; add your own in RULES.
* Failure-safe — fetch errors / blocked / suspicious pages are reported as
  ERROR (and pushed to you, rate-limited) instead of being silently treated as
  "nothing happened". This is the failure mode that bites naive scrapers.
* Per-target lifecycle — each target fires once, then is skipped; other targets
  keep running. State persists in .watch_state.json (committed by CI).

Config (watchers.toml)
----------------------
    [[target]]
    name = "Steam Frame"
    url  = "https://store.steampowered.com/hardware/steamframe"
    rule = "steam_commerce"          # see RULES below
    sanity = "Steam Frame"           # page must contain this or it's an ERROR
    notify_title = "Steam Frame is available!"
    enabled = true

Rules
-----
    text_appears:<substr>     fire when <substr> is present
    text_absent:<substr>      fire when <substr> is NOT present
    regex:<pattern>           fire when <pattern> matches
    regex_absent:<pattern>    fire when <pattern> does NOT match
    content_changed           fire when the page content changes vs last snapshot
    steam_commerce            fire when a Steam hardware page leaves NOTIFY_ONLY
                              (gains a reservation/purchase widget)

Usage
-----
    python3 watch.py run            # check all enabled, not-yet-fired targets
    python3 watch.py run --no-notify
    python3 watch.py list           # show targets + their state
    python3 watch.py test "Steam Frame"   # evaluate one target now (no notify)
    python3 watch.py heartbeat      # push a one-line "still alive" summary
    python3 watch.py watch --interval 300 # local continuous loop

Exit codes (run): 0 = something fired, 3 = something errored (and none fired),
                  2 = all quiet, 1 = fatal.
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sys
import time
import tomllib
import urllib.error
import urllib.request
from datetime import datetime, timezone

CONFIG_PATH = os.environ.get("WATCH_CONFIG", "watchers.toml")
STATE_PATH = os.environ.get("WATCH_STATE", ".watch_state.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
# Age-gate / language cookies so stores like Steam render the real page.
DEFAULT_COOKIE = (
    "wants_mature_content=1; birthtime=470703601; "
    "lastagecheckage=1-0-1985; Steam_Language=english"
)

HIT, WAITING, ERROR = "hit", "waiting", "error"


# --------------------------------------------------------------------------- #
# Fetch + text helpers
# --------------------------------------------------------------------------- #


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def fetch(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-US,en;q=0.9",
            "Cookie": DEFAULT_COOKIE,
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def normalize(raw: str) -> str:
    """Undo HTML-entity + backslash escaping so embedded JSON is matchable."""
    t = html.unescape(raw)
    return t.replace('\\"', '"').replace("\\/", "/")


# --------------------------------------------------------------------------- #
# Rules
# --------------------------------------------------------------------------- #
#
# A rule is a callable (target, text, state_entry) -> (outcome, detail).
#   outcome: HIT | WAITING | ERROR
#   detail:  short human string for logs / notifications
# `state_entry` is the target's persisted dict (mutable; e.g. snapshots).


def _rule_text_appears(target, text, se, arg):
    if arg is None:
        return ERROR, "text_appears needs an argument"
    return (HIT, f"found {arg!r}") if arg in text else (WAITING, f"no {arg!r} yet")


def _rule_text_absent(target, text, se, arg):
    if arg is None:
        return ERROR, "text_absent needs an argument"
    return (HIT, f"{arg!r} gone") if arg not in text else (WAITING, f"{arg!r} still present")


def _rule_regex(target, text, se, arg):
    if arg is None:
        return ERROR, "regex needs a pattern"
    try:
        m = re.search(arg, text)
    except re.error as exc:
        return ERROR, f"bad regex: {exc}"
    return (HIT, f"matched /{arg}/") if m else (WAITING, f"no match /{arg}/")


def _rule_regex_absent(target, text, se, arg):
    if arg is None:
        return ERROR, "regex_absent needs a pattern"
    try:
        m = re.search(arg, text)
    except re.error as exc:
        return ERROR, f"bad regex: {exc}"
    return (HIT, f"/{arg}/ gone") if not m else (WAITING, f"/{arg}/ still matches")


def _rule_content_changed(target, text, se, arg):
    # Optionally narrow to a regex-extracted region to reduce noise.
    region = text
    if arg:
        try:
            matches = re.findall(arg, text)
        except re.error as exc:
            return ERROR, f"bad extract regex: {exc}"
        region = "\n".join(matches)
    digest = hashlib.sha256(region.encode("utf-8", "replace")).hexdigest()
    prev = se.get("snapshot")
    se["snapshot"] = digest
    if prev is None:
        return WAITING, "baseline snapshot recorded"
    if digest != prev:
        return HIT, "content changed"
    return WAITING, "unchanged"


# --- Steam-specific detector (ported from the original Steam Frame watcher) -- #

_WIDGET_RE = re.compile(r'"internal_type":"([^"]+)"')
_PURCHASE_WIDGET_RE = re.compile(r"cart|purchase|buy|while_supplies_last|checkout", re.I)
_PRICE_TAG_RE = re.compile(r"\[price packageid=\d+[^\]]*\]")


def _rule_steam_commerce(target, text, se, arg):
    """Steam hardware page left NOTIFY_ONLY (gained reservation/purchase widget)."""
    widgets = sorted(set(_WIDGET_RE.findall(text)))
    purchase = (
        any(_PURCHASE_WIDGET_RE.search(w) for w in widgets)
        or '"purchase_package":' in text
        or "localized_out_of_stock_override" in text
        or any("display=reservation" not in t for t in _PRICE_TAG_RE.findall(text))
    )
    reservation = "reservation_widget" in widgets or '"reservation_package":' in text

    if purchase:
        return HIT, "FOR SALE (purchase widget / buy tags present)"
    if reservation:
        return HIT, "reservation/waitlist open (reservation widget present)"
    if widgets:
        return HIT, f"commerce widget appeared: {widgets}"
    # No commerce widget at all. If it doesn't even look like a Steam page,
    # treat as ERROR so we don't silently sit on a blocked/changed page.
    if "Notify me" in text or "add_to_wishlist" in text:
        return WAITING, "still NOTIFY_ONLY (no commerce widget)"
    return ERROR, "no commerce widget and page looks unexpected (blocked?)"


RULES = {
    "text_appears": _rule_text_appears,
    "text_absent": _rule_text_absent,
    "text_disappears": _rule_text_absent,  # alias
    "regex": _rule_regex,
    "regex_absent": _rule_regex_absent,
    "content_changed": _rule_content_changed,
    "steam_commerce": _rule_steam_commerce,
}


def eval_rule(rule_str: str, target: dict, text: str, se: dict):
    name, _, arg = rule_str.partition(":")
    arg = arg if _ else None
    fn = RULES.get(name.strip())
    if fn is None:
        return ERROR, f"unknown rule {name!r} (have: {', '.join(RULES)})"
    return fn(target, text, se, arg)


# --------------------------------------------------------------------------- #
# Notifications (ntfy.sh)
# --------------------------------------------------------------------------- #


def ntfy_topic_for(target: dict) -> str | None:
    # Per-target override is allowed but, since configs may live in a public
    # repo, the default is a single shared topic supplied via env (a secret).
    return target.get("notify_topic") or os.environ.get("NTFY_TOPIC")


def push(topic: str, title: str, message: str, *, priority: str = "default",
         tags: str = "", click: str = "") -> bool:
    headers = {"Title": title, "Priority": priority}
    if tags:
        headers["Tags"] = tags
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}",
        data=message.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            return True
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        print(f"  ! ntfy push failed: {exc}", file=sys.stderr)
        return False


def notify_hit(target, detail):
    topic = ntfy_topic_for(target)
    if not topic:
        print("  ! no NTFY_TOPIC set; skipping push", file=sys.stderr)
        return
    name = target["name"]
    url = target["url"]
    push(
        topic,
        title=target.get("notify_title") or f"{name} fired!",
        message=f"{detail}\n{url}",
        priority="urgent",
        tags="rotating_light",
        click=url,
    )


def notify_error(target, detail):
    topic = ntfy_topic_for(target)
    if not topic:
        return
    push(
        topic,
        title=f"Watcher can't read: {target['name']}",
        message=f"{detail}\n{target['url']}",
        priority="low",
        tags="warning",
    )


def notify_recovered(target):
    topic = ntfy_topic_for(target)
    if not topic:
        return
    push(
        topic,
        title=f"Watcher recovered: {target['name']}",
        message="Reading the page again normally.",
        priority="min",
        tags="white_check_mark",
    )


# --------------------------------------------------------------------------- #
# Config + state
# --------------------------------------------------------------------------- #


def load_config(path: str) -> list[dict]:
    with open(path, "rb") as fh:
        data = tomllib.load(fh)
    targets = data.get("target", [])
    for i, t in enumerate(targets):
        if "name" not in t or "url" not in t:
            raise ValueError(f"target #{i} needs both 'name' and 'url'")
        t.setdefault("rule", "content_changed")
        t.setdefault("enabled", True)
    return targets


def load_state(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {"targets": {}}


def save_state(path: str, state: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
        fh.write("\n")


# --------------------------------------------------------------------------- #
# Core run
# --------------------------------------------------------------------------- #


def check_target(target: dict, se: dict, do_notify: bool):
    """Evaluate one target, mutate its state entry, notify. Returns outcome."""
    try:
        text = normalize(fetch(target["url"]))
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, ValueError) as exc:
        outcome, detail = ERROR, f"fetch failed: {exc}"
    else:
        sanity = target.get("sanity")
        if sanity and sanity not in text:
            outcome, detail = ERROR, f"sanity token missing: {sanity!r} (blocked/changed?)"
        else:
            outcome, detail = eval_rule(target["rule"], target, text, se)

    if outcome == HIT:
        se["status"] = "fired"
        se["fired_at"] = now_iso()
        se["error_notified"] = False
        se.pop("last_error", None)
        if do_notify:
            notify_hit(target, detail)
    elif outcome == ERROR:
        se["status"] = "error"
        se["last_error"] = detail
        if do_notify and not se.get("error_notified"):
            notify_error(target, detail)
            se["error_notified"] = True
    else:  # WAITING
        recovered = se.get("error_notified")
        se["status"] = "waiting"
        se.pop("last_error", None)
        if recovered:
            se["error_notified"] = False
            if do_notify:
                notify_recovered(target)
    return outcome, detail


def run(config_path: str, state_path: str, do_notify: bool = True):
    targets = load_config(config_path)
    state = load_state(state_path)
    tstate = state.setdefault("targets", {})

    any_hit = any_error = False
    enabled_targets = []
    print(f"watch run — {now_iso()}")

    for t in targets:
        name = t["name"]
        se = tstate.setdefault(name, {"status": "waiting", "error_notified": False})
        if not t.get("enabled", True):
            se["status"] = "disabled"
            print(f"  - {name}: disabled (skipped)")
            continue
        enabled_targets.append(name)
        if se.get("status") == "fired":
            print(f"  - {name}: already fired ({se.get('fired_at')}) — skipped")
            continue

        outcome, detail = check_target(t, se, do_notify)
        icon = {HIT: "[HIT]", WAITING: "[...]", ERROR: "[ERR]"}[outcome]
        print(f"  {icon} {name}: {detail}")
        any_hit = any_hit or outcome == HIT
        any_error = any_error or outcome == ERROR

    save_state(state_path, state)

    all_done = bool(enabled_targets) and all(
        tstate.get(n, {}).get("status") == "fired" for n in enabled_targets
    )
    code = 0 if any_hit else (3 if any_error else 2)

    gh_out = os.environ.get("GITHUB_OUTPUT")
    if gh_out:
        with open(gh_out, "a", encoding="utf-8") as fh:
            fh.write(f"code={code}\n")
            fh.write(f"all_done={'true' if all_done else 'false'}\n")

    print(f"  => code={code} all_done={all_done}")
    return code


def cmd_list(config_path: str, state_path: str):
    targets = load_config(config_path)
    tstate = load_state(state_path).get("targets", {})
    print(f"{'STATUS':<9} {'RULE':<22} NAME")
    for t in targets:
        st = tstate.get(t["name"], {}).get("status", "waiting")
        en = "" if t.get("enabled", True) else " (disabled)"
        print(f"{st:<9} {t['rule']:<22} {t['name']}{en}")
    return 0


def cmd_test(config_path: str, name: str):
    targets = {t["name"]: t for t in load_config(config_path)}
    t = targets.get(name)
    if not t:
        print(f"No target named {name!r}. Have: {', '.join(targets)}", file=sys.stderr)
        return 1
    se = {}
    outcome, detail = check_target(t, se, do_notify=False)
    print(f"{name}: {outcome.upper()} — {detail}")
    return 0


def cmd_heartbeat(config_path: str, state_path: str):
    targets = load_config(config_path)
    tstate = load_state(state_path).get("targets", {})
    enabled = [t for t in targets if t.get("enabled", True)]
    counts = {"waiting": 0, "fired": 0, "error": 0}
    for t in enabled:
        st = tstate.get(t["name"], {}).get("status", "waiting")
        counts[st] = counts.get(st, 0) + 1
    msg = (f"{len(enabled)} active: {counts['waiting']} waiting, "
           f"{counts['fired']} fired, {counts['error']} error.")
    print("heartbeat:", msg)
    topic = os.environ.get("NTFY_TOPIC")
    if topic:
        prio = "low" if counts["error"] == 0 else "default"
        push(topic, title="Watcher heartbeat", message=msg,
             priority=prio, tags="eyes" if counts["error"] == 0 else "warning")
    return 0


def cmd_watch(config_path: str, state_path: str, interval: int, do_notify: bool):
    print(f"Watching every {interval}s. Ctrl-C to stop.\n")
    try:
        while True:
            run(config_path, state_path, do_notify=do_notify)
            print(f"  next check in {interval}s ...\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generic rule-based web watcher.")
    p.add_argument("--config", default=CONFIG_PATH)
    p.add_argument("--state", default=STATE_PATH)
    sub = p.add_subparsers(dest="cmd")

    pr = sub.add_parser("run", help="check all enabled targets (default)")
    pr.add_argument("--no-notify", action="store_true")

    sub.add_parser("list", help="show targets and their state")

    pt = sub.add_parser("test", help="evaluate one target now (no notify)")
    pt.add_argument("name")

    sub.add_parser("heartbeat", help="push a one-line status summary")

    pw = sub.add_parser("watch", help="continuous local loop")
    pw.add_argument("--interval", type=int, default=300)
    pw.add_argument("--no-notify", action="store_true")

    args = p.parse_args(argv)

    try:
        if args.cmd in (None, "run"):
            return run(args.config, args.state,
                       do_notify=not getattr(args, "no_notify", False))
        if args.cmd == "list":
            return cmd_list(args.config, args.state)
        if args.cmd == "test":
            return cmd_test(args.config, args.name)
        if args.cmd == "heartbeat":
            return cmd_heartbeat(args.config, args.state)
        if args.cmd == "watch":
            return cmd_watch(args.config, args.state, args.interval,
                             do_notify=not args.no_notify)
    except FileNotFoundError as exc:
        print(f"Config not found: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 — top-level guard
        print(f"Fatal: {exc}", file=sys.stderr)
        return 1
    return 1


if __name__ == "__main__":
    sys.exit(main())
