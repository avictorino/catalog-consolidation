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
  No DI container, no generic repository layer, no deep class hierarchies.

## Design constraints

- **DB refactor**: implement exactly as `spec/data-profile.md#refactored-database`
  specifies — do not restate the schema or the steps elsewhere. SQLAlchemy Core for the
  schema and statements (no ORM); the refactor is a single Alembic revision (`0001`)
  driven programmatically with an injected connection, so it runs inside the setup
  transaction. `uuid4` `TEXT` primary keys, no `AUTOINCREMENT`. Conditional on
  Alembic's `alembic_version` marker (legacy source migrated, already-migrated source
  left alone, unrecognized schema aborts).
- Commit the schema refactor before feed processing, then use one transaction per feed
  entry. A failed entry is rolled back in isolation, recorded in the final report, and
  does not prevent later entries from being attempted. All feed writes are idempotent.
- Foreign keys may remain disabled during the schema rebuild only. After its commit,
  enable and verify enforcement on the import connection before consuming the JSON,
  including when the source is already migrated. Abort if enforcement is unavailable.
- `Brand` / `Category` are reference tables (nullable FK), not junctions.
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

Expected against the published sources, for both backends: 637 brands, 43 categories,
975 products, 20 sellers, 256 links, 1 threat.

## Out of scope

- Indexed candidate reduction (FTS5 / trigram / spellfix1).
- SQLAlchemy ORM, an Alembic revision chain / autogenerate / offline mode (only the
  single hand-written `0001` revision), `Brand`/`Category` `DisplayName`, a persisted
  product name index, global product identity, processing resume.
- CSV input, local-file input, AI-based review.
