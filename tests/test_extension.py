"""The browser extension, and the CRX3 the machine signs it into.

Chromium installs a force-listed extension only if the CRX verifies: the right
magic and version, a header it can parse, an RSA proof whose key hashes to the
declared id, and a signature over exactly the bytes its verifier recomputes.
Getting any of that wrong does not fail loudly - the browser just has no
extension, and the child no block page. So the file is taken apart here with a
parser written independently of the packer, the way Chromium would.

The extension itself is plain JavaScript that cannot run here; what can be
checked statically is the boundary it must stay inside (SECURITY.md section 7):
the launcher hears a host name and nothing else.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import struct
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa

from biokids_core import admin, extension_pack

EXTENSION_DIR = Path(__file__).resolve().parents[1] / "extension"
BACKGROUND_JS = EXTENSION_DIR / "background.js"
MANIFEST = EXTENSION_DIR / "manifest.json"

SIGNATURE_CONTEXT = b"CRX3 SignedData\x00"


# ---------------------------------------------------- an independent CRX3 reader


def read_varint(data: bytes, offset: int) -> tuple[int, int]:
    value = shift = 0
    while True:
        byte = data[offset]
        offset += 1
        value |= (byte & 0x7F) << shift
        if not byte & 0x80:
            return value, offset
        shift += 7


def read_fields(message: bytes) -> list[tuple[int, bytes]]:
    """(field number, payload) for every field; all of CRX3's are length-delimited."""
    fields: list[tuple[int, bytes]] = []
    offset = 0
    while offset < len(message):
        tag, offset = read_varint(message, offset)
        assert tag & 0x07 == 2, f"field {tag >> 3} is not length-delimited"
        length, offset = read_varint(message, offset)
        fields.append((tag >> 3, message[offset : offset + length]))
        offset += length
    assert offset == len(message), "a field ran past the end of its message"
    return fields


class ParsedCrx:
    def __init__(self, crx: bytes) -> None:
        assert crx[:4] == b"Cr24"
        self.version, self.header_length = struct.unpack_from("<II", crx, 4)
        self.header = crx[12 : 12 + self.header_length]
        self.archive = crx[12 + self.header_length :]
        self.proofs: list[tuple[bytes, bytes]] = []
        self.signed_header_data = b""
        for number, payload in read_fields(self.header):
            if number == 2:
                proof = dict(read_fields(payload))
                self.proofs.append((proof[1], proof[2]))
            elif number == 10000:
                self.signed_header_data = payload
        signed = dict(read_fields(self.signed_header_data))
        self.crx_id = signed[1]


@pytest.fixture
def bundle(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """The real extension directory, packed with a fresh key."""
    out = tmp_path / "out"
    key = tmp_path / "keys" / "extension-key.pem"
    ext_id, version = extension_pack.write_bundle(EXTENSION_DIR, out, key)
    return out, key, ext_id, version


def public_der(key_path: Path) -> bytes:
    key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    return key.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )


# ------------------------------------------------------------------ the format


def test_the_crx_has_the_crx3_preamble(bundle: tuple[Path, Path, str, str]) -> None:
    out, *_ = bundle
    crx = (out / "biokids.crx").read_bytes()
    parsed = ParsedCrx(crx)

    assert crx[:4] == b"Cr24"
    assert parsed.version == 3
    assert 12 + parsed.header_length < len(crx)
    assert zipfile.is_zipfile(io.BytesIO(parsed.archive))


def test_the_header_carries_one_rsa_proof_and_the_signed_data(
    bundle: tuple[Path, Path, str, str],
) -> None:
    out, key_path, *_ = bundle
    parsed = ParsedCrx((out / "biokids.crx").read_bytes())

    assert len(parsed.proofs) == 1
    public_key, signature = parsed.proofs[0]
    assert public_key == public_der(key_path)
    assert len(signature) == 256  # 2048-bit RSA
    assert read_fields(parsed.signed_header_data) == [(1, parsed.crx_id)]


