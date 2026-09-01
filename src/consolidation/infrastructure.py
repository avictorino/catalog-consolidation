"""Infrastructure — adapters to the outside world and every database access
outside the repositories.

Everything that talks to something external or runs SQL against the connection:
the catalog download and the streamed seller feed, the byte-stream transports
they read through (``HttpByteSource`` / ``S3ByteSource``, adapters for the
``services.ByteSource`` port), the anti-corruption layer (``ProductEntry``), the
SQL-injection screen, the concrete similarity backends, the Alembic wiring, the
schema-refactor steps the Alembic revision executes (source classification + the
staged rebuild), and ``SqliteCatalogRepository`` — the SQLite adapter for the
``CatalogRepository`` port the use cases depend on. Nothing here holds business
rules.

The declarative table metadata itself stays in :mod:`consolidation.schema`;
this module only *executes* against a database.

Depends on: :mod:`consolidation.domain`, :mod:`consolidation.services`,
:mod:`consolidation.repository`; plus requests / ijson / pydantic / libinjection /
rapidfuzz / alembic / SQLAlchemy. ``boto3`` is imported lazily, only when the S3
transport is selected.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from difflib import SequenceMatcher
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import urlparse

import ijson
import libinjection
import requests
from alembic import command
from alembic.config import Config as AlembicConfig
from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Connection, Engine, Transaction

from consolidation.domain import ThreatFinding, new_uuid, normalize
from consolidation.repository import CatalogRepositories
from consolidation.services import ByteSource, Similarity

logger = logging.getLogger("consolidation")

_TIMEOUT = (10, 60)  # (connect, read) seconds
_PROBE_SIZE = 64 * 1024
_CHUNK = 1 << 16  # 64 KiB
_SQLITE_MAGIC = b"SQLite format 3\x00"
_STRING_FIELDS = ("Id", "SellerName", "Name", "Brand", "Category")

MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"


# --------------------------------------------------------------------------- #
# Byte-stream transports for the ``services.ByteSource`` port. Both the catalog
# download and the seller feed read through one of these; the choice is made at
# the composition root and injected, exactly like the similarity backend.
# --------------------------------------------------------------------------- #
class HttpByteSource:
    """Stream an object over HTTP(S) with ``requests``."""

    name = "http"

    @contextmanager
    def open(self, ref: str) -> Iterator[BinaryIO]:
        with requests.get(ref, stream=True, timeout=_TIMEOUT) as response:
            response.raise_for_status()
            response.raw.decode_content = True  # transparently undo any transfer encoding
            yield response.raw


class _S3ReadAdapter:
    """Wrap a botocore ``StreamingBody`` so ``read(-1)`` / ``read()`` mean "read the
    rest" — the contract :class:`_PrefixedReader` and ``ijson`` rely on."""

    def __init__(self, body: object) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        if size is None or size < 0:
            return self._body.read()
        return self._body.read(size)


class S3ByteSource:
    """Stream a **public** S3 object anonymously with ``boto3`` ``get_object``.

    Accepts ``s3://bucket/key`` or a virtual-hosted / path-style
    ``https://…amazonaws.com/…`` reference. The request is unsigned, so the object
    must be publicly readable — no credentials are resolved or required.
    """

    name = "s3"

    @contextmanager
    def open(self, ref: str) -> Iterator[BinaryIO]:
        import boto3
        from botocore import UNSIGNED
        from botocore.config import Config

        bucket, key = parse_s3_ref(ref)
        client = boto3.client("s3", config=Config(signature_version=UNSIGNED))
        body = client.get_object(Bucket=bucket, Key=key)["Body"]
        try:
            yield _S3ReadAdapter(body)
        finally:
            body.close()


