# Japan Quake Monitor — backend + infra update

**What this archive is:** the backend and infrastructure parts of the review, as
complete replacement files. Unzip over the root of your working copy and commit.

**What it is NOT:** `index.html` is not here (see below), and none of this has
been executed, built, or tested. It is code I wrote by reading your repo, not
code I ran.

---

## Files in this archive

```
backend/main.py               FULL REPLACEMENT — backend v3
backend/test_parsers.py       NEW — pytest suite for both parsers
backend/requirements.txt      pinned versions
backend/Dockerfile            copies assets/ into static/
Dockerfile                    copies assets/ into the nginx docroot
nginx.conf                    security headers + CSP + asset caching
docker-compose.yml            backend healthcheck added
.github/workflows/deploy.yml  validate job, needs:, per-image concurrency
.github/dependabot.yml        NEW
.gitignore                    __pycache__, data/, venv, _site
.dockerignore                 dev cruft out of build context
```

## Not included — `index.html`

The frontend changes (sections 3a–3g of the review) are **not** in this archive.
I never held the real bytes of that file: my first fetch truncated at ~30 KB and
the remainder came from your paste. Reconstructing a 194 KB file from a paste and
shipping it back to you would risk silently mangling it, so apply those edits by
hand. They are listed at the end of this file as a checklist.

Because of that, **`assets/eew-chime.mp3` is still duplicated as a base64 blob
inside `index.html`** until you do 3a.

---

## Apply order

### 0. Back up the archive first (do not skip)

```bash
cp data/quakes.db data/quakes.db.bak
```

`init_db()` mutates an existing database in place: it adds `ts` and `quake_id`
columns, backfills them, and creates the `quakes` / `quake_prefs` / `raw_messages`
tables. There is no down-migration. If the migration goes wrong, the `.bak` file
is your only way back.

### 1. Backend

```bash
unzip japan-quake-monitor-backend-and-infra.zip -d /tmp/jqm
cp /tmp/jqm/backend/main.py              backend/main.py
cp /tmp/jqm/backend/test_parsers.py      backend/test_parsers.py
cp /tmp/jqm/backend/requirements.txt     backend/requirements.txt
pip install -r backend/requirements.txt pytest
DATA_DIR=/tmp/jqmtest python -m pytest backend -q
```

Expected: all tests pass. If `pip` cannot resolve a pinned version, loosen the
pin rather than skipping the test — the pins are there so two builds a week
apart are identical, not because those exact versions are required.

Then run it against a **copy** of your data and check the migration:

```bash
cp data/quakes.db /tmp/jqmtest/quakes.db
DATA_DIR=/tmp/jqmtest python backend/main.py
curl -s localhost:8000/api/healthz | python -m json.tool
```

What to look for in that output:

- `schema_version: 3`
- `quakes_total` **lower than** `events_total` — that gap is the JMA/USGS merge
  working. If they are equal, no quakes merged, and the statistics are still
  double-counting.
- `collector_stale` all `false` after ~2 minutes of uptime
- `history_since` roughly matches your old archive's earliest event

Only then point it at the real `data/`.

### 2. Infra

```bash
cp /tmp/jqm/nginx.conf                    nginx.conf
cp /tmp/jqm/Dockerfile                    Dockerfile
cp /tmp/jqm/backend/Dockerfile            backend/Dockerfile
cp /tmp/jqm/docker-compose.yml            docker-compose.yml
cp /tmp/jqm/.github/workflows/deploy.yml  .github/workflows/deploy.yml
cp /tmp/jqm/.github/dependabot.yml        .github/dependabot.yml
cp /tmp/jqm/.gitignore                    .gitignore
cp /tmp/jqm/.dockerignore                 .dockerignore
git rm -r --cached backend/__pycache__
```

Verify the headers actually land (they are easy to lose — see the note at the
bottom of `nginx.conf`):

```bash
docker build -t jqm-test . && docker run --rm -p 8080:8080 jqm-test &
curl -sI localhost:8080/ | grep -i -E 'content-security|x-content-type|referrer'
curl -sI localhost:8080/index.html | grep -i -E 'content-security|x-content-type|referrer'
```

The second command is the one that matters. If it prints nothing, you have hit
the `add_header` inheritance trap and the security headers are missing on the
main page.

---

## Ordering trap: CSP vs the inline chime

The new `nginx.conf` ships `media-src 'self' data:`. The `data:` is there
**only** because `index.html` still contains the base64 chime as a data URI.

Once you apply frontend section 3a (delete the base64 line, point `playChime()`
at `assets/eew-chime.mp3`), change that directive to `media-src 'self'` —
otherwise you are leaving a hole open for no reason.

