# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Security

- `X-Forwarded-For` is believed only from the reverse proxies listed in the new
  setting `MAXADS_TRUSTED_PROXIES` (default `127.0.0.1,::1`). Before, any client
  could send a different address with every request and inflate impressions and
  clicks past the per-day deduplication (#2). Behind a proxy that is not on the
  same machine (for example a proxy container in front of the Docker setup), set
  this to the proxy's address or network. The Docker image and the systemd unit
  now start uvicorn with `--no-proxy-headers`, so the setting is the only place
  that decides.

### Fixed

- Moving the data directory (`MAXADS_BASE`) no longer breaks every banner: media
  paths are stored relative to it. Paths that v0.1 stored absolute are converted
  on the first start of this version, as long as they lie under the configured
  data directory; anything else is left as it is and logged. So update first,
  then move the directory.

### Added

- `python -m app.backup <dir>` (or `-` for stdout) writes one tar with a
  consistent copy of the database, the originals, the media and the generated
  secrets, safe while the service runs; `python -m app.restore <file>` (or `-`)
  checks it and puts it back, moving the current data aside instead of deleting
  it. README and the systemd guide describe backup, restore and update (#4).

- Web service and worker warn at start about `MAXADS_` variables they do not
  know, with the closest known name (a typo used to leave the default in place
  without a word).

## [0.1.0] - 2026-10-06

### Added

- First public release: banner upload with WebP/H.264 optimisation in a separate
  worker, five embedding routes (script, script with options, iframe, plain
  image, JSON), per-day deduplicated impressions and clicks without stored IPs,
  site metrics, storage overview, Docker and systemd setups.
- The first admin password can be set with `MAXADS_ADMIN_PASSWORD`; a generated
  one is shown once on stderr, never logged, and must be changed on first login.
- Mount guard can be switched off for container volumes (`MAXADS_REQUIRE_MOUNT=0`).

### Changed

- English user interface; environment variables are prefixed `MAXADS_`.
- The host port of the Docker quick start is configurable with `MAXADS_PORT`.

### Fixed

- A failed dashboard login no longer writes the client IP address to the log.
- A banner title can no longer inject script into the delete confirmation; an
  apostrophe in the title used to skip the confirmation (#10).
- A click on a banner without a target URL is no longer counted; a failed or
  converting banner no longer shows "on air" (#10).
- Dashboard form errors (file type, size, password length, duplicate user) show
  on the dashboard instead of as raw JSON (#12).
