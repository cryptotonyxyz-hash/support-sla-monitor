"""HTTP bridge between the SQLite tickets database and Make.

Make has no native SQLite module, so it cannot read a .db file directly.
This tiny API is the bridge: it exposes the tickets table over HTTP, and
Make calls it with its HTTP modules.

Endpoints:
  GET  /health                    -> {"status": "ok"}
  POST /tickets                   -> intake: a form or CRM posts a new ticket
  GET  /tickets/new               -> tickets waiting to be triaged (status 'new')
  GET  /tickets?status=open       -> list tickets, optionally filtered
  GET  /tickets/{ticket_id}       -> one ticket
  POST /tickets/{ticket_id}/triage -> Make writes back priority + deadlines
  POST /tickets/{ticket_id}/auto-triage -> API computes priority +
     deadlines itself and flips the ticket to 'open' (recommended:
     Make needs no formulas at all)

Flow:
  1. Website form / CRM posts to POST /tickets. Ticket is stored with
     status 'new', no priority, no deadlines.
  2. Make (on a schedule, e.g. every 15 minutes) calls GET /tickets/new.
  3. For each ticket, Make calls POST /tickets/{id}/auto-triage.
     The API computes the priority from the same rule, looks up the
     SLA hours, computes both deadlines, and flips the status to
     'open'. (The older /triage endpoint still works if Make wants to
     compute the values itself and write them back.)
  4. Make logs the triaged ticket to the Google Sheets Tickets Log and,
     if priority is urgent, posts a Slack alert.

Run it (from this folder, using the project virtualenv):
  .venv/bin/uvicorn api:app --host 0.0.0.0 --port 8000
Docs (auto-generated): http://localhost:8000/docs

For Make to reach it from the cloud during a demo, expose it with ngrok
(`ngrok http 8000`) and use the https URL it prints as the API base URL
in the Make HTTP modules. For production, deploy this file to a host
(Render, Railway, or any Python host) with the .db file alongside it.
"""

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "sla_monitor.db"

app = FastAPI(title="Support SLA Monitor API")


@app.get("/", response_class=HTMLResponse)
def serve_intake_form():
    """The customer-facing Contact Support form.

    Served from the API itself so the form posts to /tickets on the
    same origin (no CORS needed). Fill it in and submit to create a
    new ticket with status 'new', ready for Make to pick up.
    """
    return (HERE / "intake-form.html").read_text(encoding="utf-8")


@app.get("/dashboard", response_class=HTMLResponse)
def serve_dashboard():
    """The manager's SLA dashboard.

    A single page that fetches /tickets from this API and computes the
    KPIs live in the browser: first-response and resolution compliance,
    waiting and breaching tickets, slowest first reply by category,
    and the oldest open tickets. Same origin as the API, so no CORS
    is needed.
    """
    return (HERE / "dashboard.html").read_text(encoding="utf-8")


class TicketIn(BaseModel):
    customer_name: str
    channel: str
    category: str
    sentiment: str
    product_mentioned: str = ""
    message_text: str
    received_at: str | None = None


class TriageIn(BaseModel):
    priority: str
    first_response_deadline: str
    resolution_deadline: str


def connect():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    return con


