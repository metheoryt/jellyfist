"""On-demand canonical-hub playlist edits: merge near-duplicates, dedup canon-duplicate members.

These verbs edit only the canonical `Playlist`/`PlaylistTrack` graph (and `PlaylistMerge`
tombstones); they never talk to a remote. The existing `sync` then propagates the result outward
as ordinary "ours" changes. See docs/design/playlist-tools.md for the locked decisions.
"""

from sqlalchemy import select
from sqlalchemy.orm import Session

from airdrome.models import Playlist, PlaylistMerge, PlaylistTrack, Track


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
