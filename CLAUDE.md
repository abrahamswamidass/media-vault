# Media Vault

Personal media system, single user. A **local agent** indexes ~1 TB across NAS,
Google Drive, and exported archives; a **web module** in Google Cloud is where
decisions get made. The NAS is the source of truth and nothing else ever writes
to it.

## The three rules

1. **Only the agent touches files.** The web module cannot delete, move, or read
   a byte off the NAS. It writes *intents*; the agent decides whether to honour them.
2. **Dry-run is the default.** Every mutating path takes `commit=False` and
   previews. A caller who forgets the flag gets a preview, never a mutation.
3. **One writer per collection.** `items/` is agent-written, `intents/` is
   web-written (except `status`/`result`, which the agent owns). There is no merge
   logic anywhere in this project because no document has two authors.

## Layout

```
agent/           Module 1 — local Python. The only thing that touches files.
  src/mediavault/
    ports.py       Connector + BlobStore interfaces (the ports)
    connectors/    one adapter per source: nas, drive, archive, amazon
    blobstore.py   blob adapters: LocalBlobStore (tests), GCSBlobStore (real)
    imaging.py     the one place that decodes a photo
    catalog/       SQLite index: store, resumable scanner, dedup engine
    actions/       every mutation, as a Command object
    sync/          intents in, facts out
    doctor.py      preflight config check
    cli.py
  tests/
web/             Module 2 — Firebase Hosting. Currently a minimal static
                 read-only viewer (index.html/app.js/style.css, no build
                 step, no auth) — a placeholder MVP, not the eventual React
                 app. Reads `items` from Firestore, renders public thumbnail
                 URLs. See docs/setup.md's "Web viewer" section.
shared/contracts/ JSON Schema both modules validate against. Change here first.
docs/            architecture.html, setup.md, command-catalog.html
```

Dependency direction is strictly one-way and must stay that way:
`sync → actions → {ports, blobstore, imaging}`. `actions/` importing from `sync/`
is a circular import — that already happened once.

## Design decisions worth not re-deriving

- **Content-addressed blobs.** Derived images are keyed by the source file's
  `quick_hash`: `thumbs/<hash>.webp`, `previews/<variant>/<hash>.<ext>`. This is
  what makes uploads idempotent, collapses duplicates, and survives renames. It
  is also the idempotency guarantee for replayed intents — a repeat fetch finds
  the blob present and returns `no-op` without touching the NAS.
- **`quick_hash` never reads a whole file.** Size + SHA-1 of first and last 64 KB.
  Reading 1 TB over a network mount to hash it is not acceptable.
- **Previews expire, thumbnails don't.** A GCS lifecycle rule empties `previews/`
  after a day. That rule is load-bearing; without it, full-res fetches accumulate.
- **Firebase, not Cloud Run.** Auth + Firestore + Storage + Hosting with security
  rules means no backend service, so nothing idles and nothing needs patching.
- **Stdlib-only core.** Connectors, actions, and CLI import nothing outside the
  standard library. Pillow, Google clients, and exiftool are optional extras, each
  gated behind an explicit import or env flag.
- **Live switches default off.** `DRIVE_LIVE=0`, `GCS_LIVE=0`. A half-configured
  machine cannot reach a cloud API.

## Deduplication rules — do not relax these

The NAS holds everything; Drive holds a curated copy of the good things; Amazon is
push-only for TV playback. Therefore:

- **Duplicates are compared within a single source, never across.** A photo in both
  the NAS and Drive is correct. `Catalog.duplicate_groups` is scoped to one source
  precisely so cross-source pairs are unreachable, and a test asserts it.
- **Only exact duplicates auto-archive**, confirmed by a full SHA-256 over the
  members. Files over 128 KB share a `quick_hash` on size plus first/last 64 KB —
  that is a candidate signal, not proof. Perceptual near-duplicates are review-only
  and are deliberately not wired to an automatic action.
- **The keeper is the oldest**, tie-broken by shallowest path, then alphabetical.
  Deterministic, so a re-run proposes the same thing.
- **Archiving is always reversible** — NAS trash folder, or Drive's 30-day trash.
  Permanent delete is never used by dedup.
- A stale catalog refuses to act: if any group member is missing from disk, the
  action fails and asks for a re-scan rather than risking the last copy.

## Adding a capability to the UI

Add one line to `REGISTRY` in `agent/src/mediavault/sync/intents.py`, and the
matching enum entry in `shared/contracts/intent.schema.json`. That table is the
entire surface area the web module gets; anything not in it is refused.

## Commands

```bash
cd agent
python -m pytest tests/ -q                     # 27 tests, no cloud account needed
PYTHONPATH=src python -m mediavault.cli doctor

# via Docker, from the repo root:
./run.sh doctor                                # what's configured, what isn't
./run.sh index nas                             # resumable walk into the catalog
./run.sh dedup nas                             # preview duplicate archiving
./run.sh dedup nas --commit                    # do it (recoverable)
```

CI (`.github/workflows/agent.yml`) runs the suite on every push and publishes a
multi-arch image to `ghcr.io/OWNER/media-vault/agent` from main and version tags.

`sample/` is a fake NAS so everything above runs with zero setup.

## Priorities, in order

Readability, then recognised patterns, then low cost. Prefer a pattern the user
can look up (Ports & Adapters, Command, Transactional Outbox, content-addressed
storage) over a clever one. Target spend is ~$0.10–1.00/month; flag one-time
costs explicitly.
