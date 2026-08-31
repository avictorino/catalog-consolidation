# CLAUDE.md

Working rules for AI assistance on this repository.

## Scope

- Implement the catalog consolidation tool described in [`spec/`](spec/) and [`prd.md`](prd.md).
- The specification is authoritative. If code and spec disagree, fix the code or raise
  the discrepancy — do not silently diverge.
- Keep the surface small: a handful of modules, small functions, explicit composition.
  No DI container, no generic repository layer, no deep class hierarchies.

## Design constraints

- One transaction per import, never per entry.
- Stream the feed: no `response.json()`, `response.content`, `list(iterator)`, or a
  local copy of the JSON.
- Validate feed objects one at a time with Pydantic v2.
- All SQL touching external data is parameterized.
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

Expected against the published sources: 976 products, 268 links, for both backends.

## Out of scope

- FTS5 candidate source (documented as future work only).
- Persisted normalized columns, global product identity, processing resume.
- CSV input, local-file input, an ORM, AI-based review.
