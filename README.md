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
below), and rebuilds the output from scratch. The previous output is replaced
atomically, and only after a fully successful run.

## Database refactor

The given `SellerProduct` table is compromised — it denormalizes the seller name,
types the seller's product id as `INTEGER` (the feed sends UUID strings), carries a
pointless surrogate key, and has no uniqueness constraint. On every run, right after
download, it is rebuilt into a normalized model:

```sql
CREATE TABLE Seller (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE
);

CREATE TABLE SellerProduct (          -- pure many-to-many link
    SellerId  INTEGER NOT NULL REFERENCES Seller (Id),
    ProductId INTEGER NOT NULL REFERENCES Product (Id),
    PRIMARY KEY (SellerId, ProductId)
);
```

`Product` is unchanged. The seller's own SKU (`SellerProductId`) is intentionally
dropped — recording *which sellers offer each product* is all the challenge asks, and
the composite key makes that relationship idempotent. Full rationale and the accepted
risk are in [`prd.md`](prd.md#database-refactor).

## Planned CLI

```
python -m consolidation.cli \
  [--catalog-url URL] [--products-url URL] [--output PATH] \
  [--matcher difflib|rapidfuzz] [--threshold FLOAT]
```

- `--matcher` selects the similarity backend. `difflib` (standard library) is the
  default; `rapidfuzz` (external) is the alternative. Both implement the same
  `score(a, b) -> float` contract.
- `--threshold` overrides the backend's suggested similarity cutoff.

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Linux / macOS
pip install -r requirements.txt -r requirements-dev.txt
pre-commit install
cp .env.example .env
```

## Verification (once code exists)

```bash
ruff check . && ruff format --check .
pytest
python -m consolidation.cli --matcher difflib
python -m consolidation.cli --matcher rapidfuzz
```

Against the currently published sources, both matchers are expected to produce
**976 products**, **20 sellers**, and **257 seller-product links**. This is a check,
not a guarantee — the remote content may change.

## Key design decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Database model | rebuild `SellerProduct` on every run: extract a `Seller` table, make the link `(SellerId, ProductId)` with a composite key, drop the seller SKU | the given model denormalizes the seller name and mistypes the SKU; the challenge allows DB changes |
| Product identity | normalized `Name`; `Brand` only as a tie-break gate; `Category` never; `Id` never | the feed `Id` is a seller-scoped SKU (reused, some malformed) and is not stored; category disagrees even for true duplicates |
| Normalization | Python, shared by catalog and feed | SQLite `lower()` is ASCII-only and cannot fold accents (`Câmera` → `camera`) |
| Matching | two-stage: exact normalized name, then gated fuzzy | catalog names are unique after normalization; fuzzy only rescues translation-style variants |
| Matcher backends | `difflib` vs `rapidfuzz`, same `score()` contract, injected at the CLI edge | clean interchangeability; no DI container |
| Candidate retrieval | full scan | 975 products; a linear scan is instant |
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
- The seller's own product id is not persisted; if per-listing data is later needed it
  returns as a nullable column on `SellerProduct` (see `prd.md`).

Not presented as a high-performance design for very large catalogs; it targets
incremental consumption at the challenge's volume.
