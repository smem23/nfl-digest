#!/usr/bin/env python3
"""NFL Daily Digest.

Fetch NFL scores, standings, headlines and the upcoming schedule, render them as
a clean HTML email, and send it via Resend.

By default the digest is built entirely in Python and costs nothing to run.
Set USE_LLM = True (and provide an Anthropic API key + credit) to have Claude
write an editorial version of the prose instead.

Env-var switches (see README):

    DIGEST_SKIP_LLM=1   fetch data and print the plain-text digest, then stop
    DIGEST_DRY_RUN=1    build the digest but print it instead of emailing
    DIGEST_FORCE=1      ignore the "is it 11:00 in Dublin?" cron guard

Structured so a per-team "Your Team" block can be added later without a rewrite:
set TEAM and fill in build_team_section().
"""

from __future__ import annotations

import html
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

def _flag(name: str, default: bool) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() not in ("0", "", "false", "no")


# What the run produces. Both default on; the workflow flips these per step
# with DIGEST_WRITE_PAGE / DIGEST_SEND_EMAIL.
WRITE_PAGE = _flag("DIGEST_WRITE_PAGE", True)   # phone-friendly page for GitHub Pages
SEND_EMAIL = _flag("DIGEST_SEND_EMAIL", True)   # also email the digest via Resend
PAGE_PATH = "public/index.html"

# Free by default. True routes the prose through the Anthropic API — pay-as-you-go
# (~$1/month here), needs ANTHROPIC_API_KEY plus loaded credit and the `anthropic`
# line uncommented in requirements.txt.
USE_LLM = False
MODEL = "claude-opus-5"  # used only when USE_LLM is True; "claude-sonnet-5" is cheaper

# Future: an ESPN team abbreviation (e.g. "PHI") to enable the "Your Team" block.
TEAM: str | None = None

ESPN_SITE = "https://site.api.espn.com/apis/site/v2/sports/football/nfl"
ESPN_STANDINGS_URLS = [
    "https://cdn.espn.com/core/nfl/standings?xhr=1",
    "https://site.api.espn.com/apis/v2/sports/football/nfl/standings",
]

RSS_FEEDS = [
    ("ESPN", "https://www.espn.com/espn/rss/nfl/news"),
    ("CBS Sports", "https://www.cbssports.com/rss/headlines/nfl/"),
    ("ProFootballTalk", "https://profootballtalk.nbcsports.com/feed/"),
]

MAX_STORYLINES = 8
MAX_QB_HEADLINES = 8
HTTP_TIMEOUT = 20
HEADERS = {"User-Agent": "nfl-digest/2.0 (github actions cron)"}

# Headline is "QB news" if it mentions the position or a current-ish starting QB.
# Full names for common surnames (Allen/Jackson/Smith/etc. are too ambiguous alone).
_QB_TERMS = re.compile(r"\b(quarterbacks?|qbs?|qb1|signal[- ]caller|backup qb|"
                       r"starting job under center)\b", re.I)
_QB_NAMES = re.compile(
    r"\b(mahomes|joe burrow|lamar jackson|jalen hurts|justin herbert|dak prescott|"
    r"trevor lawrence|jordan love|c\.?\s?j\.? stroud|jayden daniels|drake maye|"
    r"caleb williams|bo nix|brock purdy|jared goff|matthew stafford|aaron rodgers|"
    r"kirk cousins|sam darnold|baker mayfield|tua tagovailoa|tagovailoa|kyler murray|"
    r"russell wilson|justin fields|deshaun watson|anthony richardson|michael penix|"
    r"j\.?\s?j\.? mccarthy|josh allen|geno smith|will levis|bryce young|mac jones|"
    r"jacoby brissett|quinn ewers|shedeur sanders|jaxson dart|spencer rattler|"
    r"aidan o'connell|jameis winston|daniel jones|joe flacco|jarrett stidham)\b", re.I)


def is_qb_headline(title: str) -> bool:
    return bool(_QB_TERMS.search(title) or _QB_NAMES.search(title))


class FetchError(Exception):
    """A data source could not be fetched or parsed."""


def _get_json(url: str, params: dict | None = None) -> dict:
    r = requests.get(url, params=params, headers=HEADERS, timeout=HTTP_TIMEOUT)
    r.raise_for_status()
    return r.json()


def _team_label(competitor: dict) -> str:
    team = competitor.get("team", {})
    return (team.get("shortDisplayName") or team.get("name")
            or team.get("displayName") or team.get("abbreviation") or "?")


def _team_abbr(competitor: dict) -> str:
    return (competitor.get("team", {}).get("abbreviation") or "").upper()


def team_logo(abbr: str) -> str:
    return (f"https://a.espncdn.com/i/teamlogos/nfl/500/{abbr.lower()}.png"
            if abbr else "")


def _kickoff(ev: dict) -> datetime:
    return datetime.fromisoformat(ev["date"].replace("Z", "+00:00")).astimezone(LOCAL_TZ)


def _short_date(d) -> str:
    """'Sat 30 Aug' — portable (no %-d)."""
    return f"{d:%a} {d.day} {d:%b}"


def _short_datetime(dt: datetime) -> str:
    """'Sat 30 Aug, 18:00'."""
    return f"{_short_date(dt)}, {dt:%H:%M}"


# --------------------------------------------------------------------------- #
# Fetchers — each returns structured data or raises FetchError
# --------------------------------------------------------------------------- #

