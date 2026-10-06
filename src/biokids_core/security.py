"""Hashing, recovery codes, tokens and secret-at-rest encryption.

Everything in `SECURITY.md` §3 that is not a policy question lives here.

The PIN is what a parent will actually use on a phone, so it is short; argon2id
with the parameters below plus the lockout in `auth.py` is what makes a 6-digit
secret defensible. The passphrase gate for destructive actions exists because a
PIN shoulder-surfed by a ten-year-old should not be able to turn the product off.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import stat
import unicodedata
from pathlib import Path
from typing import Final

from argon2 import PasswordHasher

# `InvalidHash`, not `InvalidHashError`: the newer name arrived in argon2-cffi
# 21.2, and Debian 13 ships 21.1. The old name is still an alias in every later
# release, so this one import works on both (D-154).
from argon2.exceptions import InvalidHash, VerificationError, VerifyMismatchError
from cryptography.fernet import Fernet, InvalidToken

# SECURITY.md §3: m=64MiB, t=3, p=4.
_HASHER: Final = PasswordHasher(
    time_cost=3,
    memory_cost=64 * 1024,
    parallelism=4,
    hash_len=32,
    salt_len=16,
)

PIN_LENGTH: Final = 6
PASSPHRASE_MIN_LENGTH: Final = 10
RECOVERY_WORDS: Final = 8

# 256 short, unambiguous Turkish nouns. Eight words is 64 bits of entropy, which
# is far beyond what a rate-limited local gate needs. Words are chosen to be easy
# to write on paper and read back: no homophones, no words that differ only by a
# Turkish diacritic, nothing longer than seven letters.
WORDLIST: Final = [
    "adem",
    "almaz",
    "altin",
    "ampul",
    "anahtar",
    "aslan",
    "armut",
    "asma",
    "ates",
    "ayak",
    "ayna",
    "ayva",
    "badem",
    "bahce",
    "bakir",
    "balik",
    "balkon",
    "bardak",
    "barut",
    "basak",
    "batik",
    "bayrak",
    "bebek",
    "beyaz",
    "bicak",
    "bilek",
    "bina",
    "bisiklet",
    "bogaz",
    "boncuk",
    "boya",
    "bozuk",
    "bulut",
    "burun",
    "buz",
    "cadir",
    "camur",
    "canta",
    "cesme",
    "ceviz",
    "cicek",
    "cilek",
    "cinar",
    "cizgi",
    "coban",
    "corba",
    "damla",
    "dalga",
    "defne",
    "deniz",
    "ders",
    "devek",
    "dilim",
    "dokuz",
    "dolap",
    "domates",
    "dosya",
    "duman",
    "durak",
    "duvar",
    "duygu",
    "ekmek",
    "elma",
    "elmas",
    "erik",
    "eser",
    "eylul",
    "fener",
    "fidan",
    "fikir",
    "filiz",
    "firin",
    "fisik",
    "fistik",
    "gemi",
    "geyik",
    "gonca",
    "gozluk",
    "gumus",
    "gunes",
    "guvercin",
    "halat",
    "halka",
    "hamak",
    "harita",
    "hasat",
    "hava",
    "hediye",
    "hilal",
    "horoz",
    "ilkbahar",
    "inci",
    "ipek",
    "irmak",
    "kadife",
    "kagit",
    "kale",
    "kalem",
    "kanat",
    "kapak",
    "kaplan",
    "karpuz",
    "kase",
    "kavun",
    "kaya",
    "kayik",
    "kazan",
    "kelebek",
    "kemer",
    "kiraz",
    "kitap",
    "kivi",
    "kizak",
    "kohne",
    "komur",
    "konak",
    "korfez",
    "koyun",
    "kule",
    "kumas",
    "kunduz",
    "kurbaga",
    "kutu",
    "kuyu",
    "kuzey",
    "lale",
    "lamba",
    "leylek",
    "liman",
    "limon",
    "lokma",
    "makas",
    "manda",
    "mantar",
    "marul",
    "masal",
    "masa",
    "mavi",
    "mercan",
    "merdiven",
    "mese",
    "meyve",
    "midye",
    "mimar",
    "mine",
    "mola",
    "mor",
    "mudur",
    "mumya",
    "murver",
    "muz",
    "nakis",
    "nane",
    "nar",
    "nehir",
    "nilufer",
    "nohut",
    "nokta",
    "odun",
    "oglak",
    "okul",
    "olta",
    "orman",
    "ordek",
    "orgu",
    "otel",
    "oyun",
    "ozan",
    "palmiye",
    "pamuk",
    "papatya",
    "patika",
    "pelin",
    "pencere",
    "perde",
    "pergel",
    "peynir",
    "pirinc",
    "pinar",
    "pirlanta",
    "portakal",
    "poyraz",
    "pusula",
    "radyo",
    "rehber",
    "renk",
    "resim",
    "rota",
    "ruzgar",
    "saat",
    "sabun",
    "sahil",
    "salep",
    "sandal",
    "saz",
    "sedef",
    "selvi",
    "sepet",
    "serce",
    "sifir",
    "simit",
    "sincap",
    "sirke",
    "soba",
    "sofra",
    "somon",
    "sucuk",
    "sunger",
    "susam",
    "sutun",
    "taban",
    "takvim",
    "tarak",
    "tarla",
    "tas",
    "tavsan",
    "tepe",
    "terazi",
    "tilki",
    "tomurcuk",
    "toprak",
    "tuz",
    "ufuk",
    "uskumru",
    "uzum",
    "vadi",
    "vapur",
    "varil",
    "vazo",
    "vitrin",
    "volkan",
    "yagmur",
    "yaka",
    "yamac",
    "yaprak",
    "yastik",
    "yayla",
    "yelken",
    "yemis",
    "yildiz",
    "yonca",
    "yorgan",
    "yosun",
    "yumurta",
    "yunus",
    "zambak",
    "zeytin",
    "zil",
    "zincir",
    "zurna",
    "sarki",
    "bulgur",
    "cengel",
    "dalyan",
    "egrelti",
    "fasulye",
]

assert len(WORDLIST) == 256, f"wordlist must be 256 words, got {len(WORDLIST)}"
assert len(set(WORDLIST)) == 256, "wordlist has duplicates"
_WORDS = frozenset(WORDLIST)


# ------------------------------------------------------------------ hashing


def hash_secret(secret: str) -> str:
    """argon2id hash of any secret we store: PIN, passphrase or recovery code."""
    return _HASHER.hash(secret)


def verify_secret(stored_hash: str, candidate: str) -> bool:
    """Constant-time-ish verification. A malformed stored hash is a failure, not a crash."""
    if not stored_hash:
        return False
    try:
        return _HASHER.verify(stored_hash, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHash):
        return False


def needs_rehash(stored_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we use now."""
    if not stored_hash:
        return False
    try:
        return _HASHER.check_needs_rehash(stored_hash)
    except InvalidHash:
        return True