def test_the_signature_verifies_over_exactly_what_chromium_recomputes(
    bundle: tuple[Path, Path, str, str],
) -> None:
    out, *_ = bundle
    parsed = ParsedCrx((out / "biokids.crx").read_bytes())
    public_key_der, signature = parsed.proofs[0]
    public_key = serialization.load_der_public_key(public_key_der)
    assert isinstance(public_key, rsa.RSAPublicKey)

    signed = (
        SIGNATURE_CONTEXT
        + struct.pack("<I", len(parsed.signed_header_data))
        + parsed.signed_header_data
        + parsed.archive
    )
    public_key.verify(signature, signed, padding.PKCS1v15(), hashes.SHA256())

    # And a single changed byte of the archive does not.
    tampered = signed[:-1] + bytes([signed[-1] ^ 0x01])
    with pytest.raises(InvalidSignature):
        public_key.verify(signature, tampered, padding.PKCS1v15(), hashes.SHA256())


def test_the_crx_id_is_the_hash_of_the_embedded_key(
    bundle: tuple[Path, Path, str, str],
) -> None:
    out, _key, ext_id, _version = bundle
    parsed = ParsedCrx((out / "biokids.crx").read_bytes())
    public_key_der, _signature = parsed.proofs[0]

    assert parsed.crx_id == hashlib.sha256(public_key_der).digest()[:16]
    assert extension_pack.crx_id(public_key_der) == parsed.crx_id
    expected = "".join(chr(ord("a") + int(digit, 16)) for digit in parsed.crx_id.hex())
    assert ext_id == expected


def test_the_id_is_thirty_two_letters_a_to_p(bundle: tuple[Path, Path, str, str]) -> None:
    out, _key, ext_id, _version = bundle
    assert re.fullmatch(r"[a-p]{32}", ext_id)
    assert (out / "extension-id").read_text(encoding="ascii").strip() == ext_id


def test_the_large_field_number_is_encoded_as_a_three_byte_tag() -> None:
    """signed_header_data is field 10000: tag (10000 << 3) | 2 = 80002 = 82 f1 04."""
    assert extension_pack._field(10000, b"\x01") == b"\x82\xf1\x04\x01\x01"
    assert extension_pack._varint(300) == b"\xac\x02"
    assert extension_pack._varint(0) == b"\x00"


# ----------------------------------------------------------------- the archive


def test_the_archive_holds_the_extension_and_not_its_readme(
    bundle: tuple[Path, Path, str, str],
) -> None:
    out, _key, _id, version = bundle
    parsed = ParsedCrx((out / "biokids.crx").read_bytes())
    with zipfile.ZipFile(io.BytesIO(parsed.archive)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("manifest.json"))
        assert archive.read("background.js") == BACKGROUND_JS.read_bytes()

    assert "manifest.json" in names
    assert manifest["background"]["service_worker"] in names
    assert "README.md" not in names
    assert names == sorted(names)
    assert manifest["version"] == version


def test_the_same_source_and_key_pack_to_the_same_bytes(tmp_path: Path) -> None:
    key = tmp_path / "key.pem"
    first = extension_pack.write_bundle(EXTENSION_DIR, tmp_path / "a", key)
    second = extension_pack.write_bundle(EXTENSION_DIR, tmp_path / "b", key)

    assert first == second
    crx_a = (tmp_path / "a" / "biokids.crx").read_bytes()
    crx_b = (tmp_path / "b" / "biokids.crx").read_bytes()
    assert ParsedCrx(crx_a).archive == ParsedCrx(crx_b).archive
    # PKCS#1 v1.5 is deterministic, so the whole file is.
    assert crx_a == crx_b
    assert extension_pack.deterministic_zip(EXTENSION_DIR) == ParsedCrx(crx_a).archive


def test_a_zip_does_not_depend_on_file_times(tmp_path: Path) -> None:
    source = tmp_path / "ext"
    source.mkdir()
    (source / "manifest.json").write_text('{"version": "1.0"}', encoding="utf-8")
    (source / "b.js").write_text("b", encoding="utf-8")
    (source / "a.js").write_text("a", encoding="utf-8")
    first = extension_pack.deterministic_zip(source)
    os.utime(source / "a.js", (1_000_000_000, 1_000_000_000))
    assert extension_pack.deterministic_zip(source) == first
    with zipfile.ZipFile(io.BytesIO(first)) as archive:
        assert archive.namelist() == ["a.js", "b.js", "manifest.json"]


def test_a_different_key_gives_a_different_id(tmp_path: Path) -> None:
    one = extension_pack.write_bundle(EXTENSION_DIR, tmp_path / "a", tmp_path / "1.pem")
    two = extension_pack.write_bundle(EXTENSION_DIR, tmp_path / "b", tmp_path / "2.pem")
    assert one[0] != two[0]


