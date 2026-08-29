#!/usr/bin/env python3
"""NFL Daily Digest.

Fetch NFL scores, standings and headlines, assemble a short plain-text digest,
and email it.

By default the digest is built entirely in Python and costs nothing to run.
Set USE_LLM = True (and provide an Anthropic API key + credit) to have Claude
write a smoother, more editorial version instead.

Build steps (see README) are driven by environment variables so each stage can
be tested on its own:

    DIGEST_SKIP_LLM=1   fetch data and print the raw sections, then stop
    DIGEST_DRY_RUN=1    build the full digest but print it instead of emailing
    DIGEST_FORCE=1      ignore the "is it 11:00 in Dublin?" cron guard

The script is deliberately structured so a per-team "Your Team" section can be
added later without a rewrite: set TEAM and fill in build_team_section().
"""

from __future__ import annotations

import os
import re
import sys
import traceback
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import feedparser


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #

LOCAL_TZ = ZoneInfo("Europe/Dublin")
EASTERN_TZ = ZoneInfo("America/New_York")  # NFL scheduling is US-Eastern

# Free by default. Set True to have Claude write the digest — this calls the
# Anthropic API, which is pay-as-you-go (~$1/month here) and needs ANTHROPIC_API_KEY
# plus loaded credit. See README "Optional: LLM-written digest".
USE_LLM = False
MODEL = "claude-opus-5"  # used only when USE_LLM is True; "claude-sonnet-5" is cheaper

# Future: set to a team abbreviation (e.g. "PHI") to enable the "Your Team"
# section at the top of the digest. While None, this is a general league digest.
TEAM: str | None = None

ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_STANDINGS_URLS = [
    "https://cdn.espn.com/core/nfl/standings?xhr=1",
    "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
]

RSS_FEEDS = [
    ("ESPN NFL", "https://www.espn.com/espn/rss/nfl/news"),
    ("CBS Sports NFL", "https://www.cbssports.com/rss/headlines/nfl/"),
    ("ProFootballTalk", "https://profootballtalk.nbcsports.com/feed/"),
]

HTTP_TIMEOUT = 20
HEADERS = {"User-Agent": "nfl-digest/1.0 (github actions cron)"}


class FetchError(Exception):
    """A data source could not be fetched or parsed."""


def _get_json(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


# --------------------------------------------------------------------------- #
# Scores
# --------------------------------------------------------------------------- #

def _summarize_event(ev: dict) -> str:
    comp = ev["competitions"][0]
    status = comp["status"]["type"]
    state = status.get("state")
    sides = {c["homeAway"]: c for c in comp["competitors"]}
    home, away = sides["home"], sides["away"]
    hname = home["team"]["displayName"]
    aname = away["team"]["displayName"]

    if state == "post":
        line = f'{aname} {away.get("score", "?")} @ {hname} {home.get("score", "?")} (Final)'
    elif state == "in":
        line = (f'{aname} {away.get("score", "0")} @ {hname} {home.get("score", "0")} '
                f'({status.get("shortDetail", "in progress")})')
    else:
        kickoff = datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)
        return f'{aname} @ {hname} ({kickoff:%a %d %b %H:%M} Dublin)'

    # Notable context is only meaningful for games that have actually been played.
    notes: list[str] = []
    for note in comp.get("notes") or []:
        if note.get("headline"):
            notes.append(note["headline"])
    for group in comp.get("leaders") or []:
        top = (group.get("leaders") or [])[:1]
        if top:
            athlete = top[0].get("athlete", {}).get("shortName", "")
            notes.append(f'{group.get("shortDisplayName", group.get("name", ""))} '
                         f'{top[0].get("displayValue", "")} ({athlete})')
    if notes:
        line += "  [" + "; ".join(n for n in notes if n) + "]"
    return line


