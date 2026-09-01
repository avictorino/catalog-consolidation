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
| Byte transport | `requests` (streaming) / `boto3` (`get_object`) | interchangeable `ByteSource` backends behind `--source http\|s3`; both feed the same chunked download and streamed feed. `boto3` imported lazily |
| JSON | `ijson` | incremental array parsing; no full document in memory |
| Feed validation | `pydantic` v2 | one object at a time (`model_validate`) |
| Similarity | `difflib` (stdlib) / `rapidfuzz` | interchangeable `score()` backends |
| SQL injection screen | `libinjection` (`libinjection-python`) | WAF-grade tokenizer (ModSecurity uses libinjection); wrapper is GPLv3, core is BSD-3, no cp311/cp312 wheel |
| Database access + schema | **SQLAlchemy Core 2.x** | declarative target schema, parameterized statements, `insert()` for the refactor |
| Schema refactor | **Alembic 1.14** (single revision `0001`) | one hand-written migration, driven programmatically with an injected connection inside the setup transaction; `alembic_version` is the legacy/migrated marker |
| Primary keys | **UUID (`uuid4`) as `TEXT`**, generated in Python | non-enumerable, generated without DB coordination, stable across environments; no `AUTOINCREMENT` anywhere |
| Config | `python-dotenv` | every CLI option has a default in `.env` |

`libinjection-python` is GPLv3 (the underlying libinjection C library is BSD-3). If a
permissive-only dependency tree is required, the screening module is the single swap
point — its `Similarity`-style seam takes any `is_sqli(str) -> bool`.