# ------------------------------------------------------------- the update file


def test_updates_xml_names_the_id_the_version_and_the_package(
    bundle: tuple[Path, Path, str, str],
) -> None:
    out, _key, ext_id, version = bundle
    text = (out / "updates.xml").read_text(encoding="utf-8")
    root = ElementTree.fromstring(text)
    ns = "{http://www.google.com/update2/response}"

    assert root.tag == f"{ns}gupdate"
    assert root.get("protocol") == "2.0"
    app = root.find(f"{ns}app")
    assert app is not None
    assert app.get("appid") == ext_id
    check = app.find(f"{ns}updatecheck")
    assert check is not None
    assert check.get("version") == version
    assert check.get("codebase") == (out.absolute() / "biokids.crx").as_uri()
    assert check.get("codebase", "").startswith("file:///")


def test_updates_xml_is_the_documented_gupdate_shape() -> None:
    text = extension_pack.updates_xml(
        "a" * 32, "1.0.0", "file:///var/lib/biokids-ext/biokids.crx"
    )
    assert "<gupdate xmlns='http://www.google.com/update2/response' protocol='2.0'>" in text
    assert "<app appid='aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa'>" in text
    assert (
        "<updatecheck codebase='file:///var/lib/biokids-ext/biokids.crx' version='1.0.0'/>"
        in text
    )


def test_updates_xml_refuses_what_chromium_would_not_read() -> None:
    with pytest.raises(ValueError):
        extension_pack.updates_xml("not-an-id", "1.0.0", "file:///x.crx")
    with pytest.raises(ValueError):
        extension_pack.updates_xml("a" * 32, "1.0.0'/><evil", "file:///x.crx")
    quoted = extension_pack.updates_xml("a" * 32, "1.0", "file:///it's.crx")
    assert ElementTree.fromstring(quoted)[0][0].get("codebase") == "file:///it's.crx"


# ------------------------------------------------------------- files and modes


def test_the_key_is_made_once_and_reused(tmp_path: Path) -> None:
    key_path = tmp_path / "key.pem"
    first = extension_pack.load_or_create_key(key_path)
    second = extension_pack.load_or_create_key(key_path)
    assert first.key_size == 2048
    assert extension_pack.public_key_der(first) == extension_pack.public_key_der(second)
    assert [p.name for p in tmp_path.iterdir()] == ["key.pem"], "a temporary file was left"


def test_a_key_that_is_not_rsa_is_refused(tmp_path: Path) -> None:
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(
        ec.generate_private_key(ec.SECP256R1()).private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    with pytest.raises(ValueError, match="RSA"):
        extension_pack.load_or_create_key(key_path)


@pytest.mark.skipif(sys.platform == "win32", reason="Unix permissions")
def test_the_key_is_private_and_the_bundle_is_readable(
    bundle: tuple[Path, Path, str, str],
) -> None:
    out, key_path, *_ = bundle
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert out.stat().st_mode & 0o777 == 0o755
    for name in ("biokids.crx", "updates.xml", "extension-id"):
        assert (out / name).stat().st_mode & 0o777 == 0o644, name


def test_the_bundle_leaves_no_temporary_files(bundle: tuple[Path, Path, str, str]) -> None:
    out, key_path, *_ = bundle
    assert sorted(p.name for p in out.iterdir()) == [
        "biokids.crx",
        "extension-id",
        "updates.xml",
    ]
    assert [p.name for p in key_path.parent.iterdir()] == [key_path.name]


def test_a_manifest_without_a_version_is_refused(tmp_path: Path) -> None:
    source = tmp_path / "ext"
    source.mkdir()
    (source / "manifest.json").write_text('{"name": "x"}', encoding="utf-8")
    with pytest.raises(ValueError, match="version"):
        extension_pack.write_bundle(source, tmp_path / "out", tmp_path / "key.pem")
    assert not (tmp_path / "out").exists()


# --------------------------------------------------------------- biokids-admin


def test_biokids_admin_packs_the_extension(tmp_path: Path) -> None:
    out = tmp_path / "out"
    code = admin.main(
        [
            "--config",
            str(tmp_path / "nonexistent.toml"),
            "pack-extension",
            "--source",
            str(EXTENSION_DIR),
            "--out",
            str(out),
            "--key",
            str(tmp_path / "key.pem"),
        ]
    )
    assert code == 0
    ext_id = (out / "extension-id").read_text(encoding="ascii").strip()
    assert ext_id == extension_pack.extension_id(public_der(tmp_path / "key.pem"))


def test_biokids_admin_reports_a_missing_source(tmp_path: Path) -> None:
    code = admin.main(
        [
            "--config",
            str(tmp_path / "nonexistent.toml"),
            "pack-extension",
            "--source",
            str(tmp_path / "missing"),
            "--out",
            str(tmp_path / "out"),
            "--key",
            str(tmp_path / "key.pem"),
        ]
    )
    assert code == 1


def test_pack_extension_defaults_to_the_installed_paths(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, Path, Path]] = []

    def fake_write_bundle(source: Path, out: Path, key: Path) -> tuple[str, str]:
        calls.append((source, out, key))
        return "a" * 32, "1.0.0"

    monkeypatch.setattr(extension_pack, "write_bundle", fake_write_bundle)
    code = admin.main(["--config", str(tmp_path / "nonexistent.toml"), "pack-extension"])

    assert code == 0
    assert calls == [
        (
            Path("/usr/share/biokids/extension"),
            Path("/var/lib/biokids-ext"),
            Path("/var/lib/biokids/extension-key.pem"),
        )
    ]


