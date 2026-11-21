"""Signature editions (书帖版): a frozen imposition of one catalog revision.

A signature edition binds **one complete catalog revision** of one volume.
At creation time the in-use (active) pages of the volume are taken in
physical order and chunked into independent signatures of 4 or 8 positions;
the last signature is padded with blanks (空位) at its tail.  The layout is
then frozen: positions map back to the stable page identities of the bound
revision, and later catalog changes (insert/move/deactivate) never rewrite
an existing edition.  Blanks are real empty slots -- they never invent a
page.

Imposition rule (positions are 1-based inside a signature of ``N``
positions; sheets are numbered from the outside in, ``0 <= i < N/4``)::

    sheet i front: positions (N-2i, 1+2i)
    sheet i back:  positions (2+2i, N-1-2i)

So an 8-position signature puts positions 2 and 7 together on the outer
sheet's back side, and a 4-position signature puts (4,1) on the front and
(2,3) on the back of its single sheet.

A **paper side** (纸面) is the unit the press mounts on a rack; it carries
exactly two slots (格位).  Its stable id inside one edition is
``sig{k}/sheet{i}/{front|back}``.

New editions may change the signature size or bind another (later) catalog
revision; old editions stay queryable and can be reprinted (复印) with
``copy_edition``, which duplicates the frozen layout under a new edition id.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from typing import Any, Iterator

from . import db
from .catalog import Catalog
from .errors import (
    CATALOG_REVISION_MISMATCH,
    EDITION_EXISTS,
    EDITION_NOT_FOUND,
    INVALID_SIGNATURE_SIZE,
    PAGE_NOT_IN_EDITION,
    EditionError,
)

FORMAT = "signature-edition/1"
SIGNATURE_SIZES = (4, 8)
SIDES = ("front", "back")

SCHEMA = """
CREATE TABLE IF NOT EXISTS signature_editions (
    edition_id       TEXT PRIMARY KEY,
    volume_id        TEXT NOT NULL,
    catalog_revision INTEGER NOT NULL,  -- the bound, complete catalog revision
    signature_size   INTEGER NOT NULL CHECK (signature_size IN (4, 8)),
    page_count       INTEGER NOT NULL,  -- in-use pages bound into the edition
    signature_count  INTEGER NOT NULL,
    source_edition_id TEXT,             -- set when reprinted (复印) from an edition
    layout_digest    TEXT NOT NULL,     -- sha256 over the frozen layout
    created_at       TEXT NOT NULL
);

-- One row per signature position.  page_id is NULL for a blank (空位);
-- blanks never reference a page.  Rows are frozen at creation and never
-- updated, so an old edition keeps pointing at the bound revision's pages.
CREATE TABLE IF NOT EXISTS signature_slots (
    edition_id      TEXT NOT NULL
        REFERENCES signature_editions(edition_id) ON DELETE CASCADE,
    signature_index INTEGER NOT NULL,
    position        INTEGER NOT NULL,   -- 1..N inside the signature
    page_id         TEXT,               -- NULL == blank (空位)
    sheet_index     INTEGER NOT NULL,
    side            TEXT NOT NULL CHECK (side IN ('front', 'back')),
    slot            INTEGER NOT NULL CHECK (slot IN (0, 1)),
    PRIMARY KEY (edition_id, signature_index, position)
);
CREATE INDEX IF NOT EXISTS signature_slots_side
    ON signature_slots (edition_id, signature_index, sheet_index, side);
CREATE INDEX IF NOT EXISTS signature_slots_page
    ON signature_slots (edition_id, page_id);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def canonical_json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_digest(obj: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_json(obj).encode("utf-8")).hexdigest()


def side_id(signature_index: int, sheet_index: int, side: str) -> str:
    """The stable textual id of one paper side inside an edition."""

    return f"sig{signature_index}/sheet{sheet_index}/{side}"


