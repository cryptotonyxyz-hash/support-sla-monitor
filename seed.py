"""Build sla_monitor.db from seed.sql.

Used by Render's build step: the SQLite file itself is binary and is not
committed to the repo, so the build recreates it from this SQL dump.
The dump holds the 80 seed tickets plus the 3 test tickets
(MSG-081/082/083) sitting with status 'new', ready for the Make run.
Rebuilding gives the same database every time.
"""

import sqlite3
from pathlib import Path

HERE = Path(__file__).resolve().parent
DB_PATH = HERE / "sla_monitor.db"
SEED_PATH = HERE / "seed.sql"

if DB_PATH.exists():
    DB_PATH.unlink()

con = sqlite3.connect(DB_PATH)
con.executescript(SEED_PATH.read_text())
con.commit()
count = con.execute("SELECT COUNT(*) FROM tickets").fetchone()[0]
new = con.execute("SELECT COUNT(*) FROM tickets WHERE status = 'new'").fetchone()[0]
print(f"Seeded {DB_PATH}: {count} tickets, {new} new")
con.close()
