# nfl-digest

An automated daily NFL digest — scores, standings, storylines, schedule and a
stat of the day — published as a **phone-friendly web page** and (optionally)
emailed. Runs on GitHub Actions on a schedule; no machine of your own stays on.

- **Page:** rebuilt every run and deployed to **GitHub Pages** →
  `https://<user>.github.io/nfl-digest/`
- **Email:** optional, via the [Resend](https://resend.com) API (free tier);
  sent only on the 11:00 Europe/Dublin run
- **Runner:** GitHub Actions (free tier)
- **Data:** ESPN's public (unofficial) API for scores/standings + RSS feeds for headlines
- **Digest text:** assembled in plain Python by default (free); optionally
  written by Claude (see below)
- **Secrets:** GitHub encrypted repository secrets — never committed

`digest.py` config flags at the top: `WRITE_PAGE`, `SEND_EMAIL`, `USE_LLM`
(the workflow overrides the first two per step via `DIGEST_WRITE_PAGE` /
`DIGEST_SEND_EMAIL`).

## Cost

The default setup is **free**: GitHub Actions, the ESPN API, the RSS feeds and
Resend's free tier all cost nothing at this volume (~30 emails/month).

The only paid option is turning on the LLM-written digest (`USE_LLM = True`),
which calls the Anthropic API — pay-as-you-go, roughly **$1/month** here, with a
$5 minimum credit top-up. Off by default.

## Digest format

A clean HTML email (with a plain-text fallback), phone-readable in under two
minutes, always in this order:

1. **Scores** — last slate's finals, winner in bold, plus anything live or later today
2. **Standings** — all eight divisions, two columns
3. **Storylines** — recent headlines (top 8), each linked to the article
4. **This week** — upcoming games with kickoff times
5. **Stat of the day** — widest margin + highest-scoring game from the last slate

A per-team **"Your Team"** section is stubbed out (`TEAM` + `build_team_section()`
in `digest.py`) and can be switched on later without restructuring anything.

## Setup

### 1. Resend account + API key

1. Sign up at <https://resend.com> — **use the address you want the digest
   delivered to** (e.g. `hello@sammemery.com`). Resend's shared sender
   (`onboarding@resend.dev`) only delivers to the account owner's address until
   you verify your own domain, so this needs to match `RECIPIENT_EMAIL`.
2. **API Keys → Create API Key** → name it `nfl-digest`, permission "Sending
   access" → copy the `re_...` value.
3. *(Optional, later)* verify a domain under **Domains**, then set the `EMAIL_FROM`
   secret to `NFL Digest <nfl@yourdomain.com>` to send from your own address and
   to any recipient.

### 2. Repository secrets

Repo → **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
| --- | --- |
| `RESEND_API_KEY` | your Resend key (`re_...`) |
| `RECIPIENT_EMAIL` | where the digest should land (= your Resend signup email) |
| `EMAIL_FROM` | *(optional)* custom From once you've verified a domain in Resend |
| `ANTHROPIC_API_KEY` | *(optional)* only if you set `USE_LLM = True` |

### 3. Enable GitHub Pages

Repo → **Settings → Pages → Build and deployment → Source: GitHub Actions**.
(Pages needs the repo to be **public** on the free plan.)

### 4. Run it manually first

Repo → **Actions → NFL Daily Digest → Run workflow**. Confirm it goes green, the
page loads at `https://<user>.github.io/nfl-digest/`, and (if enabled) the email
arrives — before relying on the schedule.

## Local testing

```bash
pip install -r requirements.txt

# 1. fetch data only — print the raw sections, then stop
DIGEST_SKIP_LLM=1 python digest.py

# 2. build the full digest, print it instead of emailing
DIGEST_DRY_RUN=1 python digest.py

# 3. full run — actually sends the email
export RESEND_API_KEY=re_...
export RECIPIENT_EMAIL=you@example.com
python digest.py
```

## Optional: LLM-written digest

To have Claude turn the raw data into a smoother, more editorial digest
(with real "why it matters" lines and a hand-picked stat):

1. In `digest.py`, set `USE_LLM = True` (and pick `MODEL` — `claude-opus-5` or the
   cheaper `claude-sonnet-5`).
2. In `requirements.txt`, uncomment the `anthropic` line.
3. Get a key at <https://console.anthropic.com/> → **API Keys**, add credit under
   **Billing**, and add it as the `ANTHROPIC_API_KEY` repository secret.

If the Claude call fails, the run emails you the raw data instead of failing
silently.

## How the schedule handles DST

Cron in GitHub Actions is UTC and doesn't follow daylight saving. The workflow
fires at **both** 10:00 and 11:00 UTC; `digest.py` checks the actual
`Europe/Dublin` local time and the run that isn't at 11:00 exits immediately
without sending. Set `DIGEST_FORCE=1` to bypass that guard when testing.

## Error handling

If a data source fails, the digest still sends with a note saying what was
missing. If *every* source fails you get an email saying so, rather than silence.
