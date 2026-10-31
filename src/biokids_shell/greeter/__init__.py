"""The login screen: the family's pictures, tapped (D-251).

LightDM shows this instead of its GTK greeter. A child cannot read a list of
names; a four-year-old can find their own picture. So every child is a big
picture, first, and the grown-ups come after them with the house (D-221). A
child's tap logs them in - the PAM rule in /etc/pam.d/biokids-lightdm lets a
child with no session open in without a password (D-99, D-155) - and a
grown-up's tap asks for their password. "Başka bir hesap" asks for a name too,
for an account the list does not show.

This module is what decides; `__main__` draws it with GTK and speaks to LightDM.
Nothing here or there changes who may log in: that is PAM's, as before.

Never a locked-out parent (principle 4): /usr/lib/biokids/biokids-greeter runs
Debian's own greeter whenever this one stops with an error.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from typing import Final, Protocol

CHILD_GROUP: Final = "biokids-child"
CANNOT_START: Final = 75
"""EX_TEMPFAIL: this greeter could not start, before it spoke to LightDM."""


class UserLike(Protocol):
    """What LightDM.User gives; a test can stand in for it."""

    def get_name(self) -> str: ...

    def get_display_name(self) -> str | None: ...

    def get_image(self) -> str | None: ...

    def get_logged_in(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class Face:
    """One person on the login screen."""

    user: str
    name: str
    picture: str
    child: bool
    logged_in: bool


def child_accounts() -> set[str]:
    """The children's accounts: the members of `biokids-child` (D-99)."""
    import grp  # POSIX only; the tests run on other systems too

    try:
        return set(grp.getgrnam(CHILD_GROUP).gr_mem)  # type: ignore[attr-defined, unused-ignore]
    except KeyError:
        return set()


def faces(users: Iterable[UserLike], children: set[str]) -> list[Face]:
    """Everyone LightDM lists, children first, each group in name order."""
    found = []
    for user in users:
        name = user.get_name()
        if not name:
            continue
        found.append(
            Face(
                user=name,
                name=(user.get_display_name() or name).strip() or name,
                picture=user.get_image() or "",
                child=name in children,
                logged_in=bool(user.get_logged_in()),
            )
        )
    return sorted(found, key=lambda face: (not face.child, face.name.casefold()))


class Answer(StrEnum):
    """What the screen does when PAM asks something of the person who tapped."""

    ASK = "ask"
    """Show the question: a grown-up's password, or a name for "another account"."""
    CHILD_KEPT_OUT = "child_kept_out"
    """A child was asked for a password: their session is open behind the
    screen, and only a grown-up may let them back into it (D-155)."""


def on_question(face: Face | None) -> Answer:
    return Answer.CHILD_KEPT_OUT if face is not None and face.child else Answer.ASK
