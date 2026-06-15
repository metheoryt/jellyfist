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
    recreate it. Deleting the absorbed `Playlist` cascades to its `PlaylistTrack` rows (ORM
    relationship) and its `PlaylistLink` rows (DB-level ON DELETE CASCADE FK).
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
