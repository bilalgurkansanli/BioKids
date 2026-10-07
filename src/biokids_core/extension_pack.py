"""Pack the BioKids Chromium extension as a signed CRX3, on the machine itself.

Chromium force-installs the extension through `ExtensionInstallForcelist`, and
an extension that does not come from the Web Store has to arrive as a CRX
signed by the key its id is derived from. No private key may live in the
repository, so each machine makes its own at install time and packs the
extension with it: the id is per machine, and nothing outside the machine can
sign an update to it.

The output, all world-readable because the child's Chromium reads it:

    <out>/biokids.crx     the signed package
    <out>/updates.xml     the update manifest the policy points at
    <out>/extension-id    the 32-letter id, for the policy writer

The CRX3 layout follows Chromium's `components/crx_file/crx3.proto` and
`crx_creator.cc`. The protobuf is three length-delimited fields, so it is
written by hand rather than pulling in a protobuf runtime.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import tempfile
import zipfile
from pathlib import Path
from xml.sax.saxutils import escape

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

CRX3_MAGIC = b"Cr24"
CRX3_VERSION = 3
SIGNATURE_CONTEXT = b"CRX3 SignedData\x00"
"""Prefixed to what is signed, so a CRX signature cannot be replayed as anything else."""

KEY_BITS = 2048
KEY_EXPONENT = 65537

# Field numbers in crx3.proto.
_HEADER_SHA256_WITH_RSA = 2
_HEADER_SIGNED_HEADER_DATA = 10000
_PROOF_PUBLIC_KEY = 1
_PROOF_SIGNATURE = 2
_SIGNED_DATA_CRX_ID = 1
_WIRE_LENGTH_DELIMITED = 2

ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)
"""The earliest a zip entry can carry. Fixed, so the same source packs to the same bytes."""

EXCLUDED_NAMES = frozenset({"README.md"})
"""Files in the source directory that are documentation, not the extension."""

CRX_NAME = "biokids.crx"
UPDATES_NAME = "updates.xml"
ID_NAME = "extension-id"

DEFAULT_SOURCE = Path("/usr/share/biokids/extension")
DEFAULT_OUT = Path("/var/lib/biokids-ext")
DEFAULT_KEY = Path("/var/lib/biokids/extension-key.pem")

_ID_ALPHABET = str.maketrans("0123456789abcdef", "abcdefghijklmnop")
_EXTENSION_ID = re.compile(r"^[a-p]{32}$")
_VERSION = re.compile(r"^\d{1,9}(\.\d{1,9}){0,3}$")
"""Chromium's extension version: one to four dot-separated integers."""


# ------------------------------------------------------------------- the key


def load_or_create_key(path: Path) -> rsa.RSAPrivateKey:
    """The machine's signing key, made on first use.

    A new key is written through a file that is 0600 before any key bytes go
    into it, then renamed into place, so a crash leaves either no key or a
    whole one. Losing it changes the extension id; the next pack makes a new
    one and the policy must follow (`extension-id` says what it is).
    """
    path = Path(path)
    try:
        pem = path.read_bytes()
    except FileNotFoundError:
        return _create_key(path)
    key = serialization.load_pem_private_key(pem, password=None)
    if not isinstance(key, rsa.RSAPrivateKey):
        raise ValueError(f"{path} does not hold an RSA private key")
    return key


def _create_key(path: Path) -> rsa.RSAPrivateKey:
    key = rsa.generate_private_key(public_exponent=KEY_EXPONENT, key_size=KEY_BITS)
    pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    _write_atomically(path, pem, mode=0o600)
    return key


def public_key_der(private_key: rsa.RSAPrivateKey) -> bytes:
    """The DER SubjectPublicKeyInfo, which is what Chromium hashes into the id."""
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


# -------------------------------------------------------------------- the id


def crx_id(public_key_der: bytes) -> bytes:
    """The first 16 bytes of SHA-256 over the public key."""
    return hashlib.sha256(public_key_der).digest()[:16]


def extension_id(public_key_der: bytes) -> str:
    """The id Chromium shows: the crx_id in hex, digits 0-f written as a-p."""
    return crx_id(public_key_der).hex().translate(_ID_ALPHABET)


# ------------------------------------------------------------------- packing


def deterministic_zip(source_dir: Path) -> bytes:
    """Zip every file under `source_dir`, the same way every time.

    Entries are sorted by path, carry a fixed timestamp and fixed Unix
    permissions, and hidden files and `EXCLUDED_NAMES` are left out.
    """
    source_dir = Path(source_dir)
    files = sorted(
        (path.relative_to(source_dir).as_posix(), path)
        for path in source_dir.rglob("*")
        if path.is_file() and _packable(path.relative_to(source_dir))
    )
    if not files:
        raise ValueError(f"{source_dir} has nothing to pack")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, path in files:
            info = zipfile.ZipInfo(filename=name, date_time=ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # Unix, whatever host packs it
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes(), compresslevel=9)
    return buffer.getvalue()


