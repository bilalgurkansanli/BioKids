"""The child's socket, and the one decision it rests on.

`/run/biokids/shell.sock` is world-connectable. That is safe for exactly one
reason: **no operation on it takes a child id as an argument**, and the daemon
decides who is calling from the peer's uid, which the kernel fills in and a
process cannot forge about itself (D-123).

So the tests that matter are about `_identify`. If it ever returns a caller for
a uid that is not a managed child, or the wrong child for a uid, the open file
mode stops being a considered decision and becomes a hole.
"""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.orm import Session

from biokids_core.clock import FakeClock
from biokids_core.config import Config
from biokids_core.db import AuditLog, Child, Database, Policy
from biokids_core.ipc import Op, Request, ShellOp
from biokids_daemon.bus import EventBus
from biokids_daemon.enforcer import Enforcer, RecordingRunner
from biokids_daemon.sessions import FakeSessionSource, SessionWatcher
from biokids_daemon.shell_server import ShellCaller, ShellServer
from biokids_daemon.supervisor import Supervisor


@pytest.fixture
async def running(
    db: Database, config: Config, clock: FakeClock, session: Session
) -> dict[str, Any]:
    """A daemon with two children, so "the other child" is a real possibility."""
    from biokids_core.db import Family

    session.add(Family())
    for name, user, year in (("Elif", "elif", 2020), ("Kerem", "kerem", 2017)):
        child = Child(unix_user=user, display_name=name, birth_year=year)
        session.add(child)
        session.flush()
        session.add(Policy(child_id=child.id))
    session.commit()

    source = FakeSessionSource()
    bus = EventBus(queue_size=256, history_size=256)
    supervisor = Supervisor(
        database=db,
        bus=bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, RecordingRunner()),
        clock=clock,
        config=config,
    )
    await supervisor.start()
    server = ShellServer(supervisor, bus, config)
    return {"supervisor": supervisor, "server": server, "source": source, "bus": bus}


def caller_for(server: ShellServer, unix_user: str, uid: int = 1000) -> ShellCaller:
    child_id = server.supervisor.by_user[unix_user]
    return ShellCaller(uid=uid, runtime=server.supervisor.children[child_id])


# ------------------------------------------------------------- who is calling


def test_an_unknown_uid_is_nobody(running: dict[str, Any], monkeypatch: Any) -> None:
    server: ShellServer = running["server"]
    monkeypatch.setattr("biokids_daemon.shell_server.username_for_uid", lambda uid: "postgres")
    assert server._identify(4242) is None


def test_root_is_refused_on_the_unprivileged_socket(running: dict[str, Any]) -> None:
    """There is nothing here root cannot get from the privileged socket."""
    server: ShellServer = running["server"]
    assert server._identify(0) is None


def test_a_uid_that_does_not_exist_is_nobody(running: dict[str, Any], monkeypatch: Any) -> None:
    server: ShellServer = running["server"]
    monkeypatch.setattr("biokids_daemon.shell_server.username_for_uid", lambda uid: None)
    assert server._identify(9999) is None


def test_no_peer_credentials_means_no_caller(running: dict[str, Any]) -> None:
    """A transport that cannot say who connected gets nothing, not a default."""
    server: ShellServer = running["server"]
    assert server._identify(None) is None


def test_a_managed_child_is_identified_as_themselves(
    running: dict[str, Any], monkeypatch: Any
) -> None:
    server: ShellServer = running["server"]
    monkeypatch.setattr("biokids_daemon.shell_server.username_for_uid", lambda uid: "kerem")
    caller = server._identify(1001)
    assert caller is not None
    assert caller.runtime.unix_user == "kerem"


async def test_a_connection_from_nobody_is_told_once_and_closed(
    running: dict[str, Any], monkeypatch: Any
) -> None:
    """And is told nothing about which children exist."""
    server: ShellServer = running["server"]
    monkeypatch.setattr("biokids_daemon.shell_server.username_for_uid", lambda uid: None)

    written: list[bytes] = []

    class _Writer:
        def get_extra_info(self, _name: str) -> None:
            return None

        def write(self, data: bytes) -> None:
            written.append(data)

        async def drain(self) -> None:
            return None

        def close(self) -> None:
            return None

        async def wait_closed(self) -> None:
            return None

    class _Reader:
        async def readline(self) -> bytes:
            raise AssertionError("must not be read from")

    await server._serve(_Reader(), _Writer())  # type: ignore[arg-type]

    assert len(written) == 1
    body = written[0].decode()
    assert "shell.not_a_child" in body
    assert "Elif" not in body
    assert "Kerem" not in body


# ------------------------------------------------------------ what they can do


