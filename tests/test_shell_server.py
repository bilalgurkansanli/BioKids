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

import asyncio
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
    runner = RecordingRunner()
    supervisor = Supervisor(
        database=db,
        bus=bus,
        watcher=SessionWatcher(source),
        enforcer=Enforcer(config, runner),
        clock=clock,
        config=config,
    )
    await supervisor.start()
    server = ShellServer(supervisor, bus, config)
    return {
        "supervisor": supervisor,
        "server": server,
        "source": source,
        "bus": bus,
        "runner": runner,
    }


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


async def test_the_rest_of_an_answer_is_asked_for_the_caller_alone(
    running: dict[str, Any],
) -> None:
    """D-216: the answer id names an answer; whose it is, the kernel says."""

    class Gate:
        def __init__(self) -> None:
            self.asked: list[tuple[int, str]] = []

        async def more(self, child_id: int, answer_id: str) -> dict[str, Any]:
            self.asked.append((child_id, answer_id))
            return {"done": False, "sentences": ["Bir."]}

        async def aclose(self) -> None:
            return None

    gate = Gate()
    server = ShellServer(running["supervisor"], running["bus"], assistant=gate)  # type: ignore[arg-type]
    elif_id = server.supervisor.by_user["elif"]
    kerem_id = server.supervisor.by_user["kerem"]

    response, _ = await server._dispatch(
        caller_for(server, "elif"),
        Request(
            op=str(ShellOp.ASSISTANT_MORE), args={"answer_id": "x" * 500, "child_id": kerem_id}
        ).encode(),
    )
    assert response.ok and response.data["sentences"] == ["Bir."]
    assert gate.asked == [(elif_id, "x" * 64)], "the caller's child, a bounded id"


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


async def settled(supervisor: Supervisor) -> None:
    """Wait for what the daemon answered for and then went on to do (D-209)."""
    while supervisor._afterwards:
        await asyncio.gather(*list(supervisor._afterwards), return_exceptions=True)


async def test_a_child_cannot_shut_down_while_a_sibling_is_logged_in(
    running: dict[str, Any],
) -> None:
    """One child must not be able to close another child's work.

    Their own, though, they can (D-209): the computer stays on and only the
    caller's session ends. The launcher is told which, and not who else is
    there - the child's screen has no use for another account's name.
    """
    server: ShellServer = running["server"]
    source: FakeSessionSource = running["source"]
    runner: RecordingRunner = running["runner"]
    source.log_in("elif")
    source.log_in("kerem", uid=1001)
    await server.supervisor.watcher.refresh()

    response, _ = await server._dispatch(
        caller_for(server, "elif"), Request(op=str(ShellOp.REQUEST_SHUTDOWN)).encode()
    )
    await settled(server.supervisor)

    assert response.ok is True
    assert response.data == {"poweroff": False, "outcome": "logout"}
    assert "kerem" not in response.encode().decode().lower()
    assert runner.ran("loginctl", "terminate-user", "elif")
    assert not runner.ran("loginctl", "terminate-user", "kerem")
    assert not runner.ran("systemctl", "poweroff")


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
    await settled(server.supervisor)

    assert response.ok is True
    assert response.data == {"poweroff": True, "outcome": "poweroff"}
    runner: RecordingRunner = running["runner"]
    assert runner.ran("systemctl", "poweroff")


async def test_subscribing_asks_for_a_stream_rather_than_answering(
    running: dict[str, Any],
) -> None:
    server: ShellServer = running["server"]
    response, subscribe = await server._dispatch(
        caller_for(server, "elif"), Request(op=str(ShellOp.SUBSCRIBE_EVENTS)).encode()
    )
    assert response.ok
    assert subscribe is True


# ------------------------------------------------- stopping while somebody listens


async def test_stopping_cancels_a_subscriber_instead_of_waiting_for_it(
    running: dict[str, Any],
) -> None:
    """Nothing ever called ShellServer.start() or stop(), and this is what that cost.

    Since CPython 3.12 `Server.wait_closed()` waits for every connection handler
    task. `_stream` parks on the subscriber queue forever, so a launcher with an
    open subscription - which is every launcher, for as long as the kiosk is on
    screen - held the daemon open for the full 90 s stop timeout. systemd then
    SIGKILLed it, `$SERVICE_RESULT` became `timeout` rather than `success`, and
    the unit's own ExecStopPost fired `biokids-admin lockdown`: an upgrade
    locked the child out of a machine they were legitimately using, and the
    ledger was never flushed because supervisor.stop() never ran.

    No socket here on purpose: the defect is the order of two lines in stop(),
    and that is testable on any platform.
    """
    server: ShellServer = running["server"]
    parked = asyncio.Event()

    async def never_returns() -> None:
        parked.set()
        await asyncio.Event().wait()

    task = asyncio.create_task(never_returns())
    server._connections.add(task)
    await parked.wait()

    await asyncio.wait_for(server.stop(), timeout=5)

    assert task.cancelled() or task.done(), "the subscriber was left running"
    assert not server._connections, "the connection set was not cleared"


