# catalog-consolidation

Catalog consolidation for a marketplace: given a base product catalog (SQLite) and a
feed of products submitted by many sellers (JSON), link each seller entry to the
right catalog product — creating a new product only when the feed genuinely
introduces one, never duplicating an existing item.

> VTEX AI Coding Interview take-home. The challenge deliberately contains ambiguities;
> how they are resolved is documented in [`spec/`](spec/) and [`prd.md`](prd.md).

## Status

Project scaffolding and specification only. No application code yet.

- [`prd.md`](prd.md) — product/solution design agreed up front, including the database refactor.
- [`spec/contract.md`](spec/contract.md) — IO contract, CLI, validation, schema, matcher interface.
- [`spec/acceptance.md`](spec/acceptance.md) — acceptance criteria and test scenarios.
- [`spec/data-profile.md`](spec/data-profile.md) — profile of the real input data and the expected result.

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

The given model is compromised — `SellerName`, `Product.Brand`, and `Product.Category`
are denormalized free text, `SellerProductId` is typed `INTEGER` while the feed sends
UUID strings, the link table has a pointless surrogate key and no uniqueness
constraint, and the keys are enumerable `AUTOINCREMENT` integers. On every run, right
after download, both given tables **are really replaced** (SQLAlchemy Core; no ORM, no
Alembic). Every primary key becomes a `uuid4` `TEXT`:

```sql
CREATE TABLE Brand    (Id TEXT PRIMARY KEY, Name TEXT NOT NULL UNIQUE);  -- Id = uuid4
CREATE TABLE Category (Id TEXT PRIMARY KEY, Name TEXT NOT NULL UNIQUE);

CREATE TABLE Product (
    Id         TEXT PRIMARY KEY,                    -- uuid4
    Name       TEXT NOT NULL,
    BrandId    TEXT REFERENCES Brand (Id),          -- nullable
    CategoryId TEXT REFERENCES Category (Id)        -- nullable
);

CREATE TABLE Seller (Id TEXT PRIMARY KEY, Name TEXT NOT NULL UNIQUE);

CREATE TABLE SellerProduct (              -- many-to-many link + the seller's own SKU
    SellerId    TEXT NOT NULL REFERENCES Seller (Id),
    ProductId   TEXT NOT NULL REFERENCES Product (Id),
    ExternalSku TEXT NOT NULL,            -- the feed entry's Id
    PRIMARY KEY (SellerId, ProductId),
    UNIQUE (SellerId, ExternalSku)
);
```

**Kept / replaced / deleted:** `Product` is dropped and recreated — `Name` carried over
for all 975 rows, `Id` reissued as a `uuid4`, the `Brand` and `Category` text columns
replaced by `BrandId` / `CategoryId` after a **data migration** into the new reference
tables. `SellerProduct` is likewise dropped and recreated (`SellerName` → `Seller`,
`SellerProductId` → `ExternalSku`, surrogate `Id` gone). `Brand`, `Category`, `Seller`
are new. The PK type change forces a rebuild, so nothing is altered in place.

`Brand` and `Category` are **reference tables** (a product has 0..1 of each → nullable
FK), not `BrandProduct` / `CategoryProduct` junctions. `SellerProduct` stays a junction
because a product genuinely has many sellers.

