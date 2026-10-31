#!/bin/sh
# Vercel's build of the website (vercel.json, D-258), and CI's: Jinja2, then
# the site, checked, so a broken link fails the deployment rather than
# reaching a parent. Vercel serves website/_site.
#
# Jinja2 goes into a virtualenv of its own: Vercel's Python is managed by uv
# and refuses packages installed into it (PEP 668, "externally managed").
set -eu

VENV="${TMPDIR:-/tmp}/biokids-website-venv"
rm -rf "$VENV"
python3 --version

if python3 -m venv "$VENV"; then
  "$VENV/bin/python" -m pip install --disable-pip-version-check --quiet "jinja2>=3.1"
elif command -v uv >/dev/null 2>&1; then
  # A Python without ensurepip cannot make a virtualenv with pip in it; uv can.
  rm -rf "$VENV"
  uv venv --quiet --python python3 "$VENV"
  uv pip install --quiet --python "$VENV/bin/python" "jinja2>=3.1"
else
  echo "vercel-build: cannot make a virtualenv for Jinja2" >&2
  exit 1
fi

"$VENV/bin/python" website/build.py --check
