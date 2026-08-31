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
and Alembic (there is no persistent database evolving across versions; every run
downloads a fresh `catalog.db` and applies the same one-shot refactor). The "migration"
is a single idempotent `refactor(engine)` step, expressed with Core constructs.

## Data (profiled from the published sources)

- `Product`: 975 rows. `Id INTEGER PRIMARY KEY AUTOINCREMENT`, `Name NOT NULL`,
  `Brand` and `Category` nullable. Names are unique after normalization (no collisions).
  639 distinct raw brand strings; **637** after normalization (`BLACK+DECKER` /
  `Black+Decker`, `Simplehuman` / `simplehuman`); **119** rows have no brand.
- `SellerProduct`: **empty**. Original columns `Id`, `SellerName`, `ProductId`,
  `SellerProductId INTEGER NOT NULL`. No index, no uniqueness constraint.
- `ProductEntry.json`: 269 entries, all five fields always present. `Brand` is null in 3.
  `Id` is a text UUID (3 malformed), reused only across different sellers — not a product
  identity, kept as `SellerProduct.ExternalSku`.
- 20 distinct seller names in the feed.
- Expected result (see [`spec/acceptance.md`](spec/acceptance.md)): **975 products**,
  **637 brands**, **20 sellers**, **256 seller-product links**, **1 threat**.

See [`spec/data-profile.md`](spec/data-profile.md) for the full profile.

## Database refactor

The given model is compromised and is rebuilt on every run, right after the catalog is
downloaded, inside the import transaction, before any feed processing.

### Why the original model is wrong

```sql
CREATE TABLE SellerProduct (
    Id              INTEGER PRIMARY KEY AUTOINCREMENT,
    SellerName      TEXT NOT NULL,
    ProductId       INTEGER NOT NULL REFERENCES Product (Id),
    SellerProductId INTEGER NOT NULL
);
```

- **`SellerName` is denormalized.** A seller is an entity with its own identity (and,
  later, attributes). Repeating its name as a string on every link row is the exact
  inconsistency class we fight in product names, and "list all sellers" is a `DISTINCT`
  scan.
- **`Product.Brand` is denormalized** the same way — a free-text string repeated across
  products, already inconsistent in the catalog (`BLACK+DECKER` vs `Black+Decker`).
- **`SellerProductId INTEGER` has the wrong type.** The feed's identifier is a UUID
  string; 3 values are not valid UUIDs; it is reused across sellers, so it is not a key.
- **A surrogate `Id` on a junction table adds nothing.** The natural key is
  `(SellerId, ProductId)`.
- **No uniqueness constraint** on the seller/product pair.

### New model