def fetch_scores() -> str:
    """Recent finals / in-progress games plus today's slate (US-Eastern dates)."""
    try:
        today_et = datetime.now(EASTERN_TZ).date()
        finals_by_day: dict[str, list[str]] = {}
        live: list[str] = []
        scheduled: list[str] = []
        for offset in (3, 2, 1, 0):
            day = today_et - timedelta(days=offset)
            label = day.strftime("%a %d %b")
            data = _get_json(f"{ESPN_SITE}/scoreboard",
                             params={"dates": day.strftime("%Y%m%d")})
            for ev in data.get("events", []):
                state = ev["competitions"][0]["status"]["type"].get("state")
                summary = _summarize_event(ev)
                if state == "post":
                    finals_by_day.setdefault(label, []).append(summary)
                elif state == "in":
                    live.append(summary)
                elif offset == 0:
                    scheduled.append(summary)

        blocks: list[str] = []
        if finals_by_day:
            # Keep only the most recent day that has finals — that's "last night".
            label, games = list(finals_by_day.items())[-1]
            blocks.append(f"Final scores — {label}:\n"
                          + "\n".join(f"  - {x}" for x in games))
        if live:
            blocks.append("In progress right now:\n" + "\n".join(f"  - {x}" for x in live))
        if scheduled:
            blocks.append("Also scheduled today:\n" + "\n".join(f"  - {x}" for x in scheduled))
        if not blocks:
            return "No NFL games in the last few days or scheduled today (bye week or offseason)."
        return "\n\n".join(blocks)
    except Exception as e:  # noqa: BLE001 - surface any failure as a FetchError
        raise FetchError(f"scores: {e}") from e


# --------------------------------------------------------------------------- #
# Standings
# --------------------------------------------------------------------------- #

def _parse_standings_cdn(data: dict) -> str:
    groups = data["content"]["standings"]["groups"]
    lines: list[str] = []
    for conference in groups:
        conf_name = conference.get("name", "")
        for division in conference.get("groups", []) or [conference]:
            lines.append(division.get("name", conf_name))
            entries = division.get("standings", {}).get("entries", [])
            for entry in entries:
                stats = {s.get("name"): s.get("displayValue") for s in entry.get("stats", [])}
                record = (stats.get("overall") or stats.get("Overall")
                          or stats.get("record")
                          or f'{stats.get("wins", "0")}-{stats.get("losses", "0")}-{stats.get("ties", "0")}')
                lines.append(f'  {entry["team"]["displayName"]}: {record}')
            lines.append("")
    return "\n".join(lines).strip()


def _parse_standings_site(data: dict) -> str:
    lines: list[str] = []
    for child in data.get("children", []):
        lines.append(child.get("name", ""))
        for entry in child.get("standings", {}).get("entries", []):
            stats = {s.get("type"): s.get("displayValue") for s in entry.get("stats", [])}
            record = stats.get("total") or stats.get("overall") or ""
            lines.append(f'  {entry["team"]["displayName"]}: {record}')
        lines.append("")
    return "\n".join(lines).strip()


def fetch_standings() -> str:
    errors: list[str] = []
    for url in ESPN_STANDINGS_URLS:
        try:
            data = _get_json(url)
            text = _parse_standings_cdn(data) if "content" in data else _parse_standings_site(data)
            if text.strip():
                return text
            errors.append(f"{url}: parsed but empty")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
    raise FetchError("standings: " + " | ".join(errors))


# --------------------------------------------------------------------------- #
# This week's schedule
# --------------------------------------------------------------------------- #

def fetch_schedule() -> str:
    try:
        today_et = datetime.now(EASTERN_TZ).date()
        end = today_et + timedelta(days=7)
        data = _get_json(
            f"{ESPN_SITE}/scoreboard",
            params={"dates": f"{today_et:%Y%m%d}-{end:%Y%m%d}", "limit": 200},
        )
        games = [
            _summarize_event(ev)
            for ev in data.get("events", [])
            if ev["competitions"][0]["status"]["type"].get("state") == "pre"
        ]
        if not games:
            return "No NFL games scheduled in the next 7 days."
        return "\n".join(f"  - {g}" for g in games)
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"schedule: {e}") from e


