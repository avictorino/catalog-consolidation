# PRD — Catalog consolidation

## Context

A traditional e-commerce company is becoming a marketplace. It already has a product
catalog; now it must ingest product feeds from many sellers. The same product is often
sold by several sellers, each registering it slightly differently. Duplicating products
is undesirable, but every seller offering a product must be recorded.

Input is a SQLite `catalog.db` with two tables and a JSON file of seller product
entries. For a duplicate product the system must **not** insert into the product table;
it must link the existing product to the seller in the link table.

The challenge explicitly values demonstrated mastery of the problem and its
ambiguities over a scalable, production-ready implementation, and it permits changing
the database.

## Technology choices

| Concern | Choice | Notes |
| --- | --- | --- |
| HTTP | `requests` (streaming) | chunked download of the DB; streamed body for the feed |
| JSON | `ijson` | incremental array parsing; no full document in memory |
| Feed validation | `pydantic` v2 | one object at a time (`model_validate`) |
| Similarity | `difflib` (stdlib) / `rapidfuzz` | interchangeable `score()` backends |
| SQL injection screen | `libinjection` (`libinjection-python`) | WAF-grade tokenizer (ModSecurity uses libinjection); wrapper is GPLv3, core is BSD-3, no cp311/cp312 wheel |
| Database access + schema | **SQLAlchemy Core 2.x** | declarative target schema, parameterized statements, `insert().from_select()` for the refactor |
| Config | `python-dotenv` | every CLI option has a default in `.env` |

`libinjection-python` is GPLv3 (the underlying libinjection C library is BSD-3). If a
permissive-only dependency tree is required, the screening module is the single swap
point — its `Similarity`-style seam takes any `is_sqli(str) -> bool`.

**Not used:** the SQLAlchemy ORM (no object graph here — bulk reads and inserts only)
and Alembic (there is no chain of versioned schema revisions to replay). The refactor is
a single [conditional migration](#when-the-refactor-runs--conditional-migration) guarded
by `PRAGMA user_version`, expressed with Core constructs.

## Data (profiled from the published sources)

- `Product`: 975 rows. `Id INTEGER PRIMARY KEY AUTOINCREMENT`, `Name NOT NULL`,
  `Brand` and `Category` nullable. Names are unique after normalization (no collisions).
  - `Brand`: 639 distinct raw strings, **637** after normalization
    (`BLACK+DECKER` / `Black+Decker`, `Simplehuman` / `simplehuman`); **119** rows null.
  - `Category`: **43** distinct strings (no normalization collisions); **34** rows null.
- `SellerProduct`: **empty**. Original columns `Id`, `SellerName`, `ProductId`,
  `SellerProductId INTEGER NOT NULL`. No index, no uniqueness constraint.
- `ProductEntry.json`: 269 entries, all five fields always present. `Brand` is null in 3;
  `Category` always present. `Id` is a text UUID (3 malformed), reused only across
  different sellers — not a product identity, kept as `SellerProduct.ExternalSku`.
- 20 distinct seller names in the feed.
- Expected result (see [`spec/acceptance.md`](spec/acceptance.md)): **975 products**,
  **637 brands**, **43 categories**, **20 sellers**, **256 seller-product links**,
  **1 threat**.

See [`spec/data-profile.md`](spec/data-profile.md) for the full profile.

## Database refactor

The given model is compromised. It **is really altered** — not wrapped or shadowed —
on the downloaded copy, inside the import transaction, before any feed processing.
SQLAlchemy Core issues the DDL and the data-migration statements.

### The given model

```sql
CREATE TABLE Product (
    Id       INTEGER PRIMARY KEY AUTOINCREMENT,
    Name     TEXT NOT NULL,
    Brand    TEXT,          -- free text, inconsistent (BLACK+DECKER vs Black+Decker)
    Category TEXT            -- free text
);

CREATE TABLE SellerProduct (
    Id              INTEGER PRIMARY KEY AUTOINCREMENT,   -- pointless surrogate
    SellerName      TEXT NOT NULL,                       -- denormalized entity
    ProductId       INTEGER NOT NULL REFERENCES Product (Id),
    SellerProductId INTEGER NOT NULL                     -- wrong type: feed sends UUID strings
);
```

`SellerName` and `Product.Brand` / `Product.Category` are denormalized free text;
`SellerProductId` is typed `INTEGER` but the feed's identifier is a UUID string (3 are
not valid UUIDs) reused across sellers; the link table has a useless surrogate key and
no uniqueness constraint.

### Target model