def fetch_scores() -> dict:
    """Most recent slate of finals, plus anything live or scheduled for today."""
    try:
        today_et = datetime.now(EASTERN_TZ).date()
        finals_by_day: dict[str, list[dict]] = {}
        live: list[dict] = []
        scheduled: list[dict] = []

        for offset in (3, 2, 1, 0):
            day = today_et - timedelta(days=offset)
            label = _short_date(day)
            data = _get_json(f"{ESPN_SITE}/scoreboard",
                             params={"dates": day.strftime("%Y%m%d")})
            for ev in data.get("events", []):
                comp = ev["competitions"][0]
                state = comp["status"]["type"].get("state")
                sides = {c["homeAway"]: c for c in comp["competitors"]}
                if "home" not in sides or "away" not in sides:
                    continue
                away, home = sides["away"], sides["home"]
                game = {"away": _team_label(away), "home": _team_label(home),
                        "away_abbr": _team_abbr(away), "home_abbr": _team_abbr(home)}

                if state == "post":
                    try:
                        a, h = int(away["score"]), int(home["score"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    game.update(away_score=a, home_score=h,
                                winner="away" if a > h else "home" if h > a else "tie")
                    finals_by_day.setdefault(label, []).append(game)
                elif state == "in":
                    game.update(away_score=away.get("score", "0"),
                                home_score=home.get("score", "0"),
                                detail=comp["status"]["type"].get("shortDetail", ""))
                    live.append(game)
                elif offset == 0:
                    game["kickoff"] = _kickoff(ev)
                    scheduled.append(game)

        final_day, finals = (list(finals_by_day.items())[-1]
                             if finals_by_day else (None, []))
        return {"final_day": final_day, "finals": finals,
                "live": live, "scheduled": scheduled}
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"scores: {e}") from e


def _as_int(value) -> int:
    try:
        return int(float(value))  # ESPN sends "3.0" / 3.0
    except (TypeError, ValueError):
        return 0


def _entry_record(entry: dict) -> str:
    stats = {s.get("name"): s for s in entry.get("stats", [])}
    for key in ("overall", "record"):
        if key in stats and stats[key].get("displayValue"):
            return stats[key]["displayValue"]
    if "wins" in stats:
        wins = _as_int(stats["wins"].get("value"))
        losses = _as_int(stats.get("losses", {}).get("value"))
        ties = _as_int(stats.get("ties", {}).get("value"))
        return f"{wins}-{losses}" + (f"-{ties}" if ties else "")
    return "—"


def _walk_standings(node: dict, out: list[dict]) -> None:
    """Recurse to leaf groups only — the CDN response nests divisions under
    conferences and also carries conference-level totals we don't want."""
    subgroups = node.get("groups")
    if subgroups:
        for child in subgroups:
            _walk_standings(child, out)
        return
    entries = node.get("standings", {}).get("entries", [])
    if not entries:
        return
    teams = [
        {"name": (e.get("team", {}).get("shortDisplayName")
                  or e.get("team", {}).get("displayName", "?")),
         "abbr": (e.get("team", {}).get("abbreviation") or "").upper(),
         "record": _entry_record(e)}
        for e in entries
    ]
    out.append({"division": node.get("name", ""), "teams": teams})


def _parse_standings_cdn(data: dict) -> list[dict]:
    out: list[dict] = []
    for group in data["content"]["standings"]["groups"]:
        _walk_standings(group, out)
    return out


def _parse_standings_site(data: dict) -> list[dict]:
    out: list[dict] = []
    for child in data.get("children", []):
        teams: list[dict] = []
        for entry in child.get("standings", {}).get("entries", []):
            stats = {s.get("type"): s.get("displayValue") for s in entry.get("stats", [])}
            team = entry.get("team", {})
            teams.append({
                "name": team.get("shortDisplayName") or team.get("displayName", "?"),
                "abbr": (team.get("abbreviation") or "").upper(),
                "record": stats.get("total") or stats.get("overall") or "—",
            })
        if teams:
            out.append({"division": child.get("name", ""), "teams": teams})
    return out


def fetch_standings() -> list[dict]:
    errors: list[str] = []
    for url in ESPN_STANDINGS_URLS:
        try:
            data = _get_json(url)
            divisions = (_parse_standings_cdn(data) if "content" in data
                         else _parse_standings_site(data))
            if divisions:
                return divisions
            errors.append(f"{url}: parsed but empty")
        except Exception as e:  # noqa: BLE001
            errors.append(f"{url}: {e}")
    raise FetchError("standings: " + " | ".join(errors))


def _pre_games(data: dict) -> list[dict]:
    games: list[dict] = []
    for ev in data.get("events", []):
        comp = ev["competitions"][0]
        if comp["status"]["type"].get("state") != "pre":
            continue
        sides = {c["homeAway"]: c for c in comp["competitors"]}
        if "home" not in sides or "away" not in sides:
            continue
        games.append({"away": _team_label(sides["away"]),
                      "home": _team_label(sides["home"]),
                      "away_abbr": _team_abbr(sides["away"]),
                      "home_abbr": _team_abbr(sides["home"]),
                      "kickoff": _kickoff(ev)})
    return games


def fetch_schedule() -> list[dict]:
    try:
        today_et = datetime.now(EASTERN_TZ).date()
        end = today_et + timedelta(days=9)  # reach the next slate even mid-week
        # 1) the coming week by date range (right during the season)
        # 2) regular-season Week 1 (covers the preseason -> Week 1 dead period)
        # 3) ESPN's "current" slate (last resort)
        attempts = [
            {"dates": f"{today_et:%Y%m%d}-{end:%Y%m%d}", "limit": 200},
            {"seasontype": 2, "week": 1, "limit": 100},
            {"limit": 100},
        ]
        for params in attempts:
            games = _pre_games(_get_json(f"{ESPN_SITE}/scoreboard", params=params))
            if games:
                games.sort(key=lambda g: g["kickoff"])
                return games[:16]  # one slate's worth
        return []
    except Exception as e:  # noqa: BLE001
        raise FetchError(f"schedule: {e}") from e


def fetch_headlines() -> list[dict]:
    per_feed: list[list[dict]] = []
    errors: list[str] = []
    for name, url in RSS_FEEDS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
            parsed = feedparser.parse(resp.content)
            if not parsed.entries:
                raise RuntimeError(getattr(parsed, "bozo_exception", "no entries"))
            items = [
                {"source": name, "title": title, "link": entry.get("link", "")}
                for entry in parsed.entries[:15]
                if (title := (entry.get("title") or "").strip())
            ]
            if items:
                per_feed.append(items)
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")

    interleaved: list[dict] = []
    for i in range(max((len(f) for f in per_feed), default=0)):
        for feed in per_feed:
            if i < len(feed):
                interleaved.append(feed[i])

    if not interleaved:
        raise FetchError("headlines: " + " | ".join(errors))
    return interleaved


def compute_stat_of_day() -> str | None:
    """One number pulled from the most recent slate of finals."""
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
                    a = int(sides["away"]["score"])
                    h = int(sides["home"]["score"])
                except (KeyError, TypeError, ValueError):
                    continue
                finals.append((_team_label(sides["away"]), a, _team_label(sides["home"]), h))
            if not finals:
                continue
            blowout = max(finals, key=lambda g: abs(g[1] - g[3]))
            margin = abs(blowout[1] - blowout[3])
            winner = blowout[0] if blowout[1] > blowout[3] else blowout[2]
            shootout = max(finals, key=lambda g: g[1] + g[3])
            combined = shootout[1] + shootout[3]
            return (
                f"{winner} won by {margin} — the widest margin of the slate "
                f"({blowout[0]} {blowout[1]}, {blowout[2]} {blowout[3]}). "
                f"The night's shootout: {shootout[0]} {shootout[1]} at "
                f"{shootout[2]} {shootout[3]}, {combined} combined points."
            )
        return None
    except Exception:  # noqa: BLE001
        return None


def build_team_section() -> str | None:
    """Return a 'Your Team' block, or None to omit it.

    To enable later: set TEAM to a team abbreviation, fetch that team's last
    result + next game (f"{ESPN_SITE}/teams/{TEAM}/schedule") and team news
    (f"{ESPN_SITE}/news?team={TEAM}"), and return a short string. main() puts it
    first and both renderers lead with it.
    """
    if not TEAM:
        return None
    return None  # TODO: implement per-team fetch


# --------------------------------------------------------------------------- #
# HTML rendering (default, free)
# --------------------------------------------------------------------------- #

_FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,"
         "Helvetica,Arial,sans-serif")
