# catalog-consolidation

Catalog consolidation for a marketplace: given a base product catalog (SQLite) and a
feed of products submitted by many sellers (JSON), link each seller entry to the
right catalog product — creating a new product only when the feed genuinely
introduces one, never duplicating an existing item.

> VTEX AI Coding Interview take-home. The challenge deliberately contains ambiguities;
> how they are resolved is documented in [`spec/`](spec/) and [`prd.md`](prd.md).

## Quick start

After copying `.env.example` to `.env`, run the basic command:

```bash
python -m consolidation.cli
```

The default configuration uses the published S3 sources, writes
`catalog_output.db`, uses `rapidfuzz`, and applies a fuzzy threshold of `0.90`.

An expected successful run has the following shape. Timestamps, UUIDs, and temporary
paths vary between executions:

```text
HH:MM:SS [INFO] configuration catalog_url=https://engineering-hiring-process.s3.us-east-1.amazonaws.com/catalog.db products_url=https://engineering-hiring-process.s3.us-east-1.amazonaws.com/ProductEntry.json output=catalog_output.db matcher=rapidfuzz threshold=0.9
HH:MM:SS [INFO] run config products_url=https://engineering-hiring-process.s3.us-east-1.amazonaws.com/ProductEntry.json matcher=rapidfuzz threshold=0.9
HH:MM:SS [INFO] downloading catalog url=https://engineering-hiring-process.s3.us-east-1.amazonaws.com/catalog.db dest=<temporary catalog path>.db.tmp
HH:MM:SS [INFO] download complete bytes=61440
HH:MM:SS [INFO] source classified as=legacy
HH:MM:SS [INFO] extracted reference table=Brand rows=637
HH:MM:SS [INFO] extracted reference table=Category rows=43
HH:MM:SS [INFO] extracted product sellers rows=0 column=absent
HH:MM:SS [INFO] rebuilt Product rows=975
HH:MM:SS [INFO] rebuilt SellerProduct sellers=0 links=0
HH:MM:SS [INFO] schema refactor committed
HH:MM:SS [INFO] foreign key enforcement enabled
HH:MM:SS [INFO] catalog loaded products=975
HH:MM:SS [INFO] streaming seller feed url=https://engineering-hiring-process.s3.us-east-1.amazonaws.com/ProductEntry.json
HH:MM:SS [INFO] first feed record received
HH:MM:SS [WARNING] event=approximate_match record=57 product_id=<uuid> score=0.909
HH:MM:SS [WARNING] event=approximate_match record=64 product_id=<uuid> score=0.964
HH:MM:SS [WARNING] event=category_divergence record=87 product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=87 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [WARNING] event=sqli_attempt record=180 field=Brand fingerprint=s;E1; value="TestBrand'; SELECT 1; --"
HH:MM:SS [INFO] event=duplicate_listing record=257 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=258 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=259 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=260 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=261 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=262 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=263 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=264 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=265 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] event=duplicate_listing record=266 seller_id=<uuid> product_id=<uuid>
HH:MM:SS [INFO] feed summary processed=269 new=0 linked=256 skipped=0 threat=1 failed=0
HH:MM:SS [WARNING] feed threats rejected=1
HH:MM:SS [INFO] feed processing complete
HH:MM:SS [WARNING] replacing existing output path=<workspace>/catalog_output.db
HH:MM:SS [INFO] published output path=<workspace>/catalog_output.db
```

If a feed item cannot be persisted, the import continues and prints the failure reason
after the feed is exhausted:

```text
HH:MM:SS [ERROR] feed item failures count=1; details follow
HH:MM:SS [ERROR] feed item failure record=123 reason=IntegrityError: FOREIGN KEY constraint failed
```

## Status

Download, schema refactor, and feed import are implemented. The feed is streamed from
JSON, validated one record at a time, screened for injection, and resolved against the
catalog before publication.

