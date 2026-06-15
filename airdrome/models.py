from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, TypeVar

import sqlalchemy as sa
from mutagen import File
from sqlalchemy import ForeignKey, Index, UniqueConstraint, create_engine, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, relationship, validates
from sqlalchemy.types import TypeDecorator

from .cloud.apple.utils import generate_path
from .conf import settings
from .enums import Source
from .library import MAIN_SUBDIR
from .normalize.norm import normalize_name


if TYPE_CHECKING:
    from airdrome.cloud.sources import SourceTrack


T = TypeVar("T", bound="Base")


AwareDatetime = Annotated[datetime, mapped_column(sa.DateTime(timezone=True))]

AwareDatetimeDefNow = Annotated[
    datetime,
    mapped_column(sa.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)),
]


class PathType(TypeDecorator):
    impl = sa.String
    cache_ok = True

    def process_bind_param(self, value: Path | str | None, dialect):
        if value is None:
            return None
        if isinstance(value, str):
            return value
        return value.as_posix()

    def process_result_value(self, value: str, dialect):
        if value is None:
            return None
        return Path(value)


class AirdromeBase(DeclarativeBase):
    pass


class Base(AirdromeBase):
    __abstract__ = True

    @classmethod
    def get_or_create(
        cls: type[T], session: Session, defaults: dict[str, Any] | None = None, **lookups: Any
    ) -> tuple[T, bool]:
        instance = session.scalars(select(cls).filter_by(**lookups)).one_or_none()

        if instance:
            return instance, False

        params = {**lookups, **(defaults or {})}
        instance = cls(**params)
        session.add(instance)
        session.flush([instance])
        return instance, True

    def fill_nulls(self, data: dict[str, Any]) -> bool:
        """Set any field in *data* where the current value is None. Returns True if any field changed."""
        changed = False
        for field, value in data.items():
            if value is not None and getattr(self, field, None) is None:
                setattr(self, field, value)
                changed = True
        return changed

    @classmethod
    def truncate_cascade(cls, session: Session):
        session.execute(text(f"TRUNCATE TABLE {cls.__tablename__} RESTART IDENTITY CASCADE;"))
        session.commit()