async def test_the_state_answers_about_the_caller_whatever_is_asked_for(
    running: dict[str, Any],
) -> None:
    server: ShellServer = running["server"]
    elif_caller = caller_for(server, "elif")
    kerem_id = server.supervisor.by_user["kerem"]

    response, _ = await server._dispatch(
        elif_caller, Request(op=str(ShellOp.GET_STATE), args={"child_id": kerem_id}).encode()
    )

    assert response.ok
    assert response.data["display_name"] == "Elif"


@pytest.mark.parametrize(
    "forbidden",
    [Op.GRANT_TIME, Op.LOCK_NOW, Op.POWEROFF_NOW, Op.SET_POLICY, Op.RELOAD, Op.GET_STATUS],
)
async def test_a_privileged_operation_is_simply_not_there(
    running: dict[str, Any], forbidden: Op
) -> None:
    server: ShellServer = running["server"]
    response, _ = await server._dispatch(
        caller_for(server, "elif"), Request(op=str(forbidden), args={"seconds": 9999}).encode()
    )
    assert response.ok is False
    assert response.error == "shell.not_allowed"


async def test_a_malformed_frame_gets_an_answer_rather_than_a_dropped_connection(
    running: dict[str, Any],
) -> None:
    server: ShellServer = running["server"]
    response, _ = await server._dispatch(caller_for(server, "elif"), b"{not json\n")
    assert response.ok is False
    assert response.error == "protocol.malformed"


async def test_a_launch_request_names_the_app_but_never_the_child(
    running: dict[str, Any],
) -> None:
    server: ShellServer = running["server"]
    response, _ = await server._dispatch(
        caller_for(server, "elif"),
        Request(op=str(ShellOp.LAUNCH), args={"app_slug": "tuxpaint", "child_id": 99}).encode(),
    )
    assert response.ok
    assert response.data["allow"] is False
    assert response.data["reason_code"] == "app.not_allowed"


async def test_asking_a_parent_files_a_request_against_the_caller(
    running: dict[str, Any], db: Database
) -> None:
    from biokids_core.db import AccessRequest

    server: ShellServer = running["server"]
    elif_id = server.supervisor.by_user["elif"]

    response, _ = await server._dispatch(
        caller_for(server, "elif"),
        Request(op=str(ShellOp.ASK_A_PARENT), args={"url": "https://example.com/x"}).encode(),
    )
    assert response.ok

    with db.session() as session:
        rows = session.query(AccessRequest).all()
    assert len(rows) == 1
    assert rows[0].child_id == elif_id
    assert rows[0].status == "pending"


async def test_the_first_run_flag_is_recorded_once(
    running: dict[str, Any], db: Database
) -> None:
    server: ShellServer = running["server"]
    caller = caller_for(server, "elif")
    frame = Request(op=str(ShellOp.FIRST_RUN_DONE)).encode()

    await server._dispatch(caller, frame)
    await server._dispatch(caller, frame)

    with db.session() as session:
        child = session.get(Child, server.supervisor.by_user["elif"])
        assert child is not None and child.first_run_done
        entries = [row.action for row in session.query(AuditLog).all()]
    assert entries.count("first_run_explained") == 1, "the second call is a no-op"


async def test_a_child_cannot_shut_down_while_a_sibling_is_logged_in(
    running: dict[str, Any],
) -> None:
    """One child must not be able to close another child's work."""
    server: ShellServer = running["server"]
    source: FakeSessionSource = running["source"]
    source.log_in("elif")
    source.log_in("kerem", uid=1001)
    await server.supervisor.watcher.refresh()

    response, _ = await server._dispatch(
        caller_for(server, "elif"), Request(op=str(ShellOp.REQUEST_SHUTDOWN)).encode()
    )
    assert response.ok is False
    assert response.error == "shell.not_allowed"


async def test_a_child_alone_at_the_machine_may_shut_it_down(
    running: dict[str, Any],
) -> None:
    """D-100: from a clearly marked tile, with a confirmation."""
    server: ShellServer = running["server"]
    source: FakeSessionSource = running["source"]
    source.log_in("elif")
    await server.supervisor.watcher.refresh()

    response, _ = await server._dispatch(
        caller_for(server, "elif"), Request(op=str(ShellOp.REQUEST_SHUTDOWN)).encode()
    )
    assert response.ok is True


async def test_subscribing_asks_for_a_stream_rather_than_answering(
    running: dict[str, Any],
) -> None:
    server: ShellServer = running["server"]
    response, subscribe = await server._dispatch(
        caller_for(server, "elif"), Request(op=str(ShellOp.SUBSCRIBE_EVENTS)).encode()
    )
    assert response.ok
    assert subscribe is True