def parse_side_id(text: str) -> tuple[int, int, str]:
    """Inverse of :func:`side_id`; raises EditionError on malformed input."""

    signature_index: int
    sheet_index: int
    side: str
    try:
        sig_part, sheet_part, side = text.split("/")
        if not sig_part.startswith("sig") or not sheet_part.startswith("sheet"):
            raise ValueError
        signature_index = int(sig_part[3:])
        sheet_index = int(sheet_part[5:])
    except (ValueError, AttributeError):
        raise EditionError(
            "invalid_side_id", f"malformed side id {text!r}",
            details={"side": text},
        ) from None
    if side not in SIDES or signature_index < 0 or sheet_index < 0:
        raise EditionError(
            "invalid_side_id", f"malformed side id {text!r}",
            details={"side": text},
        )
    return signature_index, sheet_index, side


def impose_signature(size: int) -> list[tuple[int, str, int, int]]:
    """The sheet/side/slot assignment of one signature of ``size`` positions.

    Returns ``(sheet_index, side, slot, position)`` rows.  Position is
    1-based; slot 0 is the first position of the pair given by the rule.
    """

    rows: list[tuple[int, str, int, int]] = []
    for sheet in range(size // 4):
        rows.append((sheet, "front", 0, size - 2 * sheet))
        rows.append((sheet, "front", 1, 1 + 2 * sheet))
        rows.append((sheet, "back", 0, 2 + 2 * sheet))
        rows.append((sheet, "back", 1, size - 1 - 2 * sheet))
    return rows


@dataclass(frozen=True)
class Slot:
    signature_index: int
    position: int
    page_id: str | None  # None == blank (空位)
    sheet_index: int
    side: str
    slot: int

    @property
    def blank(self) -> bool:
        return self.page_id is None

    @property
    def side_id(self) -> str:
        return side_id(self.signature_index, self.sheet_index, self.side)

    def as_dict(self) -> dict[str, Any]:
        return {
            "signature_index": self.signature_index,
            "position": self.position,
            "page_id": self.page_id,
            "blank": self.blank,
            "sheet_index": self.sheet_index,
            "side": self.side,
            "slot": self.slot,
            "side_id": self.side_id,
        }


@dataclass(frozen=True)
class SignatureEdition:
    edition_id: str
    volume_id: str
    catalog_revision: int
    signature_size: int
    page_count: int
    signature_count: int
    source_edition_id: str | None
    layout_digest: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "edition_id": self.edition_id,
            "volume_id": self.volume_id,
            "catalog_revision": self.catalog_revision,
            "signature_size": self.signature_size,
            "page_count": self.page_count,
            "signature_count": self.signature_count,
            "source_edition_id": self.source_edition_id,
            "layout_digest": self.layout_digest,
        }


