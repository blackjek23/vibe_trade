# deploy/ibgateway/

Assets for running IB Gateway unattended. The setup guide is
[`docs/playbooks/ib-gateway.md`](../../docs/playbooks/ib-gateway.md).

| File | What |
|---|---|
| `ib-gateway.service` | systemd unit — IBC under xvfb, `Restart=always`, credentials from `/etc/vibe-trade/ibc.env` |
| `config.ini.example` | IBC config template. Login fields intentionally blank — secrets come from `ibc.env`. |

> These files have not been executed on a live box. Verify against your IBC
> version's `gatewaystart.sh --help` before trusting them.
