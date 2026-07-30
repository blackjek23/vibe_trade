# IB Gateway — unattended setup

**Problem this solves.** Between 2026-05 and 2026-07, ten trading days produced no
`daily_pnl` row because IB Gateway wasn't running. The bot behaved correctly — the
`_run_with_crash_alert` wrapper fired a `[CRITICAL]` Telegram alert on each failed
job — but ~24 alerts arrived and didn't turn into action. Adding more alerting
would not have helped. The fix is to stop needing a human at all.

Two pieces:

1. **systemd + IBC** keep Gateway up and logged in, and bring it back if it dies.
2. **`vibe-trade preflight`** at 15:50 checks that submit can actually work, and
   reports success as well as failure — so *silence* becomes the anomaly instead
   of one more red message in a stream of red messages.

> ⚠️ **These unit files have not been executed.** They were written against IBC's
> documented interface but not run on a live box, and IBC's flags and `config.ini`
> keys do change between major versions. Treat the install below as a checklist to
> verify, not as known-good. The `preflight` command *is* tested (10 unit tests) —
> it's the part that tells you whether the rest actually worked.

---

## Install

Assumes Debian/Ubuntu and a `vibe` user that owns the deployment. `$REPO` below is
the checkout root:

```bash
REPO=/opt/vibe-trade
```

### 1. Dependencies

```bash
sudo apt update && sudo apt install -y xvfb unzip openjdk-17-jre
```

Gateway is a Java GUI app and needs a display even headless — that's what `xvfb`
is for.

### 2. IB Gateway + IBC

```bash
# IB Gateway (standalone, offline installer from IBKR)
# https://www.interactivebrokers.com/en/trading/ibgateway-stable.php
sudo mkdir -p /opt/ibgateway && cd /opt/ibgateway
# ...run the IBKR installer, note the install path it reports...

# IBC — https://github.com/IbcAlpha/IBC/releases
sudo mkdir -p /opt/ibc && cd /opt/ibc
sudo unzip ~/IBCLinux-3.*.zip
sudo chmod +x *.sh
```

Confirm the script name and flags for **your** IBC version:

```bash
/opt/ibc/gatewaystart.sh --help
```

If `--mode/--user/--pw/--inline` differ, fix [`ib-gateway.service`](../../deploy/ibgateway/ib-gateway.service) to match.

### 3. Config and credentials

```bash
sudo cp "$REPO"/deploy/ibgateway/config.ini.example /opt/ibc/config.ini

sudo mkdir -p /etc/vibe-trade
sudo tee /etc/vibe-trade/ibc.env >/dev/null <<'EOF'
IB_USER=your_paper_username
IB_PASSWORD=your_paper_password
TRADING_MODE=paper
EOF
sudo chmod 600 /etc/vibe-trade/ibc.env
sudo chown root:root /etc/vibe-trade/ibc.env
```

Credentials live **only** in `ibc.env`. `config.ini` keeps its login fields blank
so it never holds a secret.

### 4. Service

```bash
sudo mkdir -p /var/log/vibe-trade && sudo chown vibe:vibe /var/log/vibe-trade
sudo cp "$REPO"/deploy/ibgateway/ib-gateway.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ib-gateway
```

### 5. Verify — do not skip this

```bash
systemctl status ib-gateway
sudo tail -f /var/log/vibe-trade/ibgateway.log

# The real test: is the API port answering?
ss -lntp | grep 7497
```

Then the end-to-end check:

```bash
cd "$REPO"/deploy && docker compose run --rm preflight
```

Expect a table of `OK` rows and `READY`. If Gateway is still logging in you'll
get `ib_account: FAIL … net_liq=$0.00 … Gateway may still be logging in` — that's
the check doing its job, not a bug. Wait a minute and re-run.

Finally, prove the recovery works:

```bash
sudo systemctl kill -s SIGKILL ib-gateway   # simulate a crash
sleep 45
systemctl status ib-gateway                  # should be running again
```

---

## Cron

Add preflight 10 minutes ahead of submit (see [`deploy/crontab.example`](../../deploy/crontab.example)):

```cron
50 15 * * 1-5  cd /opt/vibe-trade/deploy && docker compose run --rm preflight >> ./logs/cron.log 2>&1
```

10 minutes is enough to `systemctl restart ib-gateway` and re-run before 16:00.

---

## The daily re-auth problem

IB force-logs-out once every 24h. `AutoRestartTime=05:00` in `config.ini` makes
Gateway restart *itself* without needing credentials again — this works for about
a week, after which a full re-login happens (IBC handles it from `ibc.env`).

Leave `IbAutoClosedown=no`. With it set to `yes`, Gateway shuts down mid-week and
systemd restarts it into a login it may not complete unattended.

## Known limits

- **2FA blocks this for live accounts.** Fine for paper (username + password). A
  live account with IBKR Mobile two-factor needs a phone approval per login and
  cannot be fully automated. `config.toml` is currently `mode = "paper"`; if that
  ever changes, this setup stops being unattended.
- **systemd can't fix a dead box.** If the host or Docker is down, nothing here
  runs and nothing alerts — cron never fires, so there's no process to complain.
  Catching that needs a check *outside* the box (an external dead-man's-switch
  that expects a daily ping and shouts when it stops arriving). Not built.
- **DST gap.** Israel leaves summer time in late October, the US in early
  November. For roughly a week the US open lands at 17:30 Israel time, so the
  15:50 preflight and 16:35 record both run early. Preflight will pass (Gateway
  is up); record will simply see no fills and reconcile picks them up at 23:30.