class ImpositionStore:
    """Creates, copies and queries signature editions on top of a Catalog."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.conn = catalog.conn
        ensure_schema(self.conn)

    # ------------------------------------------------------------------
    # creation
    # ------------------------------------------------------------------
    def create_edition(
        self,
        volume_id: str,
        signature_size: int,
        *,
        edition_id: str | None = None,
        expected_revision: int | None = None,
    ) -> SignatureEdition:
        """Bind the volume's current, complete catalog revision into an edition.

        ``expected_revision`` makes the freeze explicit: when given, it must
        equal the current catalog revision, otherwise nothing is written --
        an edition is never bound to a half-seen state.
        """

        if signature_size not in SIGNATURE_SIZES:
            raise EditionError(
                INVALID_SIGNATURE_SIZE,
                f"signature size must be one of {SIGNATURE_SIZES}",
                details={"signature_size": signature_size},
            )
        self.catalog.require_volume(volume_id)
        revision = self.catalog.revision()
        if expected_revision is not None and expected_revision != revision:
            raise EditionError(
                CATALOG_REVISION_MISMATCH,
                f"catalog is at revision {revision}, not {expected_revision}",
                details={
                    "expected_revision": expected_revision,
                    "catalog_revision": revision,
                },
            )

        in_use = [p.page_id for p in self.catalog.pages(volume_id, include_inactive=False)]
        edition_id = self._alloc_edition_id(edition_id, volume_id, revision, signature_size)
        slots = self._layout(signature_size, in_use)
        edition = SignatureEdition(
            edition_id=edition_id,
            volume_id=volume_id,
            catalog_revision=revision,
            signature_size=signature_size,
            page_count=len(in_use),
            signature_count=(len(in_use) + signature_size - 1) // signature_size,
            source_edition_id=None,
            layout_digest=self._layout_digest(
                volume_id, revision, signature_size, slots
            ),
        )
        self._write_edition(edition, slots)
        return edition

    def copy_edition(self, source_edition_id: str, *, edition_id: str) -> SignatureEdition:
        """Reprint (复印) an old edition: same frozen layout, new edition id."""

        source = self.get_edition(source_edition_id)
        if self._edition_or_none(edition_id) is not None:
            raise EditionError(
                EDITION_EXISTS, f"edition {edition_id!r} already exists",
                details={"edition_id": edition_id},
            )
        slots = self.slots(source_edition_id)
        edition = SignatureEdition(
            edition_id=edition_id,
            volume_id=source.volume_id,
            catalog_revision=source.catalog_revision,
            signature_size=source.signature_size,
            page_count=source.page_count,
            signature_count=source.signature_count,
            source_edition_id=source.edition_id,
            layout_digest=source.layout_digest,  # a faithful reprint
        )
        self._write_edition(edition, slots)
        return edition

    # ------------------------------------------------------------------
    # reading
    # ------------------------------------------------------------------
    def get_edition(self, edition_id: str) -> SignatureEdition:
        edition = self._edition_or_none(edition_id)
        if edition is None:
            raise EditionError(
                EDITION_NOT_FOUND, f"edition {edition_id!r} does not exist",
                details={"edition_id": edition_id},
            )
        return edition

    def editions(self, volume_id: str | None = None) -> list[SignatureEdition]:
        sql = "SELECT * FROM signature_editions"
        params: list[Any] = []
        if volume_id is not None:
            sql += " WHERE volume_id = ?"
            params.append(volume_id)
        sql += " ORDER BY created_at, edition_id"
        return [self._row_to_edition(r) for r in self.conn.execute(sql, params)]

    def slots(self, edition_id: str) -> list[Slot]:
        self.get_edition(edition_id)
        return [
            self._row_to_slot(r)
            for r in self.conn.execute(
                "SELECT * FROM signature_slots WHERE edition_id = ? "
                "ORDER BY signature_index, position",
                (edition_id,),
            )
        ]

    def iter_sides(self, edition_id: str) -> Iterator[dict[str, Any]]:
        """Every paper side of the edition with its two slots, in order."""

        by_side: dict[tuple[int, int, str], list[Slot]] = {}
        for slot in self.slots(edition_id):
            by_side.setdefault(
                (slot.signature_index, slot.sheet_index, slot.side), []
            ).append(slot)
        for key in sorted(by_side):
            members = sorted(by_side[key], key=lambda s: s.slot)
            yield {
                "side_id": side_id(*key),
                "signature_index": key[0],
                "sheet_index": key[1],
                "side": key[2],
                "slots": [s.as_dict() for s in members],
            }

    def side_layout(
        self, edition_id: str, signature_index: int, sheet_index: int, side: str
    ) -> list[Slot]:
        self.get_edition(edition_id)
        rows = [
            self._row_to_slot(r)
            for r in self.conn.execute(
                "SELECT * FROM signature_slots WHERE edition_id = ? "
                "AND signature_index = ? AND sheet_index = ? AND side = ? "
                "ORDER BY slot",
                (edition_id, signature_index, sheet_index, side),
            )
        ]
        if not rows:
            raise EditionError(
                EDITION_NOT_FOUND,
                f"edition {edition_id!r} has no side "
                f"{side_id(signature_index, sheet_index, side)!r}",
                details={
                    "edition_id": edition_id,
                    "side_id": side_id(signature_index, sheet_index, side),
                },
            )
        return rows

    def locate_page(self, edition_id: str, page_id: str) -> Slot:
        """Independently locate one page: its sheet, side and slot (格位)."""

        self.get_edition(edition_id)
        row = self.conn.execute(
            "SELECT * FROM signature_slots WHERE edition_id = ? AND page_id = ?",
            (edition_id, page_id),
        ).fetchone()
        if row is None:
            raise EditionError(
                PAGE_NOT_IN_EDITION,
                f"page {page_id!r} is not bound in edition {edition_id!r}",
                details={"edition_id": edition_id, "page_id": page_id},
            )
        return self._row_to_slot(row)

    def page_side_map(self, edition_id: str) -> dict[str, str]:
        """page_id -> side_id for every real (non-blank) page of the edition."""

        return {
            row["page_id"]: side_id(
                row["signature_index"], row["sheet_index"], row["side"]
            )
            for row in self.conn.execute(
                "SELECT signature_index, sheet_index, side, page_id "
                "FROM signature_slots WHERE edition_id = ? AND page_id IS NOT NULL",
                (edition_id,),
            )
        }

    # ------------------------------------------------------------------
    # export (offline consumption)
    # ------------------------------------------------------------------
    def export_edition(self, edition_id: str) -> dict[str, Any]:
        """A self-contained ``signature-edition/1`` document.

        The document carries every slot (blanks included, as ``page_id:
        null``) so a press plan can be replayed offline against it without
        opening the database.
        """

        edition = self.get_edition(edition_id)
        return {
            "format": FORMAT,
            **edition.as_dict(),
            "slots": [s.as_dict() for s in self.slots(edition_id)],
        }

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _alloc_edition_id(
        self, requested: str | None, volume_id: str, revision: int, size: int
    ) -> str:
        if requested is not None:
            if self._edition_or_none(requested) is not None:
                raise EditionError(
                    EDITION_EXISTS, f"edition {requested!r} already exists",
                    details={"edition_id": requested},
                )
            return requested
        base = f"{volume_id}-r{revision}-s{size}"
        candidate, n = base, 2
        while self._edition_or_none(candidate) is not None:
            candidate = f"{base}.{n}"
            n += 1
        return candidate

    @staticmethod
    def _layout(signature_size: int, in_use: list[str]) -> list[Slot]:
        """Frozen slot list for the whole volume (blanks pad the last signature)."""

        slots: list[Slot] = []
        count = (len(in_use) + signature_size - 1) // signature_size
        assignment = impose_signature(signature_size)
        for sig in range(count):
            position_to_page: dict[int, str | None] = {}
            for position in range(1, signature_size + 1):
                idx = sig * signature_size + (position - 1)
                position_to_page[position] = in_use[idx] if idx < len(in_use) else None
            for sheet, side, slot, position in assignment:
                slots.append(
                    Slot(
                        signature_index=sig,
                        position=position,
                        page_id=position_to_page[position],
                        sheet_index=sheet,
                        side=side,
                        slot=slot,
                    )
                )
        return sorted(slots, key=lambda s: (s.signature_index, s.position))

    @staticmethod
    def _layout_digest(
        volume_id: str, revision: int, signature_size: int, slots: list[Slot]
    ) -> str:
        """Digest over the *layout* only -- a reprint shares its source's digest."""

        return content_digest(
            {
                "format": FORMAT,
                "volume_id": volume_id,
                "catalog_revision": revision,
                "signature_size": signature_size,
                "slots": [
                    [
                        s.signature_index,
                        s.position,
                        s.page_id,
                        s.sheet_index,
                        s.side,
                        s.slot,
                    ]
                    for s in slots
                ],
            }
        )

    def _write_edition(self, edition: SignatureEdition, slots: list[Slot]) -> None:
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO signature_editions "
                "(edition_id, volume_id, catalog_revision, signature_size, "
                " page_count, signature_count, source_edition_id, layout_digest, "
                " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    edition.edition_id,
                    edition.volume_id,
                    edition.catalog_revision,
                    edition.signature_size,
                    edition.page_count,
                    edition.signature_count,
                    edition.source_edition_id,
                    edition.layout_digest,
                    datetime.datetime.now(datetime.timezone.utc).isoformat(),
                ),
            )
            for s in slots:
                self.conn.execute(
                    "INSERT INTO signature_slots "
                    "(edition_id, signature_index, position, page_id, "
                    " sheet_index, side, slot) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        edition.edition_id,
                        s.signature_index,
                        s.position,
                        s.page_id,
                        s.sheet_index,
                        s.side,
                        s.slot,
                    ),
                )

    def _edition_or_none(self, edition_id: str) -> SignatureEdition | None:
        row = self.conn.execute(
            "SELECT * FROM signature_editions WHERE edition_id = ?", (edition_id,)
        ).fetchone()
        return self._row_to_edition(row) if row is not None else None

    @staticmethod
    def _row_to_edition(row: sqlite3.Row) -> SignatureEdition:
        return SignatureEdition(
            edition_id=row["edition_id"],
            volume_id=row["volume_id"],
            catalog_revision=int(row["catalog_revision"]),
            signature_size=int(row["signature_size"]),
            page_count=int(row["page_count"]),
            signature_count=int(row["signature_count"]),
            source_edition_id=row["source_edition_id"],
            layout_digest=row["layout_digest"],
        )

    @staticmethod
    def _row_to_slot(row: sqlite3.Row) -> Slot:
        return Slot(
            signature_index=int(row["signature_index"]),
            position=int(row["position"]),
            page_id=row["page_id"],
            sheet_index=int(row["sheet_index"]),
            side=row["side"],
            slot=int(row["slot"]),
        )