If you apply `nginx.conf` *before* touching the frontend, the alert sound still
works. If you tighten `media-src` first, the chime goes silent, which is a
confusing thing to debug from a phone at 2 a.m.

Similarly, `script-src` still contains `'unsafe-inline'` because the whole app is
one inline `<script>`. That token is the main reason this CSP is weak. It can go
the moment you split the file (section 5 of the review).

---

## API contract changes you must not overlook

`/api/events` and `/api/stats` now read from the merged `quakes` table, not from
`events`. Two consequences:

1. `?source=JMA` now means "quakes that have a JMA report", not "rows whose
   `source` column is JMA". A quake reported by both networks matches both
   `source=JMA` and `source=USGS`. That is the intent, but it is a behaviour
   change.
2. Each returned event is keyed by `primary_event_id` — the same `p2p-<id>` /
   `usgs-<id>` string the live feed uses, so the dashboard dedupes hydrated
   history against WebSocket events correctly. **Do not change that id**
   without changing the frontend, or `hydrateFromBackend()` starts duplicating
   every event it loads.

`/api/healthz` gained `quakes_total`, `schema_version`, `collector_beats` and
`collector_stale`. `ok` is now `false` when a collector has missed 5 intervals —
so a green box no longer hides a dead poller. Kubernetes readiness probes only
check for HTTP 2xx and still pass; nothing restarts just because `ok` is false.
A new `/livez` endpoint touches no database at all and is the right target for a
liveness probe.

---

## Known-fragile spots, flagged rather than hidden

- **`find_quake` scans a ±120 s window** on every incoming event. Fine at
  ~1–2k events/day. At 100k it needs an index-backed spatial query. Not a
  problem for you now.
- **`upsert()` does a `SELECT` per event** to count new-vs-updated. It is inside
  one transaction so it is correct, just not optimal.
- **`link_orphans()` runs on every startup** and returns immediately when there
  is nothing to do. On a large legacy archive the first run will take a while.
- **`requirements.txt` pins may not resolve** as written. If so, loosen them.
- **Nothing here was executed.** The parser tests are the cheapest way to prove
  the code is coherent before you trust it with real data.

---

## Frontend checklist (NOT in this archive)

Apply by hand from the review message:

- [ ] **3a** — delete `const EEW_CHIME = 'data:audio/mpeg;base64,...'`, replace
      `playChime()` with the `assets/eew-chime.mp3` version.
      **First** confirm the MP3 and the embedded blob are the same recording
      (play both); if they differ, keep whichever you want and update the file.
- [ ] **3a** — the three `assets/` copies are already done for you here in both
      Dockerfiles and the Pages job. Nothing to do on that front.
- [ ] **3b** — `jmaTimeToISO` (anchor the regex, truncate the fraction) and both
      parsers (sentinel guards, `msg.id` -> `msg._id` fallback).
- [ ] **3c** — replace the event store: minute-bucketed index, `nearest()`,
      `mergedIds`, delete `dedupeTwins()` and both its call sites.
- [ ] **3d** — marker diffing in `render()` instead of `clearLayers()`.
- [ ] **3e** — `saveSoon()` sorts before slicing; quota-expiry fallback.
- [ ] **3f** — visibility pause + `BroadcastChannel` leader election, plus the
      `'standby'` case in `setStatus()`.
- [ ] **3g** — cap `seenTsu`, `aria-live` on `#toasts`, `role="alert"` on
      `#connBanner`.
- [ ] **3a follow-up** — drop `data:` from `media-src` in `nginx.conf`.

Two frontend facts I could not verify and you should, before or during 3b:

1. Whether P2PQuake v2 sends `msg.id` or `msg._id`. Log `Object.keys(msg)` once
   from the WebSocket handler. Symptom if the fallback is wrong: exactly one
   event in the list where you expect many.
2. Whether the MP3 and the base64 blob are the same audio.

---

## Still outstanding after this archive

- `README.md` is now wrong in places: it documents backend v2's health payload,
  claims "the same file runs three ways" (untrue the moment the chime is
  external), and omits `/livez`, `RAW_RETENTION_DAYS`, the `quakes` table and the
  `?source=` semantics change. I have not rewritten it.
- Bump the backend PVC from `1Gi` to `10Gi` in `k8s/backend.yaml`. Raw payloads
  are now compressed and pruned to 30 days, so you are unlikely to hit it, but a
  full volume takes the archive offline and `strategy: Recreate` will not fix it.
- No `LICENSE`. If you add MIT, carve out `assets/eew-chime.mp3` explicitly —
  it is a recording you may not hold the rights to relicense.
