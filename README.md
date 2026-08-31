# nfl-digest

An automated daily NFL digest — scores, standings, quarterback news, storylines,
schedule and a stat of the day — published as a **phone-friendly web page**.

Runs on GitHub Actions on a schedule and deploys to **GitHub Pages**. No machine
of your own stays on, no API keys, no cost.

- **Page:** `https://<user>.github.io/nfl-digest/`
- **Updates:** every morning by 10:00 Europe/Dublin (see *Schedule* below)
- **Data:** ESPN's public (unofficial) API for scores/standings/schedule; RSS
  feeds (ESPN, CBS Sports, ProFootballTalk) for headlines
- **Everything is free:** GitHub Actions, GitHub Pages, ESPN, the RSS feeds

## Page sections

1. **Scores** — the last slate's finals (winner in bold), plus anything live or later today
2. **Quarterbacks** — headlines filtered to QB news (`is_qb_headline()` in `digest.py`)
3. **Standings** — all eight divisions, AFC/NFC tabs, as tables
4. **Storylines** — recent headlines, each linked to the article
5. **This week** — the upcoming slate with kickoff times
6. **Stat of the day** — widest margin + highest-scoring game from the last slate

A per-team **"Your Team"** section is stubbed out (`TEAM` + `build_team_section()`
in `digest.py`) and can be switched on later without restructuring anything.

## Setup

### 1. Enable GitHub Pages

Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.
Pages needs the repo to be **public** on the free plan.

### 2. Run it manually first

Repo → **Actions → NFL Daily Digest → Run workflow**. Confirm it goes green and
the page loads at `https://<user>.github.io/nfl-digest/` before relying on the
schedule.

### 3. Add it to your phone

Open the page in your phone browser → Share → **Add to Home Screen**.

## Local testing

```bash
pip install -r requirements.txt

# print the fetched data as plain text (no file written)
DIGEST_PRINT=1 python digest.py

# build the page to a chosen path and open it
DIGEST_PAGE_PATH=/tmp/nfl.html python digest.py && open /tmp/nfl.html
```

The workflow just runs `python digest.py`, which writes `public/index.html`.

## Schedule

Cron in GitHub Actions is UTC with no daylight-saving handling, so the workflow
declares **two** entries — `09:00 UTC` (= 10:00 Dublin in summer / IST) and
`10:00 UTC` (= 10:00 Dublin in winter / GMT). The page is tiny and cheap to
rebuild, so both runs redeploy it; the result is that the page is always fresh
by 10:00 local time year-round. GitHub may delay scheduled runs by a few minutes
under load.

## Error handling

If some data sources fail, the page still rebuilds with a banner naming what was
missing. If **every** source fails, the run exits non-zero without touching the
page, so Pages keeps serving the last good version.

## Seasonal upkeep

The `_QB_NAMES` regex near the top of `digest.py` is a hand-maintained list of
starting quarterbacks — refresh it once a season as starters change.
