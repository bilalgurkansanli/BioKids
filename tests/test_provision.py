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
    monkeypatch.setattr(prov.shutil, "which", lambda _name: None)

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
