# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Changed

- Renamed from maxads to adyard: repository `Juice-de-Orange/adyard`, product
  name, page titles, log names (`adyard.web`, `adyard.worker`), Docker image
  (`<project>-app`, default project `adyard`), container user, systemd unit
  templates (`adyard.service`, `adyard-worker.service`) and their paths. Older
  entries below keep the old name. An existing installation keeps working
  without changes (README → "Coming from maxads"):
  - Settings are `ADYARD_*` now. The old `MAXADS_*` names are still read, with
    one warning per old name at start; when both are set, the `ADYARD_` one
    wins.
  - The database file is `adyard.db`; an existing `maxads.db` is used as long as
    there is no `adyard.db` next to it. Nothing is renamed automatically.
  - The Compose data volume is still named after the checkout directory, so an
    update in place keeps it.
  - Backups are `adyard-backup-*.tar`; backups made under the old name restore
    as before.
  - The session cookie is `adyard_session`: everyone signs in once more.
  - The embed box carries `class="adyard"` and `data-adyard` instead of
    `maxads` / `data-maxads`.

### Security

- `X-Forwarded-For` is believed only from the reverse proxies listed in the new
  setting `ADYARD_TRUSTED_PROXIES` (default `127.0.0.1,::1`). Before, any client
  could send a different address with every request and inflate impressions and
  clicks past the per-day deduplication (#2). Behind a proxy that is not on the
  same machine (for example a proxy container in front of the Docker setup), set
  this to the proxy's address or network. The Docker image and the systemd unit
  now start uvicorn with `--no-proxy-headers`, so the setting is the only place
  that decides.
- Password guessing on the dashboard login is slowed down: after five failed
  logins from one client address or for one account name, further attempts are
  answered with `429 Too Many Requests` and `Retry-After` for 30 seconds,
  doubling with every further failure up to 15 minutes; a successful login
  clears the count. The counters are kept in memory only.
- The dashboard no longer loads Tailwind's Play CDN script from a third party:
  the stylesheet is prebuilt into `app/static/` (`scripts/build-css.sh`), the
  page behaviour moved from inline handlers to `app/static/dashboard.js`, and
  dashboard pages send a Content-Security-Policy that allows script only from
  the service itself (#1).

### Fixed

- Moving the data directory (`ADYARD_BASE`) no longer breaks every banner: media
  paths are stored relative to it. Paths that v0.1 stored absolute are converted
  on the first start of this version, as long as they lie under the configured
  data directory; anything else is left as it is and logged. So update first,
  then move the directory.
- A banner that was paused or deleted no longer leaves an empty box on host
  pages that still have its embed script cached (up to five minutes): the
  script removes its box when the media does not load (#11).
- The Docker image is named after the Compose project (`<project>-app`) instead
  of the fixed `maxads:local`, so two checkouts on one host no longer overwrite
  each other's image (#11). The old `maxads:local` image can be removed with
  `docker image rm maxads:local`.

### Added

- `python -m app.backup <dir>` (or `-` for stdout) writes one tar with a
  consistent copy of the database, the originals, the media and the generated
  secrets, safe while the service runs; `python -m app.restore <file>` (or `-`)
  checks it and puts it back, moving the current data aside instead of deleting
  it. README and the systemd guide describe backup, restore and update (#4).
- Accounts can be removed from the dashboard (✕ next to the name, with a
  confirmation). Their banners stay on air and, like the accounts they created,
  move to whoever removed them. Nobody can remove their own account or the last
  one (#3).

- Web service and worker warn at start about `ADYARD_` (and old `MAXADS_`)
  variables they do not know, with the closest known name (a typo used to leave
  the default in place without a word).

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
