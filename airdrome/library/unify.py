from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from rich.progress import BarColumn, MofNCompleteColumn, Progress, TaskID, TextColumn, TimeElapsedColumn
from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from airdrome.cloud.sources import SourcePlaylist, SourceTrack
from airdrome.console import console
from airdrome.enums import Source
from airdrome.models import AwareDatetime, Playlist, PlaylistTrack, Track, TrackFile


def _bind_track_files(source_track: SourceTrack, s: Session) -> list[TrackFile]:
    tfs = []
    for rel_path in source_track.possible_locations(max_suffix=2):
        # Case-insensitive: the on-disk path's casing can differ from the casing
        # generate_path() derives from source metadata (ILIKE, not case-sensitive LIKE).
        tf: TrackFile | None = s.scalars(
            select(TrackFile).where(TrackFile.source_path.icontains(rel_path))
        ).one_or_none()
        if tf and tf.track_id is None:
            tfs.append(tf)
    return tfs


def _unify_source_tracks(s: Session) -> Iterator[tuple[bool, bool, int]]:
    for st in s.scalars(select(SourceTrack).where(SourceTrack.track_id.is_(None))):
        st: SourceTrack
        track_defaults = {
            "track_n": st.track_number,
            "disc_n": st.disc_number,
            "compilation": st.compilation,
            "year": st.year,
            "duration": round(st.duration_ms / 1000) if st.duration_ms else None,
            "loved": st.loved or None,
            "album_loved": st.album_loved or None,
            "rating": st.rating if not st.rating_computed else None,
            "album_rating": st.album_rating if not st.album_rating_computed else None,
            "date_added": st.date_added,
        }
        track, track_created = Track.get_or_create(
            s,
            title=st.title,
            artist=st.artist,
            album=st.album,
            album_artist=st.album_artist,
            defaults=track_defaults,
        )
        track_updated = not track_created and track.fill_nulls(track_defaults)
        st.track = track

        # Rely on FS discovery for everyone; the flag only tells us whether to complain on a miss.
        tfs = _bind_track_files(st, s)
        for tf in tfs:
            track.files.append(tf)
        n_files = len(tfs)
        if not tfs and st.expects_local_file:
            console.print(f"[dim yellow]expected but not found: {st.possible_locations()[0]!r}[/dim yellow]")

        s.flush()
        yield track_created, track_updated, n_files


def unify_source_tracks(
    s: Session, progress: Progress | None = None, task: TaskID | None = None
) -> tuple[int, int, int]:
    """
    Create canonical Track records from SourceTrack data,
    then bind matching TrackFile records via possible_locations() DB lookup.
    Returns (created, updated, files_bound) Track counts.
    """
    created = updated = files_bound = 0
    for was_created, was_updated, n_files in _unify_source_tracks(s):
        created += was_created
        updated += was_updated
        files_bound += n_files
        if progress is not None:
            progress.update(task, advance=1, created=created, updated=updated, files_bound=files_bound)
    return created, updated, files_bound


@dataclass
class _SourcePlaylist:
    name: str
    date_modified: AwareDatetime | None
    date_added: AwareDatetime | None
    description: str | None
    platform: Source
    source_id: str
    track_ids: list[int]


def _gather_source_playlists(s: Session) -> list[_SourcePlaylist]:
    result = []
    stmt = select(SourcePlaylist).where(~SourcePlaylist.folder)
    for pl in s.scalars(stmt):
        track_dates = [m.track.date_added for m in pl.members if m.track.date_added is not None]
        track_ids = [
            m.track.track_id
            for m in sorted(pl.members, key=lambda m: m.position)
            if m.track.track_id is not None
        ]
        result.append(
            _SourcePlaylist(
                name=pl.name,
                # XML playlists carry no own dates → derive from members; MS supplies its own.
                date_modified=pl.date_modified or (max(track_dates) if track_dates else None),
                date_added=pl.date_added or (min(track_dates) if track_dates else None),
                description=pl.description or None,
                platform=pl.provider,
                source_id=pl.source_id,
                track_ids=track_ids,
            )
        )
    return result