**Not used:** the SQLAlchemy ORM (no object graph here — bulk reads and inserts only),
and Alembic's revision-chain / autogenerate / offline (`--sql`) machinery. The refactor
is a single [conditional migration](spec/data-profile.md#conditional-migration): one
hand-written Alembic revision (`0001`), driven programmatically from
`consolidation.infrastructure` (the SQLite `CatalogRepository` adapter) with the live
connection injected via `config.attributes["connection"]` so it executes inside the
setup transaction. Alembic's `alembic_version` table replaces a `PRAGMA user_version`
guard as the legacy/already-migrated marker.

## Architecture (DDD, simplified)

Seven modules, one per layer — full walkthrough in
[`docs/arquitetura.md`](docs/arquitetura.md):

| Module | Layer | What it holds |
| --- | --- | --- |
| `domain.py` | domain | `normalize` / `new_uuid` / `brands_compatible`, the `Product` and `Catalog` entities, the `Submission` contract — no project or I/O imports |
| `services.py` | domain services / ports | `resolve_product` (the identity rules) wrapped by the `ProductIdentityResolver` facade (backend + threshold); the `Similarity` port; the `ByteSource` port |
| `repository.py` | repositories | the five aggregate repositories + `CatalogRepositories` (bundle): the **only** code that touches the connection — `load_catalog`, `entry_transaction()`, `reload()`; the connection is private (`_conn`) |
| `schema.py` | persistence schema | SQLAlchemy `Table` metadata only; no function takes a `Connection` |
| `infrastructure.py` | infrastructure | `HttpByteSource`/`S3ByteSource` + `build_source`, `download_to` / `verify_sqlite_header`, streamed feed (`iter_feed`) + ACL (`ProductEntry`), `screen_entry`, `Difflib`/`RapidFuzz` backends + `build_similarity`, Alembic wiring, the migration steps (`classify_source`, `rebuild_*`, …), and `SqliteCatalogRepository` (the `CatalogRepository` adapter) |
| `usecase.py` | application | `PrepareCatalogDatabaseUseCase`, `ConsolidateFeedUseCase`, `ConsolidateEntryUseCase`, the `ConsolidateCatalogUseCase` coordinator, the `Report` read model, and the `CatalogRepository` port definition |
| `cli.py` | interface / composition root | resolves config, builds the `ProductIdentityResolver` and `SqliteCatalogRepository`, runs the two use cases in order |

Injected, never built by the use cases: **one** `CatalogRepository` (prepares the DB
*and* hands out the `CatalogRepositories` bundle) and **one** `ProductIdentityResolver`
(already carrying the similarity backend and the threshold).

## Data

The published sources profile out to a **975-row `Product` table** (637 distinct
brands, 43 categories after normalization; some brand/category values null), an
**empty** `SellerProduct` link table with a mistyped SKU column, and a **269-entry
feed** from **20 sellers** whose `Id` field is a per-listing UUID (a few malformed,
some reused across sellers).

Full profile and every ambiguity class: [`spec/data-profile.md`](spec/data-profile.md).
Expected result: [`spec/acceptance.md`](spec/acceptance.md).

## Database refactor

> **Schema, `alembic_version` guard, kept/replaced/deleted breakdown, and migration
> steps: [`spec/data-profile.md#refactored-database`](spec/data-profile.md#refactored-database)** —
> the single source of truth. This section covers only *why*.

The given model is compromised, so when the source is legacy — before any feed processing,
inside the setup transaction — both given tables are dropped and recreated and the three
reference tables (`Brand`, `Category`, `Seller`) plus `ProductCategory` are added, all
with `uuid4` `TEXT` primary keys where applicable. It is a conditional migration keyed on Alembic's `alembic_version` marker
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

- **`Brand` is a reference table and `Category` is a reference table with memberships.**
  A product has 0..1 brand through nullable `Product.BrandId`, while
  `ProductCategory(ProductId, CategoryId)` allows multiple category memberships.
  `SellerProduct` remains a junction because a product genuinely has many sellers.
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
- **`ProductCategory` allows products with no category memberships** — 34 base rows have
  none. Existing products retain their memberships and can gain a new category when a
  linked feed entry supplies a category not already associated with the product.

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
  -> exact normalized token multiset, ignoring word order (shared)
  -> gated fuzzy scan scored by Similarity (injected; difflib | rapidfuzz)
  -> identity policy + threshold (shared)
  -> outcome: link | insert + link | skip and report
```

Normalization (one function; catalog names, feed names, brands, categories): lowercase,
strip accents (NFKD), collapse whitespace, remove punctuation and quote marks, keep
digits. SQLite `lower()` is ASCII-only, so this is done in Python.

When exact-name lookup misses, compare normalized word multisets: order may differ,
but word counts, repetitions and model/capacity tokens must remain identical. Link
only a unique brand-compatible candidate; otherwise report ambiguity or a brand
conflict. This rule is independent of the matcher and its threshold. It assumes word
order alone does not change identity; directional names may need domain-specific
handling beyond this policy.

Fuzzy gate (only when both exact-name and word-multiset lookup miss): brands equal
after normalization when both are present, same word count, same numeric tokens,
`score >= threshold`.

Outcomes per entry:

| Situation | Action |
| --- | --- |
| A field trips the SQL injection screen | reject the entry entirely; log `WARNING`; `threat += 1` |
| Exactly one product (exact name, same words, or one eligible fuzzy candidate) | `get_or_create` the seller, then `INSERT OR IGNORE` the `(SellerId, ProductId, ExternalSku)` link |
| No product and no eligible candidate | `get_or_create` the brand and category, insert a new `Product`, add its `ProductCategory` membership, then the seller link |
| Link already present, same or different feed `Id` | no change; if the incoming SKU differs, log `event=duplicate_listing` |
| Incoming `(SellerId, ExternalSku)` already maps to a different product | skip the entry, record it in the report (no silent re-association) |
| Two or more eligible candidates, or a brand conflict on an otherwise-matching name | skip the entry, record it in the report, continue |

Existing product attributes, including `BrandId`, are never enriched or overwritten. A
linked entry's category is added to `ProductCategory` when necessary, and a category
difference is logged at `WARNING`.

## Ambiguity register (every class occurs in the real data)

| Class | Example | Decision |
| --- | --- | --- |
| Double space | `"Smartphone  Galaxy S23"` | normalization resolves |
| Accents | `"Câmera Canon EOS R6"` vs `"Camera Canon EOS R6"` | strip accents |
| Inch marks | `12.9"` / `12.9''` / `12.9` ; `55"` / `55` | remove punctuation |
| Brand spelling in the catalog | `BLACK+DECKER` vs `Black+Decker`; `Simplehuman` vs `simplehuman` | one `Brand` row per normalized name |
| Brand apostrophe in the feed | feed `"Levi's"` vs catalog `"Levis"` | normalize brand before comparing / before the `Brand` table |
| Category disagreement | feed `Photo` vs catalog `Photography` | distinct category memberships; category is not identity; link the entry to its existing product and log |
| PT<->EN translation | `"Roteador WiFi 6 TP-Link"` <-> `"Router WiFi 6 TP-Link"` (difflib 0.909) | fuzzy resolves; fragile at the 0.90 cutoff — documented |
| SQL injection probe | `Brand = "TestBrand'; SELECT 1; --"` | detected by libinjection; entry rejected, counted as `threat` |
| Invalid / reused feed `Id` | 3 non-UUID, 14 reused across sellers | stored as opaque `ExternalSku`; reuse is across different sellers, so `UNIQUE (SellerId, ExternalSku)` holds; identity still comes from `Name` |
| Same seller offers the same product twice | `GardenStore` Câmera/Camera, plus 11 more entries | one link; the first entry's SKU is kept, the rest log `duplicate_listing` |
| Null `Brand` / `Category` | brand: 3 feed / 119 catalog rows; category: 34 catalog rows | `BrandId` is `NULL` or `ProductCategory` has no row; absent brand does not block a name match |

## Injected seams (dependency injection)

Two ports, same shape, same wiring — the **port** lives in the domain-services
layer, the **adapters** in infrastructure, and the **composition root** (`cli`)
picks one by name:

- **`Similarity`** — the fuzzy-scoring backend (`difflib` / `rapidfuzz`), wrapped
  with the threshold in a `ProductIdentityResolver`.
- **`ByteSource`** — the byte-stream transport (`http` via `requests` /
  `s3` via `boto3`), used for **both** the catalog download and the feed. Chosen
  with `--source`; `boto3` is imported lazily, only on the `s3` path, and the
  request is unsigned (public objects only). This is a code-challenge showcase of
  the adapter pattern applied to external I/O beyond the matcher — the S3 path is
  not otherwise needed for the given HTTPS sources.

Neither `similarity`, `threshold`, `requests` nor `boto3` is threaded layer by
layer; no DI container, no framework. The similarity backend + threshold ride
down the call chain inside the `ProductIdentityResolver`; the `ByteSource` is
passed to `PrepareCatalogDatabaseUseCase` and `ConsolidateCatalogUseCase`.

```python
# consolidation/services.py — the port (a domain-owned contract)
class Similarity(Protocol):
    name: str
    suggested_threshold: float
    def score(self, a: str, b: str) -> float: ...   # a, b already normalized -> [0, 1]
```

- `DifflibSimilarity` — `SequenceMatcher(None, a, b, autojunk=False).ratio()`, stdlib.
- `RapidFuzzSimilarity` — `fuzz.ratio(a, b) / 100.0`, external (imported lazily).
- `fuzz.WRatio` / `token_set_ratio` / `token_sort_ratio` are disallowed — they hide
  extra terms that matter for identity (capacity, model).
- `fuzz.ratio` and `difflib.ratio` are both `2M/T`; on this data both give `0.909`
  for `Roteador/Router`, so the same `0.90` threshold works. Not guaranteed in
  general, hence each backend carries its own `suggested_threshold`; a `THRESHOLD` in
  `.env` or the environment overrides it.

```python
# consolidation/services.py — the second port (also a domain-owned contract)
class ByteSource(Protocol):
    name: str
    def open(self, ref: str) -> AbstractContextManager[BinaryIO]: ...   # stream at ref start

# consolidation/infrastructure.py — the factories (adapters + lazy import)
def build_similarity(name: str) -> Similarity:
    if name == "difflib":
        return DifflibSimilarity()
    if name == "rapidfuzz":
        return RapidFuzzSimilarity()          # `from rapidfuzz import fuzz` happens inside .score
    raise ValueError(f"unknown matcher: {name!r} (options: difflib, rapidfuzz)")

def build_source(name: str) -> ByteSource:
    if name == "http":
        return HttpByteSource()
    if name == "s3":
        return S3ByteSource()                 # `import boto3` happens inside .open
    raise ValueError(f"unknown source: {name!r} (options: http, s3)")

# consolidation/cli.py — composition root: build the collaborators once, inject them
resolver = ProductIdentityResolver(build_similarity(config["matcher"]), threshold)
source = build_source(config["source"])
PrepareCatalogDatabaseUseCase(repository, source).execute(catalog_url, dest_dir)
ConsolidateCatalogUseCase(repository, resolver, source).execute(prepared, products_url, output)
#   -> ConsolidateFeedUseCase(repositories, resolver).execute(feed)   # feed = iter_feed(url, source)
#   -> ConsolidateEntryUseCase(repositories, resolver)   # resolver.resolve(catalog, submission)
```

The `ProductIdentityResolver` bundles the backend *and* the threshold, so nothing
downstream passes `similarity` or `threshold` around. Tests build it directly
(`ProductIdentityResolver(DifflibSimilarity(), 0.90)`) — that is the payoff of the seam.

Candidate retrieval for the fuzzy stage is a plain `select(Product)` scan (975 rows;
instant). Indexed candidate reduction is out of scope.

## Execution flow

The composition root (`cli`) runs the two use cases in order.

1. Resolve and validate configuration (CLI plus `.env` next to the entry point); the
   composition root builds the `ProductIdentityResolver` (backend + threshold), the
   `ByteSource` (`http`/`s3`) and the `SqliteCatalogRepository`.
2. **`PrepareCatalogDatabaseUseCase`** — download `catalog.db` in chunks to a temp
   file (fresh every run) through the injected `ByteSource`, then drive an injected
   `CatalogRepository` port
   (`verify_database` → `connect` → `begin` → `classify_source` → `upgrade` →
   `commit`/`rollback` → `enable_foreign_keys`); the use case never sees SQLAlchemy,
   Alembic or the `sqlite:///` scheme. The SQLite adapter verifies the header,
   classifies the source (abort if unrecognized), runs `alembic upgrade head` with
   the connection injected — revision `0001` performs the full
   [database refactor](spec/data-profile.md#refactored-database) for a legacy source
   and is a no-op for an already-migrated one — then enables and verifies
   foreign-key enforcement (abort before any JSON if unavailable). Returns the temp
   file path; on success the repository stays connected for the next step, on
   failure it is closed and the file deleted.
3. **`ConsolidateCatalogUseCase.execute(prepared_database, …)`** — takes the same
   still-connected `repository` (the single `CatalogRepository`), consumes the
   feed on its live connection, then closes it before publishing.
4. **`ConsolidateFeedUseCase`** — stream `ProductEntry.json` through the injected
   `ByteSource` + `ijson`; validate each object with Pydantic; screen each string
   field with `libinjection`. For each surviving entry, inside
   `repositories.entry_transaction()`,
   `ConsolidateEntryUseCase` resolves the product; when inserting a new product it
   mints a `uuid4` and `get_or_create`s its brand and category, adds the category
   membership; `get_or_create`s the seller; links them (idempotent); accumulate
   `processed`, `new`, `linked`, `skipped`, `threat`. The context manager commits
   on success; on an entry failure it rolls back, the failure is recorded,
   `repositories.reload()` refreshes the caches, and the loop continues.
5. Close the connection → atomically replace the output with the temp file. A published
   output with item failures returns a non-zero status and includes the failed
   records in the final log.

Any failure (network, JSON, schema validation, database) -> rollback, discard the temp
file, previous output preserved. Contained threats and skips do not fail the run. No
partial resume.

## Out of scope for the first iteration

Indexed candidate reduction (FTS5 / trigram / spellfix1), the SQLAlchemy ORM, an Alembic
revision chain / autogenerate / offline mode (only the single hand-written `0001`),
`Brand.DisplayName` / `Category.DisplayName`, `BLOB(16)` / UUIDv7 key encoding, a
persisted product name index, global product identity, labeled-data evaluation,
processing resume.
