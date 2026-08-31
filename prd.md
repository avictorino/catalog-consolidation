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

## Data (profiled from the published sources)

- `Product`: 975 rows. `Id INTEGER PRIMARY KEY AUTOINCREMENT`, `Name NOT NULL`,
  `Brand` and `Category` nullable. Names are unique after normalization (no collisions).
- `SellerProduct`: **empty**. Original columns `Id`, `SellerName`, `ProductId`
  (FK → `Product.Id`), `SellerProductId INTEGER NOT NULL`. No index, no uniqueness
  constraint. See [Database refactor](#database-refactor) — this model is replaced.
- `ProductEntry.json`: 269 entries, all five fields always present. `Brand` is null in 3.
  `Id` is a text UUID (3 are malformed), reused across sellers — it is not a product
  identity, and in the refactored model it is not stored at all.
- Distinct seller names in the feed: 20.
- Expected result: **976 products** (one new: `Security Test Product`), **20 sellers**,
  and **257 seller-product links**. Portuguese→English name variants resolve as
  matches, not as new products. The link count is below 269 because several sellers
  submit the same product more than once (different feed `Id`, name variant); the
  refactored link table records the relationship once.

See [`spec/data-profile.md`](spec/data-profile.md) for the full profile.

## Database refactor

The given `SellerProduct` model is compromised and is rebuilt on every run, right after
the catalog is downloaded, before any feed processing.

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
  later, attributes: contact, status, commission). Repeating its name as a string on
  every link row is the exact inconsistency class we fight in product names
  (`"Levi's"` vs `"Levis"`), and "list all sellers" becomes a `DISTINCT` scan.
- **`SellerProductId INTEGER` has the wrong type.** The feed's identifier is a UUID
  string; 3 values are not even valid UUIDs, and it is reused across sellers, so it
  cannot be a key.
- **A surrogate `Id` on a junction table adds nothing.** The natural key is
  `(SellerId, ProductId)`.
- **No uniqueness constraint.** Nothing prevents the same seller/product pair being
  inserted twice.

### New model

```sql
CREATE TABLE Product (          -- unchanged from the base catalog
    Id       INTEGER PRIMARY KEY AUTOINCREMENT,
    Name     TEXT NOT NULL,
    Brand    TEXT,
    Category TEXT
);

CREATE TABLE Seller (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE
);

CREATE TABLE SellerProduct (   -- pure many-to-many link
    SellerId  INTEGER NOT NULL REFERENCES Seller (Id),
    ProductId INTEGER NOT NULL REFERENCES Product (Id),
    PRIMARY KEY (SellerId, ProductId)
);
```

`Seller` owns seller identity. `SellerProduct` answers exactly one question — which
sellers offer which products — and its composite primary key makes that relationship
idempotent by construction.

### Migration steps (inside the single import transaction)

The base `SellerProduct` is empty, so in practice this is a rebuild with no row
migration. It is written to also work if rows exist:

1. `PRAGMA foreign_keys = OFF`.
2. `CREATE TABLE Seller (...)`.
3. `INSERT INTO Seller (Name) SELECT DISTINCT SellerName FROM SellerProduct` (no-op when empty).
4. `ALTER TABLE SellerProduct RENAME TO SellerProduct_old`.
5. `CREATE TABLE SellerProduct (SellerId, ProductId, PRIMARY KEY (SellerId, ProductId))`.
6. `INSERT OR IGNORE INTO SellerProduct (SellerId, ProductId)
   SELECT s.Id, o.ProductId FROM SellerProduct_old o JOIN Seller s ON s.Name = o.SellerName`.
7. `DROP TABLE SellerProduct_old`.
8. `PRAGMA foreign_keys = ON`; `PRAGMA foreign_key_check`.

### Accepted risk

Dropping `SellerProductId` means the consolidated catalog no longer stores the seller's
own product identifier (their SKU). The challenge only requires recording which sellers
offer each product, which the junction table does. Reconsider if a later requirement
needs to map a link back to the seller's catalog (per-listing price/stock updates,
order routing): `SellerProductId` returns as a nullable column on `SellerProduct`, or
as a `SellerListing` table.

## Identity model

