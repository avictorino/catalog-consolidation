# catalog-consolidation

Catalog consolidation for a marketplace: given a base product catalog (SQLite) and a
feed of products submitted by many sellers (JSON), link each seller entry to the
right catalog product — creating a new product only when the feed genuinely
introduces one, never duplicating an existing item.

> VTEX AI Coding Interview take-home. The challenge deliberately contains ambiguities;
> how they are resolved is documented in [`spec/`](spec/) and [`prd.md`](prd.md).

## Status

Project scaffolding and specification only. No application code yet.

- [`prd.md`](prd.md) — product/solution design agreed up front.
- [`spec/contract.md`](spec/contract.md) — IO contract, CLI, validation, schema changes, matcher interface.
- [`spec/acceptance.md`](spec/acceptance.md) — acceptance criteria and test scenarios.
- [`spec/data-profile.md`](spec/data-profile.md) — profile of the real input data and the expected result.

## Inputs and output

| What | Default source |
| --- | --- |
| Base catalog | `catalog.db` on S3 (`--catalog-url`) |
| Seller feed | `ProductEntry.json` on S3 (`--products-url`) |
| Output | `catalog_output.db` in the working directory (`--output`) |

Every run downloads a fresh copy of the base catalog and rebuilds the output from
scratch. The previous output is replaced atomically, and only after a fully
successful run.

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
**976 products** and **268 seller links**. This is a check, not a guarantee — the
remote content may change.

## Key design decisions

| Decision | Choice | Rationale |
| --- | --- | --- |
| Product identity | normalized `Name`; `Brand` only as a tie-break gate; `Category` never; `Id` never | `Id` in the feed is a seller-scoped SKU (reused across sellers, some malformed); category disagrees even for true duplicates |
| Normalization | Python, shared by catalog and feed | SQLite `lower()` is ASCII-only and cannot fold accents (`Câmera` → `camera`) |
| Matching | two-stage: exact normalized name, then gated fuzzy | catalog names are unique after normalization; fuzzy only rescues translation-style variants |
| Matcher backends | `difflib` vs `rapidfuzz`, same `score()` contract, injected at the CLI edge | clean interchangeability; no DI container |
| Candidate retrieval | full scan | 975 products; a linear scan is instant. FTS5 blocking is documented as future work |
| Ambiguous match | skip the row and report it, do not abort the import | one ambiguous row should not hide the outcome of the other 268 |
| Transaction | one per import, rollback on any failure | all-or-nothing; the previous output survives a failed run |
| Schema | `SellerProduct.SellerProductId` → `TEXT`, add `UNIQUE(SellerName, SellerProductId)` | feed ids are UUID strings, not integers; the challenge allows DB changes |

## Known limitations

- The similarity cutoff of `0.90` is load-bearing: the translation case
  `Roteador WiFi 6 TP-Link` ↔ `Router WiFi 6 TP-Link` scores `0.909`. Raising the
  cutoff turns that match into a new product.
- Streaming bounds memory but not search cost: for `N` feed entries and `M` catalog
  products, the worst case visits about `N × M` records.
- `difflib.SequenceMatcher` can be quadratic in string length.

Not presented as a high-performance design for very large catalogs; it targets
incremental consumption at the challenge's volume.
