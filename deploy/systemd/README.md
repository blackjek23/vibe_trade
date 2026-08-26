# deploy/systemd/

systemd timer replacement for `deploy/crontab.example`'s four trading-day
jobs plus backup and the weekly report. Fixes H-1b (PROJECT_EVALUATION.md):
Ubuntu's default `cron` package explicitly does not support per-job or
per-section timezones (`TZ=`/`CRON_TZ=` in a crontab only sets an environment
variable for the spawned process -- it does not affect *when* cron fires the
job; confirmed against `/usr/share/doc/cron/FEATURES` on Ubuntu 3.0pl1).
A crontab pinned to a fixed Asia/Jerusalem clock time therefore cannot track
the US market's actual local time -- it drifts by up to an hour during the
~19 days a year Israel and the US are on different sides of a DST transition.

systemd's `OnCalendar=` *does* support an explicit trailing timezone and
computes it correctly against each zone's own DST rules -- verified here with
`systemd-analyze calendar`, which shows `submit`'s timer landing on 16:00 IDT
on every normal day, correctly shifting to 15:00 IST for the Oct 26-30 2026
window where Israel has already left DST and the US hasn't yet, then back to
16:00 IST once the US follows on Nov 2. That is the exact drift H-1 describes,
and this is empirical proof the fix computes it correctly with zero manual
seasonal adjustment.

## Why the market-tied jobs are anchored where they are

| Job | Anchor | Why |
|---|---|---|
| `preflight` | 08:50 America/New_York | 10 min before submit |
| `submit` | 09:00 America/New_York | 30 min before the 09:30 ET open |
| `record` | 09:35 America/New_York | 5 min after the open, so fills exist |
| `reconcile` | 16:30 America/New_York | 30 min after the 16:00 ET close |

`backup` (23:45 Asia/Jerusalem) and `report-weekly` (Saturday 09:00
Asia/Jerusalem) are **not** tied to the US market clock and stay on the host's
local time. `backup` firing 15-75 minutes after `reconcile` (the gap varies
with the DST mismatch, but is never negative) was checked by hand against
both the matched and mismatched cases.

## Files

| File | What |
|---|---|
| `vibe-trade-{preflight,submit,record,reconcile,backup,report-weekly}.service` | oneshot units, one per job — same `docker compose run --rm <job>` (or the backup's `docker run`) as the crontab line it replaces |
| `vibe-trade-*.timer` | the actual schedule; each references its `.service` implicitly by matching filename |

All `.service`/`.timer` pairs passed `systemd-analyze verify` (static syntax
check) and every `OnCalendar=` line was checked with `systemd-analyze
calendar` for plausible, correctly-shifting next-run times.

**Confirmed installed and running** on `jeki-MINIPC` (2026-08-26) — the
dev/bring-up host this repo runs on day to day, **not** the `/opt/vibe-trade`
+ `vibe` user prod host these checked-in unit files assume. That host got
its own generated units (`User=jeki`,
`WorkingDirectory=/home/jeki/Projects/vibe_trade/deploy`) rather than these
files directly. `systemctl list-timers 'vibe-trade-*'` there shows all six
timers enabled with correct next-fire times. **The checked-in files below are
still unverified against the eventual real prod box** — re-verify paths and
user there before trusting it unattended, same caveat as
`deploy/ibgateway/README.md`.

## Install

Assumes the repo is at `/opt/vibe-trade` and jobs run as the `vibe` user
(same convention as `deploy/ibgateway/ib-gateway.service`) who is in the
`docker` group. Adjust paths in the `.service` files first if yours differ.

```bash
sudo cp deploy/systemd/vibe-trade-*.service deploy/systemd/vibe-trade-*.timer /etc/systemd/system/
sudo systemctl daemon-reload

# Enable + start every timer (the .service files are triggered by their timer,
# never run directly or on boot on their own).
for t in preflight submit record reconcile backup report-weekly; do
  sudo systemctl enable --now "vibe-trade-${t}.timer"
done

# Verify:
systemctl list-timers 'vibe-trade-*'
```

## Removing the old crontab entries

Once the timers are confirmed running (watch `logs/cron.log` through at least
one real trading day), remove the corresponding lines from whatever crontab
`deploy/crontab.example` was installed into (`crontab -e`) so jobs don't fire
twice. Don't remove them before that — these timers are unverified against a
real host; keep the working cron schedule as a fallback until the timers have
proven themselves.

## Rollback

```bash
for t in preflight submit record reconcile backup report-weekly; do
  sudo systemctl disable --now "vibe-trade-${t}.timer"
done
sudo rm /etc/systemd/system/vibe-trade-*.service /etc/systemd/system/vibe-trade-*.timer
sudo systemctl daemon-reload
```

Then reinstall `deploy/crontab.example` if you removed it.
