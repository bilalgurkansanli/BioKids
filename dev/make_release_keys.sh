#!/bin/sh
# Make the two keys a release needs, once, on the owner's computer.
#
#  - The archive key signs the APT repository and the release's SHA256SUMS. Its
#    public half goes into the repository as debian/biokids-archive-keyring.asc;
#    its private half goes into GitHub Secrets (GPG_PRIVATE_KEY, with the
#    passphrase as GPG_PASSPHRASE) and onto an offline backup, and nowhere else.
#  - The deploy key lets release.yml push the archive to the BioKids-apt
#    repository (D-257): its public half is that repository's deploy key with
#    write access, its private half the APT_DEPLOY_KEY secret.
#
# Everything is written to one new folder outside the repository, default
# ~/BioKids-anahtarlar, with a GnuPG home of its own, so the owner's personal
# keyring never holds the archive key and nothing here can be committed by
# accident. It refuses to touch a folder that exists: running it twice must not
# replace a key that has been published.
#
#     sh dev/make_release_keys.sh [FOLDER]
#
# Git Bash on Windows has both gpg and ssh-keygen. POSIX sh.

set -eu

OUT="${1:-$HOME/BioKids-anahtarlar}"
# Git Bash's gpg reads C:/... as a relative path.
if command -v cygpath >/dev/null; then
    OUT="$(cygpath -u "$OUT")"
fi
NAME="BioKids Archive Signing Key <bilalsanli129@gmail.com>"

die() {
    printf '%s\n' "$*" >&2
    exit 1
}

command -v gpg >/dev/null || die "gpg is not installed (it comes with Git Bash)."
command -v ssh-keygen >/dev/null || die "ssh-keygen is not installed (it comes with Git Bash)."
[ -e "$OUT" ] && die "$OUT already exists. Nothing was changed; move it away first."

mkdir -p "$OUT"
if command -v git >/dev/null && git -C "$OUT" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    rmdir "$OUT"
    die "$OUT is inside a git repository, where a key could be committed; choose another folder."
fi
# All or nothing: a run that stops half-way leaves no folder behind, so the
# next run is not refused by a half-made one.
trap 'gpgconf --kill all 2>/dev/null; rm -rf "$OUT"' EXIT
mkdir "$OUT/gnupg"
chmod 700 "$OUT" "$OUT/gnupg"
GNUPGHOME="$OUT/gnupg"
export GNUPGHOME

# Read here rather than by gpg's pinentry, which cannot always draw in Git
# Bash's window. Not echoed, and passed to gpg on a pipe, never as an argument.
ask() {
    printf '%s' "$1" >&2
    stty -echo 2>/dev/null || true
    IFS= read -r answer || answer=""
    stty echo 2>/dev/null || true
    printf '\n' >&2
    printf '%s' "$answer"
}

echo "Choose the archive key's passphrase: long, written down, kept with the backup."
pass="$(ask 'Passphrase: ')"
again="$(ask 'Again: ')"
[ -n "$pass" ] || die "An empty passphrase would leave the backup unprotected. Nothing was made."
[ "$pass" = "$again" ] || die "The two passphrases differ. Nothing was made; run it again."

printf '%s' "$pass" | gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 0 \
    --quick-generate-key "$NAME" rsa4096 sign never

fpr="$(gpg --list-secret-keys --with-colons | awk -F: '/^fpr:/ { print $10; exit }')"
[ -n "$fpr" ] || die "gpg made no key."

gpg --armor --export "$fpr" > "$OUT/biokids-archive-keyring.asc"
printf '%s' "$pass" | gpg --batch --quiet --pinentry-mode loopback --passphrase-fd 0 \
    --armor --export-secret-keys "$fpr" > "$OUT/GPG_PRIVATE_KEY.txt"
cp "$GNUPGHOME/openpgp-revocs.d/$fpr.rev" "$OUT/revocation-certificate.rev"
printf '%s\n' "$fpr" > "$OUT/fingerprint.txt"
pass=""
again=""

ssh-keygen -q -t ed25519 -N "" -C "BioKids release to BioKids-apt" -f "$OUT/APT_DEPLOY_KEY.txt"
mv "$OUT/APT_DEPLOY_KEY.txt.pub" "$OUT/deploy-key-public.txt"
gpgconf --kill all 2>/dev/null || true
trap - EXIT

cat <<EOF

Done, in $OUT:

  GPG_PRIVATE_KEY.txt          secret GPG_PRIVATE_KEY in BioKids (the whole file)
  APT_DEPLOY_KEY.txt           secret APT_DEPLOY_KEY in BioKids (the whole file)
  deploy-key-public.txt        BioKids-apt, Deploy keys, "Allow write access"
  biokids-archive-keyring.asc  the public key, for the repository
  fingerprint.txt              $fpr
  revocation-certificate.rev   withdraws the key if it is ever stolen
  gnupg/                       the key itself

The passphrase is secret GPG_PASSPHRASE. Then copy this folder to an offline
backup and delete it from this computer.
EOF