- [`prd.md`](prd.md) — design rationale and decisions (why).
- [`spec/data-profile.md`](spec/data-profile.md) — input-data profile, **the refactored schema and migration** (source of truth), and the expected result.
- [`spec/contract.md`](spec/contract.md) — IO contract, CLI, validation, logging, and the normative refactor / matcher rules.
- [`spec/acceptance.md`](spec/acceptance.md) — acceptance criteria and test scenarios.

## Inputs and output

| What | Default source |
| --- | --- |
| Base catalog | `catalog.db` on S3 (`--catalog-url`) |
| Seller feed | `ProductEntry.json` on S3 (`--products-url`) |
| Output | `catalog_output.db` in the working directory (`--output`) |

Every run downloads a fresh copy of the base catalog, **refactors its schema** (see
below), and rebuilds the consolidated result from scratch. The previous output is
replaced atomically, and only after a fully successful run.

## Database refactor

The given model is compromised — denormalized seller / brand / category names, an
`INTEGER` SKU column that should hold the feed's UUID strings, a useless surrogate key,
and enumerable `AUTOINCREMENT` ids. On every run, right after download, both given
tables are dropped and recreated and three reference tables — `Brand`, `Category`,
`Seller` — are added, all with `uuid4` `TEXT` primary keys. It runs as a single Alembic
revision (`0001`) driven programmatically with the import connection injected, keyed on
the `alembic_version` marker (a legacy source is migrated; an already-migrated one
leaves `alembic upgrade head` a no-op), so the tool can also be re-run against its own
output.