```sql
CREATE TABLE Brand (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE          -- normalized brand string
);

CREATE TABLE Category (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE          -- normalized category string
);

CREATE TABLE Product (
    Id         INTEGER PRIMARY KEY AUTOINCREMENT,
    Name       TEXT NOT NULL,
    BrandId    INTEGER REFERENCES Brand (Id),      -- nullable (119 base rows have no brand)
    CategoryId INTEGER REFERENCES Category (Id)    -- nullable (34 base rows have no category)
);

CREATE TABLE Seller (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE
);

CREATE TABLE SellerProduct (              -- many-to-many link + the seller's own SKU
    SellerId    INTEGER NOT NULL REFERENCES Seller (Id),
    ProductId   INTEGER NOT NULL REFERENCES Product (Id),
    ExternalSku TEXT NOT NULL,            -- the feed entry's Id (seller's product id)
    PRIMARY KEY (SellerId, ProductId),
    UNIQUE (SellerId, ExternalSku)
);
```

### What is kept, altered, deleted

| Table | Change | Detail |
| --- | --- | --- |
| `Product` | **altered in place** | **kept**: `Id` (all 975 rows, values and `sqlite_sequence` preserved), `Name`. **added**: `BrandId`, `CategoryId` (nullable FKs). **deleted**: the `Brand` and `Category` text columns, after their data is migrated. |
| `SellerProduct` | **dropped and recreated** | `Id` removed (surrogate); `SellerName` removed (moved to `Seller`); `SellerProductId INTEGER` removed (becomes `ExternalSku TEXT`); `ProductId` kept. New `PRIMARY KEY (SellerId, ProductId)` and `UNIQUE (SellerId, ExternalSku)`. |
| `Brand` | **created** | populated from `Product.Brand` (see migration 1) |
| `Category` | **created** | populated from `Product.Category` (see migration 2) |
| `Seller` | **created** | populated from `SellerProduct.SellerName` (see migration 3) |

- **`Brand` and `Category` are reference tables, not junctions.** A product has zero or
  one of each, so each is a nullable foreign key on `Product`. A `BrandProduct` /
  `CategoryProduct` junction was rejected — it would permit a product with two brands
  (or two categories), and a `UNIQUE(ProductId)` to forbid that just re-creates the
  foreign key with extra steps.
- **`SellerProduct` is a junction** because a product genuinely has many sellers.
- **`ExternalSku`** carries the seller's own product identifier (the feed entry's `Id`),
  opaque text. `UNIQUE (SellerId, ExternalSku)` enforces that one of a seller's SKUs
  maps to at most one product. PascalCase for consistency with the given schema.
- `Brand.Name` / `Category.Name` store the normalized value; a human-readable display
  form is future work (`Brand.DisplayName`).

### When the refactor runs — conditional migration

Guarded by `PRAGMA user_version`, so the tool can be pointed at a database it has
already produced and used incrementally later.

| Detected source | Action |
| --- | --- |
| `user_version = 0` **and** legacy tables match (`Product.Brand` + `Product.Category` text columns, `SellerProduct` with `SellerName` + `SellerProductId`, no `Brand`/`Category` tables) | run the migrations below, then `PRAGMA user_version = 1` |
| `user_version = 1` **and** target tables match (`Brand`, `Category`, `Seller` present; `SellerProduct` has `ExternalSku`) | skip the migrations; go straight to feed processing |
| anything else | abort with an "unrecognized schema" error before any write |

The published `catalog.db` is always `user_version = 0` / legacy. Running against a
previously consolidated output (`user_version = 1`) re-applies the feed with idempotent
writes and changes nothing when the feed is unchanged.

### Migration steps (inside the single import transaction)

`PRAGMA foreign_keys = OFF` for the whole block; `ON` + `foreign_key_check` at the end.
Normalization is a Python function, so each extraction reads distinct values, folds
them in Python, and inserts through SQLAlchemy Core (not a pure `INSERT ... SELECT`).

**Migration 1 — `Product.Brand` → `Brand`**

1. `CREATE TABLE Brand (...)`.
2. `INSERT INTO Brand (Name)` — distinct normalized non-empty `Product.Brand` values (→ 637 rows).
3. `ALTER TABLE Product ADD COLUMN BrandId INTEGER REFERENCES Brand (Id)`.
4. `UPDATE Product SET BrandId = <lookup(normalized(Brand))>` — `NULL` where `Brand` is null/empty.
5. `ALTER TABLE Product DROP COLUMN Brand`.