Product identity = **normalized `Name`**. `Brand` (normalized) is only a tie-break
gate. `Category` never participates. `Id` never participates (it is a seller SKU and is
not stored).

Per-entry pipeline:

```
normalize (shared)
  -> exact lookup by normalized name (shared)
  -> gated fuzzy scan scored by Similarity (injected; difflib | rapidfuzz)
  -> identity policy + threshold (shared)
  -> outcome: link | insert + link | skip and report
```

Normalization (one function, applied to catalog names, feed names, and brands):
lowercase, strip accents (NFKD), collapse whitespace, remove punctuation and quote
marks, keep digits. SQLite `lower()` is ASCII-only, so normalization is done in Python.

Fuzzy gate (only when the exact lookup misses): brands equal after normalization when
both are present, same word count, same numeric tokens, `score >= backend threshold`.

Outcomes per entry:

| Situation | Action |
| --- | --- |
| Exactly one product (exact match or one eligible fuzzy candidate) | `get_or_create` the seller, then `INSERT OR IGNORE` the `(SellerId, ProductId)` link |
| No product and no eligible candidate | insert a new `Product`, then the link |
| Link already present | no change (absorbed by the composite primary key) |
| Two or more eligible candidates, or a brand conflict on an otherwise-matching name | skip the entry, record it in the report, continue (do not abort the import) |

Existing product attributes are never enriched or overwritten. A category difference
between a linked entry and its product is logged at `WARNING` and otherwise ignored.

## Ambiguity register (every class occurs in the real data)

| Class | Example | Decision |
| --- | --- | --- |
| Double space | `"Smartphone  Galaxy S23"` | normalization resolves |
| Accents | `"Câmera Canon EOS R6"` vs `"Camera Canon EOS R6"` | strip accents |
| Inch marks | `12.9"` / `12.9''` / `12.9` ; `55"` / `55` | remove punctuation |
| Brand apostrophe | feed `"Levi's"` vs catalog `"Levis"` | normalize brand before comparing |
| Category disagreement | feed `Photo` vs catalog `Photography` | category is not identity; link and log |
| PT<->EN translation | `"Roteador WiFi 6 TP-Link"` <-> `"Router WiFi 6 TP-Link"` (difflib 0.909) | fuzzy resolves; fragile at the 0.90 cutoff — documented |
| SQL injection probe | `Brand = "TestBrand'; SELECT 1; --"` | parameterized SQL; legitimate new product |
| Invalid / reused feed `Id` | 3 non-UUID, 14 reused across sellers | `Id` is opaque and not stored; identity comes from `Name` |
| Same seller offers the same product twice | `GardenStore` Câmera/Camera, plus 11 more entries | one `(SellerId, ProductId)` link; the extra entries collapse |
| Null `Brand` | `"Cable Organizer Kit"`, `"Bed Frame Wood King"`, `"Round Rug 6 Feet"` | absent brand does not block a name match |

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
  general, hence each backend carries its own `suggested_threshold`.

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
def consolidate(entries, conn, *, similarity: Similarity, threshold: float) -> Report: ...
```

Candidate retrieval is a plain full scan of the catalog (975 rows; instant). Indexed
candidate reduction is not implemented — see [Out of scope](#out-of-scope-for-the-first-iteration).

## Execution flow

1. Resolve and validate configuration (CLI plus optional `.env` next to the entry point).
2. Download `catalog.db` in chunks to a temp file in the output directory (fresh every run).
3. Verify the SQLite header and the expected schema.
4. Open the connection, begin **one** transaction, run the [database refactor](#migration-steps-inside-the-single-import-transaction).
5. Stream `ProductEntry.json` with `requests` + `ijson`; validate each object with
   Pydantic (`model_validate`, one at a time).
6. For each entry: resolve the product, `get_or_create` the seller, link them;
   accumulate counters and the skipped list.
7. Consume the entire document before committing.
8. Commit -> close the connection -> atomically replace the output with the temp file.

Any failure (network, JSON, validation, database) -> rollback, discard the temp file,
previous output preserved. No partial resume.

## Out of scope for the first iteration

Indexed candidate reduction (FTS5 / trigram / spellfix1), persisted normalized columns,
storing the seller SKU, global product identity, labeled-data evaluation, processing
resume.