def parse_s3_ref(ref: str) -> tuple[str, str]:
    """``(bucket, key)`` from ``s3://b/k`` or an ``…amazonaws.com`` HTTP(S) URL."""
    parsed = urlparse(ref)
    if parsed.scheme == "s3":
        return parsed.netloc, parsed.path.lstrip("/")
    if parsed.scheme in ("http", "https") and parsed.netloc.endswith("amazonaws.com"):
        host, path = parsed.netloc, parsed.path.lstrip("/")
        if host.startswith(("s3.", "s3-")):  # path-style: s3.<region>.amazonaws.com/<bucket>/<key>
            bucket, _, key = path.partition("/")
            return bucket, key
        return host.split(".s3", 1)[0], path  # virtual-hosted: <bucket>.s3.<region>.amazonaws.com/…
    raise ValueError(f"not an S3 reference: {ref!r}")


def build_source(name: str) -> ByteSource:
    """Build a byte-stream transport; ``boto3`` is imported only for the S3 path."""
    if name == "http":
        return HttpByteSource()
    if name == "s3":
        return S3ByteSource()
    raise ValueError(f"unknown source: {name!r} (options: http, s3)")


# --------------------------------------------------------------------------- #
# Catalog download
# --------------------------------------------------------------------------- #
def download_to(url: str, dest_dir: Path, source: ByteSource) -> Path:
    """Stream ``url`` in chunks into a fresh temp file inside ``dest_dir``.

    The body is never held whole in memory. Returns the temp file path.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    tmp = dest_dir / f".catalog-{uuid.uuid4().hex}.db.tmp"
    logger.info("downloading catalog url=%s via=%s dest=%s", url, source.name, tmp)

    bytes_written = 0
    try:
        with source.open(url) as stream, tmp.open("wb") as handle:
            while chunk := stream.read(_CHUNK):
                handle.write(chunk)
                bytes_written += len(chunk)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise

    logger.info("download complete bytes=%d", bytes_written)
    return tmp


def verify_sqlite_header(path: Path) -> None:
    """Raise ``ValueError`` unless ``path`` starts with the SQLite magic string."""
    with path.open("rb") as handle:
        header = handle.read(len(_SQLITE_MAGIC))
    if header != _SQLITE_MAGIC:
        raise ValueError(f"{path} is not a SQLite database (bad header)")


# --------------------------------------------------------------------------- #
# Alembic wiring — migrations run online with the setup connection injected.
# --------------------------------------------------------------------------- #
def alembic_config(connection) -> AlembicConfig:
    cfg = AlembicConfig()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.set_main_option("sqlalchemy.url", "sqlite://")  # placeholder; connection is injected
    cfg.attributes["connection"] = connection
    return cfg


# --------------------------------------------------------------------------- #
# Anti-corruption layer: the raw seller feed record.
# --------------------------------------------------------------------------- #
class FeedError(RuntimeError):
    """A feed could not be parsed or consumed."""


class FeedValidationError(FeedError):
    """A single feed record did not satisfy the input contract."""

    def __init__(self, record_index: int, fields: tuple[str, ...]) -> None:
        self.record_index = record_index
        self.fields = fields
        super().__init__(f"invalid feed record {record_index}: fields={','.join(fields)}")


class ProductEntry(BaseModel):
    """One seller listing, validated without materializing the feed.

    This is the boundary type. It structurally satisfies ``domain.Submission`` so
    the domain can consume it without importing pydantic.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    Id: str
    SellerName: str
    Name: str
    Brand: str | None = None
    Category: str | None = None

    @field_validator("Id", "SellerName", "Name")
    @classmethod
    def require_non_empty(cls, value: str) -> str:
        if not value:
            raise ValueError("must not be empty")
        return value


class _PrefixedReader:
    """Expose an already-read probe followed by the original response stream."""

    def __init__(self, prefix: bytes, stream: BinaryIO) -> None:
        self._prefix = prefix
        self._stream = stream

    def read(self, size: int = -1) -> bytes:
        if not self._prefix:
            return self._stream.read(size)
        if size == 0:
            return b""
        if size < 0:
            prefix, self._prefix = self._prefix, b""
            return prefix + self._stream.read()
        prefix, self._prefix = self._prefix[:size], self._prefix[size:]
        return prefix