_INK = "#1f2937"
_MUTED = "#6b7280"
_FAINT = "#9ca3af"
_RULE = "#e5e7eb"
_ACCENT = "#1d4ed8"


def _esc(value) -> str:
    return html.escape(str(value), quote=True)


def _label(text: str) -> str:
    return (f'<div style="font-size:12px;color:{_MUTED};margin:14px 0 4px;'
            f'text-transform:uppercase;letter-spacing:.5px;">{_esc(text)}</div>')


def _section(title: str, inner: str) -> str:
    return (f'<h2 style="font-size:13px;font-weight:700;letter-spacing:1px;'
            f'text-transform:uppercase;color:#111827;margin:30px 0 12px;">'
            f'{_esc(title)}</h2>{inner}')


def _score_row(away: str, a, home: str, h, winner: str | None, note: str = "") -> str:
    def side(name: str, score, win: bool) -> str:
        weight = "700" if win else "400"
        color = "#111827" if win else _MUTED
        return (f'<span style="font-weight:{weight};color:{color};">'
                f'{_esc(name)} {_esc(score)}</span>')

    left = side(away, a, winner == "away")
    right = side(home, h, winner == "home")
    tail = (f' &nbsp;<span style="color:{_FAINT};font-size:13px;">{_esc(note)}</span>'
            if note else "")
    return (f'<div style="padding:6px 0;border-bottom:1px solid #f3f4f6;font-size:15px;">'
            f'{left} <span style="color:#d1d5db;">·</span> {right}{tail}</div>')


def _scores_html(scores: dict | None) -> str:
    if not scores or not (scores["finals"] or scores["live"] or scores["scheduled"]):
        return _muted_line("No games in the last few days or scheduled today.")
    out: list[str] = []
    if scores["finals"]:
        out.append(_label(f"Final — {scores['final_day']}"))
        for g in scores["finals"]:
            out.append(_score_row(g["away"], g["away_score"], g["home"],
                                  g["home_score"], g.get("winner")))
    if scores["live"]:
        out.append(_label("In progress"))
        for g in scores["live"]:
            out.append(_score_row(g["away"], g["away_score"], g["home"],
                                  g["home_score"], None, g.get("detail", "")))
    if scores["scheduled"]:
        out.append(_label("Later today"))
        for g in scores["scheduled"]:
            out.append(f'<div style="padding:6px 0;font-size:15px;">{_esc(g["away"])} '
                       f'<span style="color:#d1d5db;">at</span> {_esc(g["home"])} '
                       f'<span style="color:{_MUTED};">· {g["kickoff"]:%H:%M}</span></div>')
    return "".join(out)


