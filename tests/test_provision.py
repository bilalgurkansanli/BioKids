"""Making a child real on the machine, not only in the database.

The gap these cover is the one that looks exactly like success: a parent adds a
child, the panel fills with quotas and windows and a friendly avatar, and
nothing on the machine changed. `unix_user` names an account that does not
exist, so there is no avatar on the login screen; or the account exists but the
session configuration never reached its home, so the child logs in to an
ordinary XFCE desktop with Alt+F4 and a file manager while the parent's Today
screen says they are managed.

The development host is Windows and has neither `adduser` nor a password
database, so the tests that need a real one say so. What can be tested anywhere
is the shape of the failures, and that matters most: this code's job is to be
honest about not having worked.
"""

from __future__ import annotations

import pathlib
from pathlib import Path
from typing import Any

import pytest

from biokids_core import provision as prov
from biokids_core.config import Config


class FakeEntry:
    """What `pwd.getpwnam` returns, in the two fields this module reads."""

    def __init__(self, home: Path, uid: int = 4001, gid: int = 4001) -> None:
        self.pw_dir = str(home)
        self.pw_uid = uid
        self.pw_gid = gid


class FakePwd:
    def __init__(self, users: dict[str, FakeEntry]) -> None:
        self.users = users

    def getpwnam(self, name: str) -> FakeEntry:
        try:
            return self.users[name]
        except KeyError:
            raise KeyError(name) from None


@pytest.fixture
def share(config: Config) -> Path:
    """The installed side: /usr/share/biokids/xfce, as debian/rules lays it out."""
    source = config.paths.share_dir / "xfce"
    source.mkdir(parents=True, exist_ok=True)
    (source / "xfce4-session.xml").write_text("<channel/>", encoding="utf-8")
    (source / "xfwm4.xml").write_text("<channel/>", encoding="utf-8")
    (source / "biokids-shell.desktop").write_text("[Desktop Entry]\n", encoding="utf-8")
    return source


@pytest.fixture
def home(tmp_path: Path) -> Path:
    place = tmp_path / "home" / "ayse"
    place.mkdir(parents=True)
    return place


@pytest.fixture
def with_user(monkeypatch: pytest.MonkeyPatch, home: Path) -> Any:
    entry = FakeEntry(home)
    fake = FakePwd({"ayse": entry})
    monkeypatch.setattr(prov, "_pwd", fake)
    # The fake account's uid is not a real one. On Linux as an ordinary user a
    # real chown to it fails with EPERM, and as root it succeeds and leaves
    # files owned by a stranger; neither is what these tests are about. The
    # tests that are about ownership record the calls instead.
    monkeypatch.setattr(prov.os, "chown", lambda *_a: None, raising=False)
    return fake


# ------------------------------------------------------------------ the account


def test_a_user_who_does_not_exist_is_not_quietly_treated_as_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(prov, "_pwd", FakePwd({}))
    assert prov.account_exists("ayse") is False
    assert prov.home_of("ayse") is None