async def test_stopping_twice_is_not_an_error(running: dict[str, Any]) -> None:
    """systemd can send SIGTERM while an orderly shutdown is already under way."""
    server: ShellServer = running["server"]
    await asyncio.wait_for(server.stop(), timeout=5)
    await asyncio.wait_for(server.stop(), timeout=5)


# ------------------------------------------- a refused search, for the parent (D-222)


async def _report(
    server: ShellServer, user: str, words: str, host: str = "www.google.com"
) -> None:
    response, _ = await server._dispatch(
        caller_for(server, user),
        Request(op=str(ShellOp.REPORT_SEARCH), args={"words": words, "host": host}).encode(),
    )
    assert response.ok


async def test_a_refused_search_is_kept_for_the_parent_with_its_words(
    running: dict[str, Any], db: Database
) -> None:
    from biokids_core.db import AiAlert

    server: ShellServer = running["server"]
    await _report(server, "elif", "  porno   izle ")

    with db.session() as session:
        [alert] = session.query(AiAlert).all()
        audit = session.query(AuditLog).filter(AuditLog.action == "search_refused").one()
        assert alert.child_id == server.supervisor.by_user["elif"]
        assert (alert.app, alert.text, alert.dismissed) == ("search", "porno izle", False)
        # The audit log is kept for good: it names the site, never the words.
        assert "porno" not in audit.detail_json
        assert audit.detail == {"host": "www.google.com"}
        assert audit.actor == "child:elif"


async def test_the_same_search_again_soon_is_one_search(
    running: dict[str, Any], db: Database, clock: FakeClock
) -> None:
    from biokids_core.db import AiAlert

    server: ShellServer = running["server"]
    await _report(server, "elif", "porno")
    await _report(server, "elif", "PORNO")
    await _report(server, "kerem", "porno")
    clock.advance(11 * 60)
    await _report(server, "elif", "porno")

    with db.session() as session:
        assert session.query(AiAlert).count() == 3


async def test_a_child_testing_the_wall_fills_no_more_than_a_days_worth(
    running: dict[str, Any], db: Database
) -> None:
    from biokids_core.db import AiAlert
    from biokids_daemon.operations import SEARCH_ALERTS_PER_DAY

    server: ShellServer = running["server"]
    for n in range(SEARCH_ALERTS_PER_DAY + 5):
        await _report(server, "elif", f"porno {n}")
    await _report(server, "elif", "")

    with db.session() as session:
        assert session.query(AiAlert).count() == SEARCH_ALERTS_PER_DAY


# ------------------------------------------------------ the English corner (D-225)


async def test_an_english_answer_moves_the_callers_box_and_nobody_elses(
    running: dict[str, Any], db: Database
) -> None:
    from biokids_core.db import EnglishItem

    server: ShellServer = running["server"]
    elif_caller = caller_for(server, "elif")
    response, _ = await server._dispatch(
        elif_caller,
        Request(
            op=str(ShellOp.ENGLISH_ANSWER), args={"item": "animals/cat", "right": True}
        ).encode(),
    )
    assert response.ok and response.data == {"box": 1}
    state, _ = await server._dispatch(
        elif_caller, Request(op=str(ShellOp.ENGLISH_STATE)).encode()
    )
    assert list(state.data["items"]) == ["animals/cat"]
    assert state.data["items"]["animals/cat"][0] == 1

    kerem, _ = await server._dispatch(
        caller_for(server, "kerem"), Request(op=str(ShellOp.ENGLISH_STATE)).encode()
    )
    assert kerem.data["items"] == {}
    with db.session() as session:
        [row] = session.query(EnglishItem).all()
        assert row.child_id == server.supervisor.by_user["elif"]
        assert row.right == 1 and row.wrong == 0


async def test_only_the_corners_own_items_are_kept(
    running: dict[str, Any], db: Database
) -> None:
    from biokids_core.db import EnglishItem

    server: ShellServer = running["server"]
    response, _ = await server._dispatch(
        caller_for(server, "elif"),
        Request(
            op=str(ShellOp.ENGLISH_ANSWER), args={"item": "x" * 500, "right": True}
        ).encode(),
    )
    assert not response.ok and response.error == "english.unknown_item"
    with db.session() as session:
        assert session.query(EnglishItem).count() == 0


async def test_a_word_said_back_is_heard_in_english(
    running: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    server: ShellServer = running["server"]
    heard: list[str | None] = []

    async def hear(child_id: int, audio: str, language: str | None = None) -> dict[str, Any]:
        heard.append(language)
        return {"ok": True, "text": "cat"}

    monkeypatch.setattr(server.assistant, "hear", hear)
    for language in ("en", "fr", None):
        args = {"audio": "UklGRg=="} | ({"language": language} if language else {})
        await server._dispatch(
            caller_for(server, "elif"),
            Request(op=str(ShellOp.HEAR_ASSISTANT), args=args).encode(),
        )
    assert heard == ["en", None, None], "English, or the child's own language"
