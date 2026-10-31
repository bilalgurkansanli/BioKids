#!/bin/sh
# Vercel's build of the website (vercel.json, D-258): Jinja2, then the site,
# checked exactly as CI checks it, so a broken link fails the deployment
# rather than reaching a parent. Vercel serves website/_site.
set -eu

python3 --version
python3 -m pip install --disable-pip-version-check --quiet "jinja2>=3.1" \
  || python3 -m pip install --disable-pip-version-check --quiet --user "jinja2>=3.1"

python3 website/build.py --check