# ----------------------------------------------------------------------
# Offline helpers: rebuild the page/side views from an exported document.
# ----------------------------------------------------------------------
def edition_page_side_map(document: dict[str, Any]) -> dict[str, str]:
    """page_id -> side_id from a ``signature-edition/1`` export (no database)."""

    out: dict[str, str] = {}
    for slot in document.get("slots", []):
        if slot.get("page_id") is not None:
            out[slot["page_id"]] = slot["side_id"]
    return out


def verify_edition_document(document: dict[str, Any]) -> list[str]:
    """Structural + digest check of a ``signature-edition/1`` export."""

    problems: list[str] = []
    if document.get("format") != FORMAT:
        problems.append(f"bad format {document.get('format')!r}")
        return problems
    slots = document.get("slots")
    if not isinstance(slots, list):
        problems.append("slots must be a list")
        return problems
    seen_positions: set[tuple[int, int]] = set()
    for i, slot in enumerate(slots):
        for key in (
            "signature_index", "position", "page_id", "blank",
            "sheet_index", "side", "slot", "side_id",
        ):
            if key not in slot:
                problems.append(f"slot {i}: missing {key}")
        if not all(k in slot for k in ("signature_index", "position", "page_id")):
            continue
        pos_key = (slot["signature_index"], slot["position"])
        if pos_key in seen_positions:
            problems.append(f"slot {i}: duplicate position {pos_key}")
        seen_positions.add(pos_key)
        if slot.get("blank") != (slot.get("page_id") is None):
            problems.append(f"slot {i}: blank flag disagrees with page_id")
        expected_side = side_id(
            slot["signature_index"], slot["sheet_index"], slot["side"]
        )
        if slot.get("side_id") != expected_side:
            problems.append(f"slot {i}: side_id mismatch")
    if problems:
        return problems
    recomputed = ImpositionStore._layout_digest(
        document["volume_id"],
        document["catalog_revision"],
        document["signature_size"],
        [
            Slot(
                signature_index=s["signature_index"],
                position=s["position"],
                page_id=s["page_id"],
                sheet_index=s["sheet_index"],
                side=s["side"],
                slot=s["slot"],
            )
            for s in sorted(slots, key=lambda s: (s["signature_index"], s["position"]))
        ],
    )
    if recomputed != document.get("layout_digest"):
        problems.append("layout_digest mismatch")
    return problems