def _standings_html(divisions: list[dict]) -> str:
    if not divisions:
        return _muted_line("Standings unavailable today.")
    blocks: list[str] = []
    for div in divisions:
        rows = "".join(
            f'<tr><td style="padding:2px 0;font-size:14px;">{_esc(t["name"])}</td>'
            f'<td align="right" style="padding:2px 0;font-size:14px;color:{_MUTED};'
            f'white-space:nowrap;">{_esc(t["record"])}</td></tr>'
            for t in div["teams"]
        )
        blocks.append(
            f'<div style="display:inline-block;width:100%;max-width:240px;'
            f'vertical-align:top;margin:0 16px 14px 0;">'
            f'<div style="font-weight:700;font-size:13px;margin-bottom:4px;">'
            f'{_esc(div["division"])}</div>'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="line-height:1.35;">{rows}</table></div>'
        )
    return "".join(blocks)


def _headlines_html(items: list[dict], limit: int = MAX_STORYLINES,
                    empty: str = "No headlines today.") -> str:
    if not items:
        return _muted_line(empty)
    lis: list[str] = []
    for it in items[:limit]:
        title = _esc(it["title"])
        if it.get("link"):
            title = (f'<a href="{_esc(it["link"])}" style="color:{_ACCENT};'
                     f'text-decoration:none;">{title}</a>')
        lis.append(f'<li style="margin:8px 0;line-height:1.45;font-size:15px;">{title}'
                   f' <span style="color:{_FAINT};font-size:12px;">'
                   f'{_esc(it["source"])}</span></li>')
    return f'<ul style="margin:0;padding-left:20px;">{"".join(lis)}</ul>'


def _schedule_html(games: list[dict]) -> str:
    if not games:
        return _muted_line("No games scheduled in the coming week.")
    rows = [
        f'<div style="padding:5px 0;font-size:15px;">'
        f'<span style="color:{_MUTED};">{_short_datetime(g["kickoff"])}</span>'
        f'&nbsp; {_esc(g["away"])} <span style="color:#d1d5db;">at</span> {_esc(g["home"])}</div>'
        for g in games
    ]
    return "".join(rows)


def _stat_html(stat: str | None) -> str:
    if not stat:
        return ""
    return (f'<div style="background:#eff6ff;border-left:3px solid {_ACCENT};'
            f'padding:14px 16px;border-radius:4px;font-size:15px;line-height:1.55;">'
            f'{_esc(stat)}</div>')


def _muted_line(text: str) -> str:
    return f'<div style="color:{_MUTED};font-size:14px;font-style:italic;">{_esc(text)}</div>'


def render_html(sections: dict, errors: list[str]) -> str:
    today = datetime.now(LOCAL_TZ)
    date_str = f"{today:%A} {today.day} {today:%B %Y}"

    notice = ""
    if errors:
        names = ", ".join(sorted({e.split(":")[0] for e in errors}))
        notice = (f'<div style="background:#fef2f2;border-left:3px solid #ef4444;'
                  f'padding:10px 14px;font-size:13px;color:#991b1b;margin-bottom:8px;">'
                  f'Some sources were unavailable today: {_esc(names)}.</div>')

    body = notice
    if sections.get("team"):
        body += _section("Your Team",
                         f'<div style="font-size:15px;line-height:1.5;">'
                         f'{_esc(sections["team"])}</div>')
    body += _section("Scores", _scores_html(sections.get("scores")))
    body += _section("Quarterbacks", _headlines_html(
        sections.get("qbs") or [], limit=MAX_QB_HEADLINES, empty="No quarterback news today."))
    body += _section("Standings", _standings_html(sections.get("standings") or []))
    body += _section("Storylines", _headlines_html(sections.get("headlines") or []))
    body += _section("This week", _schedule_html(sections.get("schedule") or []))
    if sections.get("stat"):
        body += _section("Stat of the day", _stat_html(sections["stat"]))

    return f"""<!doctype html>
<html>
<head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f4f4f5;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f4f5;padding:24px 10px;">
<tr><td align="center">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="width:600px;max-width:100%;background:#ffffff;border-radius:12px;">
<tr><td style="padding:32px 28px;font-family:{_FONT};color:{_INK};">
<div style="font-size:12px;font-weight:700;letter-spacing:1.5px;color:{_ACCENT};text-transform:uppercase;">NFL Daily Digest</div>
<div style="font-size:22px;font-weight:700;margin-top:4px;">{_esc(date_str)}</div>
<div style="height:1px;background:{_RULE};margin:18px 0 0;"></div>
{body}
<div style="height:1px;background:{_RULE};margin:30px 0 14px;"></div>
<div style="font-size:12px;color:{_FAINT};line-height:1.5;">Sent automatically by nfl-digest &middot; scores &amp; standings via ESPN &middot; headlines via ESPN, CBS Sports &amp; ProFootballTalk</div>
</td></tr>
</table>
</td></tr>
</table>
</body>
</html>"""


