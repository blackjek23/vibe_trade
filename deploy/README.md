# deploy/

Deployment **assets** live here. The **playbooks** that use them live in
[`docs/playbooks/`](../docs/playbooks/) — centralised so operational procedures are
in one place.

| Need | Go to |
|---|---|
| First-time deploy, Docker, cron install, updating | [docs/playbooks/deployment.md](../docs/playbooks/deployment.md) |
| Fresh Linux host, end to end | [docs/playbooks/linux-bringup.md](../docs/playbooks/linux-bringup.md) |
| IB Gateway unattended (systemd + IBC) | [docs/playbooks/ib-gateway.md](../docs/playbooks/ib-gateway.md) |
| Day-to-day running | [docs/playbooks/daily-operations.md](../docs/playbooks/daily-operations.md) |

## Files here

| File | What |
|---|---|
| `Dockerfile` | python:3.11-slim + uv, single image for all jobs |
| `docker-compose.yml` | `preflight`, `submit`, `record`, `reconcile`, `report-weekly` services (`network_mode: host`) |
| `crontab.example` | Mon–Fri 15:50 / 16:00 / 16:35 / 23:30 + Sat 09:00, Asia/Jerusalem. **Read the record-timing comment before editing.** |
| `smoke-test.sh` | Runs the jobs sequentially |
| `.env.example` | Telegram secrets template |
| `ibgateway/` | systemd unit + IBC config template (see the ib-gateway playbook) |
