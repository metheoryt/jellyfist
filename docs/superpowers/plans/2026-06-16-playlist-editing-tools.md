# Playlist Editing Tools (`merge` + `dedup-members`) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `playlists` CLI group with two on-demand canonical-hub editing verbs — `merge` (fold near-duplicate playlists into one, durably) and `dedup-members` (collapse canon-duplicate member rows) — that edit only the canonical `Playlist`/`PlaylistTrack` graph and let the existing `sync` propagate the result outward.

**Architecture:** A new pure-logic module `airdrome/playlists/edit.py` holds the merge core (`merge_playlists`), `dedup_members`, and same-name grouping. A new `PlaylistMerge` tombstone table durably suppresses absorbed source identities so a later `land` can't recreate them; `unify_source_playlists` consults it. A new `airdrome/terminal/playlists.py` Typer group exposes the verbs. The legacy `land --merge-playlists` / `unify_source_playlists(merge_by_name=...)` path is removed (design decision #2) — same-name collapse now lives in `playlists merge --same-name`, operating on canonical playlists.

**Tech Stack:** Python 3.14, SQLAlchemy 2.x (declarative `Mapped`), Typer CLI, Rich console, Alembic migrations, pytest against PostgreSQL (test DB built from metadata via `create_all`).

**Design source:** `docs/design/playlist-tools.md` (decisions locked 2026-06-09). Read it before starting.

**Conventions to honor (from AGENTS.md):**
- Business logic takes `s: Session`, uses `s.flush()`, never `s.commit()`. CLI commands take `ctx: typer.Context` first, read `state: AppState = ctx.obj`, set `state.dry_run = dry_run`, pass `state.session`.
- One-line docstring on every function/method. Comment non-obvious decisions.
- Line length 110. Ruff rules `E,F,I,W,UP,B,SIM,C4,PIE,RUF`. Run `ruff check . && ruff format .` before each commit.
- Tests: `uv run pytest` (PostgreSQL must be up: `docker compose up -d`). The `session` fixture rolls back after each test; tables are built from model metadata, so a new model needs **no** migration for tests to see it.
- All datetimes timezone-aware UTC.

---

## File Structure

- **Create** `airdrome/playlists/edit.py` — merge core, `dedup_members`, same-name grouping, tombstone helper. Pure logic, no Typer.
- **Modify** `airdrome/models.py` — add `PlaylistMerge` tombstone model.
- **Modify** `airdrome/library/unify.py` — consult tombstones in `_unify_per_source`; remove the `merge_by_name` branch from `unify_source_playlists` and the `merge_playlists` arg from `do_unify`.
- **Create** `airdrome/terminal/playlists.py` — the `playlists` Typer group (`merge`, `dedup-members`) + name/`#id` resolver.
- **Modify** `airdrome/terminal/app.py` — register the group, drop the `--merge-playlists` option from `land`, add the group to the help-callback stack.
- **Create** `tests/test_playlist_edit.py` — unit tests for the new module.
- **Modify** `tests/test_apple_ingest.py` — delete the obsolete `test_unify_playlists_merge_by_name_collapses`; add a tombstone-skip test.
- **Modify** `tests/test_cli_defaults.py` — add `playlists` CLI wiring tests.
- **Regenerate** the single Alembic migration under `alembic/versions/`.
- **Modify** `AGENTS.md`, `ROADMAP.md`, `docs/design/playlist-tools.md` — fold current-state in, mark shipped/built.

---

## Task 1: `PlaylistMerge` tombstone model + migration

**Files:**
- Modify: `airdrome/models.py` (add model after `PlaylistLink`, ends at `models.py:609`)
- Regenerate: `alembic/versions/2026_06_05_initial_schema.py` (delete + autogenerate)

- [ ] **Step 1: Write the failing test**

Create `tests/test_playlist_edit.py`:

```python
"""Tests for the on-demand playlist editing verbs (merge, dedup-members) and tombstones.

These edit only the canonical Playlist/PlaylistTrack graph; `sync` propagates the result.
The PlaylistMerge tombstone makes a merge durable across a later `land`.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from airdrome.enums import Source
from airdrome.models import Playlist, PlaylistMerge, PlaylistTrack

from factories import make_track


def _pl(s, name: str, tracks, *, platform: Source = Source.APPLE_MS, source_id: str = "", modified=None):
    """Create a canonical playlist holding `tracks` (Track objects, in order; dups allowed)."""
    pl = Playlist(
        name=name,
        platform=platform,
        source_id=source_id or f"src-{name}-{id(tracks)}",
        date_modified=modified,
    )
    s.add(pl)
    s.flush()
    for pos, t in enumerate(tracks, start=1):
        s.add(PlaylistTrack(playlist_id=pl.id, track_id=t.id, position=pos))
    s.flush()
    return pl


def _members(s, pl):
    """Return a playlist's track_ids ordered by position."""
    return list(
        s.scalars(
            select(PlaylistTrack.track_id)
            .where(PlaylistTrack.playlist_id == pl.id)
            .order_by(PlaylistTrack.position)
        )
    )


def test_playlistmerge_tombstone_roundtrips(session):
    """A PlaylistMerge row stores (provider, source_id) -> surviving playlist id."""
    t = make_track(session, "a")
    base = _pl(session, "Base", [t], source_id="base-1")
    session.add(
        PlaylistMerge(provider=Source.APPLE_MS, source_id="other-1", surviving_playlist_id=base.id)
    )
    session.flush()
    row = session.get(PlaylistMerge, (Source.APPLE_MS, "other-1"))
    assert row is not None
    assert row.surviving_playlist_id == base.id
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_playlist_edit.py::test_playlistmerge_tombstone_roundtrips -v`
Expected: FAIL with `ImportError: cannot import name 'PlaylistMerge'`.

- [ ] **Step 3: Add the model**

In `airdrome/models.py`, immediately after the `PlaylistLink` class (before `class DedupGroup`), add:

```python
class PlaylistMerge(Base):
    """Tombstone: an absorbed playlist's source identity, suppressed from recreation.

    When `playlists merge` folds playlist B into A, B's canonical `Playlist` is deleted but its
    underlying `SourcePlaylist` row survives — so the next `land`'s `get_or_create(platform,
    source_id)` would recreate B and silently undo the merge. This row records B's source identity
    so `unify_source_playlists` skips recreating it. `surviving_playlist_id` points to A for
    traceability only; it is nullable (ON DELETE SET NULL) because a later `--rebuild-playlists`
    may drop A without invalidating the suppression — the (provider, source_id) key is what
    `land` checks.
    """

    __tablename__ = "playlistmerge"

    provider: Mapped[Source] = mapped_column(sa.Enum(Source, native_enum=False), primary_key=True)
    source_id: Mapped[str] = mapped_column(primary_key=True)
    surviving_playlist_id: Mapped[int | None] = mapped_column(
        ForeignKey("playlist.id", ondelete="SET NULL")
    )
```