# ---------------------------------------------------------------------- PIN


class WeakPinError(ValueError):
    """The PIN is well-formed but trivially guessable."""


_TRIVIAL_PINS: Final = frozenset(
    {
        "000000",
        "111111",
        "222222",
        "333333",
        "444444",
        "555555",
        "666666",
        "777777",
        "888888",
        "999999",
        "123456",
        "654321",
        "012345",
        "543210",
        "121212",
        "123123",
        "112233",
        "102030",
    }
)


def validate_pin(pin: str) -> str:
    """Return the PIN if it is acceptable, otherwise raise.

    A PIN that is six identical digits or an obvious run is refused. This is not
    security theatre: the whole threat model is a child who watches the parent
    type, and `111111` is the first thing they will try.
    """
    cleaned = pin.strip()
    if not re.fullmatch(rf"\d{{{PIN_LENGTH}}}", cleaned):
        raise ValueError("wizard.parent.pin.format")
    if cleaned in _TRIVIAL_PINS:
        raise WeakPinError("wizard.parent.pin.weak")
    return cleaned


def validate_passphrase(passphrase: str) -> str:
    cleaned = passphrase.strip()
    if len(cleaned) < PASSPHRASE_MIN_LENGTH:
        raise ValueError("wizard.passphrase.short")
    return cleaned


# ----------------------------------------------------------- recovery codes


def generate_recovery_code(words: int = RECOVERY_WORDS) -> str:
    """Eight words drawn with a CSPRNG. 64 bits of entropy; shown exactly once."""
    return " ".join(secrets.choice(WORDLIST) for _ in range(words))


def normalise_recovery_code(code: str) -> str:
    """Make the comparison forgiving of how a tired parent types at 23:00.

    Case, extra whitespace, punctuation and Turkish diacritics are all
    normalised away. The words themselves are ASCII in the list, so a parent who
    types "çiçek" for "cicek" still gets in.
    """
    decomposed = unicodedata.normalize("NFKD", code.strip().lower())
    folded = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    folded = folded.replace("ı", "i").replace("ş", "s").replace("ğ", "g")
    tokens = re.findall(r"[a-z0-9]+", folded)
    return " ".join(tokens)


def is_recovery_code(code: str) -> bool:
    """Eight words, every one of them from the list: the shape of a real code.

    Checked before anything is hashed or compared. The wizard used to hash
    whatever its hidden field sent back, and the recovery page compared whatever
    was typed, so a form posted with an empty code stored the hash of "" - and
    an empty code then reset the PIN.
    """
    words = normalise_recovery_code(code).split()
    return len(words) == RECOVERY_WORDS and all(word in _WORDS for word in words)


def verify_recovery_code(stored_hash: str, candidate: str) -> bool:
    if not is_recovery_code(candidate):
        return False
    return verify_secret(stored_hash, normalise_recovery_code(candidate))


# ---------------------------------------------------------------- tokens


def generate_token(length: int = 32) -> str:
    """URL-safe random token. Used for panel sessions and the extension token."""
    return secrets.token_urlsafe(length)


def token_fingerprint(token: str) -> str:
    """SHA-256 of a token, for storing beside a session without storing the token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def constant_time_equals(left: str, right: str) -> bool:
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))


# ------------------------------------------------- secrets encrypted at rest


def load_or_create_key(path: Path) -> bytes:
    """Read `/etc/biokids/secret.key`, creating it 0600 if it is not there yet."""
    if path.is_file():
        data = path.read_bytes().strip()
        if data:
            return data
    path.parent.mkdir(parents=True, exist_ok=True)
    key = Fernet.generate_key()
    # Create with a tight mode from the start; never world-readable, not even briefly.
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, key)
    finally:
        os.close(fd)
    if os.name == "posix":
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    return key


def encrypt_secret(plaintext: str, key: bytes) -> str:
    return Fernet(key).encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(ciphertext: str, key: bytes) -> str | None:
    """None when the ciphertext does not belong to this key, rather than raising.

    A rotated or restored `secret.key` should degrade to "re-enter your SMTP
    password", not to a panel that will not start.
    """
    try:
        return Fernet(key).decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return None


REDACTED: Final = "***"


def redact(value: str | None) -> str:
    """What a secret looks like in an export or a log line (`SECURITY.md` §3)."""
    return REDACTED if value else ""