# --------------------------------------------------------------------------- #
# Plain-text rendering (email fallback + local preview + LLM input)
# --------------------------------------------------------------------------- #

def render_text(sections: dict, errors: list[str]) -> str:
    today = datetime.now(LOCAL_TZ)
    lines = [f"NFL Daily Digest — {today:%A} {today.day} {today:%B %Y}", ""]
    if errors:
        names = ", ".join(sorted({e.split(":")[0] for e in errors}))
        lines += [f"(some sources unavailable: {names})", ""]

    sc = sections.get("scores")
    lines.append("SCORES")
    if sc and sc["finals"]:
        lines.append(f"Final — {sc['final_day']}")
        for g in sc["finals"]:
            lines.append(f"  {g['away']} {g['away_score']} – {g['home']} {g['home_score']}")
    for g in (sc or {}).get("live", []):
        lines.append(f"  LIVE  {g['away']} {g['away_score']} – {g['home']} {g['home_score']}"
                     f"  ({g.get('detail','')})")
    for g in (sc or {}).get("scheduled", []):
        lines.append(f"  {g['away']} at {g['home']} — {g['kickoff']:%H:%M}")
    if not sc or not (sc["finals"] or sc["live"] or sc["scheduled"]):
        lines.append("  No games in the last few days or scheduled today.")

    lines += ["", "QUARTERBACKS"]
    for it in (sections.get("qbs") or [])[:MAX_QB_HEADLINES]:
        lines.append(f"  - {it['title']} ({it['source']})")
    if not sections.get("qbs"):
        lines.append("  no quarterback news today")

    lines += ["", "STANDINGS"]
    for div in sections.get("standings") or []:
        lines.append(div["division"])
        for t in div["teams"]:
            lines.append(f"  {t['name']:<24} {t['record']}")
    if not sections.get("standings"):
        lines.append("  unavailable today")

    lines += ["", "STORYLINES"]
    for it in (sections.get("headlines") or [])[:MAX_STORYLINES]:
        lines.append(f"  - {it['title']} ({it['source']})")
    if not sections.get("headlines"):
        lines.append("  unavailable today")

    lines += ["", "THIS WEEK"]
    for g in sections.get("schedule") or []:
        lines.append(f"  {_short_datetime(g['kickoff'])}  {g['away']} at {g['home']}")
    if not sections.get("schedule"):
        lines.append("  no games in the coming week")

    if sections.get("stat"):
        lines += ["", "STAT OF THE DAY", f"  {sections['stat']}"]

    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# LLM rendering (optional, USE_LLM = True)
# --------------------------------------------------------------------------- #

DIGEST_INSTRUCTIONS = """\
You write a daily NFL email digest for someone who follows the league but has no \
favourite team yet. Use ONLY the data given to you — never invent scores, names, \
records or storylines. If a section is missing, say "data unavailable today".

Plain prose, no markdown. Keep this order, with a short heading line for each:
Scores · Standings snapshot · Storylines (3–5) · This week (with a one-line \
"why it matters" per game) · Stat of the day.

If a "Your Team" block is present, lead with it. Phone-readable in under two \
minutes. Dry, warm, concise. No preamble, no sign-off."""


def generate_digest(sections: dict) -> str:
    import anthropic  # imported lazily so the free path needs no anthropic install

    client = anthropic.Anthropic()
    today = datetime.now(LOCAL_TZ)
    message = client.messages.create(
        model=MODEL,
        max_tokens=2000,
        system=DIGEST_INSTRUCTIONS,
        messages=[{
            "role": "user",
            "content": (f"Today is {today:%A %d %B %Y}. Here is today's data:\n\n"
                        + render_text(sections, [])),
        }],
    )
    return "".join(b.text for b in message.content if b.type == "text").strip()


def prose_to_html(prose: str) -> str:
    paras = [p.strip() for p in prose.split("\n\n") if p.strip()]
    body = "".join(
        f'<p style="margin:0 0 15px;line-height:1.6;font-size:15px;">'
        f'{_esc(p).replace(chr(10), "<br>")}</p>'
        for p in paras
    )
    return (f'<!doctype html><html><body style="margin:0;background:#f4f4f5;">'
            f'<div style="max-width:600px;margin:0 auto;padding:32px;background:#fff;'
            f'font-family:{_FONT};color:{_INK};">{body}</div></body></html>')


# --------------------------------------------------------------------------- #
# Web page rendering (phone-friendly, for GitHub Pages)
# --------------------------------------------------------------------------- #