**Migration 2 — `Product.Category` → `Category`** (same shape as migration 1)

1. `CREATE TABLE Category (...)`.
2. `INSERT INTO Category (Name)` — distinct normalized non-empty `Product.Category` values (→ 43 rows).
3. `ALTER TABLE Product ADD COLUMN CategoryId INTEGER REFERENCES Category (Id)`.
4. `UPDATE Product SET CategoryId = <lookup(normalized(Category))>` — `NULL` where absent.
5. `ALTER TABLE Product DROP COLUMN Category`.

**Migration 3 — `SellerProduct` rebuild** (PK changes, so a staged table, not `ALTER`)

1. `CREATE TABLE Seller (...)`; `INSERT INTO Seller (Name)` — distinct `SellerProduct.SellerName` (base table empty → 0 rows here; sellers are created later from the feed).
2. `CREATE TABLE SellerProduct_new (SellerId, ProductId, ExternalSku, PK, UNIQUE)`.
3. `INSERT INTO SellerProduct_new SELECT s.Id, o.ProductId, CAST(o.SellerProductId AS TEXT) FROM SellerProduct o JOIN Seller s ON s.Name = o.SellerName` (no-op — empty; written to work with data).
4. `DROP TABLE SellerProduct`; `ALTER TABLE SellerProduct_new RENAME TO SellerProduct`.

**Finalize:** `PRAGMA foreign_keys = ON`; `PRAGMA foreign_key_check`; `PRAGMA user_version = 1`.

### Accepted risks

- **Catalog brand and category strings are canonicalized** to their normalized form
  during the refactor; the original raw spelling is not preserved (`DisplayName` later).
- **`ExternalSku` is the SKU of the first feed entry that created the link.** When a
  seller sends the same resolved product again under a different feed `Id` (name
  variant), the later SKU is logged (`event=duplicate_listing`) and not stored.
- **`Product.CategoryId` allows `NULL`** even though the feed always carries a category —
  34 base rows have none, and existing products are never enriched.

## Identity model

Product identity = **normalized `Name`**. Brand (normalized) is only a tie-break gate.
Category never participates (it is a taxonomy, not an identity — and it disagrees even
for true duplicates). The feed `Id` never participates in identity — it is carried
through only as `SellerProduct.ExternalSku`.

Per-entry pipeline:

```
schema validation (pydantic)
  -> SQL injection screen (libinjection)  -- reject -> threat
  -> normalize (shared)
  -> exact lookup by normalized name (shared)
  -> gated fuzzy scan scored by Similarity (injected; difflib | rapidfuzz)
  -> identity policy + threshold (shared)
  -> outcome: link | insert + link | skip and report
```

Normalization (one function; catalog names, feed names, brands, categories): lowercase,
strip accents (NFKD), collapse whitespace, remove punctuation and quote marks, keep
digits. SQLite `lower()` is ASCII-only, so this is done in Python.

Fuzzy gate (only when the exact lookup misses): brands equal after normalization when
both are present, same word count, same numeric tokens, `score >= threshold`.

Outcomes per entry:

| Situation | Action |
| --- | --- |
| A field trips the SQL injection screen | reject the entry entirely; log `WARNING`; `threat += 1` |
| Exactly one product (exact match or one eligible fuzzy candidate) | `get_or_create` the seller, then `INSERT OR IGNORE` the `(SellerId, ProductId, ExternalSku)` link |
| No product and no eligible candidate | `get_or_create` the brand and the category, insert a new `Product`, then the link |
| Link already present, same or different feed `Id` | no change; if the incoming SKU differs, log `event=duplicate_listing` |
| Incoming `(SellerId, ExternalSku)` already maps to a different product | skip the entry, record it in the report (no silent re-association) |
| Two or more eligible candidates, or a brand conflict on an otherwise-matching name | skip the entry, record it in the report, continue |

Existing product attributes (including `BrandId` and `CategoryId`) are never enriched
or overwritten. A category difference between a linked entry and its product is logged
at `WARNING`.

## Ambiguity register (every class occurs in the real data)