# --------------------------------------------------------------------------- #
# Headlines
# --------------------------------------------------------------------------- #

def fetch_headlines() -> str:
    per_feed: list[list[str]] = []
    errors: list[str] = []
    for name, url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                raise RuntimeError(getattr(parsed, "bozo_exception", "no entries"))
            feed_items = [
                f"  - [{name}] {title}"
                for entry in parsed.entries[:8]
                if (title := (entry.get("title") or "").strip())
            ]
            if feed_items:
                per_feed.append(feed_items)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")

    # Round-robin across feeds so the top of the list is a mix of sources.
    interleaved: list[str] = []
    for i in range(max((len(f) for f in per_feed), default=0)):
        for feed in per_feed:
            if i < len(feed):
                interleaved.append(feed[i])

    if not interleaved:
        raise FetchError("headlines: " + " | ".join(errors))
    if errors:
        interleaved.append("  (feeds that failed: " + "; ".join(errors) + ")")
    return "\n".join(interleaved)


# --------------------------------------------------------------------------- #
# Stat of the day (computed from the box scores — no LLM needed)
# --------------------------------------------------------------------------- #

def compute_stat_of_day() -> str | None:
    """Pull one number out of the most recent slate of finals."""
    try:
        today_et = datetime.now(EASTERN_TZ).date()
        for offset in (1, 2, 3):
            day = today_et - timedelta(days=offset)
            data = _get_json(f"{ESPN_SITE}/scoreboard",
                             params={"dates": day.strftime("%Y%m%d")})
            finals: list[tuple[str, int, str, int]] = []
            for ev in data.get("events", []):
                comp = ev["competitions"][0]
                if comp["status"]["type"].get("state") != "post":
                    continue
                sides = {c["homeAway"]: c for c in comp["competitors"]}
                try:
                    away_pts = int(sides["away"].get("score"))
                    home_pts = int(sides["home"].get("score"))
                except (TypeError, ValueError):
                    continue
                finals.append((sides["away"]["team"]["displayName"], away_pts,
                               sides["home"]["team"]["displayName"], home_pts))
            if not finals:
                continue
            blowout = max(finals, key=lambda g: abs(g[1] - g[3]))
            margin = abs(blowout[1] - blowout[3])
            winner = blowout[0] if blowout[1] > blowout[3] else blowout[2]
            shootout = max(finals, key=lambda g: g[1] + g[3])
            combined = shootout[1] + shootout[3]
            return (
                f"Widest margin: {winner} by {margin} "
                f"({blowout[0]} {blowout[1]}, {blowout[2]} {blowout[3]}).\n"
                f"Highest-scoring: {shootout[0]} {shootout[1]} @ {shootout[2]} {shootout[3]} "
                f"— {combined} combined points."
            )
        return None
    except Exception:  # noqa: BLE001 - a missing stat shouldn't break the digest
        return None


# --------------------------------------------------------------------------- #
# Your Team (future addition — intentionally a no-op for now)
# --------------------------------------------------------------------------- #

def build_team_section() -> str | None:
    """Return a 'Your Team' block, or None to omit it.

    To enable later:
      1. set TEAM to the team's ESPN abbreviation (e.g. "PHI", "KC", "DAL")
      2. fetch that team's most recent result + next game from
         f"{ESPN_SITE}/teams/{TEAM}/schedule" or the scoreboard filtered by team
      3. pull team-specific news from
         f"{ESPN_SITE}/news?team={TEAM}" (or a team RSS feed)
      4. return a short text block; main() already puts it first in the digest,
         and both digest builders lead with it when present.
    """
    if not TEAM:
        return None
    # TODO: implement per-team fetch here.
    return None


# --------------------------------------------------------------------------- #
# Digest assembly — plain Python (default, free)
# --------------------------------------------------------------------------- #