The refactor is a **conditional migration** guarded by `PRAGMA user_version`: a legacy
source (the published `catalog.db`) is migrated and stamped `user_version = 1`; a source
already at `user_version = 1` skips straight to feed processing. Combined with idempotent
writes, the tool can be re-run against its own output incrementally. Full rationale and
accepted risks are in [`prd.md`](prd.md#database-refactor).

## Planned CLI

```
python -m consolidation.cli \
  [--catalog-url URL] [--products-url URL] [--output PATH] \
  [--matcher difflib|rapidfuzz] [--threshold FLOAT]
```

- Every option has a default in `.env` (copied from `.env.example`), so a bare
  `python -m consolidation.cli` runs against the S3 sources with `difflib` at `0.90`.
- `--matcher` selects the similarity backend: `difflib` (stdlib, default) or `rapidfuzz`
  (external). Both implement the same `score(a, b) -> float` contract.
- `--threshold` overrides the backend's suggested cutoff and the `.env` value.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install
cp .env.example .env
```

`libinjection-python` has no wheel for Python 3.11/3.12 yet and builds from source: on
Linux/CI the toolchain is present; on Windows install MSVC build tools or use WSL.

## Verification (once code exists)

```bash
ruff check . && ruff format --check .
pytest
python -m consolidation.cli --matcher difflib
python -m consolidation.cli --matcher rapidfuzz
```

Against the currently published sources, both matchers are expected to produce
**637 brands**, **43 categories**, **975 products**, **20 sellers**,
**256 seller-product links**, and **1 threat**. This is a check, not a guarantee — the
remote content may change.

## Key design decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Database model | on every run, migrate the given DB: extract `Brand`, `Category` (reference tables) and `Seller`; link is `SellerProduct (SellerId, ProductId, ExternalSku)` with a composite key | the given model denormalizes `SellerName`, `Brand`, `Category` and mistypes the SKU; the challenge allows DB changes |
| Primary keys | `uuid4` stored as `TEXT`, minted in Python; no `AUTOINCREMENT` | non-enumerable, no DB coordination to mint, stable across environments; accepted cost: larger indexes, worse insert locality |
| `Brand` / `Category` as reference, not junction | nullable `Product.BrandId` / `Product.CategoryId` FK + data migration then drop the text column | a product has one brand and one category; a junction would allow two |
| Keep the seller SKU | `SellerProduct.ExternalSku` (opaque text) + `UNIQUE (SellerId, ExternalSku)` | needed to map a listing back to the seller's catalog; reuse is only across sellers |
| DB access | SQLAlchemy Core (no ORM, no Alembic) | declarative schema + parameterized statements; the refactor is one `user_version`-guarded migration, not a revision chain |
| Conditional migration | run only when the source is legacy (`user_version = 0`); skip an already-migrated DB | idempotent feed writes make incremental re-runs against a previous output safe |
| Product identity | normalized `Name`; brand only as a tie-break gate; category never; feed `Id` never | category disagrees even for true duplicates; `Id` is a seller SKU |
| Normalization | Python, shared by catalog / feed names, brands, categories | SQLite `lower()` is ASCII-only and cannot fold accents (`Câmera` → `camera`) |
| SQL injection | `libinjection` screen; reject and count as `threat` | WAF-grade tokenizer, no false positive on `"Levi's"`; parameterized SQL is still the real defense |
| Matcher backends | `difflib` vs `rapidfuzz`, same `score()` contract, injected at the CLI edge | clean interchangeability; no DI container |
| Ambiguous match | skip the row and report it, do not abort the import | one ambiguous row should not hide the outcome of the rest |
| Transaction | one per import (refactor + feed), rollback on any failure | all-or-nothing; the previous output survives a failed run |

## Known limitations

- The similarity cutoff of `0.90` is load-bearing: the translation case
  `Roteador WiFi 6 TP-Link` ↔ `Router WiFi 6 TP-Link` scores `0.909`. Raising the
  cutoff turns that match into a new product.
- Streaming bounds memory but not search cost: for `N` feed entries and `M` catalog
  products, the worst case visits about `N × M` records. Indexed candidate reduction
  (FTS5 / trigram) is deliberately out of scope for this iteration.
- `difflib.SequenceMatcher` can be quadratic in string length.
- The refactor canonicalizes catalog brand and category spelling to the normalized
  form; human-readable `DisplayName` columns are future work.
- UUID `TEXT` primary keys cost index size and insert locality versus integer keys;
  fine at this volume, but `BLOB(16)` / UUIDv7 would be the move if it mattered.
- The `libinjection` screen may in principle reject a legitimate product whose text
  looks like SQL; every rejection is in the `threat` report for review.

Not presented as a high-performance design for very large catalogs; it targets
incremental consumption at the challenge's volume.
