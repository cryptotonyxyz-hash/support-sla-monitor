# Support SLA & Escalation Monitor — API

The HTTP bridge for the Support SLA & Escalation Monitor, a support
operations system for a fictional brand (Zuri Fragrances). It exposes a
SQLite tickets database over HTTP so Make (which has no SQLite module)
can run the automation: pick up new tickets, triage them, log them to
Google Sheets, and alert on Slack when one is urgent.

## Endpoints

| Method | Endpoint | What it does |
|---|---|---|
| GET | `/health` | `{"status": "ok"}` |
| POST | `/tickets` | Intake a new ticket. Returns it with status `new`. |
| GET | `/tickets/new` | Tickets waiting to be triaged, oldest first. |
| GET | `/tickets?status=open` | List tickets, optionally filtered by status. |
| GET | `/tickets/{ticket_id}` | One ticket. |
| POST | `/tickets/{ticket_id}/triage` | Write back priority + deadlines, flip to `open`. 409 if already triaged. |
| POST | `/tickets/{ticket_id}/auto-triage` | The API computes the priority and both deadlines itself from the same rule, flips the ticket to `open`, and returns the triaged ticket. No body needed. |

Priority rule: urgent = negative sentiment AND category in (delivery,
returns); high = negative sentiment OR category = returns; normal = the
rest. SLA limits live in the `sla_rules` table: urgent 4h/24h, high
12h/48h, normal 24h/72h.

## Run it locally

```bash
pip install -r requirements.txt
python seed.py        # builds sla_monitor.db (80 seed tickets + 3 test tickets as 'new')
uvicorn api:app --host 0.0.0.0 --port 8000
```

Docs: `http://localhost:8000/docs`

## Deploy

This repo has a `render.yaml` Blueprint. Connect it to Render as a
Blueprint (or a Python web service) and it deploys as a free web
service at `https://zuri-sla-api.onrender.com`. The build step runs
`seed.py`, so the database is recreated from `seed.sql` on every
deploy. Note the free tier sleeps after 15 minutes idle and the
filesystem is ephemeral: on restart the database resets to the seeded
state, which is the known-good demo state.

Part of the Support SLA & Escalation Monitor portfolio project. The
full write-up (Make scenarios, Google Sheets log, Slack alerts,
dashboard, case study) lives with the project, not in this repo.