def test_a_host_with_no_password_database_says_no_rather_than_crashing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Windows, where this is developed. `import pwd` raises there."""
    monkeypatch.setattr(prov, "_pwd", None)
    assert prov.account_exists("ayse") is False
    assert prov.home_of("ayse") is None


def test_provision_refuses_to_report_success_when_it_could_not_create_the_account(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    monkeypatch.setattr(prov, "_pwd", FakePwd({}))
    # `_find`, not `shutil.which`: when `which` finds nothing, `_find` goes on
    # to look in /usr/sbin - so on a Linux machine faking `which` alone ran the
    # real adduser, and under root would have created a real account.
    monkeypatch.setattr(prov, "_find", lambda _program: None)

    result = prov.provision("ayse", "Ayşe", config)

    assert not result.ok
    assert not result.account_created
    assert any("adduser" in problem for problem in result.problems)


def test_no_create_does_not_invent_an_account(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """`--no-create` repairs an install; it must never add a user as a side effect."""
    monkeypatch.setattr(prov, "_pwd", FakePwd({}))
    monkeypatch.setattr(
        prov.shutil, "which", lambda _n: pytest.fail("adduser was reached with --no-create")
    )

    result = prov.provision("ayse", config=config, create=False)

    assert not result.ok
    assert "does not exist" in " ".join(result.problems)


def test_the_new_account_gets_no_groups_and_no_password() -> None:
    """The child must not land in `biokids`: that group can grant time (D-123).

    Asserted on the argv rather than on the result, because the mistake this
    guards against is a helpful flag added later - `--ingroup`, `--add-extra-groups`
    - which would still make every other test here pass.
    """
    seen: list[list[str]] = []

    def record(argv: list[str]) -> tuple[bool, str]:
        seen.append(list(argv))
        return True, ""

    original = prov._run
    try:
        prov._run = record  # type: ignore[assignment]
        prov.create_account("ayse", "Ayşe")
    finally:
        prov._run = original  # type: ignore[assignment]

    argv = seen[0]
    assert argv[0] == "adduser"
    assert "--disabled-password" in argv
    assert not any(flag.startswith("--ingroup") for flag in argv)
    assert "biokids" not in argv
    assert "sudo" not in argv


# ------------------------------------------------------------- the session files


def test_the_session_configuration_reaches_the_childs_home(
    with_user: Any, share: Path, home: Path, config: Config
) -> None:
    written, problems = prov.install_session_files("ayse", config)

    assert not problems
    session_xml = home / prov.XFCE_CHANNEL_DIR / "xfce4-session.xml"
    autostart = home / prov.AUTOSTART_DIR / "biokids-shell.desktop"
    assert session_xml.is_file(), "the child would get an ordinary XFCE desktop"
    assert autostart.is_file(), "nothing would ever start the launcher"
    assert str(session_xml) in written


def test_running_it_again_changes_nothing(
    with_user: Any, share: Path, home: Path, config: Config
) -> None:
    prov.install_session_files("ayse", config)
    (home / prov.XFCE_CHANNEL_DIR / "xfwm4.xml").write_text("<edited/>", encoding="utf-8")

    written, problems = prov.install_session_files("ayse", config)

    assert written == [], "a second run rewrote files it should have left alone"
    assert not problems
    edited = (home / prov.XFCE_CHANNEL_DIR / "xfwm4.xml").read_text(encoding="utf-8")
    assert edited == "<edited/>", "a parent's edit was overwritten"


def test_overwrite_is_available_for_repairing_an_install(
    with_user: Any, share: Path, home: Path, config: Config
) -> None:
    prov.install_session_files("ayse", config)
    broken = home / prov.XFCE_CHANNEL_DIR / "xfwm4.xml"
    broken.write_text("<broken", encoding="utf-8")

    written, problems = prov.install_session_files("ayse", config, overwrite=True)

    assert not problems
    assert str(broken) in written
    assert broken.read_text(encoding="utf-8") == "<channel/>"


def test_a_machine_without_biokids_shell_says_which_piece_is_missing(
    with_user: Any, config: Config
) -> None:
    """No /usr/share/biokids/xfce. The message has to point at the package."""
    written, problems = prov.install_session_files("ayse", config)

    assert written == []
    assert len(problems) == 1
    assert "biokids-shell" in problems[0]


def test_files_are_owned_by_the_child_not_by_root(
    with_user: Any, share: Path, home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The panel runs as root, so every file it writes starts out root-owned.

    A root-owned `.config/autostart` is a child who cannot start their own
    session, which is indistinguishable from BioKids refusing to let them in.
    """
    owned: list[tuple[str, int, int]] = []

    def record(path: object, uid: int, gid: int) -> None:
        owned.append((str(path), uid, gid))

    monkeypatch.setattr(prov.os, "chown", record, raising=False)

    prov.install_session_files("ayse", config)

    assert owned, "nothing was chowned; the files stay root-owned"
    assert all(uid == 4001 and gid == 4001 for _p, uid, gid in owned)
    targets = {path for path, _u, _g in owned}
    assert any(path.endswith("biokids-shell.desktop") for path in targets)
    assert any("autostart" in path for path in targets), "the directory needs it too"


