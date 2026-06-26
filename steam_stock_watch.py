#!/usr/bin/env python3
"""
steam_stock_watch.py — detect when a Steam hardware product becomes purchasable.

How it works
------------
Steam hardware pages ship a big (double-escaped) JSON blob in the HTML that
renders commerce UI from typed widgets: {"internal_type": "<widget>"}. Reading
that widget set is far more robust than matching one product's bespoke package
names, because the recently-launched products share the same schema. Observed
live (Jun 2026):

    Steam Frame      -> NO commerce widget                     => NOTIFY_ONLY
    Steam Controller -> reservation_widget                     => RESERVATION_OPEN
    Steam Machine    -> reservation_widget                     => RESERVATION_OPEN
    Steam Deck       -> reservation_widget + while_supplies_last
                        + ~29 add-to-cart price tags           => FOR_SALE

Frame is the *closest* to Controller/Machine, so its likely path is:
    NOTIFY_ONLY  ->  RESERVATION_OPEN  ->  FOR_SALE

For new Valve hardware the reservation/lottery opening is usually the actionable
moment, so the watcher alerts on ANY upward transition (not only "buy now").
Detection is therefore based on:
    FOR_SALE         -> a purchase-type widget (cart/purchase/while_supplies_last),
                        `purchase_package`, `localized_out_of_stock_override`,
                        or real (non-reservation) `[price packageid=N]` buy tags.
    RESERVATION_OPEN -> a `reservation_widget` / `reservation_package`.
    NOTIFY_ONLY      -> no commerce widget (wishlist / "Notify me" only).

Usage
-----
    # one-shot status of the upcoming product (default: Steam Frame)
    python3 steam_stock_watch.py

    # one-shot status of everything
    python3 steam_stock_watch.py --all

    # watch Steam Frame and alert the moment it's available for ANYTHING
    # (reservation OR sale) — i.e. it leaves the NOTIFY_ONLY stage
    python3 steam_stock_watch.py --watch --interval 300

    # only care about actual purchase (ignore reservation)
    python3 steam_stock_watch.py --watch --sale-only

    # watch specific products and open the page in a browser on availability
    python3 steam_stock_watch.py --watch frame machine --open

Exit codes (handy for cron / CI):
    0 = a watched product is available (reservation or sale;
        with --sale-only: purchasable)
    2 = still NOTIFY_ONLY / nothing actionable yet
    1 = a hard error (network, etc.)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime

# --------------------------------------------------------------------------- #
# Products
# --------------------------------------------------------------------------- #

PRODUCTS: dict[str, dict[str, str]] = {
    "deck": {
        "name": "Steam Deck",
        "url": "https://store.steampowered.com/steamdeck",
    },
    "machine": {
        "name": "Steam Machine",
        "url": "https://store.steampowered.com/hardware/steammachine",
    },
    "frame": {
        "name": "Steam Frame",
        "url": "https://store.steampowered.com/hardware/steamframe",
    },
    "controller": {
        "name": "Steam Controller",
        "url": "https://store.steampowered.com/hardware/steamcontroller",
    },
}

# Product(s) to watch by default: the upcoming, not-yet-buyable one.
DEFAULT_TARGETS = ["frame"]

# --------------------------------------------------------------------------- #
# State detection
# --------------------------------------------------------------------------- #
#
# Steam hardware pages render commerce UI from typed widgets embedded as
#   {"internal_type": "<widget>"}
# inside a (double-escaped) JSON blob. Reading that widget set is far more
# robust than matching Deck-specific package names, because the not-yet-released
# products (Frame today; Controller/Machine recently) all share this schema:
#
#   Frame      -> NO commerce widget at all          (Notify me / wishlist only)
#   Controller -> reservation_widget                 (reserve / lottery)
#   Machine    -> reservation_widget                 (reserve / lottery)
#   Deck       -> reservation_widget + while_supplies_last + buy price tags
#                                                     (actually purchasable)
#
# So the real "became available" event for Frame is: a commerce widget appears,
# and specifically a PURCHASE-capable one (or real add-to-cart price tags).

# A widget whose internal_type contains any of these is a "you can buy it" widget.
PURCHASE_WIDGET_RE = re.compile(r"cart|purchase|buy|while_supplies_last|checkout", re.I)

# Inventory / checkout machinery that only exists once a SKU is actually sellable.
PURCHASE_MARKERS = (
    '"purchase_package":',
    "localized_out_of_stock_override",
)

# Reservation / waitlist signal.
RESERVATION_WIDGET = "reservation_widget"
RESERVATION_MARKERS = ('"reservation_package":',)

# Best-effort hints that purchasable stock is currently exhausted. NOTE: the
# localized override text can be embedded even when in stock (it's the label to
# show *if* OOS), so we only treat the rendered/standalone tokens as a soft hint.
OUT_OF_STOCK_HINTS = (
    "lebutton_soldout",
    "Sold Out",
)


def normalize(raw: str) -> str:
    """Undo HTML-entity + backslash escaping so the embedded JSON is matchable."""
    t = html.unescape(raw)
    return t.replace('\\"', '"').replace("\\/", "/")


def widget_types(text: str) -> list[str]:
    """All distinct {"internal_type": "..."} widget names rendered on the page."""
    return sorted(set(re.findall(r'"internal_type":"([^"]+)"', text)))


def buy_price_tags(text: str) -> list[str]:
    """`[price packageid=N ...]` tags that drive add-to-cart (not reservations)."""
    tags = re.findall(r"\[price packageid=\d+[^\]]*\]", text)
    return [t for t in tags if "display=reservation" not in t]

STATE_FOR_SALE = "FOR_SALE"
STATE_RESERVATION = "RESERVATION_OPEN"
STATE_NOTIFY = "NOTIFY_ONLY"
STATE_UNKNOWN = "UNKNOWN"

STATE_RANK = {
    STATE_UNKNOWN: 0,
    STATE_NOTIFY: 1,
    STATE_RESERVATION: 2,
    STATE_FOR_SALE: 3,
}

STATE_LABEL = {
    STATE_FOR_SALE: "FOR SALE  (add-to-cart / re-sale available)",
    STATE_RESERVATION: "reservation / waitlist open",
    STATE_NOTIFY: "coming soon (notify me only)",
    STATE_UNKNOWN: "unknown (page layout changed?)",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    # Age-gate / region cookies so the store renders the real product page.
    "Cookie": (
        "wants_mature_content=1; birthtime=470703601; "
        "lastagecheckage=1-0-1985; Steam_Language=english"
    ),
}

STATE_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), ".steam_stock_state.json"
)


# --------------------------------------------------------------------------- #
# Core logic
# --------------------------------------------------------------------------- #


@dataclass
class Status:
    key: str
    name: str
    url: str
    state: str
    available: bool          # available for ANYTHING (reservation OR sale)
    for_sale: bool           # specifically purchasable / add-to-cart
    out_of_stock_hint: bool
    widgets: list
    matched: dict
    checked_at: str
    error: str | None = None


def fetch_html(url: str, timeout: int = 30) -> str:
    req = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        charset = resp.headers.get_content_charset() or "utf-8"
        return resp.read().decode(charset, errors="replace")


def _found(text: str, markers: tuple[str, ...]) -> list[str]:
    return [m for m in markers if m in text]


def classify(raw: str) -> tuple[str, list, dict, bool]:
    """Return (state, widget_types, matched_markers_by_category, oos_hint)."""
    text = normalize(raw)
    widgets = widget_types(text)

    purchase_widgets = [w for w in widgets if PURCHASE_WIDGET_RE.search(w)]
    purchase_markers = _found(text, PURCHASE_MARKERS)
    price_tags = buy_price_tags(text)

    has_purchase = bool(purchase_widgets or purchase_markers or price_tags)
    has_reservation = (RESERVATION_WIDGET in widgets) or bool(
        _found(text, RESERVATION_MARKERS)
    )
    oos = _found(text, OUT_OF_STOCK_HINTS)

    matched = {
        "purchase_widgets": purchase_widgets,
        "purchase_markers": purchase_markers,
        "buy_price_tags": len(price_tags),
        "reservation": (
            [RESERVATION_WIDGET] if RESERVATION_WIDGET in widgets else []
        ) + _found(text, RESERVATION_MARKERS),
        "out_of_stock_hint": oos,
    }

    if has_purchase:
        state = STATE_FOR_SALE
    elif has_reservation:
        state = STATE_RESERVATION
    elif "Notify me" in text or "add_to_wishlist" in text or widgets == []:
        state = STATE_NOTIFY
    else:
        state = STATE_UNKNOWN

    return state, widgets, matched, bool(oos)


def check(key: str, timeout: int = 30) -> Status:
    p = PRODUCTS[key]
    now = datetime.now().isoformat(timespec="seconds")
    try:
        html = fetch_html(p["url"], timeout=timeout)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as exc:
        return Status(
            key=key, name=p["name"], url=p["url"], state=STATE_UNKNOWN,
            available=False, for_sale=False, out_of_stock_hint=False,
            widgets=[], matched={}, checked_at=now, error=str(exc),
        )

    state, widgets, matched, oos = classify(html)
    # "Available for anything" = the page now renders any commerce widget
    # (reservation OR purchase), i.e. it has left the NOTIFY_ONLY stage.
    available = bool(widgets) or state in (STATE_RESERVATION, STATE_FOR_SALE)
    return Status(
        key=key, name=p["name"], url=p["url"], state=state,
        available=available, for_sale=(state == STATE_FOR_SALE),
        out_of_stock_hint=oos, widgets=widgets, matched=matched, checked_at=now,
    )


# --------------------------------------------------------------------------- #
# State persistence (so repeated runs only alert on *change*)
# --------------------------------------------------------------------------- #


def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, indent=2)
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# Alerting
# --------------------------------------------------------------------------- #


def alert(status: Status, prior: str | None = None, open_browser: bool = False) -> None:
    title = f"{status.name} is now {STATE_LABEL[status.state]}"
    if prior:
        title += f"  (was: {STATE_LABEL.get(prior, prior)})"
    msg = status.url
    print("\a", end="")  # terminal bell
    print("\n" + "=" * 60)
    print(f"  *** {title} ***")
    print(f"  {msg}")
    print("=" * 60 + "\n")

    if sys.platform == "darwin":
        # Native macOS notification + sound.
        script = (
            f'display notification {json.dumps(msg)} '
            f'with title {json.dumps(title)} sound name "Glass"'
        )
        subprocess.run(["osascript", "-e", script], check=False)
        if open_browser:
            subprocess.run(["open", status.url], check=False)


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def print_status(status: Status, verbose: bool = False) -> None:
    icon = {
        STATE_FOR_SALE: "[BUY]",
        STATE_RESERVATION: "[RES]",
        STATE_NOTIFY: "[...]",
        STATE_UNKNOWN: "[ ? ]",
    }[status.state]
    line = f"{icon} {status.name:<18} {STATE_LABEL[status.state]}"
    if status.for_sale and status.out_of_stock_hint:
        line += "  (note: a sold-out marker is present)"
    if status.error:
        line += f"  ERROR: {status.error}"
    print(line)
    if verbose and not status.error:
        print(f"      - commerce widgets: {status.widgets or '(none)'}")
        for cat, hits in status.matched.items():
            if hits:
                shown = hits if isinstance(hits, list) else hits
                print(f"      - {cat}: {shown}")
        print(f"      - {status.url}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


def resolve_targets(args_targets: list[str], want_all: bool) -> list[str]:
    if want_all:
        return list(PRODUCTS.keys())
    if not args_targets:
        return DEFAULT_TARGETS
    keys = []
    for t in args_targets:
        t = t.lower()
        if t not in PRODUCTS:
            sys.exit(f"Unknown product '{t}'. Choices: {', '.join(PRODUCTS)}")
        keys.append(t)
    return keys


def run_once(targets: list[str], verbose: bool, alert_on_change: bool,
             open_browser: bool, sale_only: bool = False) -> int:
    prev = load_state() if alert_on_change else {}
    new_state = dict(prev)
    any_hit = False

    print(f"Steam stock check  —  {datetime.now().isoformat(timespec='seconds')}")
    for key in targets:
        st = check(key)
        print_status(st, verbose=verbose)
        any_hit = any_hit or (st.for_sale if sale_only else st.available)

        if alert_on_change and not st.error:
            had_baseline = key in prev
            prior = prev.get(key, {}).get("state", STATE_UNKNOWN)
            rose = STATE_RANK[st.state] > STATE_RANK.get(prior, 0)
            # Alert on any observed upward transition out of NOTIFY_ONLY:
            # reservation opening is the actionable moment for new Valve
            # hardware, not just "buy now". (With --sale-only, alert only when
            # it actually becomes purchasable.) The first baseline reading is
            # recorded silently, never alerted.
            interesting = st.for_sale if sale_only else st.available
            if had_baseline and rose and prior != STATE_UNKNOWN and interesting:
                alert(st, prior=prior, open_browser=open_browser)
            new_state[key] = {"state": st.state, "checked_at": st.checked_at}

    if alert_on_change:
        save_state(new_state)

    return 0 if any_hit else 2


def run_watch(targets: list[str], interval: int, verbose: bool,
              open_browser: bool, sale_only: bool = False) -> int:
    names = ", ".join(PRODUCTS[k]["name"] for k in targets)
    goal = "purchasable" if sale_only else "available (reservation or sale)"
    print(f"Watching: {names}")
    print(f"Goal: alert when {goal}.")
    print(f"Polling every {interval}s. Ctrl-C to stop.\n")
    try:
        while True:
            code = run_once(targets, verbose=verbose, alert_on_change=True,
                            open_browser=open_browser, sale_only=sale_only)
            if code == 0:
                print(f"  (a watched product is now {goal} — see alert above)")
            print(f"  next check in {interval}s ...\n")
            time.sleep(interval)
    except KeyboardInterrupt:
        print("\nStopped.")
        return 0


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Detect when a Steam hardware product becomes purchasable.",
    )
    parser.add_argument(
        "targets", nargs="*",
        help=f"Products to check ({', '.join(PRODUCTS)}). Default: {DEFAULT_TARGETS[0]}",
    )
    parser.add_argument("--all", action="store_true", help="Check every product.")
    parser.add_argument("--watch", action="store_true",
                        help="Poll continuously and alert on availability.")
    parser.add_argument("--interval", type=int, default=300,
                        help="Seconds between checks in --watch mode (default 300).")
    parser.add_argument("--open", dest="open_browser", action="store_true",
                        help="Open the page in a browser on availability (macOS).")
    parser.add_argument("--sale-only", action="store_true",
                        help="Only treat actual purchase/add-to-cart as a hit "
                             "(default: any availability, incl. reservation).")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Show which markers matched.")
    args = parser.parse_args(argv)

    targets = resolve_targets(args.targets, args.all)

    try:
        if args.watch:
            return run_watch(targets, args.interval, args.verbose,
                             args.open_browser, sale_only=args.sale_only)
        return run_once(targets, verbose=args.verbose, alert_on_change=True,
                        open_browser=args.open_browser, sale_only=args.sale_only)
    except Exception as exc:  # noqa: BLE001 — top-level guard
        print(f"Fatal error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