_PAGE_CSS = """
*,*::before,*::after{box-sizing:border-box}
:root{--bg:#f1f2f4;--card:#fff;--ink:#0f172a;--muted:#64748b;--faint:#94a3b8;
--line:#e7e9ee;--accent:#2563eb;--win:#0f172a;--shadow:0 1px 3px rgba(15,23,42,.06)}
@media (prefers-color-scheme:dark){:root{--bg:#0b0e13;--card:#151a21;--ink:#e8eaed;
--muted:#98a1ad;--faint:#6b7482;--line:#242b34;--accent:#5b93ff;--win:#f3f4f6;--shadow:none}}
html{-webkit-text-size-adjust:100%}
body{margin:0;background:var(--bg);color:var(--ink);
font:16px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text","Segoe UI",Roboto,Helvetica,Arial,sans-serif;
-webkit-font-smoothing:antialiased}
.wrap{max-width:620px;margin:0 auto;padding:22px 14px 60px}
header{padding:4px 4px 2px}
.eyebrow{font-size:12px;font-weight:800;letter-spacing:.16em;color:var(--accent);text-transform:uppercase}
h1{margin:4px 0 3px;font-size:27px;font-weight:800;letter-spacing:-.02em}
.updated{font-size:13px;color:var(--muted)}
.card{background:var(--card);border-radius:18px;padding:18px 16px;margin-top:14px;box-shadow:var(--shadow)}
.card h2{margin:0 0 12px;font-size:12px;font-weight:800;letter-spacing:.11em;text-transform:uppercase;color:var(--muted)}
.none{margin:0;color:var(--muted);font-style:italic;font-size:14px}
.err{background:var(--card);border-left:3px solid #ef4444;border-radius:10px;padding:10px 14px;
margin-top:14px;font-size:13px;color:#b91c1c}
@media (prefers-color-scheme:dark){.err{color:#fca5a5}}
.subhead{font-size:11px;font-weight:800;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin:14px 0 2px}
.subhead:first-child{margin-top:0}
.game{display:grid;grid-template-columns:1fr auto 1fr;align-items:center;gap:8px;
padding:11px 0;border-top:1px solid var(--line)}
.card h2+.game,.subhead+.game{border-top:0}
.t{display:flex;align-items:center;gap:8px;min-width:0;font-size:15px}
.t.h{flex-direction:row-reverse}
.t img{width:24px;height:24px;object-fit:contain;flex:none}
.t .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.win .nm{font-weight:800;color:var(--win)}
.lose .nm{color:var(--muted)}
.sc{font-size:15px;font-weight:700;font-variant-numeric:tabular-nums;color:var(--muted);white-space:nowrap;text-align:center}
.sc b{color:var(--ink)}
.tabs{display:flex;gap:6px;margin-bottom:14px}
.tab{flex:1;padding:9px 0;border:0;border-radius:11px;background:var(--bg);color:var(--muted);
font:inherit;font-weight:800;font-size:13px;cursor:pointer}
.tab[aria-selected=true]{background:var(--accent);color:#fff}
.conf[hidden]{display:none}
.div{margin-top:16px}
.div:first-child{margin-top:0}
.div h3{margin:0 0 6px;font-size:13px;font-weight:800}
table.st{width:100%;border-collapse:collapse;font-size:14px}
table.st thead th{padding:0 0 5px;font-size:10px;font-weight:800;letter-spacing:.06em;
text-transform:uppercase;color:var(--faint);text-align:right}
table.st thead th:first-child{text-align:left}
table.st td{padding:6px 0;border-top:1px solid var(--line);text-align:right;
font-variant-numeric:tabular-nums;color:var(--muted);width:30px}
table.st td:first-child{text-align:left;color:var(--ink);width:auto}
table.st td:first-child span{display:inline-flex;align-items:center;gap:8px;min-width:0}
table.st td:first-child .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
table.st img{width:20px;height:20px;object-fit:contain;flex:none}
.list .story:first-child,.list .sg:first-child{border-top:0}
.story{display:block;padding:13px 0;border-top:1px solid var(--line);text-decoration:none;color:inherit}
.story .hl{font-size:15.5px;line-height:1.4;font-weight:600;color:var(--ink)}
.story .src{margin-top:4px;font-size:11px;font-weight:800;letter-spacing:.06em;text-transform:uppercase;color:var(--faint)}
.sg{display:flex;gap:12px;align-items:baseline;padding:9px 0;border-top:1px solid var(--line);font-size:14.5px}
.sg time{color:var(--muted);white-space:nowrap;min-width:120px;flex:none}
.at{color:var(--faint)}
.stat{font-size:15px;line-height:1.6;background:#eef4ff;border:1px solid #cddbfe;border-radius:14px;padding:14px 16px}
@media (prefers-color-scheme:dark){.stat{background:#15223a;border-color:#2a4064}}
footer{margin-top:26px;text-align:center;font-size:12px;line-height:1.7;color:var(--faint)}
"""

_PAGE_JS = """
(function(){var t=document.querySelectorAll('.tab');t.forEach(function(b){
b.addEventListener('click',function(){t.forEach(function(x){x.setAttribute('aria-selected',x===b)});
document.querySelectorAll('.conf').forEach(function(c){c.hidden=c.dataset.conf!==b.dataset.conf})})})})();
"""


def _logo_img(abbr: str) -> str:
    return (f'<img src="{_esc(team_logo(abbr))}" alt="" loading="lazy">'
            if abbr else "")


def _pg_team(name: str, abbr: str, cls: str, side: str) -> str:
    return (f'<span class="t {side} {cls}">{_logo_img(abbr)}'
            f'<span class="nm">{_esc(name)}</span></span>')


def _pg_final(g: dict) -> str:
    aw, hw = g.get("winner") == "away", g.get("winner") == "home"
    a_cls = "win" if aw else ("lose" if hw else "")
    h_cls = "win" if hw else ("lose" if aw else "")
    a = f'<b>{_esc(g["away_score"])}</b>' if aw else _esc(g["away_score"])
    h = f'<b>{_esc(g["home_score"])}</b>' if hw else _esc(g["home_score"])
    return (f'<div class="game">{_pg_team(g["away"], g.get("away_abbr", ""), a_cls, "a")}'
            f'<span class="sc">{a} &ndash; {h}</span>'
            f'{_pg_team(g["home"], g.get("home_abbr", ""), h_cls, "h")}</div>')