def _packable(relative: Path) -> bool:
    if any(part.startswith(".") or part == "__pycache__" for part in relative.parts):
        return False
    return relative.name not in EXCLUDED_NAMES


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        low = value & 0x7F
        value >>= 7
        if value:
            out.append(low | 0x80)
        else:
            out.append(low)
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:
    """A length-delimited protobuf field: tag, length, bytes."""
    return _varint((number << 3) | _WIRE_LENGTH_DELIMITED) + _varint(len(payload)) + payload


def pack_crx3(zip_bytes: bytes, private_key: rsa.RSAPrivateKey) -> bytes:
    """A CRX3 file: magic, version, header length, `CrxFileHeader`, the zip.

    The signature is RSA PKCS#1 v1.5 with SHA-256 over
    `SIGNATURE_CONTEXT + uint32le(len(signed_header_data)) + signed_header_data
    + zip_bytes`, which is what Chromium's verifier recomputes.
    """
    der = public_key_der(private_key)
    signed_header_data = _field(_SIGNED_DATA_CRX_ID, crx_id(der))
    signature = private_key.sign(
        SIGNATURE_CONTEXT
        + struct.pack("<I", len(signed_header_data))
        + signed_header_data
        + zip_bytes,
        padding.PKCS1v15(),
        hashes.SHA256(),
    )
    proof = _field(_PROOF_PUBLIC_KEY, der) + _field(_PROOF_SIGNATURE, signature)
    header = _field(_HEADER_SHA256_WITH_RSA, proof) + _field(
        _HEADER_SIGNED_HEADER_DATA, signed_header_data
    )
    return CRX3_MAGIC + struct.pack("<II", CRX3_VERSION, len(header)) + header + zip_bytes


def updates_xml(ext_id: str, version: str, crx_url: str) -> str:
    """The update manifest (gupdate, protocol 2.0) that points Chromium at the CRX."""
    if not _EXTENSION_ID.fullmatch(ext_id):
        raise ValueError(f"not an extension id: {ext_id!r}")
    if not _VERSION.fullmatch(version):
        raise ValueError(f"not an extension version: {version!r}")
    return (
        "<?xml version='1.0' encoding='UTF-8'?>\n"
        "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>\n"
        f"  <app appid='{ext_id}'>\n"
        f"    <updatecheck codebase='{_attribute(crx_url)}' version='{version}'/>\n"
        "  </app>\n"
        "</gupdate>\n"
    )


def _attribute(value: str) -> str:
    return escape(value, {"'": "&apos;", '"': "&quot;"})


# ------------------------------------------------------------------- the bundle


def write_bundle(source_dir: Path, out_dir: Path, key_path: Path) -> tuple[str, str]:
    """Pack `source_dir` into `out_dir`; return (extension id, version).

    The CRX is written before the manifest that names it, so Chromium never
    reads a version whose package is not there yet. Every file is replaced
    atomically and is 0644; `out_dir` is created 0755.
    """
    source_dir, out_dir = Path(source_dir), Path(out_dir)
    version = read_version(source_dir)
    key = load_or_create_key(Path(key_path))
    crx = pack_crx3(deterministic_zip(source_dir), key)
    ext_id = extension_id(public_key_der(key))

    if not out_dir.is_dir():
        out_dir.mkdir(mode=0o755, parents=True, exist_ok=True)
        out_dir.chmod(0o755)  # whatever the umask took away; Chromium runs as the child
    crx_path = out_dir.absolute() / CRX_NAME
    _write_atomically(crx_path, crx, mode=0o644)
    _write_atomically(
        out_dir / UPDATES_NAME,
        updates_xml(ext_id, version, crx_path.as_uri()).encode("utf-8"),
        mode=0o644,
    )
    _write_atomically(out_dir / ID_NAME, f"{ext_id}\n".encode("ascii"), mode=0o644)
    return ext_id, version


def read_version(source_dir: Path) -> str:
    """The `version` in the extension's manifest.json."""
    manifest = json.loads((Path(source_dir) / "manifest.json").read_text(encoding="utf-8"))
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(version, str) or not _VERSION.fullmatch(version):
        raise ValueError(f"manifest.json in {source_dir} has no usable version: {version!r}")
    return version


def _write_atomically(path: Path, data: bytes, *, mode: int) -> None:
    """Write through a temporary file in the same directory, then rename.

    `mkstemp` creates the file 0600, so nothing is readable before it is
    complete; the final mode is set before the rename.
    """
    descriptor, name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        temporary.replace(path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
