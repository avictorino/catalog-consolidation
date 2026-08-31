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
| Database access + schema | **SQLAlchemy Core 2.x** | declarative target schema, parameterized statements, `insert()` for the refactor |
| Schema refactor | **Alembic 1.14** (single revision `0001`) | one hand-written migration, driven programmatically with an injected connection so it shares the import transaction; `alembic_version` is the legacy/migrated marker |
| Primary keys | **UUID (`uuid4`) as `TEXT`**, generated in Python | non-enumerable, generated without DB coordination, stable across environments; no `AUTOINCREMENT` anywhere |
| Config | `python-dotenv` | every CLI option has a default in `.env` |

`libinjection-python` is GPLv3 (the underlying libinjection C library is BSD-3). If a
permissive-only dependency tree is required, the screening module is the single swap
point — its `Similarity`-style seam takes any `is_sqli(str) -> bool`.

**Not used:** the SQLAlchemy ORM (no object graph here — bulk reads and inserts only),
and Alembic's revision-chain / autogenerate / offline (`--sql`) machinery. The refactor
is a single [conditional migration](spec/data-profile.md#conditional-migration): one
hand-written Alembic revision (`0001`), run programmatically from
`consolidation.pipeline` with the live connection injected via
`config.attributes["connection"]` so it executes inside the single import transaction.
Alembic's `alembic_version` table replaces a `PRAGMA user_version` guard as the
legacy/already-migrated marker.

## Data

The published sources profile out to a **975-row `Product` table** (637 distinct
brands, 43 categories after normalization; some brand/category values null), an
**empty** `SellerProduct` link table with a mistyped SKU column, and a **269-entry
feed** from **20 sellers** whose `Id` field is a per-listing UUID (a few malformed,
some reused across sellers).

Full profile and every ambiguity class: [`spec/data-profile.md`](spec/data-profile.md).
Expected result: [`spec/acceptance.md`](spec/acceptance.md).

## Database refactor

> **Schema, `user_version` guard, kept/replaced/deleted breakdown, and migration steps:
> [`spec/data-profile.md#refactored-database`](spec/data-profile.md#refactored-database)** —
> the single source of truth. This section covers only *why*.

The given model is compromised, so on every run — before any feed processing, inside
the single import transaction — both given tables are dropped and recreated and three
reference tables (`Brand`, `Category`, `Seller`) are added, all with `uuid4` `TEXT`
primary keys. It is a conditional migration keyed on Alembic's `alembic_version` marker
(a legacy source is migrated by revision `0001`; an already-migrated one leaves
`alembic upgrade head` a no-op), so the tool can also run incrementally against its own
output.

### Why the given model is wrong

- **`SellerName` is denormalized free text.** A seller is an entity with its own
  identity (and, later, attributes); repeating its name per link row is the exact
  inconsistency class we fight in product names.
- **`Product.Brand` and `Product.Category` are denormalized the same way**, and `Brand`
  is already inconsistent in the catalog (`BLACK+DECKER` vs `Black+Decker`).
- **`SellerProductId INTEGER` is the wrong type** — the feed's identifier is a UUID
  string (3 not even valid UUIDs), reused across sellers, so it cannot be a key.
- **The surrogate `Id` on a link table adds nothing** — the natural key is
  `(SellerId, ProductId)` — and nothing enforces its uniqueness.
- **`AUTOINCREMENT` integer keys are enumerable** and need DB coordination to mint.

### Design decisions

- **`Brand` and `Category` are reference tables, not junctions.** A product has 0..1 of
  each → a nullable FK on `Product`. A `BrandProduct` / `CategoryProduct` junction was
  rejected: it would permit a product with two brands, and a `UNIQUE(ProductId)` to
  forbid that just re-creates the FK with extra steps. `SellerProduct` stays a junction
  because a product genuinely has many sellers.
- **The seller's own id is kept** as `SellerProduct.ExternalSku` (opaque text, distinct
  from our `Product.Id`), with `UNIQUE (SellerId, ExternalSku)` — needed to map a
  listing back to the seller's catalog.
- **UUID (`uuid4`) primary keys** — non-enumerable, minted without DB coordination,
  stable across environments.

### Accepted risks

- **UUID `TEXT` primary keys** are ~36 bytes and non-sequential → larger indexes and
  worse insert locality than integer keys. Negligible at this volume; `BLOB(16)` or a
  time-ordered UUIDv7 would mitigate it if it mattered.
- **Catalog brand and category strings are canonicalized** to their normalized form;
  the original raw spelling is not preserved (`DisplayName` later).
- **`ExternalSku` is the id of the first feed entry that created the link.** A later
  entry for the same resolved product logs `event=duplicate_listing` and is not stored.
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
3. Verify the SQLite header; check the table shape and the presence of `alembic_version`
   to classify the source as legacy, already-migrated, or unrecognized (abort on the last).
4. Open a SQLAlchemy engine on the temp file, begin **one** transaction; run
   `alembic upgrade head` with that connection injected — revision `0001` performs the
   full [database refactor](spec/data-profile.md#refactored-database) for a legacy
   source and is a no-op for an already-migrated one.
5. Stream `ProductEntry.json` with `requests` + `ijson`; validate each object with
   Pydantic; screen each string field with `libinjection`.
6. For each surviving entry: resolve the product; when inserting a new product, mint a
   `uuid4` and `get_or_create` its brand and category; `get_or_create` the seller; link
   them (idempotent); accumulate `processed`, `new`, `linked`, `skipped`, `threat`.
7. Consume the entire document before committing.
8. Commit -> dispose the engine -> atomically replace the output with the temp file.

Any failure (network, JSON, schema validation, database) -> rollback, discard the temp
file, previous output preserved. Contained threats and skips do not fail the run. No
partial resume.

## Out of scope for the first iteration

Indexed candidate reduction (FTS5 / trigram / spellfix1), the SQLAlchemy ORM, an Alembic
revision chain / autogenerate / offline mode (only the single hand-written `0001`),
`Brand.DisplayName` / `Category.DisplayName`, `BLOB(16)` / UUIDv7 key encoding, a
persisted product name index, global product identity, labeled-data evaluation,
processing resume.
