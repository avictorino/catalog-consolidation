# Specification — acceptance criteria

## Expected result

Against the currently published sources, with either matcher backend:

| Metric | Value | Derivation |
| --- | --- | --- |
| `Product` rows after import | **976** | 975 base + 1 new (`Security Test Product`) |
| `SellerProduct` rows after import | **268** | 269 feed entries − 1 repeated `(SellerName, Id)` pair |
| New products | 1 | only `Security Test Product`; PT→EN name variants match existing products |
| Skipped entries | 0 | no ambiguous multi-candidate or brand-conflict case in the current data |

Treated as a check, not a guarantee: if the remote content changes, the numbers change.
The tool must still be correct; only this table becomes stale.

## Acceptance scenarios

### Configuration

- Defaults resolve to the S3 URLs and `catalog_output.db` in the working directory.
- Precedence holds: CLI > env var > `.env` > default.
- `.env` is located relative to the entry point even when the process runs from another
  directory.
- A non-TLS `http://` source URL produces a `WARNING` and still runs.

### Download and rebuild

- Every run downloads the base catalog again; the previous output is never read as input.
- The base catalog is downloaded in chunks to a temp file, not held whole in memory.
- A corrupt or non-SQLite download is rejected before any write.

### Streaming

- The feed is consumed incrementally: no `response.json()`, no `response.content`, no
  `list(iterator)`, no local copy of the JSON.
- The feed arriving in many small chunks — including chunk boundaries that split a
  multi-byte UTF-8 character — is parsed correctly.

### Validation and transactions

- An invalid root (object instead of array) aborts before any write.
- A JSON document truncated mid-stream aborts; the previous output is preserved.
- An invalid record after several pending inserts triggers a full rollback; the previous
  output is preserved.
- `[]` as the feed is valid and produces an output equal to the base catalog.

### Identity

- Exact duplicate (whitespace / accent / punctuation variant) → link only, no new product.
- PT→EN translation variant within the fuzzy gate → link only.
- Missing brand on one side → still matches on name.
- Category difference between entry and product → link, `WARNING` logged.
- A model/capacity difference (`128GB` vs `256GB`) → not matched, new product.
- A constructed two-candidate case → entry skipped and reported, import continues.

### Ids and links

- Text ids are stored as `TEXT` without coercion.
- The same `Id` string under two different sellers produces two independent links.
- The same `(SellerName, Id)` pair appearing twice in the feed produces one link.
- No silent re-association: a pair mapping to a different product than before is skipped
  and reported.

### Matcher backends

- The same identity test suite passes with `--matcher difflib` and `--matcher rapidfuzz`.
- `--matcher difflib` runs without `rapidfuzz` installed.
- `--threshold` overrides the backend default and changes outcomes at the boundary
  (`Roteador/Router` at `0.909`: included at `0.90`, excluded at `0.91`).

### Security

- SQL-like text in any field (`"TestBrand'; SELECT 1; --"`) is treated purely as data;
  the row is inserted as an ordinary new product.
- Referential integrity holds; the schema adaptation loses no existing data.

### Observability

- Exit code `0` only after publication; non-zero on any failure.
- The final summary reports `processed`, `new`, `linked`, `skipped`.
- Two runs from the same sources produce logically equivalent tables.