def unify_source_playlists(
    s: Session, progress: Progress | None = None, task: TaskID | None = None
) -> tuple[int, int]:
    """
    Create deduplicated canonical Playlist records from all source playlist data.
    Processes newest-to-oldest by date_modified; same-name playlists merge (unique tracks
    appended); playlists whose track set duplicates an existing canonical are skipped.
    Returns (playlists_created, tracks_linked).
    """
    existing = list(s.scalars(select(Playlist)))
    name_to_canonical: dict[str, Playlist] = {pl.name: pl for pl in existing}

    # Mutable per-canonical track-ID sets; updated in-place as we merge
    canonical_track_ids: dict[int, set[int]] = {
        pl.id: {
            pt.track_id for pt in s.scalars(select(PlaylistTrack).where(PlaylistTrack.playlist_id == pl.id))
        }
        for pl in existing
    }

    sources = _gather_source_playlists(s)
    # Newest date_modified first; nulls sorted last
    sources.sort(
        key=lambda p: (p.date_modified is None, -p.date_modified.timestamp() if p.date_modified else 0)
    )

    playlists_created = tracks_linked = 0

    for src in sources:
        if not src.track_ids:
            if progress is not None:
                progress.update(task, advance=1)
            continue

        if src.name in name_to_canonical:
            canonical = name_to_canonical[src.name]
            existing_ids = canonical_track_ids[canonical.id]

            max_pos_row = s.scalars(
                select(PlaylistTrack)
                .where(PlaylistTrack.playlist_id == canonical.id)
                .order_by(PlaylistTrack.position.desc())
            ).first()
            next_pos = (max_pos_row.position + 1) if max_pos_row else 1

            for track_id in src.track_ids:
                if track_id not in existing_ids:
                    s.add(PlaylistTrack(playlist_id=canonical.id, track_id=track_id, position=next_pos))
                    existing_ids.add(track_id)
                    next_pos += 1
                    tracks_linked += 1

        else:
            src_track_set = frozenset(src.track_ids)
            if any(src_track_set == frozenset(ids) for ids in canonical_track_ids.values()):
                if progress is not None:
                    progress.update(task, advance=1)
                continue

            canonical = Playlist(
                name=src.name,
                platform=src.platform,
                source_id=src.source_id,
                description=src.description,
                date_added=src.date_added,
                date_modified=src.date_modified,
            )
            s.add(canonical)
            s.flush()

            name_to_canonical[src.name] = canonical
            canonical_track_ids[canonical.id] = set(src.track_ids)
            playlists_created += 1

            for pos, track_id in enumerate(src.track_ids, start=1):
                s.add(PlaylistTrack(playlist_id=canonical.id, track_id=track_id, position=pos))
                tracks_linked += 1

        s.flush()
        if progress is not None:
            progress.update(task, advance=1, pl_created=playlists_created, tr_linked=tracks_linked)

    return playlists_created, tracks_linked


def _unify_orphan_files(s: Session, progress: Progress, task: TaskID) -> tuple[int, int]:
    created = updated = 0
    stmt = select(TrackFile).where(TrackFile.track_id.is_(None), TrackFile.title.is_not(None))
    for tf in s.scalars(stmt):
        year = None
        if tf.date:
            try:
                year = int(tf.date[:4])
            except ValueError, IndexError:
                pass
        track_defaults = {
            "duration": round(tf.duration) if tf.duration else None,
            "year": year,
        }
        try:
            st = tf.source_path.stat()
            # st_ctime is creation on Windows / inode-change on Linux; mtime is content edit.
            # min() yields the oldest known timestamp for the file across platforms.
            track_defaults["date_added"] = datetime.fromtimestamp(
                min(st.st_ctime, st.st_mtime), tz=timezone.utc
            )
        except OSError:
            pass
        track, track_created = Track.get_or_create(
            s,
            title=tf.title,
            artist=tf.artist,
            album=tf.album,
            album_artist=tf.album_artist,
            defaults=track_defaults,
        )
        if track_created:
            created += 1
        elif track.fill_nulls(track_defaults):
            updated += 1

        tf.track = track
        s.flush()
        progress.update(task, advance=1, created=created, updated=updated)

    return created, updated


def do_unify(s: Session, reset_playlists: bool = False):
    if reset_playlists:
        s.execute(delete(Playlist))
        s.flush()
        console.print("[yellow]Canonical playlists reset[/yellow]")

    track_count = s.scalars(
        select(func.count()).select_from(SourceTrack).where(SourceTrack.track_id.is_(None))
    ).one()
    pl_count = s.scalars(select(func.count()).select_from(SourcePlaylist).where(~SourcePlaylist.folder)).one()
    orphan_count = s.scalars(
        select(func.count())
        .select_from(TrackFile)
        .where(TrackFile.track_id.is_(None), TrackFile.title.is_not(None))
    ).one()

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[green]{task.fields[created]} new[/green]  "
            "[yellow]{task.fields[updated]} updated[/yellow]  "
            "[cyan]{task.fields[files_bound]} files bound[/cyan]"
        ),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Tracks",
            total=track_count,
            created=0,
            updated=0,
            files_bound=0,
        )
        created, updated, files_bound = unify_source_tracks(s, progress, task)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[magenta]{task.fields[pl_created]} playlists[/magenta]  "
            "[blue]{task.fields[tr_linked]} linked[/blue]"
        ),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task(
            "Playlists",
            total=pl_count,
            pl_created=0,
            tr_linked=0,
        )
        pl_created, tr_linked = unify_source_playlists(s, progress, task)

    with Progress(
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn(
            "[green]{task.fields[created]} new[/green]  [yellow]{task.fields[updated]} updated[/yellow]"
        ),
        TimeElapsedColumn(),
        console=console,
    ) as progress:
        task = progress.add_task("Orphan files", total=orphan_count, created=0, updated=0)
        orphan_created, orphan_updated = _unify_orphan_files(s, progress, task)

    console.print(
        f"  Tracks: [green]{created} new[/green]  [yellow]{updated} updated[/yellow]  "
        f"[cyan]{files_bound} files bound[/cyan]\n"
        f"  Playlists: [magenta]{pl_created} new[/magenta]  [blue]{tr_linked} tracks linked[/blue]\n"
        f"  Orphan files: [green]{orphan_created} new[/green]  [yellow]{orphan_updated} updated[/yellow]"
    )