def test_a_home_directory_that_cannot_be_written_is_reported(
    with_user: Any, share: Path, home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise PermissionError("read-only file system")

    monkeypatch.setattr(prov.shutil, "copyfile", refuse)

    written, problems = prov.install_session_files("ayse", config)

    assert written == []
    assert len(problems) == 3, "each file reports for itself rather than stopping at the first"
    assert all("read-only" in problem for problem in problems)


# ------------------------------------------------------------------ the whole thing


def test_provision_of_an_existing_user_leaves_the_account_alone(
    with_user: Any, share: Path, home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prov.shutil, "which", lambda _n: pytest.fail("adduser ran for a user that exists")
    )

    result = prov.provision("ayse", "Ayşe", config)

    assert result.ok
    assert result.account_existed
    assert not result.account_created
    assert len(result.files_written) == 3


def test_the_audit_detail_carries_what_went_wrong(with_user: Any, config: Config) -> None:
    """The panel writes this into the log; it is how a parent learns to repair it."""
    result = prov.provision("ayse", "Ayşe", config)

    detail = result.as_detail()
    assert detail["unix_user"] == "ayse"
    assert detail["account_created"] is False
    problems = detail["problems"]
    assert isinstance(problems, list) and problems, "a silent failure is the bug itself"


def test_every_directory_on_the_way_down_belongs_to_the_child(
    with_user: Any, share: Path, home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Not just the leaf. `mkdir(parents=True)` runs as root and creates all of it.

    Writing `~/.config/autostart/biokids-shell.desktop` used to chown the file
    and its immediate parent and stop there, leaving `~/.config` itself owned by
    root. A child who cannot write `~/.config` cannot start a session at all,
    which from where they are sitting is indistinguishable from BioKids refusing
    to let them in - and every other test here passed while that was true.
    """
    owned: list[str] = []

    def record(path: object, uid: int, gid: int) -> None:
        owned.append(str(path))

    monkeypatch.setattr(prov.os, "chown", record, raising=False)

    prov.install_session_files("ayse", config)

    for directory in (
        home / ".config",
        home / ".config" / "autostart",
        home / ".config" / "xfce4",
        home / ".config" / "xfce4" / "xfconf",
        home / ".config" / "xfce4" / "xfconf" / "xfce-perchannel-xml",
    ):
        assert str(directory) in owned, f"{directory} was left owned by root"


def test_nothing_above_the_home_directory_is_touched(
    with_user: Any, share: Path, home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Walking up to chown must stop at the home directory, not at the root of the disk."""
    owned: list[str] = []
    monkeypatch.setattr(prov.os, "chown", lambda p, _u, _g: owned.append(str(p)), raising=False)

    prov.install_session_files("ayse", config)

    for path in owned:
        assert Path(path) == home or home in Path(path).parents, (
            f"chowned {path}, which is outside the child's home"
        )


# --------------------------------------------- the sandbox the panel really runs in


def test_the_panel_unit_cannot_write_etc_or_home() -> None:
    """The reason provisioning had to move out of the panel process.

    `ProtectSystem=strict` makes the hierarchy read-only except ReadWritePaths,
    and `ProtectHome=yes` mounts an empty tmpfs over /home. So `adduser` fails
    with EROFS and the child's dotfiles cannot be written — on a real .deb
    install the parent finished the wizard and there was no account at all.

    Asserted on the unit file, because that is where the constraint lives and
    because nothing else in the suite reads it.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    unit = (root / "debian/biokids-panel.service").read_text(encoding="utf-8")
    rw = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]

    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=yes" in unit
    assert rw, "the unit stopped declaring ReadWritePaths"
    assert "/etc " not in rw[0] and not rw[0].rstrip().endswith("/etc"), (
        "the panel was given /etc; the whole point of the provision unit was not to"
    )
    assert "/home" not in rw[0], "the panel was given /home"


def test_the_provision_unit_runs_one_fixed_command() -> None:
    """Its safety is that it does one thing, not that it is sandboxed.

    It cannot be sandboxed: writing /etc/passwd and /home/<child> is the job. So
    what bounds it is the command line — one argument, which
    `provision_via_systemd` has already checked against UNIX_USER_RE.
    """
    root = pathlib.Path(__file__).resolve().parent.parent
    unit = (root / "debian/biokids-panel.biokids-provision@.service").read_text(
        encoding="utf-8"
    )

    execs = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    # %I, the unescaped instance: with %i, `elif-nur` arrived as `elif\x2dnur`.
    assert execs == ["ExecStart=/usr/bin/biokids-admin provision-child %I"], execs
    assert "Type=oneshot" in unit, "the panel waits for it; it has to be oneshot"
    assert "NoNewPrivileges=yes" in unit


@pytest.mark.parametrize(
    "name",
    ["../../etc/passwd", "root; rm -rf /", "elif ali", "", "Elif", "a" * 40],
)
def test_a_name_that_is_not_a_user_never_reaches_systemd(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The one argument is interpolated into a unit name, so it is checked first."""
    monkeypatch.setattr(
        prov, "_find", lambda _p: pytest.fail("systemctl was reached with a bad name")
    )
    result = prov.provision_via_systemd(name)
    assert result is not None and not result.ok
    assert "not a valid user name" in " ".join(result.problems)


def test_without_systemd_the_caller_is_told_to_do_it_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None, not a failure: a developer running the panel by hand still gets a child."""
    monkeypatch.setattr(prov, "_find", lambda _p: None)
    assert prov.provision_via_systemd("elif") is None


def test_a_unit_that_reports_success_without_creating_the_account_is_not_believed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`systemctl start --wait` exiting 0 is not the same as the child existing."""

    class Finished:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(prov, "_find", lambda _p: "/usr/bin/systemctl")
    monkeypatch.setattr(prov.subprocess, "run", lambda *_a, **_k: Finished())
    monkeypatch.setattr(prov, "_pwd", FakePwd({}))

    result = prov.provision_via_systemd("elif")

    assert result is not None and not result.ok
    assert "still does not exist" in " ".join(result.problems)


def test_a_hyphenated_user_becomes_a_valid_unit_name() -> None:
    """systemd reads `-` in an instance name as a path separator."""
    assert prov.systemd_escape("ali-veli") == r"ali\x2dveli"
    assert prov.systemd_escape("elif") == "elif"


# ------------------------------------------- logging in by picture (D-99, D-155)


def fake_commands(
    monkeypatch: pytest.MonkeyPatch,
    *,
    groups: str | None,
    status: str | None,
    adduser_ok: bool = True,
) -> list[list[str]]:
    """`id -nG` and `passwd --status` answer from here; `adduser` is recorded."""
    ran: list[list[str]] = []

    def output(argv: list[str]) -> str | None:
        if argv[0] == "id":
            return groups
        if argv[0] == "passwd":
            return status
        return None

    def run(argv: list[str]) -> tuple[bool, str]:
        ran.append(list(argv))
        return adduser_ok, "" if adduser_ok else "adduser: group not found"

    monkeypatch.setattr(prov, "_output", output)
    monkeypatch.setattr(prov, "_run", run)
    return ran


def test_a_child_account_with_no_password_joins_the_login_group(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = "elif L 09/30/2026 0 99999 7 -1"
    ran = fake_commands(monkeypatch, groups="elif users", status=locked)

    assert prov.grant_passwordless_login("elif") == (True, "")
    assert ran == [["adduser", "--quiet", "elif", prov.CHILD_GROUP]]


@pytest.mark.parametrize(
    "groups", ["anne sudo", "anne adm", "anne biokids", "anne sudo biokids-child"]
)
def test_an_account_that_can_administer_the_machine_never_joins(
    monkeypatch: pytest.MonkeyPatch, groups: str
) -> None:
    """Not even one already in the group: the login helper refuses those too."""
    ran = fake_commands(monkeypatch, groups=groups, status="anne L 09/30/2026 0 99999 7 -1")

    granted, reason = prov.grant_passwordless_login("anne")

    assert not granted
    assert "is in" in reason
    assert ran == []


def test_an_account_with_its_own_password_keeps_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """A parent who types their own user name as the child's is not made passwordless."""
    usable = "anne P 01/01/2026 0 99999 7 -1"
    ran = fake_commands(monkeypatch, groups="anne users", status=usable)

    granted, reason = prov.grant_passwordless_login("anne")

    assert not granted
    assert "password of its own" in reason
    assert ran == []


@pytest.mark.parametrize(
    ("groups", "status"), [(None, "elif L"), ("elif users", None), ("elif users", "")]
)
def test_what_cannot_be_read_counts_as_no(
    monkeypatch: pytest.MonkeyPatch, groups: str | None, status: str | None
) -> None:
    ran = fake_commands(monkeypatch, groups=groups, status=status)

    granted, _reason = prov.grant_passwordless_login("elif")

    assert not granted
    assert ran == []


def test_a_new_child_who_cannot_log_in_by_picture_is_a_problem(
    monkeypatch: pytest.MonkeyPatch, config: Config
) -> None:
    """An account BioKids made has no password; without the group it has no way in."""
    monkeypatch.setattr(prov, "_pwd", FakePwd({}))
    monkeypatch.setattr(prov, "create_account", lambda _u, _n="": (True, ""))
    monkeypatch.setattr(prov, "grant_passwordless_login", lambda _u: (False, "no group"))

    result = prov.provision("elif", "Elif", config)

    assert not result.ok
    assert not result.passwordless_login
    assert any("by their picture" in problem for problem in result.problems)


def test_an_existing_account_that_keeps_its_password_is_not_a_problem(
    with_user: Any, share: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        prov, "grant_passwordless_login", lambda _u: (False, "ayse has a password of its own")
    )

    result = prov.provision("ayse", "Ayşe", config)

    assert result.ok
    assert not result.passwordless_login
    assert result.as_detail()["passwordless_login"] is False


def test_an_administrative_account_is_never_made_a_childs(
    with_user: Any, share: Path, home: Path, config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The session files would have turned the parent's own desktop into the kiosk."""
    monkeypatch.setattr(prov, "account_is_administrative", lambda _user: True)

    result = prov.provision("ayse", "Ayşe", config)

    assert not result.ok
    assert "administer" in " ".join(result.problems)
    assert result.files_written == []
    assert not (home / prov.AUTOSTART_DIR / prov.AUTOSTART_FILE).exists()
