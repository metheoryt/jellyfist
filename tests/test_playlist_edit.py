"""Tests for the on-demand playlist editing verbs (merge, dedup-members) and tombstones.

These edit only the canonical Playlist/PlaylistTrack graph; `sync` propagates the result.
The PlaylistMerge tombstone makes a merge durable across a later `land`.
"""

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
    session.add(PlaylistMerge(provider=Source.APPLE_MS, source_id="other-1", surviving_playlist_id=base.id))
    session.flush()
    row = session.get(PlaylistMerge, (Source.APPLE_MS, "other-1"))
    assert row is not None
    assert row.surviving_playlist_id == base.id
