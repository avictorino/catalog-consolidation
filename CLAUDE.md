# CLAUDE.md

Working rules for AI assistance on this repository.

## Scope

- Implement the catalog consolidation tool described in [`spec/`](spec/) and [`prd.md`](prd.md).
- The specification is authoritative. If code and spec disagree, fix the code or raise
  the discrepancy — do not silently diverge.
- Keep the surface small: a handful of modules, small functions, explicit composition.
  No DI container, no generic repository layer, no deep class hierarchies.

## Design constraints

- Refactor the DB via **SQLAlchemy Core** (no ORM, no Alembic), as a **conditional
  migration** guarded by `PRAGMA user_version`: migrate a legacy source (`user_version 0`,
  `SellerProduct.SellerName` present) then stamp `user_version = 1`; skip when already
  `1`; abort on an unrecognized schema. Extract `Brand (Id, Name)` and `Seller (Id, Name)`;
  `Product` keeps `Id`/`Name`/`Category` with `Brand` → nullable `BrandId`; `SellerProduct`
  becomes `(SellerId, ProductId, ExternalSku)` with `PRIMARY KEY (SellerId, ProductId)`
  and `UNIQUE (SellerId, ExternalSku)`. `Brand` is a reference table, not a junction.
- All feed writes are idempotent so a re-run against a previous output is safe.
- `ExternalSku` = the feed entry `Id`, stored opaque; first writer wins.
- One transaction per import (refactor + feed), never per entry.
- Stream the feed: no `response.json()`, `response.content`, `list(iterator)`, or a
  local copy of the JSON.
- Validate feed objects one at a time with Pydantic v2, then screen every string field
  with `libinjection`; a hit rejects the entry and increments `threat`.
- All SQL carrying external data is parameterized (SQLAlchemy Core does this).
- Normalization is one shared Python function (catalog names, feed names, brands).
- The two `Similarity` backends implement the same `score(a, b) -> float` contract and
  pass the same tests. `rapidfuzz` is imported lazily.
- `rapidfuzz.WRatio` / `token_set_ratio` / `token_sort_ratio` are disallowed.

## Verification commands

```bash
ruff check . && ruff format --check .
pytest
pre-commit run --all-files
pip-audit -r requirements.txt -r requirements-dev.txt
python -m consolidation.cli --matcher difflib
python -m consolidation.cli --matcher rapidfuzz
```

Expected against the published sources, for both backends: 637 brands, 975 products,
20 sellers, 256 links, 1 threat.

## Out of scope

- Indexed candidate reduction (FTS5 / trigram / spellfix1).
- SQLAlchemy ORM, Alembic, `Brand.DisplayName`, a persisted product name index,
  global product identity, processing resume.
- CSV input, local-file input, AI-based review.
