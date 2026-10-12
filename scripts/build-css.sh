#!/bin/sh
# Rebuild app/static/tailwind.css from the class names in app/templates/.
#
# Run it after changing classes in a template and commit the result: the CSS is
# part of the repository, so Docker, systemd and a local run all serve it without
# a build step and without loading anything from a third party at runtime. CI
# fails when the committed file does not match the templates.
#
# Uses the standalone Tailwind CLI (no Node.js needed) in the version the
# dashboard was designed with, downloaded once into ~/.cache/adyard and checked
# against the published SHA-256.
set -eu

VERSION=3.4.17
case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)            asset=tailwindcss-linux-x64;    sum=7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4 ;;
  Linux-aarch64|Linux-arm64) asset=tailwindcss-linux-arm64; sum=69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b ;;
  Darwin-arm64)            asset=tailwindcss-macos-arm64;  sum=a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3 ;;
  Darwin-x86_64)           asset=tailwindcss-macos-x64;    sum=6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3 ;;
  *) echo "No pinned Tailwind CLI for $(uname -sm); see https://github.com/tailwindlabs/tailwindcss/releases/tag/v$VERSION" >&2
     exit 1 ;;
esac

cache="${XDG_CACHE_HOME:-$HOME/.cache}/adyard"
bin="$cache/$asset-$VERSION"
if [ ! -f "$bin" ]; then
  mkdir -p "$cache"
  curl -fsSL -o "$bin.part" "https://github.com/tailwindlabs/tailwindcss/releases/download/v$VERSION/$asset"
  mv "$bin.part" "$bin"
fi
if command -v sha256sum >/dev/null 2>&1; then
  actual=$(sha256sum "$bin" | cut -d' ' -f1)
else
  actual=$(shasum -a 256 "$bin" | cut -d' ' -f1)
fi
if [ "$actual" != "$sum" ]; then
  echo "Checksum mismatch for $bin -- delete it and try again." >&2
  exit 1
fi
[ -x "$bin" ] || chmod +x "$bin"

cd "$(dirname "$0")/.."
"$bin" --content './app/templates/**/*.html' --output app/static/tailwind.css --minify