- **Target schema, migration steps, kept/replaced/deleted:**
  [`spec/data-profile.md#refactored-database`](spec/data-profile.md#refactored-database)
- **Why, and the accepted risks:** [`prd.md#database-refactor`](prd.md#database-refactor)

## CLI

```
python -m consolidation.cli \
  [--catalog-url URL] [--products-url URL] [--output PATH] \
  [--matcher difflib|rapidfuzz] [--threshold FLOAT]
```

- Every option has a default in `.env` (copied from `.env.example`), so a bare
  `python -m consolidation.cli` runs against the S3 sources with `rapidfuzz` at `0.90`.
- `--matcher` selects the similarity backend: `rapidfuzz` (default) or `difflib`
  (optional fallback). Both implement the same `score(a, b) -> float` contract.
- `--threshold` overrides the backend's suggested cutoff and the `.env` value.

### Why `rapidfuzz` is the default

Product names can contain the same words in a different order, for example
`Smartphone Galaxy S23` and `Galaxy S23 Smartphone`. The importer therefore resolves
exact normalized names and equal word multisets before it invokes fuzzy matching. This
deterministic step is what handles word order; neither fuzzy backend should be trusted
to infer that relationship by itself.

`difflib.SequenceMatcher` is sensitive to character sequence and can be quadratic for
long strings. It is useful as a dependency-free comparison backend, but it is slower
and its score can vary substantially when words move. `rapidfuzz.fuzz.ratio` preserves
the same score contract and is implemented in optimized native code, making it the
better default when the fuzzy stage scans many catalog products. The threshold and the
word-order stage remain unchanged, so switching the backend does not change the
identity rules.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt -r requirements-dev.txt
pip install -e . --no-deps        # make `python -m consolidation.cli` importable (src/ layout)
pre-commit install
cp .env.example .env
```

`libinjection-python` has no wheel for Python 3.11/3.12 yet and builds from source: on
Linux/CI the toolchain is present; on Windows install MSVC build tools or use WSL.

## Verification

```bash
ruff check . && ruff format --check .
pytest
python -m consolidation.cli --matcher difflib
python -m consolidation.cli --matcher rapidfuzz
```

Against the currently published sources, both matchers are expected to produce
**637 brands**, **44 categories**, **975 products**, **20 sellers**,
**256 seller-product links**, and **1 threat**. The migration starts with 43 catalog
categories; the feed adds the `Photo` membership/category. This is a check, not a
guarantee — the remote content may change.

## Key design decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Database model | on every run, migrate the given DB: extract `Brand`, `Category` (reference tables) and `Seller`; `Product` has one nullable `BrandId`, `ProductCategory` stores category memberships, and `SellerProduct` stores seller links | the given model denormalizes `SellerName`, `Brand`, `Category` and mistypes the SKU; the challenge allows DB changes |
| Primary keys | `uuid4` stored as `TEXT`, minted in Python; no `AUTOINCREMENT` | non-enumerable, no DB coordination to mint, stable across environments; accepted cost: larger indexes, worse insert locality |
| `Brand` / `Category` cardinality | nullable `Product.BrandId` FK for one brand; `ProductCategory (ProductId, CategoryId)` for many categories | brand is singular, while category is taxonomy membership and may contain several values |
| Keep the seller SKU | `SellerProduct.ExternalSku` (opaque text) + `UNIQUE (SellerId, ExternalSku)` | needed to map a listing back to the seller's catalog; reuse is only across sellers |
| DB access | SQLAlchemy Core (no ORM) + one Alembic revision | declarative schema + parameterized statements; the refactor runs in a setup transaction; foreign keys are enabled and verified before JSON import |
| Conditional migration | keyed on `alembic_version`: revision `0001` for a legacy source, no-op for an already-migrated DB | idempotent feed writes make incremental re-runs against a previous output safe |
| Product identity | normalized `Name`, then identical word multisets regardless of order, then gated fuzzy matching; brand compatibility required; category and feed `Id` never define identity | preserve repeated words and model/capacity tokens; skip ambiguous matches; `Id` is a seller SKU |
| Normalization | Python, shared by catalog / feed names, brands, categories | SQLite `lower()` is ASCII-only and cannot fold accents (`Câmera` → `camera`) |
| SQL injection | `libinjection` screen; reject and count as `threat` | WAF-grade tokenizer, no false positive on `"Levi's"`; parameterized SQL is still the real defense |
| Matcher backends | `rapidfuzz` by default; `difflib` remains available through the same `score()` contract | optimized fuzzy scoring without changing the matching rules; no DI container |
| Ambiguous match | skip the row and report it, do not abort the import | one ambiguous row should not hide the outcome of the rest |
| Transaction | schema refactor is committed first; one transaction per feed entry | failed entries roll back in isolation, later entries continue, and failures are reported |

## Known limitations

- The similarity cutoff of `0.90` is load-bearing: the translation case
  `Roteador WiFi 6 TP-Link` ↔ `Router WiFi 6 TP-Link` scores `0.909`. Raising the
  cutoff turns that match into a new product.
- Streaming bounds memory but not search cost: for `N` feed entries and `M` catalog
  products, the worst case visits about `N × M` records. Indexed candidate reduction
  (FTS5 / trigram) is deliberately out of scope for this iteration.
- The refactor canonicalizes catalog brand and category spelling to a title-cased
  normalized form; human-readable `DisplayName` columns are future work.
- UUID `TEXT` primary keys cost index size and insert locality versus integer keys;
  fine at this volume, but `BLOB(16)` / UUIDv7 would be the move if it mattered.
- `libinjection` screening is implemented and every rejection is included in the
  `threat` report, but it may in principle reject a legitimate product whose text
  looks like SQL.

## Future proposal: scalable deduplication

For larger catalogs, a future version could use `scikit-learn` to retrieve a small set of
candidates with TF-IDF and cosine similarity, optionally followed by a trained
`dedupe.Gazetteer` model.

Exact matching, word-multiset matching, brand/model validation, numeric-token checks, and
ambiguity handling must remain the first layer. This could reduce the current full catalog
scan, but would introduce new dependencies, model and index management, labeled training
data, threshold calibration, and precision/recall evaluation.

This is not part of the current implementation; `RapidFuzz` remains the default matcher.

Not presented as a high-performance design for very large catalogs; it targets
incremental consumption at the challenge's volume.