# ------------------------------------------------- what the extension may not do


def test_the_manifest_asks_for_nothing_more_than_it_needs() -> None:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))

    assert manifest["manifest_version"] == 3
    assert manifest["name"] == "BioKids"
    assert sorted(manifest["permissions"]) == ["storage", "webNavigation"]
    assert manifest["host_permissions"] == ["http://127.0.0.1/*"]
    assert "content_scripts" not in manifest
    assert "optional_permissions" not in manifest
    assert "optional_host_permissions" not in manifest
    assert "externally_connectable" not in manifest
    assert "web_accessible_resources" not in manifest


def _visit_bodies(source: str) -> list[str]:
    return re.findall(r"\bbody\s*:\s*([^\n]+?),?\s*$", source, flags=re.MULTILINE)


def test_the_visit_report_carries_the_host_and_nothing_else() -> None:
    """SECURITY.md section 7: the address of a site, never its path or contents.

    A static check, and deliberately a blunt one: the file builds exactly one
    request body, it is `{ host }`, and `host` only ever comes from a URL's
    hostname. Anything that wants to send more has to change this test first.
    """
    source = BACKGROUND_JS.read_text(encoding="utf-8")

    assert _visit_bodies(source) == ["JSON.stringify({ host })"]
    assert source.count("VISIT_PATH") == 2, "one definition and one request"
    for leak in (".pathname", ".search", ".hash", ".href", ".title", "document."):
        assert leak not in source, f"background.js touches {leak}"
    # Where the host comes from: the URL's hostname, through webHost().
    assert "parsed.hostname" in source
    assert re.search(r"const host = webHost\(details\.url\);", source)


def test_the_extension_loads_no_code_and_talks_only_to_the_machine() -> None:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    for forbidden in ("eval(", "new Function", "importScripts", "chrome.scripting"):
        assert forbidden not in source
    assert re.search(r'^const LAUNCHER_HOST = "127\.0\.0\.1";$', source, flags=re.MULTILINE)
    origins = re.findall(r"\w+://[^\s\"'`/]+", source)
    assert origins, "the probe builds its origins from LAUNCHER_HOST"
    for origin in origins:
        assert re.fullmatch(
            r"http://(127\.0\.0\.1:\d+|\$\{LAUNCHER_HOST\}:\$\{port\})", origin
        ), origin


def test_the_probe_range_and_the_healthz_contract_are_stated_once() -> None:
    source = BACKGROUND_JS.read_text(encoding="utf-8")
    assert re.search(r"^const PROBE_FIRST_PORT = 7879;$", source, flags=re.MULTILINE)
    assert re.search(r"^const PROBE_LAST_PORT = 7978;$", source, flags=re.MULTILINE)
    assert re.search(r'^const HEALTHZ_KEY = "biokids";$', source, flags=re.MULTILINE)
    assert re.search(r'^const HEALTHZ_VALUE = "launcher";$', source, flags=re.MULTILINE)
    assert "net::ERR_BLOCKED_BY_ADMINISTRATOR" in source
    assert "net::ERR_NAME_NOT_RESOLVED" in source
