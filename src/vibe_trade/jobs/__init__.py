"""V2 jobs — short-lived processes triggered by OS cron.

Each module here is a single phase of the daily flow:
- submit:    16:00 — exits then entries, places IB orders, no DB writes
- record:    16:25 — pulls today's fills, persists as SUBMITTED in DB
- reconcile: 23:30 — updates fill statuses, writes portfolio snapshot
"""