def _pg_simple(g: dict, mid: str) -> str:
    return (f'<div class="game">{_pg_team(g["away"], g.get("away_abbr", ""), "", "a")}'
            f'<span class="sc">{mid}</span>'
            f'{_pg_team(g["home"], g.get("home_abbr", ""), "", "h")}</div>')


def _pg_scores(sc: dict | None) -> str:
    if not sc or not (sc["finals"] or sc["live"] or sc["scheduled"]):
        return '<p class="none">No games in the last few days or scheduled today.</p>'
    out: list[str] = []
    if sc["finals"]:
        out.append(f'<div class="subhead">Final &middot; {_esc(sc["final_day"])}</div>')
        out += [_pg_final(g) for g in sc["finals"]]
    if sc["live"]:
        out.append('<div class="subhead">Live now</div>')
        out += [_pg_simple(g, f'{_esc(g.get("away_score", "0"))} &ndash; {_esc(g.get("home_score", "0"))}')
                for g in sc["live"]]
    if sc["scheduled"]:
        out.append('<div class="subhead">Later today</div>')
        out += [_pg_simple(g, f'{g["kickoff"]:%H:%M}') for g in sc["scheduled"]]
    return "".join(out)


def _record_cells(record: str) -> str:
    parts = (record or "").split("-")
    w, losses = (parts + ["", ""])[:2]
    ties = parts[2] if len(parts) > 2 else "0"
    return f"<td>{_esc(w)}</td><td>{_esc(losses)}</td><td>{_esc(ties)}</td>"


def _pg_standings(divisions: list[dict]) -> str:
    if not divisions:
        return '<p class="none">Standings unavailable today.</p>'
    afc = [d for d in divisions if d["division"].upper().startswith("AFC")]
    nfc = [d for d in divisions if d["division"].upper().startswith("NFC")]
    if not (afc and nfc):
        half = len(divisions) // 2 or len(divisions)
        afc, nfc = divisions[:half], divisions[half:]

    def conf(divs: list[dict], cid: str, hidden: bool) -> str:
        blocks = []
        for d in divs:
            rows = "".join(
                f'<tr><td><span>{_logo_img(t.get("abbr", ""))}'
                f'<span class="nm">{_esc(t["name"])}</span></span></td>'
                f'{_record_cells(t["record"])}</tr>'
                for t in d["teams"]
            )
            blocks.append(
                f'<div class="div"><h3>{_esc(d["division"])}</h3>'
                f'<table class="st"><thead><tr><th>Team</th><th>W</th><th>L</th><th>T</th></tr>'
                f'</thead><tbody>{rows}</tbody></table></div>'
            )
        return (f'<div class="conf" data-conf="{cid}"{" hidden" if hidden else ""}>'
                f'{"".join(blocks)}</div>')

    return (
        '<div class="tabs">'
        '<button class="tab" data-conf="afc" aria-selected="true">AFC</button>'
        '<button class="tab" data-conf="nfc" aria-selected="false">NFC</button></div>'
        + conf(afc, "afc", False) + conf(nfc, "nfc", True)
    )


def _pg_storylines(items: list[dict], limit: int = 12,
                   empty: str = "No headlines today.") -> str:
    if not items:
        return f'<p class="none">{_esc(empty)}</p>'
    out = []
    for it in items[:limit]:
        href = _esc(it["link"]) if it.get("link") else ""
        if href:
            out.append(f'<a class="story" href="{href}" target="_blank" rel="noopener">'
                       f'<div class="hl">{_esc(it["title"])}</div>'
                       f'<div class="src">{_esc(it["source"])}</div></a>')
        else:
            out.append(f'<div class="story"><div class="hl">{_esc(it["title"])}</div>'
                       f'<div class="src">{_esc(it["source"])}</div></div>')
    return f'<div class="list">{"".join(out)}</div>'


def _pg_schedule(games: list[dict]) -> str:
    if not games:
        return '<p class="none">No games scheduled in the coming week.</p>'
    out = [
        f'<div class="sg"><time>{_short_datetime(g["kickoff"])}</time>'
        f'<span>{_esc(g["away"])} <span class="at">at</span> {_esc(g["home"])}</span></div>'
        for g in games
    ]
    return f'<div class="list">{"".join(out)}</div>'


