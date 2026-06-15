"""Tests for the on-demand playlist editing verbs (merge, dedup-members) and tombstones.

These edit only the canonical Playlist/PlaylistTrack graph; `sync` propagates the result.
The PlaylistMerge tombstone makes a merge durable across a later `land`.
"""

from datetime import UTC, datetime

from sqlalchemy import select

from airdrome.enums import Source
from airdrome.models import Playlist, PlaylistMerge, PlaylistTrack
from airdrome.playlists.edit import dedup_members, group_by_name, merge_playlists, merge_same_name
from airdrome.playlists.sync import _three_way_merge

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
    session.add(PlaylistMerge(provider=Source.APPLE_MS, source_id="other-1", surviving_playlist_id=base.id))
    session.flush()
    row = session.get(PlaylistMerge, (Source.APPLE_MS, "other-1"))
    assert row is not None
    assert row.surviving_playlist_id == base.id


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


def test_merge_multiple_others_dedups_across_them_with_contiguous_positions(session):
    """Folding several others keeps positions gap-free and dedups a track shared between them."""
    a, b, c = make_track(session, "a"), make_track(session, "b"), make_track(session, "c")
    base = _pl(session, "Base", [a], source_id="base-1")
    other1 = _pl(session, "One", [b, c], source_id="one-1")
    other2 = _pl(session, "Two", [c], source_id="two-1")  # c is shared with other1

    appended = merge_playlists(session, base, [other1, other2])

    assert appended == 2  # b and c once each; c not re-added from other2
    assert _members(session, base) == [a.id, b.id, c.id]  # contiguous, source order preserved
    assert session.get(Playlist, other1.id) is None
    assert session.get(Playlist, other2.id) is None


def test_merge_idless_other_is_deleted_without_a_tombstone(session):
    """An absorbed playlist with no source_id folds in and is deleted, but writes no tombstone."""
    a, b = make_track(session, "a"), make_track(session, "b")
    base = _pl(session, "Base", [a], source_id="base-1")
    other = _pl(session, "Other", [b], source_id="other-1")
    other.source_id = None  # idless: nothing for `land` to recreate, so nothing to tombstone
    session.flush()

    appended = merge_playlists(session, base, [other])

    assert appended == 1
    assert _members(session, base) == [a.id, b.id]
    assert session.get(Playlist, other.id) is None
    # No tombstone row exists for this provider with a null source_id.
    rows = session.scalars(select(PlaylistMerge).where(PlaylistMerge.surviving_playlist_id == base.id)).all()
    assert rows == []


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


def test_group_by_name_groups_normalized_newest_first(session):
    """Only size>1 normalized-name groups are returned, newest date_modified anchoring first."""
    a, b = make_track(session, "a"), make_track(session, "b")
    older = _pl(session, "Mix", [a], source_id="g-1", modified=datetime(2020, 1, 1, tzinfo=UTC))
    newer = _pl(session, "  mix ", [b], source_id="g-2", modified=datetime(2024, 1, 1, tzinfo=UTC))
    solo = _pl(session, "Solo", [a], source_id="g-3")  # singleton -> excluded

    groups = group_by_name([older, newer, solo])

    assert len(groups) == 1  # only the "mix" group (size 2); Solo excluded
    assert [pl.id for pl in groups[0]] == [newer.id, older.id]  # newest anchors first


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