def iter_entries(stream: BinaryIO) -> Iterator[ProductEntry]:
    """Yield validated objects from a JSON array using bounded memory."""
    probe = stream.read(_PROBE_SIZE)
    first = probe.lstrip()[:1]
    if first != b"[":
        raise FeedError("feed root must be a JSON array")

    try:
        raw_entries = ijson.items(_PrefixedReader(probe, stream), "item")
        for index, raw_entry in enumerate(raw_entries):
            if not isinstance(raw_entry, dict):
                raise FeedValidationError(index, ("record",))
            try:
                yield ProductEntry.model_validate(raw_entry)
            except ValidationError as exc:
                fields = tuple(
                    dict.fromkeys(
                        str(error["loc"][0]) for error in exc.errors() if error.get("loc")
                    )
                ) or ("record",)
                raise FeedValidationError(index, fields) from exc
    except FeedValidationError:
        raise
    except Exception as exc:
        raise FeedError(f"invalid JSON feed: {exc}") from exc


def iter_feed(url: str, source: ByteSource) -> Iterator[ProductEntry]:
    """Stream and parse a remote seller feed without downloading it locally."""
    logger.info("streaming seller feed url=%s via=%s", url, source.name)
    with source.open(url) as stream:
        yield from iter_entries(stream)


# --------------------------------------------------------------------------- #
# SQL-injection screen (WAF-grade tokenizer). Parameterized SQL is the real
# defense; this rejects and reports probes before they reach the database.
# --------------------------------------------------------------------------- #
def screen_entry(entry: ProductEntry, record_index: int) -> ThreatFinding | None:
    """Return a :class:`ThreatFinding` when libinjection flags a string field."""
    finding: ThreatFinding | None = None
    for field_name in _STRING_FIELDS:
        value = getattr(entry, field_name)
        if value is None:
            continue
        result = libinjection.is_sql_injection(value)
        if result.get("is_sqli"):
            finding = ThreatFinding(field_name, result.get("fingerprint", ""), value[:120])
            break

    if finding is None:
        return None

    logger.warning(
        "event=sqli_attempt record=%d field=%s fingerprint=%s value=%r",
        record_index,
        finding.field,
        finding.fingerprint,
        finding.value,
    )
    return finding


# --------------------------------------------------------------------------- #
# Concrete similarity backends for the ``services.Similarity`` port.
# --------------------------------------------------------------------------- #
class DifflibSimilarity:
    name = "difflib"
    suggested_threshold = 0.90

    def score(self, a: str, b: str) -> float:
        return SequenceMatcher(None, a, b, autojunk=False).ratio()


class RapidFuzzSimilarity:
    name = "rapidfuzz"
    suggested_threshold = 0.90

    def score(self, a: str, b: str) -> float:
        from rapidfuzz import fuzz

        return fuzz.ratio(a, b) / 100.0


def build_similarity(name: str) -> Similarity:
    """Build a backend; rapidfuzz is imported only when that path is used."""
    if name == "difflib":
        return DifflibSimilarity()
    if name == "rapidfuzz":
        return RapidFuzzSimilarity()
    raise ValueError(f"unknown matcher: {name!r} (options: difflib, rapidfuzz)")


# --------------------------------------------------------------------------- #
# Schema refactor — executed by migrations/versions/0001_refactor_catalog.py.
#
# The declarative target schema lives in ``consolidation.schema``; these helpers
# run the one-way rebuild against a live connection. SQLite quirk: PRAGMA
# foreign_keys is a no-op inside a transaction, and the whole refactor runs in
# the setup transaction. FK enforcement stays off during the rebuild;
# ``foreign_key_check`` validates the result. After that transaction commits the
# use case enables and verifies enforcement before importing the feed.
# --------------------------------------------------------------------------- #
SourceKind = Literal["legacy", "migrated", "unrecognized"]