(`Source`, `sa`, `Mapped`, `mapped_column`, `ForeignKey` are already imported at the top of `models.py`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_playlist_edit.py::test_playlistmerge_tombstone_roundtrips -v`
Expected: PASS.

- [ ] **Step 5: Regenerate the migration**

The project squashes to a single migration (AGENTS.md *Migrations*). With an **empty** dev DB:

```bash
docker compose up -d
rm alembic/versions/2026_06_05_initial_schema.py
uv run alembic revision --autogenerate -m "initial schema"
```

Then open the regenerated file and confirm it contains a `playlistmerge` `create_table` with a composite primary key on `(provider, source_id)` and the `surviving_playlist_id` FK. If the autogenerate ran against a non-empty DB and produced an incremental diff instead of a full schema, drop the dev DB tables and re-run (the file's `down_revision` must be `None`).

Run: `uv run pytest -q` to confirm the whole suite still passes (tests build tables from metadata, not the migration, so this guards the model — the migration is verified by inspection above).

- [ ] **Step 6: Lint + commit**

```bash
ruff check . && ruff format .
git add airdrome/models.py alembic/versions/ tests/test_playlist_edit.py
git commit -m "Add PlaylistMerge tombstone model + regenerate migration"
```

---

## Task 2: Merge core (`merge_playlists`)

**Files:**
- Create: `airdrome/playlists/edit.py`
- Test: `tests/test_playlist_edit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playlist_edit.py`:

```python
from airdrome.playlists.edit import merge_playlists


def test_merge_appends_unique_canon_resolved_and_tombstones(session):
    """Others' members fold into base once, canon-resolved, appended; each other is tombstoned+deleted."""
    a, b, c = make_track(session, "a"), make_track(session, "b"), make_track(session, "c")
    base = _pl(session, "Base", [a, b], source_id="base-1")
    other = _pl(session, "Other", [b, c], source_id="other-1")  # b overlaps base

    appended = merge_playlists(session, base, [other])

    assert appended == 1  # only c is new; b already present
    assert _members(session, base) == [a.id, b.id, c.id]  # appended at end, order preserved
    assert session.get(Playlist, other.id) is None  # absorbed playlist deleted
    tomb = session.get(PlaylistMerge, (Source.APPLE_MS, "other-1"))
    assert tomb is not None and tomb.surviving_playlist_id == base.id


def test_merge_resolves_twins_to_canon(session):
    """A twin member of `other` folds in as its canon id, and dedups against base's canon."""
    canon = make_track(session, "canon")
    twin = make_track(session, "twin")
    twin.canon_id = canon.id
    session.flush()
    base = _pl(session, "Base", [canon], source_id="base-1")
    other = _pl(session, "Other", [twin], source_id="other-1")  # twin resolves to canon

    appended = merge_playlists(session, base, [other])

    assert appended == 0  # twin -> canon, already in base
    assert _members(session, base) == [canon.id]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_playlist_edit.py -k merge -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'airdrome.playlists.edit'`.

- [ ] **Step 3: Write the module**

Create `airdrome/playlists/edit.py`:

```python
"""On-demand canonical-hub playlist edits: merge near-duplicates, dedup canon-duplicate members.

These verbs edit only the canonical `Playlist`/`PlaylistTrack` graph (and `PlaylistMerge`
tombstones); they never talk to a remote. The existing `sync` then propagates the result outward
as ordinary "ours" changes. See docs/design/playlist-tools.md for the locked decisions.
"""

from collections import defaultdict

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.models import Playlist, PlaylistMerge, PlaylistTrack, Track
from airdrome.normalize.norm import normalize_name


def _canon(s: Session, track_id: int) -> int:
    """Resolve a track_id to its canonical (dedup) id — one hop, since canon_id is terminal."""
    track = s.get(Track, track_id)
    return (track.canon_id or track.id) if track is not None else track_id


def _next_position(s: Session, playlist_id: int) -> int:
    """The position one past the playlist's current last member (1 if empty)."""
    last = s.scalars(
        select(PlaylistTrack.position)
        .where(PlaylistTrack.playlist_id == playlist_id)
        .order_by(PlaylistTrack.position.desc())
    ).first()
    return (last + 1) if last is not None else 1


def merge_playlists(s: Session, base: Playlist, others: list[Playlist]) -> int:
    """Fold `others` into `base`, tombstone and delete each. Returns member rows appended to base.

    Others' members are canon-resolved and appended at the end in source order, skipping any whose
    canon already appears in base (reconcile decision #2: minimal, deterministic, idempotent diff —
    no reshuffle). Each absorbed playlist gets a `PlaylistMerge` tombstone so a later `land` won't
    recreate it. Deleting the absorbed `Playlist` cascades to its `PlaylistTrack`/`PlaylistLink`.
    """
    existing = {_canon(s, pt.track_id) for pt in base.tracks}
    next_pos = _next_position(s, base.id)
    appended = 0
    for other in others:
        for pt in sorted(other.tracks, key=lambda p: p.position):
            cid = _canon(s, pt.track_id)
            if cid in existing:
                continue
            s.add(PlaylistTrack(playlist_id=base.id, track_id=cid, position=next_pos))
            existing.add(cid)
            next_pos += 1
            appended += 1
        # Tombstone the absorbed source identity so unify_source_playlists won't recreate it.
        # source_id is the get_or_create key land uses; skip the (rare) idless playlist — there is
        # nothing for land to recreate by that key, so nothing to suppress.
        if other.source_id is not None:
            s.merge(
                PlaylistMerge(
                    provider=other.platform,
                    source_id=other.source_id,
                    surviving_playlist_id=base.id,
                )
            )
        s.delete(other)
    s.flush()
    return appended
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_playlist_edit.py -k merge -v`
Expected: PASS (both `test_merge_appends_...` and `test_merge_resolves_twins_to_canon`).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add airdrome/playlists/edit.py tests/test_playlist_edit.py
git commit -m "Add merge_playlists core: fold + tombstone + delete"
```

---

## Task 3: `dedup_members`

**Files:**
- Modify: `airdrome/playlists/edit.py`
- Test: `tests/test_playlist_edit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playlist_edit.py`:

```python
from airdrome.playlists.edit import dedup_members
from airdrome.playlists.sync import _three_way_merge


def test_dedup_members_collapses_canon_duplicates_keeping_earliest(session):
    """Rows resolving to the same canon collapse to the earliest position; others untouched."""
    canon = make_track(session, "canon")
    twin = make_track(session, "twin")
    twin.canon_id = canon.id
    other = make_track(session, "other")
    session.flush()
    pl = _pl(session, "P", [canon, other, twin], source_id="p-1")  # canon & twin share a canon

    removed = dedup_members(session, pl)

    assert removed == 1  # the twin row drops (canon kept, earliest position)
    assert _members(session, pl) == [canon.id, other.id]


def test_dedup_members_is_idempotent(session):
    """A second pass over an already-clean playlist removes nothing."""
    a, b = make_track(session, "a"), make_track(session, "b")
    pl = _pl(session, "P", [a, b], source_id="p-1")
    assert dedup_members(session, pl) == 0
    assert _members(session, pl) == [a.id, b.id]


def test_dedup_member_removal_sticks_against_reconcile_base(session):
    """A dedup deletion reads as an 'ours' removal vs a base that carried the dup — it stays gone.

    The base snapshot (last sync) also held the duplicate, so the multiset 3-way merge keeps the
    row removed rather than resurrecting it on the next `sync`. (Subtle interaction called out in
    the design — guarded directly against the merge primitive.)
    """
    base = [1, 1, 2]  # base + theirs both carried the duplicate 1
    ours = [1, 2]  # dedup_members dropped the redundant 1
    theirs = [1, 1, 2]
    assert _three_way_merge(base, ours, theirs) == [1, 2]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_playlist_edit.py -k dedup -v`
Expected: FAIL with `ImportError: cannot import name 'dedup_members'`.

- [ ] **Step 3: Add `dedup_members`**

Append to `airdrome/playlists/edit.py` (after `merge_playlists`):

```python
def dedup_members(s: Session, playlist: Playlist) -> int:
    """Collapse `PlaylistTrack` rows that resolve to the same canon. Returns rows removed.

    Keeps the earliest position per canon, drops the rest. Idempotent. Relative to a reconcile
    base that carried the duplicate, this reads as an "ours" deletion the multiset merge keeps
    removed — it is not resurrected on the next `sync`.
    """
    seen: set[int] = set()
    removed = 0
    for pt in sorted(playlist.tracks, key=lambda p: p.position):
        cid = _canon(s, pt.track_id)
        if cid in seen:
            s.delete(pt)
            removed += 1
        else:
            seen.add(cid)
    s.flush()
    return removed
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_playlist_edit.py -k dedup -v`
Expected: PASS (all three).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add airdrome/playlists/edit.py tests/test_playlist_edit.py
git commit -m "Add dedup_members: collapse canon-duplicate playlist rows"
```

---

## Task 4: Same-name grouping + `merge_same_name`

**Files:**
- Modify: `airdrome/playlists/edit.py`
- Test: `tests/test_playlist_edit.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_playlist_edit.py`:

```python
from airdrome.playlists.edit import group_by_name, merge_same_name


def test_group_by_name_groups_normalized_newest_first(session):
    """Only size>1 normalized-name groups are returned, newest date_modified anchoring first."""
    a, b = make_track(session, "a"), make_track(session, "b")
    older = _pl(session, "Mix", [a], source_id="g-1", modified=datetime(2020, 1, 1, tzinfo=UTC))
    newer = _pl(session, "  mix ", [b], source_id="g-2", modified=datetime(2024, 1, 1, tzinfo=UTC))
    _pl(session, "Solo", [a], source_id="g-3")  # singleton -> excluded

    groups = group_by_name([older, newer, session.get(Playlist, _pl(session, "Solo2", [b], source_id="g-4").id)])

    assert len(groups) == 1
    assert groups[0][0].id == newer.id  # newest anchors the group


def test_merge_same_name_collapses_into_newest(session):
    """merge_same_name folds older same-name playlists into the newest and tombstones them."""
    a, b = make_track(session, "a"), make_track(session, "b")
    older = _pl(session, "Mix", [a], source_id="g-1", modified=datetime(2020, 1, 1, tzinfo=UTC))
    newer = _pl(session, "mix", [b], source_id="g-2", modified=datetime(2024, 1, 1, tzinfo=UTC))

    merged_groups, appended = merge_same_name(session)

    assert merged_groups == 1
    assert appended == 1  # a folds into newer
    assert session.get(Playlist, older.id) is None
    assert set(_members(session, newer)) == {a.id, b.id}
    assert session.get(PlaylistMerge, (Source.APPLE_MS, "g-1")) is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_playlist_edit.py -k "group_by_name or same_name" -v`
Expected: FAIL with `ImportError: cannot import name 'group_by_name'`.

- [ ] **Step 3: Add grouping + sweep**

Append to `airdrome/playlists/edit.py`:

```python
def group_by_name(playlists: list[Playlist]) -> list[list[Playlist]]:
    """Group playlists by normalized name; return only size>1 groups, newest date_modified first.

    The one auto-groupable case (design decision #3): exact normalized-name match. Near-duplicate
    names ("calm" vs "calm 1") can't be grouped with confidence and need the explicit verb.
    """
    by_name: dict[str, list[Playlist]] = defaultdict(list)
    for pl in playlists:
        by_name[normalize_name(pl.name)].append(pl)
    groups: list[list[Playlist]] = []
    for members in by_name.values():
        if len(members) > 1:
            # Newest date_modified first (it anchors as base); NULL dates sort last.
            members.sort(
                key=lambda p: (
                    p.date_modified is None,
                    -p.date_modified.timestamp() if p.date_modified else 0,
                )
            )
            groups.append(members)
    return groups


def merge_same_name(s: Session) -> tuple[int, int]:
    """Auto-group canonical playlists by normalized name and merge each group into its newest.

    Returns ``(groups_merged, members_appended)``. Replaces the old land --merge-playlists sweep;
    tombstones keep every collapse durable across future imports.
    """
    playlists = list(s.scalars(select(Playlist)))
    groups_merged = members_appended = 0
    for group in group_by_name(playlists):
        base, others = group[0], group[1:]
        members_appended += merge_playlists(s, base, others)
        groups_merged += 1
    return groups_merged, members_appended
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_playlist_edit.py -k "group_by_name or same_name" -v`
Expected: PASS (both).

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add airdrome/playlists/edit.py tests/test_playlist_edit.py
git commit -m "Add --same-name grouping + merge_same_name sweep"
```

---

## Task 5: Tombstone consult in `unify_source_playlists`

**Files:**
- Modify: `airdrome/library/unify.py` (`_unify_per_source`, `unify.py:234-272`)
- Test: `tests/test_apple_ingest.py`

- [ ] **Step 1: Write the failing test**

In `tests/test_apple_ingest.py`, the imports at the top already pull `Playlist`, `PlaylistTrack`, `unify_source_tracks`, `unify_source_playlists`, `do_import_tracks`, `do_import_playlists`, `_track_data`, `_playlist_data`, `_canonical_track_id` (used by the existing tests in the file). Add `PlaylistMerge` to the `airdrome.models` import line, and `Source` to the `airdrome.enums` import if not present. Then append this test:

```python
def test_unify_skips_tombstoned_source_playlist(session):
    """A tombstoned (provider, source_id) is never recreated as a canonical playlist by land."""
    a = _track_data(name="A")
    do_import_tracks(session, {str(a["Track ID"]): a})
    unify_source_tracks(session)
    pl_data = _playlist_data(name="Gone", track_ids=[a["Track ID"]])
    do_import_playlists(session, [pl_data])

    # Pre-seed a tombstone for this source playlist's identity.
    src_pl = session.scalars(select(SourcePlaylist).where(SourcePlaylist.name == "Gone")).one()
    session.add(
        PlaylistMerge(provider=src_pl.provider, source_id=src_pl.source_id, surviving_playlist_id=None)
    )
    session.flush()

    pl_created, _ = unify_source_playlists(session)

    assert pl_created == 0
    assert session.scalars(select(Playlist).where(Playlist.name == "Gone")).one_or_none() is None
```

If `SourcePlaylist` is not already imported in this test file, add it to the existing `from airdrome.cloud.sources import ...` line (check the file head — the existing tests reference source rows).

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_apple_ingest.py::test_unify_skips_tombstoned_source_playlist -v`
Expected: FAIL — a "Gone" canonical playlist is still created (`pl_created == 1`).

- [ ] **Step 3: Consult tombstones in `_unify_per_source`**

In `airdrome/library/unify.py`, add `PlaylistMerge` to the model import (`unify.py:49`):

```python
from airdrome.models import AwareDatetime, Playlist, PlaylistLink, PlaylistMerge, PlaylistTrack, Track, TrackFile
```

Then in `_unify_per_source`, load the tombstone set once and skip tombstoned identities. Replace the function body's opening (currently `playlists_created = tracks_linked = 0` then `for src in sources:`) with:

```python
    playlists_created = tracks_linked = 0
    # Identities a `playlists merge` absorbed: skip recreating them, or the merge is undone.
    tombstoned = set(s.execute(select(PlaylistMerge.provider, PlaylistMerge.source_id)).all())
    for src in sources:
        if (src.platform, src.source_id) in tombstoned:
            if progress is not None:
                progress.update(task, advance=1)
            continue
        if not src.track_ids:
            if progress is not None:
                progress.update(task, advance=1)
            continue
```

(`select` is already imported in `unify.py:43`.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_apple_ingest.py::test_unify_skips_tombstoned_source_playlist -v`
Expected: PASS.

- [ ] **Step 5: Lint + commit**

```bash
ruff check . && ruff format .
git add airdrome/library/unify.py tests/test_apple_ingest.py
git commit -m "Honor PlaylistMerge tombstones in unify_source_playlists"
```

---

## Task 6: Remove the legacy `--merge-playlists` path

Design decision #2: `land --merge-playlists` is removed; same-name collapse now lives in `playlists merge --same-name` (Task 4). This deletes the `merge_by_name` branch and its arg threading.

**Files:**
- Modify: `airdrome/library/unify.py` (`unify_source_playlists` `unify.py:275-359`; `do_unify` `unify.py:434-473`)
- Modify: `airdrome/terminal/app.py` (`land` `app.py:147-181`)
- Modify: `tests/test_apple_ingest.py` (delete obsolete test)

- [ ] **Step 1: Delete the obsolete test**

In `tests/test_apple_ingest.py`, delete the entire `test_unify_playlists_merge_by_name_collapses` function (the block at `test_apple_ingest.py:355-378`). Its behavior moved to `test_merge_same_name_collapses_into_newest` (Task 4). Keep `test_unify_playlists_default_keeps_same_name_separate`.

- [ ] **Step 2: Simplify `unify_source_playlists`**

In `airdrome/library/unify.py`, replace the entire `unify_source_playlists` function (`unify.py:275-359`) with the no-merge-only version:

```python
def unify_source_playlists(
    s: Session,
    progress: Progress | None = None,
    task: TaskID | None = None,
) -> tuple[int, int]:
    """Create canonical Playlists from source playlists. Returns ``(playlists_created, tracks_linked)``.

    Each source playlist becomes its own canonical keyed on (platform, source_id); same-name
    playlists coexist. land only *seeds* membership on creation — existing playlists are owned by
    `sync`, and tombstoned identities (absorbed by `playlists merge`) are skipped. Same-name
    collapse is no longer done here; use `playlists merge --same-name`.
    """
    sources = _gather_source_playlists(s)
    return _unify_per_source(s, sources, progress, task)
```

- [ ] **Step 3: Drop `merge_playlists` from `do_unify`**

In `airdrome/library/unify.py`, change the `do_unify` signature and its `unify_source_playlists` call. Replace:

```python
def do_unify(s: Session, *, merge_playlists: bool = False, rebuild_playlists: bool = False):
```

with:

```python
def do_unify(s: Session, *, rebuild_playlists: bool = False):
```

In the same function, remove the `merge_playlists` paragraph from the docstring, and change the playlist-stage call:

```python
        pl_created, tr_linked = unify_source_playlists(s, progress, task, merge_by_name=merge_playlists)
```

to:

```python
        pl_created, tr_linked = unify_source_playlists(s, progress, task)
```

- [ ] **Step 4: Drop the `--merge-playlists` option from `land`**

In `airdrome/terminal/app.py`, in the `land` command (`app.py:147-181`), delete the `merge_playlists` option block:

```python
    merge_playlists: bool = typer.Option(
        False,
        "--merge-playlists",
        "-m",
        help="Merge same-name playlists into one canonical (newest anchors, duplicate tracks skipped).",
    ),
```

and change the `do_unify` call from:

```python
    do_unify(state.session, merge_playlists=merge_playlists, rebuild_playlists=rebuild_playlists)
```

to:

```python
    do_unify(state.session, rebuild_playlists=rebuild_playlists)
```

- [ ] **Step 5: Run tests to verify nothing regressed**

Run: `uv run pytest tests/test_apple_ingest.py -q`
Expected: PASS (the `merge_by_name` test is gone; `default_keeps_same_name_separate` and the tombstone-skip test pass).

Run: `uv run python -c "from airdrome.terminal.app import app"` to confirm the CLI module still imports cleanly.
Expected: no output, exit 0.

- [ ] **Step 6: Lint + commit**

```bash
ruff check . && ruff format .
git add airdrome/library/unify.py airdrome/terminal/app.py tests/test_apple_ingest.py
git commit -m "Remove legacy land --merge-playlists path (moved to playlists merge --same-name)"
```

---

## Task 7: `playlists` CLI group

**Files:**
- Create: `airdrome/terminal/playlists.py`
- Modify: `airdrome/terminal/app.py` (register group + help-callback stack)
- Test: `tests/test_cli_defaults.py`

- [ ] **Step 1: Write the failing CLI tests**

Append to `tests/test_cli_defaults.py`:

```python
def test_playlists_merge_same_name_calls_sweep(stub_session, monkeypatch):
    """`playlists merge --same-name` runs the same-name sweep, not the explicit merge."""
    sweep = MagicMock(return_value=(0, 0))
    monkeypatch.setattr("airdrome.terminal.playlists.merge_same_name", sweep)

    result = runner.invoke(app, ["playlists", "merge", "--same-name"])
    assert result.exit_code == 0
    sweep.assert_called_once()


def test_playlists_merge_requires_base_and_other(stub_session):
    """Explicit `merge` with no base/other exits non-zero with guidance."""
    result = runner.invoke(app, ["playlists", "merge"])
    assert result.exit_code != 0


def test_playlists_dedup_members_runs(stub_session, monkeypatch):
    """`playlists dedup-members` with no name sweeps all playlists via the engine."""
    monkeypatch.setattr(
        "airdrome.terminal.playlists._all_playlists", lambda s: []
    )
    spy = MagicMock(return_value=0)
    monkeypatch.setattr("airdrome.terminal.playlists.dedup_members", spy)

    result = runner.invoke(app, ["playlists", "dedup-members"])
    assert result.exit_code == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_cli_defaults.py -k playlists -v`
Expected: FAIL — `playlists` is not a registered command (exit code 2 / "No such command").

- [ ] **Step 3: Write the CLI group**

Create `airdrome/terminal/playlists.py`:

```python
"""The `playlists` group: on-demand canonical-hub editing verbs.

Siblings of `sync`/`navi`/`maint`. `merge` folds human-specified near-duplicate playlists into
one (durable via PlaylistMerge tombstones); `merge --same-name` auto-groups by normalized name
(newest anchors). `dedup-members` collapses canon-duplicate member rows. All are pure canonical
edits — `sync` carries them outward. See docs/design/playlist-tools.md.
"""

import typer
from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.console import console, done
from airdrome.models import Playlist
from airdrome.playlists.edit import dedup_members, merge_playlists, merge_same_name

from .options import DRY_RUN
from .state import AppState


playlists_app = typer.Typer(help="Edit canonical playlists (merge, dedup members); sync propagates them.")


def _all_playlists(s: Session) -> list[Playlist]:
    """Every canonical playlist, ordered by name."""
    return list(s.scalars(select(Playlist).order_by(Playlist.name)))


def _resolve(s: Session, token: str) -> Playlist:
    """Resolve a name or `#<id>` token to exactly one Playlist; exit with guidance otherwise."""
    if token.startswith("#"):
        try:
            pid = int(token[1:])
        except ValueError as exc:
            raise typer.BadParameter(f"Bad playlist id: {token!r}") from exc
        pl = s.get(Playlist, pid)
        if pl is None:
            console.print(f"[red]No playlist with id {pid}.[/red]")
            raise typer.Exit(1)
        return pl
    matches = list(s.scalars(select(Playlist).where(Playlist.name == token)))
    if not matches:
        console.print(f"[red]No playlist named {token!r}.[/red]")
        raise typer.Exit(1)
    if len(matches) > 1:
        console.print(f"[yellow]{token!r} matches {len(matches)} playlists — address one by id:[/yellow]")
        for pl in matches:
            console.print(f"  #{pl.id}  {pl.name}  [dim]({pl.platform.value} {pl.source_id})[/dim]")
        raise typer.Exit(1)
    return matches[0]


@playlists_app.command("merge")
def merge(
    ctx: typer.Context,
    base: str = typer.Argument(None, help="Base playlist (name or #id) others fold into."),
    others: list[str] = typer.Argument(None, help="Playlists (name or #id) to absorb into base."),
    same_name: bool = typer.Option(
        False, "--same-name", help="Ignore args; auto-group all playlists by name (newest anchors)."
    ),
    dry_run: bool = DRY_RUN,
):
    """Fold near-duplicate playlists into one. Explicit (base + others) or --same-name sweep."""
    state: AppState = ctx.obj
    state.dry_run = dry_run
    s = state.session

    if same_name:
        groups, appended = merge_same_name(s)
        done(f"Merged {groups} same-name group(s); appended {appended} track(s)")
        return

    if not base or not others:
        console.print("[red]merge needs a base and at least one other playlist (or --same-name).[/red]")
        raise typer.Exit(1)

    base_pl = _resolve(s, base)
    other_pls = [_resolve(s, tok) for tok in others]
    appended = merge_playlists(s, base_pl, other_pls)
    done(f"Folded {len(other_pls)} playlist(s) into {base_pl.name!r}; appended {appended} track(s)")


@playlists_app.command("dedup-members")
def dedup_members_cmd(
    ctx: typer.Context,
    names: list[str] = typer.Argument(None, help="Playlists (name or #id); all playlists if omitted."),
    dry_run: bool = DRY_RUN,
):
    """Collapse member rows that resolve to the same canon (keep earliest position)."""
    state: AppState = ctx.obj
    state.dry_run = dry_run
    s = state.session

    targets = [_resolve(s, tok) for tok in names] if names else _all_playlists(s)
    removed = sum(dedup_members(s, pl) for pl in targets)
    done(f"Removed {removed} duplicate member row(s) across {len(targets)} playlist(s)")
```

- [ ] **Step 4: Register the group in `app.py`**

In `airdrome/terminal/app.py`:

Add the import near the other group imports (after `from .sync import sync_app`, `app.py:21`):

```python
from .playlists import playlists_app
```

Register it next to the others (after `app.add_typer(sync_app, name="sync")`, `app.py:41`):

```python
app.add_typer(playlists_app, name="playlists")
```

Add it to the help-callback decorator stack so a bare `playlists` prints help. Change the stacked decorator block (`app.py:184-185`):

```python
@navi_app.callback(invoke_without_command=True)
@maint_app.callback(invoke_without_command=True)
def sub_callback(ctx: typer.Context):
```

to:

```python
@navi_app.callback(invoke_without_command=True)
@maint_app.callback(invoke_without_command=True)
@playlists_app.callback(invoke_without_command=True)
def sub_callback(ctx: typer.Context):
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_cli_defaults.py -k playlists -v`
Expected: PASS (all three).

Run: `uv run python -m airdrome.terminal.app playlists --help` — Expected: shows `merge` and `dedup-members` subcommands.

- [ ] **Step 6: Lint + commit**

```bash
ruff check . && ruff format .
git add airdrome/terminal/playlists.py airdrome/terminal/app.py tests/test_cli_defaults.py
git commit -m "Add playlists CLI group: merge + dedup-members"
```

---

## Task 8: Documentation

**Files:**
- Modify: `AGENTS.md` (CLI surface + Playlist reconcile sections)
- Modify: `ROADMAP.md` (remove the shipped item, update the merge follow-up note)
- Modify: `docs/design/playlist-tools.md` (mark built)

- [ ] **Step 1: Update AGENTS.md CLI surface**

In `AGENTS.md`, in the **CLI surface** group sentence (`AGENTS.md:54-55`), add `playlists` to the list of groups. Change "three groups hold the rest — `sync` … `navi` … and `maint`" to four groups including `playlists` (playlist editing). Update the `land` bullet (`AGENTS.md:68-70`) to drop the now-removed `-m/--merge-playlists` flag. Add a new bullet after the `sync` bullet:

```markdown
- **`playlists merge` / `playlists dedup-members`** — on-demand canonical-hub edits that `sync`
  then carries outward. `merge <base> <other>...` folds others into base (canon-resolved, appended,
  deduped) and tombstones them so `land` won't recreate them; `merge --same-name` auto-groups by
  normalized name (newest anchors). `dedup-members [<name>...]` collapses canon-duplicate member
  rows (all playlists if no name). Name args resolve to one playlist; `#<id>` addresses unambiguously.
```

- [ ] **Step 2: Update AGENTS.md Playlist reconcile section**

In the **Playlist reconcile** section, under the "`land` seeds, `sync` updates" bullet (`AGENTS.md:197-199`), add a note that `unify_source_playlists` also skips identities tombstoned by `playlists merge` (the `PlaylistMerge` table). Add `PlaylistMerge` to the **Key models** list with a one-line description (tombstone: absorbed source identity suppressed from recreation).

- [ ] **Step 3: Update ROADMAP.md**

In `ROADMAP.md`, remove the now-shipped **"Playlist editing tools (`merge` + `dedup-members`)"** bullet from the `## Now` section (`ROADMAP.md:35-41`). Keep the **"Backend orphan cleanup after merge"** follow-up bullet (`ROADMAP.md:66-70`) — it is still unbuilt — but verify its wording still matches reality. Under **Complementary editing tools**, update the "More playlist tools" bullet (`ROADMAP.md:83-85`) so `merge` + `dedup-members` are listed as shipped, leaving rename/split/reorder as the remaining ideas.

- [ ] **Step 4: Mark the design built**

In `docs/design/playlist-tools.md`, change the **Status** line (`playlist-tools.md:3`) from `🧭 designed 2026-06-09, not built` to `✅ built 2026-06-16` (keep the rest of the doc as the rationale archive).

- [ ] **Step 5: Final full-suite verification**

Run: `uv run pytest -q`
Expected: all tests pass.

Run: `ruff check . && ruff format --check .`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add AGENTS.md ROADMAP.md docs/design/playlist-tools.md
git commit -m "Document shipped playlist editing tools; mark design built"
```

---

## Self-Review notes (for the executor)

- **Spec coverage:** Task 2 = merge core (CLI surface line 1, "merge core — one function"); Task 4 = `--same-name` (CLI line 3, decision #3); Task 3 = `dedup-members` + the reconcile-base test the design *requires*; Task 1 + Task 5 = tombstone table + the land-skip durability (decision #4, "Tombstone = skip"); Task 6 = decision #2 (`--merge-playlists` removed); Task 7 = CLI group + name/`#id` resolution; Task 8 = docs.
- **Type consistency:** `_canon`, `_next_position`, `merge_playlists`, `dedup_members`, `group_by_name`, `merge_same_name` are defined once in `edit.py` and imported by name everywhere. `PlaylistMerge` PK is `(provider, source_id)` — `session.get(PlaylistMerge, (Source.X, "id"))` is used consistently in tests.
- **Backend orphan cleanup** (absorbed playlist's Navidrome counterpart left stale) is explicitly **out of scope** — it stays in ROADMAP as a follow-up (design *Follow-up* section).
- **Not handled by design:** `--rebuild-playlists` drops canonical playlists and reseeds from source; absorbed-source tracks folded by a prior merge are not re-folded (rebuild is destructive by intent). The tombstone still suppresses recreation of absorbed identities. No task changes this.
