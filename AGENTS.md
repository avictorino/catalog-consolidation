# AGENTS.md

Working rules for AI assistance on this repository.

## Conventions

- Pull request titles and descriptions are written in English, regardless of the
  language used in chat. Commit messages and code comments are English too.

## Scope

- Implement the catalog consolidation tool described in [`spec/`](spec/) and [`prd.md`](prd.md).
- The specification is authoritative. If code and spec disagree, fix the code or raise
  the discrepancy — do not silently diverge.
- Keep the surface small: a handful of modules, small functions, explicit composition.
  One `CatalogRepository` (`consolidation.repository`) owns the downloaded database —
  connection/transaction lifecycle, the Alembic-driven refactor, FK enforcement, and the
  per-entry import transactions — and `pipeline.run` depends on it through the `Catalog`
  protocol. Beyond that: no DI container, no ORM, no deep class hierarchies.

## Design constraints

- **DB refactor**: implement exactly as `spec/data-profile.md#refactored-database`
  specifies — do not restate the schema or the steps elsewhere. SQLAlchemy Core for the
  schema and statements (no ORM); the refactor is a single Alembic revision (`0001`)
  driven programmatically by `CatalogRepository` with an injected connection, so it runs
  inside the setup transaction. The declarative target schema lives in
  `consolidation.schema`; the migration steps in `consolidation._refactor`. `uuid4` `TEXT` primary keys, no `AUTOINCREMENT`. Conditional on
  Alembic's `alembic_version` marker (legacy source migrated, already-migrated source
  left alone, unrecognized schema aborts).
- Commit the schema refactor before feed processing, then use one transaction per feed
  entry. A failed entry is rolled back in isolation, recorded in the final report, and
  does not prevent later entries from being attempted. All feed writes are idempotent.
- Foreign keys may remain disabled during the schema rebuild only. After its commit,
  enable and verify enforcement on the import connection before consuming the JSON,
  including when the source is already migrated. Abort if enforcement is unavailable.
- `Brand` is a reference table with a nullable FK on `Product`; `Category` is a
  reference table connected through the `ProductCategory` junction.
  `ExternalSku` = the feed entry `Id`, stored opaque; first writer wins.
- Stream the feed: no `response.json()`, `response.content`, `list(iterator)`, or a
  local copy of the JSON.
- Validate feed objects one at a time with Pydantic v2, then screen every string field
  with `libinjection`; a hit rejects the entry and increments `threat`.
- All SQL carrying external data is parameterized (SQLAlchemy Core does this).
- Normalization is one shared Python function (names, brands, categories).
- The two `Similarity` backends implement the same `score(a, b) -> float` contract and
  pass the same tests. `rapidfuzz` is imported lazily.
  `WRatio` / `token_set_ratio` / `token_sort_ratio` are disallowed.

## Verification commands

```bash
ruff check . && ruff format --check .
pytest
pre-commit run --all-files
pip-audit -r requirements.txt -r requirements-dev.txt
python -m consolidation.cli --matcher difflib
python -m consolidation.cli --matcher rapidfuzz
```

Expected against the published sources, for both backends: 637 brands, 44 categories,
975 products, 20 sellers, 256 links, 1 threat. The migration starts with 43 catalog
categories; the feed adds `Photo`.

## Out of scope

- Indexed candidate reduction (FTS5 / trigram / spellfix1).
- SQLAlchemy ORM, an Alembic revision chain / autogenerate / offline mode (only the
  single hand-written `0001` revision), `Brand`/`Category` `DisplayName`, a persisted
  product name index, global product identity, processing resume.
- CSV input, local-file input, AI-based review.