_BOX_SCORE_NOTE = re.compile(r"\s*\[[^\]]*\]")
_MAX_STORYLINES = 8


def _plain_scores(text: str) -> str:
    """Drop the verbose passing/rushing/receiving notes for the email."""
    return _BOX_SCORE_NOTE.sub("", text)


def _plain_storylines(text: str) -> str:
    lines = [ln for ln in text.splitlines() if ln.lstrip().startswith("- ")]
    trimmed = lines[:_MAX_STORYLINES]
    if len(lines) > _MAX_STORYLINES:
        trimmed.append(f"  (+{len(lines) - _MAX_STORYLINES} more headlines)")
    return "\n".join(trimmed)


def build_digest_plaintext(sections: dict[str, str]) -> str:
    today = datetime.now(LOCAL_TZ)
    parts = [f"NFL DAILY DIGEST — {today:%A %d %B %Y}"]

    if "Your Team" in sections:
        parts.append("YOUR TEAM\n" + sections["Your Team"])

    renderers = [
        ("Scores", "1. SCORES", _plain_scores),
        ("Standings", "2. STANDINGS SNAPSHOT", None),
        ("Headlines", "3. STORYLINES", _plain_storylines),
        ("This week's schedule", "4. THIS WEEK'S SCHEDULE", None),
    ]
    for key, title, transform in renderers:
        body = sections.get(key)
        if body and transform:
            body = transform(body)
        parts.append(f"{title}\n{body}" if body else f"{title}\ndata unavailable today")

    stat = compute_stat_of_day()
    if stat:
        parts.append("5. STAT OF THE DAY\n" + stat)

    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# Digest assembly — Claude (optional, USE_LLM = True)
# --------------------------------------------------------------------------- #

DIGEST_INSTRUCTIONS = """\
You write a daily NFL email digest for someone who follows the league but has no \
favourite team yet. Use ONLY the data given to you — never add scores, names, \
records or storylines that aren't in it. If a section's data is missing or thin, \
write "data unavailable today" for that section rather than guessing.

Output plain text (no markdown headers or bold). Use this exact numbered order:

1. Scores — the most recent slate of final scores, one line each as \
"Away NN @ Home NN". Add a short parenthetical ONLY when the data shows \
something notable (upset, blowout, standout individual performance, injury note).
2. Standings snapshot — division standings, compact: division name, then one \
team per line with its record. Keep it tight.
3. Storylines — 3 to 5 short bullets on what the league is talking about, drawn \
from the headlines (trades, QB news, injuries, coaching moves).
4. This week's schedule — the upcoming games, each with a one-line "why it \
matters".
5. Stat of the day — one interesting number pulled from the data above.

If a "Your Team" section is present in the data, lead with it before section 1, \
under the heading "Your Team", covering their last result, next game and any \
team news.

Keep the whole email readable on a phone in under two minutes. Dry, friendly, \
concise. No preamble, no sign-off — just the digest."""


def generate_digest(sections: dict[str, str]) -> str:
    import anthropic  # imported lazily so the free path needs no anthropic install

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment
    today = datetime.now(LOCAL_TZ)
    data_blob = "\n\n".join(f"=== {name} ===\n{body}" for name, body in sections.items())
    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=DIGEST_INSTRUCTIONS,
        messages=[{
            "role": "user",
            "content": f"Today is {today:%A %d %B %Y}. Here is today's data:\n\n{data_blob}",
        }],
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


# --------------------------------------------------------------------------- #
# Email (Resend HTTP API — https://resend.com/docs/api-reference/emails)
# --------------------------------------------------------------------------- #

# Resend's shared sender works with no domain setup, but only delivers to the
# address that owns the Resend account. Once you verify your own domain, set the
# EMAIL_FROM secret to something like "NFL Digest <nfl@yourdomain.com>".
DEFAULT_FROM = "NFL Digest <onboarding@resend.dev>"