| Class | Example | Decision |
| --- | --- | --- |
| Double space | `"Smartphone  Galaxy S23"` | normalization resolves |
| Accents | `"Câmera Canon EOS R6"` vs `"Camera Canon EOS R6"` | strip accents |
| Inch marks | `12.9"` / `12.9''` / `12.9` ; `55"` / `55` | remove punctuation |
| Brand spelling in the catalog | `BLACK+DECKER` vs `Black+Decker`; `Simplehuman` vs `simplehuman` | one `Brand` row per normalized name |
| Brand apostrophe in the feed | feed `"Levi's"` vs catalog `"Levis"` | normalize brand before comparing / before the `Brand` table |
| Category disagreement | feed `Photo` vs catalog `Photography` | distinct `Category` rows; category is not identity; link the entry to its existing product and log |
| PT<->EN translation | `"Roteador WiFi 6 TP-Link"` <-> `"Router WiFi 6 TP-Link"` (difflib 0.909) | fuzzy resolves; fragile at the 0.90 cutoff — documented |
| SQL injection probe | `Brand = "TestBrand'; SELECT 1; --"` | detected by libinjection; entry rejected, counted as `threat` |
| Invalid / reused feed `Id` | 3 non-UUID, 14 reused across sellers | stored as opaque `ExternalSku`; reuse is across different sellers, so `UNIQUE (SellerId, ExternalSku)` holds; identity still comes from `Name` |
| Same seller offers the same product twice | `GardenStore` Câmera/Camera, plus 11 more entries | one link; the first entry's SKU is kept, the rest log `duplicate_listing` |
| Null `Brand` / `Category` | brand: 3 feed / 119 catalog rows; category: 34 catalog rows | `BrandId` / `CategoryId` is `NULL`; absent brand does not block a name match |

## Matcher layer (parameter injection)

One injection point: the similarity backend, constructed at the CLI edge and passed as
a keyword argument. No DI container, no framework.

```python
from typing import Protocol

class Similarity(Protocol):
    name: str
    suggested_threshold: float
    def score(self, a: str, b: str) -> float: ...   # a, b already normalized -> [0, 1]
```

- `DifflibSimilarity` — `SequenceMatcher(None, a, b, autojunk=False).ratio()`, stdlib.
- `RapidFuzzSimilarity` — `fuzz.ratio(a, b) / 100.0`, external.
- `fuzz.WRatio` / `token_set_ratio` / `token_sort_ratio` are disallowed — they hide
  extra terms that matter for identity (capacity, model).
- `fuzz.ratio` and `difflib.ratio` are both `2M/T`; on this data both give `0.909`
  for `Roteador/Router`, so the same `0.90` threshold works. Not guaranteed in
  general, hence each backend carries its own `suggested_threshold`; a `THRESHOLD` in
  `.env` or the environment overrides it.

Factory with lazy imports (the `difflib` path never imports `rapidfuzz`):

```python
def build_similarity(name: str) -> Similarity:
    if name == "difflib":
        from .difflib_impl import DifflibSimilarity
        return DifflibSimilarity()
    if name == "rapidfuzz":
        from .rapidfuzz_impl import RapidFuzzSimilarity
        return RapidFuzzSimilarity()
    raise SystemExit(f"unknown matcher: {name!r} (options: difflib, rapidfuzz)")
```

```python
def consolidate(entries, engine, *, similarity: Similarity, threshold: float) -> Report: ...
```

Candidate retrieval for the fuzzy stage is a plain `select(Product)` scan (975 rows;
instant). Indexed candidate reduction is out of scope.

## Execution flow

1. Resolve and validate configuration (CLI plus `.env` next to the entry point).
2. Download `catalog.db` in chunks to a temp file in the output directory (fresh every run).
3. Verify the SQLite header; read `PRAGMA user_version` and check the table shape to
   classify the source as legacy, already-migrated, or unrecognized (abort on the last).
4. Open a SQLAlchemy engine on the temp file, begin **one** transaction; run the
   [conditional refactor](#when-the-refactor-runs--conditional-migration) — full
   migration for a legacy source, nothing for an already-migrated one.
5. Stream `ProductEntry.json` with `requests` + `ijson`; validate each object with
   Pydantic; screen each string field with `libinjection`.
6. For each surviving entry: resolve the product; when inserting a new product,
   `get_or_create` its brand and category; `get_or_create` the seller; link them
   (idempotent); accumulate `processed`, `new`, `linked`, `skipped`, `threat`.
7. Consume the entire document before committing.
8. Commit -> dispose the engine -> atomically replace the output with the temp file.

Any failure (network, JSON, schema validation, database) -> rollback, discard the temp
file, previous output preserved. Contained threats and skips do not fail the run. No
partial resume.

## Out of scope for the first iteration

Indexed candidate reduction (FTS5 / trigram / spellfix1), the SQLAlchemy ORM, Alembic
versioned migrations, `Brand.DisplayName` / `Category.DisplayName`, a persisted product
name index, global product identity, labeled-data evaluation, processing resume.