def classify_source(conn: Connection) -> SourceKind:
    """Classify a downloaded database as ``legacy``, ``migrated`` or ``unrecognized``."""
    insp = inspect(conn)
    tables = set(insp.get_table_names())
    if not {"Product", "SellerProduct"} <= tables:
        return "unrecognized"

    product_cols = {c["name"]: c for c in insp.get_columns("Product")}
    sp_cols = {c["name"] for c in insp.get_columns("SellerProduct")}
    product_id_type = str(product_cols.get("Id", {}).get("type", "")).upper()

    legacy = (
        {"Brand", "Category"} <= product_cols.keys()
        and "BrandId" not in product_cols
        and product_id_type.startswith("INTEGER")
        and {"SellerName", "SellerProductId"} <= sp_cols
        and "ExternalSku" not in sp_cols
        and not {"Brand", "Category", "Seller", "alembic_version"} & tables
    )
    if legacy:
        return "legacy"

    migrated = (
        {"Brand", "Category", "ProductCategory", "Seller", "alembic_version"} <= tables
        and "BrandId" in product_cols
        and "CategoryId" not in product_cols
        and "TEXT" in product_id_type
        and "ExternalSku" in sp_cols
    )
    if migrated:
        return "migrated"

    return "unrecognized"


_STAGING_DDL = (
    """
    CREATE TABLE Brand (
        Id   TEXT PRIMARY KEY,
        Name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE Category (
        Id   TEXT PRIMARY KEY,
        Name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE Seller (
        Id   TEXT PRIMARY KEY,
        Name TEXT NOT NULL UNIQUE
    )
    """,
    """
    CREATE TABLE "Product_new" (
        Id      TEXT PRIMARY KEY,
        Name    TEXT NOT NULL,
        BrandId TEXT REFERENCES Brand (Id)
    )
    """,
    """
    CREATE TABLE "ProductCategory_new" (
        ProductId  TEXT NOT NULL REFERENCES "Product_new" (Id),
        CategoryId TEXT NOT NULL REFERENCES Category (Id),
        PRIMARY KEY (ProductId, CategoryId)
    )
    """,
    """
    CREATE INDEX ix_ProductCategory_CategoryId
        ON "ProductCategory_new" (CategoryId)
    """,
    """
    CREATE TABLE "SellerProduct_new" (
        SellerId    TEXT NOT NULL REFERENCES Seller (Id),
        ProductId   TEXT NOT NULL REFERENCES "Product_new" (Id),
        ExternalSku TEXT NOT NULL,
        PRIMARY KEY (SellerId, ProductId),
        UNIQUE (SellerId, ExternalSku)
    )
    """,
)


def create_staging_tables(conn: Connection) -> None:
    """Create reference tables and staged target tables for the refactor."""
    for ddl in _STAGING_DDL:
        conn.execute(text(ddl))


# Fixed statements — no string interpolation reaches SQL.
_REFERENCE_SQL = {
    "Brand": (
        "SELECT DISTINCT Brand AS value FROM Product",
        "INSERT INTO Brand (Id, Name) VALUES (:Id, :Name)",
    ),
    "Category": (
        "SELECT DISTINCT Category AS value FROM Product",
        "INSERT INTO Category (Id, Name) VALUES (:Id, :Name)",
    ),
}


def _extract_reference(conn: Connection, column: Literal["Brand", "Category"]) -> dict[str, str]:
    """Extract distinct normalized non-empty ``Product.<column>`` values into ``<column>``.

    Returns ``{normalized_value: new_uuid}``.
    """
    select_sql, insert_sql = _REFERENCE_SQL[column]
    rows = conn.execute(text(select_sql)).all()
    mapping: dict[str, str] = {}
    to_insert: list[dict[str, str]] = []
    for (raw,) in rows:
        norm = normalize(raw)
        if not norm or norm in mapping:
            continue
        new_id = new_uuid()
        mapping[norm] = new_id
        to_insert.append({"Id": new_id, "Name": norm.title()})
    if to_insert:
        conn.execute(text(insert_sql), to_insert)
    logger.info("extracted reference table=%s rows=%d", column, len(to_insert))
    return mapping


def extract_brands(conn: Connection) -> dict[str, str]:
    return _extract_reference(conn, "Brand")


def extract_categories(conn: Connection) -> dict[str, str]:
    return _extract_reference(conn, "Category")


