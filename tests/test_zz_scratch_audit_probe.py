from biokids_core import repo
from biokids_core.db import Database
from fastapi.testclient import TestClient
from tests.test_panel import login, _audit_rows, client, family_ready, daemon  # noqa: F401


def test_probe(client: TestClient, family_ready: None, db: Database) -> None:
    with db.session() as session:
        repo.audit(session, actor="daemon", action="daemon_stopped")
        repo.audit(session, actor="child:elif", action="access_requested", target="child:1")
        repo.audit(session, actor="parent:1", action="passphrase_failed")
    login(client)
    page = client.get("/audit").text
    rows = _audit_rows(page)
    print("ROWS-----")
    for line in rows.splitlines():
        s = line.strip()
        if s:
            print(s)
    print("DROPDOWN-----")
    start = page.index('name="action"')
    print(page[start:page.index("</select>", start)])