def next_ticket_id(con):
    row = con.execute(
        "SELECT MAX(CAST(SUBSTR(ticket_id, 5) AS INTEGER)) FROM tickets"
    ).fetchone()
    n = (row[0] or 0) + 1
    return f"MSG-{n:03d}"


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/tickets")
def create_ticket(ticket: TicketIn):
    received = ticket.received_at or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    con = connect()
    ticket_id = next_ticket_id(con)
    con.execute(
        """INSERT INTO tickets
           (ticket_id, customer_name, channel, category, sentiment, priority,
            product_mentioned, message_text, received_at,
            first_response_at, resolved_at,
            first_response_deadline, resolution_deadline, status)
           VALUES (?, ?, ?, ?, ?, NULL, ?, ?, ?, NULL, NULL, NULL, NULL, 'new')""",
        (
            ticket_id, ticket.customer_name, ticket.channel, ticket.category,
            ticket.sentiment, ticket.product_mentioned, ticket.message_text,
            received,
        ),
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    con.close()
    return dict(row)


def compute_priority(category: str, sentiment: str) -> str:
    """The priority rule, same as the database seed and the Make formula.

    urgent = negative sentiment AND category is delivery or returns.
    high   = negative sentiment OR category is returns.
    normal = everything else.
    """
    if sentiment == "negative" and category in ("delivery", "returns"):
        return "urgent"
    if sentiment == "negative" or category == "returns":
        return "high"
    return "normal"


@app.post("/tickets/{ticket_id}/auto-triage")
def auto_triage_ticket(ticket_id: str):
    """Triage a ticket without Make having to compute anything.

    The API reads the ticket's sentiment, category and received_at,
    computes the priority from the same rule, looks up the SLA hours
    from the sla_rules table, computes both deadlines, and flips the
    ticket to 'open'. Make just calls this endpoint and uses the
    returned ticket (priority, deadlines, all fields) for the Sheets
    log and the Slack alert. No Make formulas needed.

    Returns 404 if the ticket does not exist, 409 if it is not 'new'.
    """
    con = connect()
    row = con.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        con.close()
        raise HTTPException(status_code=404, detail="ticket not found")
    if row["status"] != "new":
        con.close()
        raise HTTPException(
            status_code=409, detail=f"ticket is already {row['status']}"
        )
    priority = compute_priority(row["category"], row["sentiment"])
    rule = con.execute(
        "SELECT * FROM sla_rules WHERE priority = ?", (priority,)
    ).fetchone()
    received = datetime.strptime(row["received_at"], "%Y-%m-%d %H:%M:%S")
    first_deadline = (
        received + timedelta(hours=rule["first_response_hours"])
    ).strftime("%Y-%m-%d %H:%M:%S")
    resolution_deadline = (
        received + timedelta(hours=rule["resolution_hours"])
    ).strftime("%Y-%m-%d %H:%M:%S")
    con.execute(
        """UPDATE tickets
           SET priority = ?, first_response_deadline = ?,
               resolution_deadline = ?, status = 'open'
           WHERE ticket_id = ?""",
        (priority, first_deadline, resolution_deadline, ticket_id),
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    con.close()
    return dict(row)


@app.get("/tickets/new")
def new_tickets():
    con = connect()
    rows = con.execute(
        "SELECT * FROM tickets WHERE status = 'new' ORDER BY received_at"
    ).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/tickets")
def list_tickets(status: str | None = None):
    con = connect()
    if status:
        rows = con.execute(
            "SELECT * FROM tickets WHERE status = ? ORDER BY received_at",
            (status,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM tickets ORDER BY received_at"
        ).fetchall()
    con.close()
    return [dict(r) for r in rows]


@app.get("/tickets/{ticket_id}")
def get_ticket(ticket_id: str):
    con = connect()
    row = con.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    con.close()
    if row is None:
        raise HTTPException(status_code=404, detail="ticket not found")
    return dict(row)


@app.post("/tickets/{ticket_id}/triage")
def triage_ticket(ticket_id: str, triage: TriageIn):
    con = connect()
    row = con.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    if row is None:
        con.close()
        raise HTTPException(status_code=404, detail="ticket not found")
    if row["status"] != "new":
        con.close()
        raise HTTPException(
            status_code=409, detail=f"ticket is already {row['status']}"
        )
    rule = con.execute(
        "SELECT * FROM sla_rules WHERE priority = ?", (triage.priority,)
    ).fetchone()
    if rule is None:
        con.close()
        raise HTTPException(status_code=400, detail="unknown priority")
    con.execute(
        """UPDATE tickets
           SET priority = ?, first_response_deadline = ?,
               resolution_deadline = ?, status = 'open'
           WHERE ticket_id = ?""",
        (
            triage.priority,
            triage.first_response_deadline,
            triage.resolution_deadline,
            ticket_id,
        ),
    )
    con.commit()
    row = con.execute(
        "SELECT * FROM tickets WHERE ticket_id = ?", (ticket_id,)
    ).fetchone()
    con.close()
    return dict(row)