def extract_product_sellers(conn: Connection) -> None:
    """Copy optional legacy ``Product.Seller`` values into ``Seller.Name``.

    The published legacy snapshot stores seller names on ``SellerProduct``. Some
    legacy exports instead also carry a seller column on ``Product``; those values
    are seller entities too, even though the target model does not retain the
    denormalized product column.
    """
    product_columns = {column["name"] for column in inspect(conn).get_columns("Product")}
    if "Seller" not in product_columns:
        logger.info("extracted product sellers rows=0 column=absent")
        return

    existing = {name for (name,) in conn.execute(text("SELECT Name FROM Seller")).all()}
    seller_rows: list[dict[str, str]] = []
    for (raw_name,) in conn.execute(text("SELECT Seller FROM Product")).all():
        if not isinstance(raw_name, str):
            continue
        name = raw_name.strip()
        if not name or name in existing:
            continue
        existing.add(name)
        seller_rows.append({"Id": new_uuid(), "Name": name})

    if seller_rows:
        conn.execute(text("INSERT INTO Seller (Id, Name) VALUES (:Id, :Name)"), seller_rows)
    logger.info("extracted product sellers rows=%d", len(seller_rows))


def rebuild_product(
    conn: Connection,
    brand_map: dict[str, str],
    category_map: dict[str, str],
) -> dict[int, str]:
    """Copy every legacy ``Product`` row into ``Product_new`` with a fresh ``uuid4`` id
    and ``Brand`` text replaced by a nullable FK. Legacy categories are copied into
    ``ProductCategory_new`` as one association per product when present.

    Returns ``{old_int_id: new_uuid}``.
    """
    rows = conn.execute(text("SELECT Id, Name, Brand, Category FROM Product")).all()
    id_map: dict[int, str] = {}
    batch: list[dict[str, str | None]] = []
    category_links: list[dict[str, str]] = []
    for old_id, name, brand, category in rows:
        new_id = new_uuid()
        id_map[old_id] = new_id
        category_id = category_map.get(normalize(category))
        batch.append(
            {
                "Id": new_id,
                "Name": name,
                "BrandId": brand_map.get(normalize(brand)),
            }
        )
        if category_id:
            category_links.append({"ProductId": new_id, "CategoryId": category_id})
    if batch:
        conn.execute(
            text('INSERT INTO "Product_new" (Id, Name, BrandId) VALUES (:Id, :Name, :BrandId)'),
            batch,
        )
    if category_links:
        conn.execute(
            text(
                'INSERT INTO "ProductCategory_new" (ProductId, CategoryId) '
                "VALUES (:ProductId, :CategoryId)"
            ),
            category_links,
        )
    logger.info("rebuilt Product rows=%d", len(batch))
    return id_map


def rebuild_seller_product(conn: Connection, product_id_map: dict[int, str]) -> None:
    """Extract ``SellerName`` into ``Seller`` and copy legacy ``SellerProduct`` rows into
    ``SellerProduct_new`` with remapped UUID foreign keys and the integer
    ``SellerProductId`` cast to opaque ``ExternalSku`` text.

    The published base table is empty; this stays correct if it is not.
    """
    rows = conn.execute(
        text("SELECT SellerName, ProductId, SellerProductId FROM SellerProduct")
    ).all()
    seller_map = {
        name: seller_id
        for seller_id, name in conn.execute(text("SELECT Id, Name FROM Seller")).all()
    }
    seller_rows: list[dict[str, str]] = []
    link_rows: list[dict[str, str]] = []
    for seller_name, old_product_id, sku in rows:
        seller_id = seller_map.get(seller_name)
        if seller_id is None:
            seller_id = new_uuid()
            seller_map[seller_name] = seller_id
            seller_rows.append({"Id": seller_id, "Name": seller_name})
        link_rows.append(
            {
                "SellerId": seller_id,
                "ProductId": product_id_map[old_product_id],
                "ExternalSku": str(sku),
            }
        )
    if seller_rows:
        conn.execute(text("INSERT INTO Seller (Id, Name) VALUES (:Id, :Name)"), seller_rows)
    if link_rows:
        conn.execute(
            text(
                'INSERT OR IGNORE INTO "SellerProduct_new" (SellerId, ProductId, ExternalSku) '
                "VALUES (:SellerId, :ProductId, :ExternalSku)"
            ),
            link_rows,
        )
    logger.info("rebuilt SellerProduct sellers=%d links=%d", len(seller_rows), len(link_rows))


