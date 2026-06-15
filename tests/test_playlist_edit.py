"""Tests for the on-demand playlist editing verbs (merge, dedup-members) and tombstones.

These edit only the canonical Playlist/PlaylistTrack graph; `sync` propagates the result.
The PlaylistMerge tombstone makes a merge durable across a later `land`.
"""

from sqlalchemy import select

from airdrome.enums import Source
from airdrome.models import Playlist, PlaylistMerge, PlaylistTrack
from airdrome.playlists.edit import merge_playlists

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