_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_email(subject: str, body: str) -> None:
    # .strip() guards against a trailing newline / stray space in a pasted secret.
    api_key = os.environ["RESEND_API_KEY"].strip()
    recipient = os.environ["RECIPIENT_EMAIL"].strip().strip('"').strip("'").strip()
    sender = (os.environ.get("EMAIL_FROM") or DEFAULT_FROM).strip()

    if not _EMAIL_RE.match(recipient):
        raise RuntimeError(
            "RECIPIENT_EMAIL is not a plain email address. "
            f"Got length={len(recipient)}, has_space={' ' in recipient}, "
            f"has_angle={'<' in recipient or '>' in recipient}, "
            f"at_count={recipient.count('@')}. "
            "Set the secret to exactly: you@example.com  (no name, no <>, no quotes)."
        )

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}",
                 "Content-Type": "application/json"},
        json={"from": sender, "to": [recipient], "subject": subject, "text": body},
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend API returned {resp.status_code}: {resp.text}")


def _safe_send(subject: str, body: str) -> None:
    """Send, but never raise — used for failure notifications."""
    try:
        send_email(subject, body)
        print(f"Sent failure notice: {subject}")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        print("Could not send the failure email either.", file=sys.stderr)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #

def _skip_for_wrong_hour() -> bool:
    """The workflow fires at 10:00 and 11:00 UTC to cover Irish DST; only the
    run that lands at 11:00 Dublin local time should actually send."""
    if os.environ.get("DIGEST_FORCE"):
        return False
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch":
        return False
    if not os.environ.get("CI"):
        return False
    return datetime.now(LOCAL_TZ).hour != 11


def main() -> None:
    if _skip_for_wrong_hour():
        print(f"Dublin local time is {datetime.now(LOCAL_TZ):%H:%M}; "
              f"this cron trigger is not the 11:00 one. Skipping.")
        return

    fetchers = [
        ("Scores", fetch_scores),
        ("Standings", fetch_standings),
        ("Headlines", fetch_headlines),
        ("This week's schedule", fetch_schedule),
    ]
    sections: dict[str, str] = {}
    errors: list[str] = []
    for label, fn in fetchers:
        try:
            sections[label] = fn()
            print(f"[ok]   {label}")
        except FetchError as e:
            errors.append(str(e))
            print(f"[warn] {e}", file=sys.stderr)

    team_block = build_team_section()
    if team_block:
        sections = {"Your Team": team_block, **sections}

    today = datetime.now(LOCAL_TZ)
    subject = f"NFL Daily Digest — {today:%a %d %b}"

    if os.environ.get("DIGEST_SKIP_LLM"):
        print("\n" + "=" * 60)
        for name, body in sections.items():
            print(f"\n### {name}\n{body}")
        if errors:
            print("\n### Fetch errors\n" + "\n".join(f"- {e}" for e in errors))
        return

    if not sections:
        _safe_send(
            f"{subject} (FAILED)",
            "The NFL digest could not run today — every data source failed:\n\n"
            + "\n".join(f"- {e}" for e in errors),
        )
        sys.exit(1)

    if USE_LLM:
        try:
            digest = generate_digest(sections)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _safe_send(
                f"{subject} (PARTIAL — Claude call failed)",
                f"Data was fetched but the Claude API call failed:\n\n{e}\n\n"
                "Raw data follows so you're not left in the dark:\n\n"
                + "\n\n".join(f"=== {k} ===\n{v}" for k, v in sections.items()),
            )
            sys.exit(1)
    else:
        digest = build_digest_plaintext(sections)

    if errors:
        digest = (
            "Heads up — some data sources failed today, so parts of this digest "
            "may be thin:\n" + "\n".join(f"  - {e}" for e in errors) + "\n\n" + digest
        )

    if os.environ.get("DIGEST_DRY_RUN"):
        print("\n" + "=" * 60 + f"\nSubject: {subject}\n" + "=" * 60 + "\n")
        print(digest)
        return

    send_email(subject, digest)
    print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