```sql
CREATE TABLE Brand (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE          -- normalized brand string
);

CREATE TABLE Product (
    Id       INTEGER PRIMARY KEY AUTOINCREMENT,
    Name     TEXT NOT NULL,
    BrandId  INTEGER REFERENCES Brand (Id),   -- nullable: 119 base rows have no brand
    Category TEXT
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

- **`Brand` is a reference table, not a junction.** A product has zero or one brand, so
  the relationship is `Brand` 1 : N `Product` — a nullable foreign key on `Product`.
- **`SellerProduct` is a junction** because a product genuinely has many sellers — that
  is the point of the challenge.
- **`ExternalSku`** carries the seller's own product identifier (the feed entry's `Id`),
  stored as opaque text. `UNIQUE (SellerId, ExternalSku)` enforces that one of a
  seller's SKUs maps to at most one product. Column name is PascalCase for consistency
  with the rest of the schema (the given catalog uses `SellerName`, `SellerProductId`).
- `Brand.Name` stores the normalized brand (lowercase, accents and punctuation folded).
  A human-readable display form is out of scope; add `Brand.DisplayName` if needed
  later. Consequence: the 2 catalog brand pairs that differ only by case/punctuation
  collapse to one row each.

### Rejected alternative — `BrandProduct` junction

A `BrandProduct (BrandId, ProductId)` link table was considered and rejected: it would
permit a product with two brands (nonsense in this domain), add a join to every
brand-aware query, and a `UNIQUE(ProductId)` to forbid the nonsense just re-creates a
foreign key with extra steps.

### When the refactor runs — conditional migration

The refactor is a **migration guarded by `PRAGMA user_version`**, not an unconditional
rebuild, so the tool can be pointed at a database it has already produced and used
incrementally later.

| Detected source | Action |
| --- | --- |
| `user_version = 0` **and** the legacy tables match (`Product` with a `Brand` column, `SellerProduct` with `SellerName` + `SellerProductId`, no `Brand` table) | run the migration below, then `PRAGMA user_version = 1` |
| `user_version = 1` **and** the target tables match (`Brand` table present, `SellerProduct` has `ExternalSku`) | skip the migration; go straight to feed processing |
| anything else | abort with an "unrecognized schema" error before any write |

The published `catalog.db` is always `user_version = 0` / legacy, so a normal run always
migrates. Running against a previously consolidated output (`user_version = 1`) re-applies
the feed with idempotent writes (`INSERT OR IGNORE`, `get_or_create`) and changes nothing
when the feed is unchanged.

### Migration steps (inside the single import transaction)

Normalization is a Python function, so the brand/seller extraction cannot be a pure
`INSERT ... SELECT`; it reads distinct values, folds them in Python, and inserts
through SQLAlchemy Core.

1. `PRAGMA foreign_keys = OFF`.
2. Create `Brand`, `Seller`, and the target `Product` / `SellerProduct` (staged names).
3. Populate `Brand` from `DISTINCT` non-empty `Product.Brand`, normalized and deduped.
4. Rebuild `Product`: copy `Id`, `Name`, `Category`; set `BrandId` from the normalized
   brand lookup (`NULL` when the source brand is null/empty). `Id` values are preserved.
5. Populate `Seller` from `DISTINCT SellerProduct.SellerName` (no-op — base table empty).
6. Rebuild `SellerProduct` as `(SellerId, ProductId, ExternalSku)` via the seller-name
   join, carrying `SellerProductId` across as text (no-op — base table empty).
7. Drop the old tables; rename staged tables into place.
8. `PRAGMA foreign_keys = ON`; `PRAGMA foreign_key_check`; `PRAGMA user_version = 1`.

### Accepted risks

- **Catalog brand strings are canonicalized** to their normalized form during the
  refactor; the original raw spelling is not preserved (add `Brand.DisplayName` later).
- **`ExternalSku` is the SKU of the first feed entry that created the link.** When a
  seller sends the same resolved product again under a different feed `Id` (name
  variant), the later SKU is logged (`event=duplicate_listing`) and not stored.

## Identity model

Product identity = **normalized `Name`**. Brand (normalized) is only a tie-break gate.
`Category` never participates. The feed `Id` never participates in identity — it is
carried through only as `SellerProduct.ExternalSku`.

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

Normalization (one function; catalog names, feed names, brands): lowercase, strip
accents (NFKD), collapse whitespace, remove punctuation and quote marks, keep digits.
SQLite `lower()` is ASCII-only, so this is done in Python.

Fuzzy gate (only when the exact lookup misses): brands equal after normalization when
both are present, same word count, same numeric tokens, `score >= threshold`.

Outcomes per entry:

| Situation | Action |
| --- | --- |
| A field trips the SQL injection screen | reject the entry entirely; log `WARNING`; `threat += 1` |
| Exactly one product (exact match or one eligible fuzzy candidate) | `get_or_create` the seller, then `INSERT OR IGNORE` the `(SellerId, ProductId, ExternalSku)` link |
| No product and no eligible candidate | `get_or_create` the brand, insert a new `Product`, then the link |
| Link already present, same or different feed `Id` | no change; if the incoming SKU differs, log `event=duplicate_listing` |
| Incoming `(SellerId, ExternalSku)` already maps to a different product | skip the entry, record it in the report (no silent re-association) |
| Two or more eligible candidates, or a brand conflict on an otherwise-matching name | skip the entry, record it in the report, continue |

Existing product attributes (including `BrandId`) are never enriched or overwritten.
A category difference between a linked entry and its product is logged at `WARNING`.

## Ambiguity register (every class occurs in the real data)

| Class | Example | Decision |
| --- | --- | --- |
| Double space | `"Smartphone  Galaxy S23"` | normalization resolves |
| Accents | `"Câmera Canon EOS R6"` vs `"Camera Canon EOS R6"` | strip accents |
| Inch marks | `12.9"` / `12.9''` / `12.9` ; `55"` / `55` | remove punctuation |
| Brand spelling in the catalog | `BLACK+DECKER` vs `Black+Decker`; `Simplehuman` vs `simplehuman` | one `Brand` row per normalized name |
| Brand apostrophe in the feed | feed `"Levi's"` vs catalog `"Levis"` | normalize brand before comparing |
| Category disagreement | feed `Photo` vs catalog `Photography` | category is not identity; link and log |
| PT<->EN translation | `"Roteador WiFi 6 TP-Link"` <-> `"Router WiFi 6 TP-Link"` (difflib 0.909) | fuzzy resolves; fragile at the 0.90 cutoff — documented |
| SQL injection probe | `Brand = "TestBrand'; SELECT 1; --"` | detected by libinjection; entry rejected, counted as `threat` |
| Invalid / reused feed `Id` | 3 non-UUID, 14 reused across sellers | stored as opaque `ExternalSku`; reuse is across different sellers, so `UNIQUE (SellerId, ExternalSku)` holds; identity still comes from `Name` |
| Same seller offers the same product twice | `GardenStore` Câmera/Camera, plus 11 more entries | one link; the first entry's SKU is kept, the rest log `duplicate_listing` |
| Null `Brand` | 3 feed entries, 119 catalog rows | `BrandId` is `NULL`; absent brand does not block a name match |

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
6. For each surviving entry: resolve the product, `get_or_create` the brand (only when
   inserting a new product), `get_or_create` the seller, link them (idempotent);
   accumulate `processed`, `new`, `linked`, `skipped`, `threat`.
7. Consume the entire document before committing.
8. Commit -> dispose the engine -> atomically replace the output with the temp file.

Any failure (network, JSON, schema validation, database) -> rollback, discard the temp
file, previous output preserved. Contained threats and skips do not fail the run. No
partial resume.

## Out of scope for the first iteration

Indexed candidate reduction (FTS5 / trigram / spellfix1), the SQLAlchemy ORM, Alembic
versioned migrations, `Brand.DisplayName`, a persisted product name index, global
product identity, labeled-data evaluation, processing resume.