def render_page(sections: dict, errors: list[str]) -> str:
    today = datetime.now(LOCAL_TZ)
    built = f"{_short_datetime(today)} (Dublin)"

    err_html = ""
    if errors:
        names = ", ".join(sorted({e.split(":")[0] for e in errors}))
        err_html = f'<div class="err">Some sources were unavailable today: {_esc(names)}.</div>'

    cards: list[str] = []
    if sections.get("team"):
        cards.append(f'<section class="card"><h2>Your team</h2>'
                     f'<p>{_esc(sections["team"])}</p></section>')
    cards.append(f'<section class="card"><h2>Scores</h2>{_pg_scores(sections.get("scores"))}</section>')
    cards.append(f'<section class="card"><h2>Quarterbacks</h2>'
                 f'{_pg_storylines(sections.get("qbs") or [], empty="No quarterback news today.")}</section>')
    cards.append(f'<section class="card"><h2>Standings</h2>'
                 f'{_pg_standings(sections.get("standings") or [])}</section>')
    cards.append(f'<section class="card"><h2>Storylines</h2>'
                 f'{_pg_storylines(sections.get("headlines") or [])}</section>')
    cards.append(f'<section class="card"><h2>This week</h2>'
                 f'{_pg_schedule(sections.get("schedule") or [])}</section>')
    if sections.get("stat"):
        cards.append(f'<section class="card"><h2>Stat of the day</h2>'
                     f'<div class="stat">{_esc(sections["stat"])}</div></section>')

    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">'
        '<meta name="color-scheme" content="light dark">'
        f'<title>NFL Daily &mdash; {_esc(_short_date(today))}</title>'
        f'<style>{_PAGE_CSS}</style></head><body><div class="wrap">'
        f'<header><div class="eyebrow">NFL Daily</div>'
        f'<h1>{_esc(f"{today:%A} {today.day} {today:%B}")}</h1>'
        f'<div class="updated">Updated {_esc(built)}</div></header>'
        f'{err_html}{"".join(cards)}'
        '<footer>Updates automatically every morning.<br>'
        'Scores &amp; standings via ESPN &middot; headlines via ESPN, CBS Sports &amp; ProFootballTalk.'
        f'</footer></div><script>{_PAGE_JS}</script></body></html>'
    )


# --------------------------------------------------------------------------- #
# Email (Resend HTTP API — https://resend.com/docs/api-reference/emails)
# --------------------------------------------------------------------------- #

# Resend's shared sender works with no domain setup, but only delivers to the
# address that owns the Resend account. Verify a domain, then set EMAIL_FROM.
DEFAULT_FROM = "NFL Digest <onboarding@resend.dev>"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def send_email(subject: str, text_body: str, html_body: str | None = None) -> None:
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

    payload = {"from": sender, "to": [recipient], "subject": subject, "text": text_body}
    if html_body:
        payload["html"] = html_body

    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=payload,
        timeout=30,
    )
    if resp.status_code >= 300:
        raise RuntimeError(f"Resend API returned {resp.status_code}: {resp.text}")


def _safe_send(subject: str, body: str) -> None:
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


def _write_page(html_doc: str) -> None:
    path = os.environ.get("DIGEST_PAGE_PATH", PAGE_PATH)
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html_doc)


def _build_email_bodies(sections: dict, errors: list[str], subject: str):
    if USE_LLM:
        try:
            prose = generate_digest(sections)
            return prose, prose_to_html(prose)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            _safe_send(
                f"{subject} (PARTIAL — Claude call failed)",
                f"Data was fetched but the Claude API call failed:\n\n{e}\n\n"
                "Raw digest follows:\n\n" + render_text(sections, errors),
            )
            return None, None
    return render_text(sections, errors), render_html(sections, errors)


def main() -> None:
    fetchers = [
        ("scores", fetch_scores),
        ("standings", fetch_standings),
        ("headlines", fetch_headlines),
        ("schedule", fetch_schedule),
    ]
    sections: dict = {}
    errors: list[str] = []
    for key, fn in fetchers:
        try:
            sections[key] = fn()
            print(f"[ok]   {key}")
        except FetchError as e:
            errors.append(str(e))
            print(f"[warn] {e}", file=sys.stderr)

    sections["stat"] = compute_stat_of_day()
    sections["team"] = build_team_section()
    sections["qbs"] = [h for h in (sections.get("headlines") or [])
                       if is_qb_headline(h["title"])][:MAX_QB_HEADLINES]

    today = datetime.now(LOCAL_TZ)
    subject = f"NFL Daily Digest — {today:%a} {today.day} {today:%b}"
    have_data = any(sections.get(k) for k in ("scores", "standings", "headlines", "schedule"))
    dry = bool(os.environ.get("DIGEST_DRY_RUN"))
    email_now = SEND_EMAIL and (dry or not _skip_for_wrong_hour())

    if os.environ.get("DIGEST_SKIP_LLM"):
        print("\n" + "=" * 60 + "\n")
        print(render_text(sections, errors))
        return

    # The page is always (re)built so GitHub Pages never goes stale or empty.
    if WRITE_PAGE:
        _write_page(render_page(sections, errors))
        print(f"Wrote {os.environ.get('DIGEST_PAGE_PATH', PAGE_PATH)}")

    if not have_data:
        if email_now:
            _safe_send(
                f"{subject} (FAILED)",
                "The NFL digest could not run today — every data source failed:\n\n"
                + "\n".join(f"- {e}" for e in errors),
            )
        sys.exit(1)

    if not SEND_EMAIL:
        return
    if not email_now:
        print(f"Dublin time {today:%H:%M} — not the 11:00 run; page updated, email skipped.")
        return

    text_body, html_body = _build_email_bodies(sections, errors, subject)
    if text_body is None:
        return  # LLM failure already reported; don't fail the job / block the page
    if dry:
        print("\n" + "=" * 60 + f"\nSubject: {subject}\n" + "=" * 60 + "\n" + text_body)
        return
    try:
        send_email(subject, text_body, html_body)
        print(f"Sent: {subject}")
    except Exception as e:  # noqa: BLE001 — email is secondary to the page
        traceback.print_exc()
        print(f"::error title=Digest email failed::{e}")


if __name__ == "__main__":
    main()