def swap_tables(conn: Connection) -> None:
    """Drop the legacy tables and rename the staging tables into place.

    Also clears the ``sqlite_sequence`` bookkeeping table left behind by the legacy
    ``AUTOINCREMENT`` columns — SQLite forbids dropping it, but the target schema has
    no ``AUTOINCREMENT`` so it must hold no counters.
    """
    conn.execute(text("DROP TABLE SellerProduct"))
    conn.execute(text("DROP TABLE Product"))
    conn.execute(text('ALTER TABLE "Product_new" RENAME TO "Product"'))
    conn.execute(text('ALTER TABLE "ProductCategory_new" RENAME TO "ProductCategory"'))
    conn.execute(text('ALTER TABLE "SellerProduct_new" RENAME TO "SellerProduct"'))
    if conn.execute(
        text("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'")
    ).first():
        conn.execute(text("DELETE FROM sqlite_sequence"))


def foreign_key_check(conn: Connection) -> None:
    """Raise ``RuntimeError`` if ``PRAGMA foreign_key_check`` reports any violation."""
    violations = conn.execute(text("PRAGMA foreign_key_check")).all()
    if violations:
        raise RuntimeError(f"foreign_key_check failed: {violations!r}")


# --------------------------------------------------------------------------- #
# Catalog repository — the SQLite adapter for the ``CatalogRepository`` port the
# use cases depend on. Everything engine-, driver- and Alembic-specific about
# "get a downloaded file ready to consolidate, then serve it" lives here.
# --------------------------------------------------------------------------- #
class SqliteCatalogRepository:
    """Wrap SQLAlchemy engine/connection creation, the SQLite header check, the
    Alembic upgrade and the ``CatalogRepositories`` bundle behind the port.

    ``url_template`` is the only knob: swap it (or write another adapter) to target
    a different database — the use cases never see ``sqlite:///``.
    """

    def __init__(self, *, url_template: str = "sqlite:///{path}") -> None:
        self._url_template = url_template
        self._engine: Engine | None = None
        self._conn: Connection | None = None
        self._trans: Transaction | None = None

    def verify_database(self, path: Path) -> None:
        verify_sqlite_header(path)

    def connect(self, path: Path) -> None:
        self._engine = create_engine(self._url_template.format(path=path))
        self._conn = self._engine.connect()

    def classify_source(self) -> str:
        return classify_source(self._require_conn())

    def begin(self) -> None:
        self._trans = self._require_conn().begin()

    def upgrade(self) -> None:
        command.upgrade(alembic_config(self._require_conn()), "head")

    def commit(self) -> None:
        if self._trans is not None:
            self._trans.commit()
            self._trans = None

    def rollback(self) -> None:
        if self._trans is not None and self._trans.is_active:
            self._trans.rollback()
        self._trans = None

    def enable_foreign_keys(self) -> None:
        # SQLite: ``PRAGMA foreign_keys`` is a no-op inside a transaction, so this
        # runs after the migration has committed and before any feed work begins.
        conn = self._require_conn()
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
            raise RuntimeError("foreign key enforcement could not be enabled")
        conn.commit()
        logger.info("foreign key enforcement enabled")

    def connection(self) -> Connection:
        return self._require_conn()

    def catalog_repositories(self) -> CatalogRepositories:
        return CatalogRepositories(self._require_conn())

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None
        if self._engine is not None:
            self._engine.dispose()
            self._engine = None

    def _require_conn(self) -> Connection:
        if self._conn is None:
            raise RuntimeError("migration repository is not connected; call connect() first")
        return self._conn