class Track(Base):
    """A representation of a single track in a library."""

    __tablename__ = "track"
    __table_args__ = (
        UniqueConstraint("title", "artist", "album", "album_artist"),
        # trigram indexes for matching
        Index(
            "ix_track_title_norm_trgm",
            "title_norm",
            postgresql_using="gin",
            postgresql_ops={"title_norm": "gin_trgm_ops"},
        ),
        Index(
            "ix_track_artist_norm_trgm",
            "artist_norm",
            postgresql_using="gin",
            postgresql_ops={"artist_norm": "gin_trgm_ops"},
            postgresql_where=text("artist_norm <> ''"),
        ),
        Index(
            "ix_track_album_norm_trgm",
            "album_norm",
            postgresql_using="gin",
            postgresql_ops={"album_norm": "gin_trgm_ops"},
            postgresql_where=text("album_norm <> ''"),
        ),
        Index(
            "ix_track_album_artist_norm_trgm",
            "album_artist_norm",
            postgresql_using="gin",
            postgresql_ops={"album_artist_norm": "gin_trgm_ops"},
            postgresql_where=text("album_artist_norm <> ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # basic metadata
    title: Mapped[str]
    artist: Mapped[str | None]
    album_artist: Mapped[str | None]
    album: Mapped[str | None]

    # anything we would need for other things
    track_n: Mapped[int | None]
    disc_n: Mapped[int | None]
    compilation: Mapped[bool | None]
    year: Mapped[int | None]
    duration: Mapped[int | None]
    """Duration in seconds."""
    date_added: Mapped[AwareDatetimeDefNow]
    loved: Mapped[bool | None]
    album_loved: Mapped[bool | None]
    rating: Mapped[int | None]
    album_rating: Mapped[int | None]

    title_norm: Mapped[str] = mapped_column(default="")
    artist_norm: Mapped[str] = mapped_column(default="")
    album_artist_norm: Mapped[str] = mapped_column(default="")
    album_norm: Mapped[str] = mapped_column(default="")

    # duplicates
    # Invariant: canon_id is terminal — a canon is never itself a twin, so
    # canon_id chains (A->B->C) never exist. Enforced at write time by
    # flatten_canon_chains(); all readers may resolve with a single hop.
    canon_id: Mapped[int | None] = mapped_column(ForeignKey("track.id", ondelete="SET NULL"), index=True)
    canon: Mapped[Track | None] = relationship(
        "Track",
        foreign_keys=[canon_id],
        back_populates="twins",
        remote_side=[id],
    )
    twins: Mapped[list[Track]] = relationship(
        "Track",
        foreign_keys=[canon_id],
        back_populates="canon",
    )

    # Other relations.
    # A Track can have multiple source records (across providers, or duplicate ids within
    #   a provider). This is rare and unrelated to deduplication.
    source_tracks: Mapped[list[SourceTrack]] = relationship(
        back_populates="track", cascade="all, delete-orphan"
    )
    aliases: Mapped[list[TrackAlias]] = relationship(back_populates="track", cascade="all, delete-orphan")
    files: Mapped[list[TrackFile]] = relationship(back_populates="track", cascade="all, delete-orphan")
    plays: Mapped[list[TrackPlay]] = relationship(back_populates="track", cascade="all, delete-orphan")
    playlist_memberships: Mapped[list[PlaylistTrack]] = relationship(
        back_populates="track", cascade="all, delete-orphan"
    )

    def __repr__(self):
        return f"<Track {self.title} by {self.artist} on {self.album}>"

    @validates("title", "artist", "album_artist", "album")
    def _populate_norm(self, key: str, value: Any) -> Any:
        setattr(self, f"{key}_norm", normalize_name(value))
        return value

    @property
    def table_row(self) -> tuple[str, str | None, str | None, str | None]:
        return self.title, self.artist, self.album_artist, self.album

    @property
    def duplicate_hash(self):
        """
        Generates a hash string for identifying potential duplicate tracks.

        Creates a composite hash by combining key metadata fields including title,
        artist, album artist, album, track number, disc number, year, and a bucketed
        duration value. The duration is rounded to the nearest 5-second interval to
        allow for minor variations. Fields are joined with semicolons, with None
        values represented as empty strings.

        Returns
        -------
        str
            A semicolon-delimited string containing the track's metadata fields,
            suitable for duplicate detection by comparing hash values between tracks.
        """
        duration_bucket = round(self.duration / 5) * 5 if self.duration is not None else None
        hash_fields = (
            self.title,
            self.artist,
            self.album_artist,
            self.album,
            self.track_n,
            self.disc_n,
            self.year,
            duration_bucket,
        )
        return ";".join(str(v) if v is not None else "" for v in hash_fields)

    @property
    def main_file(self) -> TrackFile | None:
        return next((t for t in self.files if t.is_main), None)

    @property
    def group(self) -> TrackGroup:
        """This track's dedup group, viewed through it as the main-file owner."""
        return TrackGroup.of(self)

    @property
    def path_artist(self):
        if self.compilation:
            return "Compilations"
        elif self.album_artist:
            return self.album_artist
        elif self.artist:
            return self.artist
        else:
            return "Unknown Artist"

    @property
    def path_album(self):
        return self.album or "Unknown Album"

    def generate_relative_path(self, ext: str, suffix: int = 0) -> Path:
        """Keep the main path consistent with Apple Library paths."""
        return generate_path(
            artist=self.path_artist,
            album=self.path_album,
            title=self.title,
            ext=ext,
            track_n=self.track_n,
            disc_n=self.disc_n,
            suffix=suffix,
        )


class TrackFile(Base):
    __tablename__ = "trackfile"
    __table_args__ = (
        # trigram indexes for matching
        Index(
            "ix_trackfile_title_norm_trgm",
            "title_norm",
            postgresql_using="gin",
            postgresql_ops={"title_norm": "gin_trgm_ops"},
        ),
        Index(
            "ix_trackfile_artist_norm_trgm",
            "artist_norm",
            postgresql_using="gin",
            postgresql_ops={"artist_norm": "gin_trgm_ops"},
            postgresql_where=text("artist_norm <> ''"),
        ),
        Index(
            "ix_trackfile_album_norm_trgm",
            "album_norm",
            postgresql_using="gin",
            postgresql_ops={"album_norm": "gin_trgm_ops"},
            postgresql_where=text("album_norm <> ''"),
        ),
        Index(
            "ix_trackfile_album_artist_norm_trgm",
            "album_artist_norm",
            postgresql_using="gin",
            postgresql_ops={"album_artist_norm": "gin_trgm_ops"},
            postgresql_where=text("album_artist_norm <> ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    # absolute path of the original file
    source_path: Mapped[Path] = mapped_column(PathType(), nullable=False, unique=True)
    # relative path of the file in the library (after organizing)
    library_path: Mapped[Path | None] = mapped_column(PathType(), nullable=True, unique=True)
    is_main: Mapped[bool] = mapped_column(default=False)

    track_id: Mapped[int | None] = mapped_column(ForeignKey("track.id", ondelete="CASCADE"), index=True)
    track: Mapped[Track | None] = relationship(back_populates="files")

    duration: Mapped[float | None]
    bitrate: Mapped[int | None]
    date: Mapped[str | None]

    title: Mapped[str | None]
    artist: Mapped[str | None]
    album_artist: Mapped[str | None]
    album: Mapped[str | None]

    title_norm: Mapped[str] = mapped_column(default="")
    artist_norm: Mapped[str] = mapped_column(default="")
    album_artist_norm: Mapped[str] = mapped_column(default="")
    album_norm: Mapped[str] = mapped_column(default="")

    @validates("title", "artist", "album_artist", "album")
    def _populate_norm(self, key: str, value: Any) -> Any:
        setattr(self, f"{key}_norm", normalize_name(value))
        return value

    @property
    def navidrome_path(self) -> str | None:
        """Path relative to the Navidrome root (Library/), used for MediaFile matching."""
        if self.library_path and self.library_path.parts[0] == MAIN_SUBDIR:
            return self.library_path.relative_to(MAIN_SUBDIR).as_posix()
        return None

    @property
    def duration_str(self):
        if self.duration is None:
            return ""
        d = int(self.duration)
        return f"{d // 60:02d}:{d % 60:02d}"

    @property
    def absolute_path(self) -> Path:
        """
        Returns the current usable absolute path of the file.
        Prefers the library_path (joined with settings) if it exists,
        otherwise falls back to source_path.
        """
        if self.library_path:
            return settings.library_dir / self.library_path
        return self.source_path

    def enrich(self):
        audio = File(self.absolute_path)
        if audio is None:
            raise ValueError("Unsupported or corrupted file")
        tags = audio.tags or {}

        def get(*keys):
            for k in keys:
                if k in tags:
                    v = tags[k]
                    return "; ".join(v) if isinstance(v, list) else str(v)
            return None

        self.artist = get("TPE1", "©ART")
        self.album = get("TALB", "©alb")
        self.album_artist = get("TPE2", "aART")
        self.title = get("TIT2", "©nam")
        self.date = get("TDRC", "TDOR", "©day")
        self.duration = audio.info.length
        self.bitrate = getattr(audio.info, "bitrate", 0)


@dataclass(frozen=True)
class TrackGroup:
    """A dedup group viewed through its organized main file.

    `main` owns the `is_main` TrackFile (the entry point when syncing) and need
    not be the canon; `canon` is the group root. Plays and ratings aggregate
    across `members` (canon + twins), while the synced MediaFile is keyed off
    `main`. canon_id is terminal (flatten_canon_chains), so one hop reaches the
    root.
    """

    main: Track  # owner of the is_main file
    canon: Track  # group root
    members: list[Track]  # [canon, *canon.twins]

    @classmethod
    def of(cls, track: Track) -> TrackGroup:
        canon = track.canon or track
        return cls(main=track, canon=canon, members=[canon, *canon.twins])

    @staticmethod
    def select_main_file(files: list[TrackFile]) -> TrackFile:
        """Pick the best copy among `files`: highest bitrate, then by container.

        Shared by organize (placing the main vs copies on disk) and dedup's
        main-file recompute, so both agree on which file leads a group.
        """
        # For equal bitrate, prefer the richer container: flac > m4a > mp3.
        # Unknown extensions sort last (0) instead of raising KeyError.
        ext_priority = {"flac": 3, "m4a": 2, "mp3": 1}
        return sorted(
            files,
            key=lambda tf: (tf.bitrate // 1000, ext_priority.get(tf.source_path.suffix[1:].lower(), 0)),
            reverse=True,
        )[0]

    def recompute_main(self) -> TrackFile | None:
        """Re-mark the single best file across the whole group as `is_main`.

        Gathers every file of the canon and its twins, picks the best via
        `select_main_file`, flags exactly that file and clears the rest. Returns
        the chosen file, or None when the group owns no files. Run after the
        canon graph changes so a merged or re-rooted group never keeps a stale
        or duplicate main.
        """
        files = [f for m in self.members for f in m.files]
        if not files:
            return None
        best = self.select_main_file(files)
        for f in files:
            f.is_main = f is best  # identity, not id: files may be unflushed
        return best

    @property
    def ids(self) -> list[int]:
        return [m.id for m in self.members]

    @property
    def main_file(self) -> TrackFile | None:
        return self.main.main_file

    @property
    def date_added(self) -> datetime:
        """When the group first appeared: its earliest member's added date."""
        return min(m.date_added for m in self.members)

    @property
    def rating(self) -> int | None:
        return self._max_rating("rating")

    @property
    def album_rating(self) -> int | None:
        return self._max_rating("album_rating")

    @property
    def loved(self) -> bool:
        return any(m.loved for m in self.members)

    @property
    def album_loved(self) -> bool:
        return any(m.album_loved for m in self.members)

    def _max_rating(self, attr: str) -> int | None:
        """Highest non-zero rating for `attr` across the group, or None if unrated."""
        ratings = [r for m in self.members if (r := getattr(m, attr))]
        return max(ratings) if ratings else None


class TrackAlias(Base):
    __tablename__ = "trackalias"
    __table_args__ = (
        UniqueConstraint("title", "album", "artist"),
        # trigram indexes for matching
        Index(
            "ix_trackalias_title_norm_trgm",
            "title_norm",
            postgresql_using="gin",
            postgresql_ops={"title_norm": "gin_trgm_ops"},
        ),
        Index(
            "ix_trackalias_artist_norm_trgm",
            "artist_norm",
            postgresql_using="gin",
            postgresql_ops={"artist_norm": "gin_trgm_ops"},
            postgresql_where=text("artist_norm <> ''"),
        ),
        Index(
            "ix_trackalias_album_norm_trgm",
            "album_norm",
            postgresql_using="gin",
            postgresql_ops={"album_norm": "gin_trgm_ops"},
            postgresql_where=text("album_norm <> ''"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)

    artist: Mapped[str | None]
    title: Mapped[str | None]
    album: Mapped[str | None]

    artist_norm: Mapped[str] = mapped_column(default="")
    title_norm: Mapped[str] = mapped_column(default="")
    album_norm: Mapped[str] = mapped_column(default="")

    track_id: Mapped[int | None] = mapped_column(ForeignKey("track.id"), index=True)
    track: Mapped[Track | None] = relationship(back_populates="aliases")

    scrobbles: Mapped[list[TrackAliasScrobble]] = relationship(
        back_populates="alias", cascade="all, delete-orphan"
    )

    @validates("title", "artist", "album")
    def _populate_norm(self, key: str, value: Any) -> Any:
        setattr(self, f"{key}_norm", normalize_name(value))
        return value

    @property
    def repr(self):
        return f"[{self.title} / {self.artist or ''} / {self.album or ''}]"


class TrackAliasScrobble(Base):
    __tablename__ = "trackaliasscrobble"

    id: Mapped[int] = mapped_column(primary_key=True)
    alias_id: Mapped[int] = mapped_column(ForeignKey("trackalias.id", ondelete="CASCADE"), index=True)
    alias: Mapped[TrackAlias] = relationship(back_populates="scrobbles")
    date: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), unique=True)
    platform: Mapped[Source] = mapped_column(sa.Enum(Source, native_enum=False))


class TrackPlay(Base):
    """A play event linked directly to a canonical Track (no alias matching required)."""

    __tablename__ = "trackplay"
    __table_args__ = (
        UniqueConstraint("track_id", "played_at"),
        UniqueConstraint(
            "source_scrobble_id",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    track_id: Mapped[int] = mapped_column(ForeignKey("track.id", ondelete="CASCADE"), index=True)
    track: Mapped[Track] = relationship(back_populates="plays")
    played_at: Mapped[AwareDatetime]
    platform: Mapped[Source] = mapped_column(sa.Enum(Source, native_enum=False))
    source_scrobble_id: Mapped[int | None] = mapped_column(
        ForeignKey("trackaliasscrobble.id", ondelete="SET NULL")
    )


class Playlist(Base):
    __tablename__ = "playlist"
    # Keyed on source identity, not name: two same-name playlists from different sources must
    # be able to coexist (name-merge is now opt-in, see unify_source_playlists). A merged
    # canonical keeps its anchor's (platform, source_id), so this stays unique in merge mode too.
    __table_args__ = (UniqueConstraint("platform", "source_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    platform: Mapped[Source] = mapped_column(sa.Enum(Source, native_enum=False))
    source_id: Mapped[str | None]
    description: Mapped[str | None]
    date_added: Mapped[AwareDatetime | None]
    date_modified: Mapped[AwareDatetime | None]

    tracks: Mapped[list[PlaylistTrack]] = relationship(
        back_populates="playlist", cascade="all, delete-orphan"
    )

    @property
    def comment(self):
        c = self.platform.service
        if self.source_id:
            c += f" #{self.source_id}"
        if self.description:
            c += f"\n{self.description}"
        return c


class PlaylistTrack(Base):
    __tablename__ = "playlisttrack"

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlist.id", ondelete="CASCADE"), index=True)
    playlist: Mapped[Playlist] = relationship(back_populates="tracks")
    track_id: Mapped[int] = mapped_column(ForeignKey("track.id", ondelete="CASCADE"), index=True)
    track: Mapped[Track] = relationship(back_populates="playlist_memberships")
    position: Mapped[int] = mapped_column(index=True)


class PlaylistLink(Base):
    """Reconcile state for a canonical Airdrome `Playlist` against one remote.

    One row per (Airdrome playlist, remote) pair, where a *remote* is any peer —
    a read-write backend (Navidrome) or a read-only source (Apple, …). Sources gain
    a base for the first time here. `synced_track_ids` is the snapshot of canonical
    track IDs reconciled on both sides at the last sync — the merge base for the next
    3-way merge. Tracks Airdrome holds but the remote can't represent, and tracks the
    remote holds that Airdrome can't resolve, are deliberately excluded from this
    snapshot so they read as steady-state rather than one-sided deletions.

    `external_id` is the remote-side playlist id — a server id for backends, a
    `SourcePlaylist.source_id` for sources. Nullable to cover the window before a
    backend playlist has been lazily created.
    """

    __tablename__ = "playlistlink"
    __table_args__ = (
        UniqueConstraint("playlist_id", "remote"),
        UniqueConstraint("remote", "external_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    playlist_id: Mapped[int] = mapped_column(ForeignKey("playlist.id", ondelete="CASCADE"), index=True)
    remote: Mapped[Source] = mapped_column(sa.Enum(Source, native_enum=False))
    external_id: Mapped[str | None]
    synced_track_ids: Mapped[list[int]] = mapped_column(sa.JSON, nullable=False)
    synced_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)


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
    surviving_playlist_id: Mapped[int | None] = mapped_column(ForeignKey("playlist.id", ondelete="SET NULL"))


class DedupGroup(Base):
    """A user-confirmed duplicate group from the manual deduplicator.

    The group's identity is the multiset of its members' `duplicate_hash`
    values, not `label` (which is an engine-specific display string kept only
    for readability). A persisted group means the user reviewed it; a group
    whose members all have `canon_hash = NULL` records "reviewed, these are
    not duplicates" and must not be re-prompted.
    """

    __tablename__ = "dedupgroup"

    id: Mapped[int] = mapped_column(primary_key=True)
    label: Mapped[str | None]
    members: Mapped[list[DedupGroupMember]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class DedupGroupMember(Base):
    """One track within a `DedupGroup`, identified by its `duplicate_hash`.

    `canon_hash` is the `duplicate_hash` of the chosen canon within the same
    group, or NULL when the track is itself a canon / unassigned.
    """

    __tablename__ = "dedupgroupmember"

    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("dedupgroup.id", ondelete="CASCADE"), index=True)
    group: Mapped[DedupGroup] = relationship(back_populates="members")
    member_hash: Mapped[str] = mapped_column(index=True)
    canon_hash: Mapped[str | None]


engine = create_engine(str(settings.db_dsn), echo=settings.db_echo)
