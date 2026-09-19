"""The secrets, and the gate in front of them.

`docs/SECURITY.md` §3 says what is stored and how. These tests say the same
thing in a form that fails when it stops being true — which matters here more
than most places, because nothing about a hash being wrong is visible until
someone needs it to be right.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from biokids_core import repo, security
from biokids_core.config import Config
from biokids_core.db import Database, Parent
from biokids_core.db.models import utcnow
from biokids_panel import auth

PIN = "284913"


# ------------------------------------------------------------------ hashing


def test_a_hash_is_argon2id_with_the_parameters_the_docs_promise() -> None:
    """m=64MiB, t=3, p=4 (SECURITY.md §3). A weaker hash is a silent downgrade."""
    encoded = security.hash_secret(PIN)
    assert encoded.startswith("$argon2id$")
    assert "m=65536" in encoded
    assert "t=3" in encoded
    assert "p=4" in encoded


def test_the_same_secret_hashes_differently_every_time() -> None:
    assert security.hash_secret(PIN) != security.hash_secret(PIN)


def test_verification_accepts_the_secret_and_nothing_else() -> None:
    stored = security.hash_secret(PIN)
    assert security.verify_secret(stored, PIN)
    assert not security.verify_secret(stored, "284914")
    assert not security.verify_secret(stored, "")
    assert not security.verify_secret(stored, PIN + " ")


@pytest.mark.parametrize("stored", ["", "not-a-hash", "$argon2id$garbage"])
def test_a_broken_stored_hash_fails_rather_than_raising(stored: str) -> None:
    """A corrupt row must lock the parent out, not crash the panel on every request."""
    assert security.verify_secret(stored, PIN) is False


# ---------------------------------------------------------------------- PIN


@pytest.mark.parametrize("pin", ["111111", "123456", "000000", "121212", "654321"])
def test_a_guessable_pin_is_refused(pin: str) -> None:
    """The whole threat model is a child who watched the parent type it."""
    with pytest.raises(security.WeakPinError):
        security.validate_pin(pin)


@pytest.mark.parametrize("pin", ["12345", "1234567", "abcdef", "12 34 56", "", "12345a"])
def test_a_malformed_pin_is_refused(pin: str) -> None:
    with pytest.raises(ValueError):
        security.validate_pin(pin)


def test_a_reasonable_pin_is_accepted_and_trimmed() -> None:
    assert security.validate_pin("  284913  ") == "284913"


def test_a_pin_error_is_an_i18n_key_not_a_sentence() -> None:
    """It is rendered to a parent, so it has to exist in both catalogues."""
    from biokids_core.i18n import load_catalog

    for pin in ("12345", "111111"):
        try:
            security.validate_pin(pin)
        except ValueError as exc:
            key = str(exc)
            assert " " not in key
            assert load_catalog("tr").has(key), key
            assert load_catalog("en").has(key), key


# ----------------------------------------------------------- recovery codes


def test_a_recovery_code_is_eight_words_from_the_list() -> None:
    words = security.generate_recovery_code().split()
    assert len(words) == 8
    assert all(word in security.WORDLIST for word in words)


def test_the_wordlist_is_what_the_entropy_claim_depends_on() -> None:
    """256 unique words × 8 draws is 64 bits. Both halves have to hold."""
    assert len(security.WORDLIST) == 256
    assert len(set(security.WORDLIST)) == 256
    assert all(word.isascii() and word.islower() for word in security.WORDLIST)


def test_two_codes_are_not_the_same() -> None:
    codes = {security.generate_recovery_code() for _ in range(50)}
    assert len(codes) == 50


@pytest.mark.parametrize(
    "typed",
    [
        "bulut deniz elma fener gemi halka inci kale",
        "BULUT DENIZ ELMA FENER GEMI HALKA INCI KALE",
        "  bulut   deniz  elma fener gemi halka inci kale  ",
        "bulut, deniz, elma, fener, gemi, halka, inci, kale",
        "bulut deniz elma fener gemi halka İNCİ kale",
    ],
)
def test_a_recovery_code_is_forgiving_of_how_it_is_typed(typed: str) -> None:
    """A parent typing this has already had a bad evening (D-50)."""
    canonical = "bulut deniz elma fener gemi halka inci kale"
    stored = security.hash_secret(security.normalise_recovery_code(canonical))
    assert security.verify_recovery_code(stored, typed)


def test_a_recovery_code_is_not_forgiving_of_being_wrong() -> None:
    stored = security.hash_secret(
        security.normalise_recovery_code("bulut deniz elma fener gemi halka inci kale")
    )
    assert not security.verify_recovery_code(
        stored, "bulut deniz elma fener gemi halka inci kaya"
    )
    assert not security.verify_recovery_code(stored, "bulut deniz elma fener")


def test_turkish_letters_fold_the_way_a_parent_would_type_them() -> None:
    assert security.normalise_recovery_code("Çiçek Ağaç Şişe Ilık") == "cicek agac sise ilik"


# --------------------------------------------------------------- at rest


def test_a_secret_round_trips_through_the_key_file(tmp_path: Path) -> None:
    key = security.load_or_create_key(tmp_path / "secret.key")
    ciphertext = security.encrypt_secret("hunter2", key)
    assert "hunter2" not in ciphertext
    assert security.decrypt_secret(ciphertext, key) == "hunter2"


def test_the_key_is_stable_across_calls(tmp_path: Path) -> None:
    """Regenerating it would make every stored secret undecryptable."""
    path = tmp_path / "secret.key"
    assert security.load_or_create_key(path) == security.load_or_create_key(path)


def test_a_rotated_key_degrades_to_none_rather_than_raising(tmp_path: Path) -> None:
    """The right outcome is "re-enter your SMTP password", not a panel that will not start."""
    first = security.load_or_create_key(tmp_path / "a.key")
    second = security.load_or_create_key(tmp_path / "b.key")
    assert security.decrypt_secret(security.encrypt_secret("x", first), second) is None
    assert security.decrypt_secret("not even ciphertext", first) is None


@pytest.mark.skipif(not hasattr(Path, "chmod"), reason="POSIX modes only")
def test_the_key_file_is_never_world_readable(tmp_path: Path) -> None:
    import os
    import stat

    if os.name != "posix":
        pytest.skip("file modes are a POSIX thing")
    path = tmp_path / "secret.key"
    security.load_or_create_key(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_redaction_says_something_without_saying_the_thing() -> None:
    assert security.redact("hunter2") == security.REDACTED
    assert "hunter" not in security.redact("hunter2")
    assert security.redact(None) == ""


def test_a_token_is_long_and_never_repeats() -> None:
    tokens = {security.generate_token() for _ in range(100)}
    assert len(tokens) == 100
    assert all(len(token) >= 40 for token in tokens)


def test_a_fingerprint_identifies_a_token_without_storing_it() -> None:
    token = security.generate_token()
    fingerprint = security.token_fingerprint(token)
    assert token not in fingerprint
    assert len(fingerprint) == 64
    assert fingerprint == security.token_fingerprint(token)


# ------------------------------------------------------------- the lockout


@pytest.fixture
def parent_row(session: Session, family: object) -> Parent:
    row = Parent(
        display_name="Anne",
        pin_hash=security.hash_secret(PIN),
        passphrase_hash=security.hash_secret("acik kapi mavi deniz"),
    )
    session.add(row)
    session.commit()
    return row


def test_the_lockout_doubles_and_is_capped(
    session: Session, config: Config, parent_row: Parent
) -> None:
    """15, 30, 60 … up to a day. The cap is what stops it becoming permanent."""
    seen: list[int] = []
    for _ in range(8):
        for _attempt in range(config.panel.pin_attempts):
            auth.attempt_login(session, config, "000001")
        result = auth.attempt_login(session, config, "000001")
        seen.append(result.lockout_minutes)
        # Serve the lockout so the next round can start.
        parent_row.pin_locked_until = utcnow() - dt.timedelta(seconds=1)

    assert seen[:4] == [15, 30, 60, 120]
    assert max(seen) <= config.panel.pin_lockout_max_minutes


def test_the_right_pin_during_a_lockout_is_still_refused(
    session: Session, config: Config, parent_row: Parent
) -> None:
    """Otherwise the lockout only inconveniences someone who already knows it."""
    for _ in range(config.panel.pin_attempts):
        auth.attempt_login(session, config, "000001")
    result = auth.attempt_login(session, config, PIN)
    assert result.ok is False
    assert result.error_key == "login.locked"


def test_a_successful_login_clears_the_counter(
    session: Session, config: Config, parent_row: Parent
) -> None:
    auth.attempt_login(session, config, "000001")
    auth.attempt_login(session, config, "000001")
    assert parent_row.pin_failures == 2

    assert auth.attempt_login(session, config, PIN).ok
    assert parent_row.pin_failures == 0
    assert parent_row.pin_locked_until is None
    assert parent_row.pin_lockout_level == 0


def test_every_attempt_is_audited(session: Session, config: Config, parent_row: Parent) -> None:
    auth.attempt_login(session, config, "000001")
    auth.attempt_login(session, config, PIN)
    actions = [entry.action for entry in repo.recent_audit(session, limit=10)]
    assert "login_failed" in actions
    assert "login_ok" in actions


def test_a_session_token_is_not_stored_in_the_database(
    session: Session, config: Config, parent_row: Parent
) -> None:
    """Only its fingerprint. A database someone reads is not a set of live sessions."""
    from biokids_core.db import PanelSession

    result = auth.attempt_login(session, config, PIN)
    session.commit()
    rows = session.query(PanelSession).all()
    assert len(rows) == 1
    assert rows[0].token_hash != result.token
    assert rows[0].token_hash == security.token_fingerprint(result.token)


def test_the_passphrase_gate_opens_for_a_while_and_then_closes(
    session: Session, config: Config, parent_row: Parent
) -> None:
    result = auth.attempt_login(session, config, PIN)
    state = auth.resolve(session, config, result.token)
    assert state.passphrase_fresh() is False

    assert auth.confirm_passphrase(session, state, "acik kapi mavi deniz")
    assert state.passphrase_fresh()

    later = utcnow() + dt.timedelta(minutes=auth.PASSPHRASE_GRACE_MINUTES + 1)
    assert state.passphrase_fresh(later) is False


def test_a_wrong_passphrase_opens_nothing_and_is_audited(
    session: Session, config: Config, parent_row: Parent
) -> None:
    result = auth.attempt_login(session, config, PIN)
    state = auth.resolve(session, config, result.token)

    assert auth.confirm_passphrase(session, state, "not it") is False
    assert state.passphrase_fresh() is False
    assert "passphrase_failed" in [e.action for e in repo.recent_audit(session, limit=5)]


def test_recovering_the_pin_signs_every_browser_out(
    session: Session, config: Config, parent_row: Parent
) -> None:
    """Whoever used the recovery code should not inherit a live session."""
    from biokids_core.db import PanelSession

    parent_row.recovery_hash = security.hash_secret(
        security.normalise_recovery_code("bulut deniz elma fener gemi halka inci kale")
    )
    auth.attempt_login(session, config, PIN)
    auth.attempt_login(session, config, PIN)
    session.commit()
    assert session.query(PanelSession).count() == 2

    assert auth.attempt_recovery(
        session, "bulut deniz elma fener gemi halka inci kale", "551234"
    ).ok
    session.commit()
    assert session.query(PanelSession).count() == 0


def test_guessing_in_parallel_does_not_buy_extra_attempts(
    config: Config, db: Database, family: object
) -> None:
    """Twenty simultaneous wrong PINs must cost twenty failures, not one.

    `parent.pin_failures += 1` is a read-modify-write: every concurrent request
    reads the same starting value and writes back the same absolute number. The
    route is a sync `def`, so FastAPI runs it in the threadpool, and argon2id at
    64 MiB holds each request open for hundreds of milliseconds — a wide window.
    Measured before the fix: twenty concurrent guesses left `pin_failures = 1`
    and no lockout, which turns "5 attempts then 15 minutes" into a few hundred.

    Each thread gets its own session, because that is what a request does.
    """
    import threading

    with db.session() as setup:
        setup.add(Parent(display_name="Anne", pin_hash=security.hash_secret(PIN)))

    attempts = 20
    results: list[str] = []
    guard = threading.Lock()
    ready = threading.Barrier(attempts)

    def guess(n: int) -> None:
        ready.wait(timeout=30)
        with db.session() as own:
            outcome = auth.attempt_login(own, config, f"{n:06d}")
        with guard:
            results.append(outcome.error_key)

    threads = [threading.Thread(target=guess, args=(n,)) for n in range(attempts)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)

    assert len(results) == attempts, "every attempt returned"
    assert "login.locked" in results, "the door closed at some point"

    with db.session() as check:
        parent = repo.get_parent(check)
        assert parent is not None
        assert parent.pin_locked_until is not None
        # The counter resets when a lockout is served, so the surviving evidence
        # is the lockout itself plus one audit row per attempt.
        locked = [
            row
            for row in repo.recent_audit(check, limit=100)
            if row.action in {"login_failed", "login_locked"}
        ]
    assert len(locked) == attempts, "each guess is counted once and audited once"


# ------------------------------------------------------------ the login redirect


@pytest.mark.parametrize(
    "target",
    [
        "//evil.example.com/",
        "/" + chr(92) + "evil.example.com/",
        "https://evil.example.com/",
        "//evil.example.com/login?next=/today",
        "http:/evil.example.com",
        "/today\r\nSet-Cookie: x=1",
        "",
        "today",
    ],
)
def test_the_login_redirect_does_not_leave_the_panel(target: str) -> None:
    """`startswith("/")` is not enough, and this is how that check fails.

    `//evil.example.com/` starts with a slash, and the browser reads a
    protocol-relative URL and leaves the site. What lands is a page with
    "BioKids" in the history and a login form that is not ours, for a product
    whose entire job is guarding one PIN.
    """
    assert auth.safe_next(target) == "/today"


@pytest.mark.parametrize(
    "target",
    ["/today", "/children/1/time", "/audit?action=login_ok", "/settings"],
)
def test_the_login_redirect_still_returns_a_parent_to_where_they_were(target: str) -> None:
    assert auth.safe_next(target) == target
