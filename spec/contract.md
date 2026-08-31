# Specification — contract

Normative contract for the consolidation tool. Acceptance criteria are in
[`acceptance.md`](acceptance.md); the data profile is in [`data-profile.md`](data-profile.md).

## 1. Command-line interface

Entry point: `python -m consolidation.cli`.

| Option | Default | Meaning |
| --- | --- | --- |
| `--catalog-url` | `https://engineering-hiring-process.s3.us-east-1.amazonaws.com/catalog.db` | HTTP(S) URL of the base SQLite catalog |
| `--products-url` | `https://engineering-hiring-process.s3.us-east-1.amazonaws.com/ProductEntry.json` | HTTP(S) URL of the seller feed |
| `--output` | `catalog_output.db` in the current working directory | destination path for the consolidated database |
| `--matcher` | `difflib` | similarity backend: `difflib` or `rapidfuzz` |
| `--threshold` | backend's `suggested_threshold` | float in `[0, 1]`; overrides the fuzzy cutoff |

- Only HTTP(S) URLs are accepted for `--catalog-url` and `--products-url`. Local files
  are not supported in this version.
- A non-TLS `http://` URL is allowed but logged as a warning.

### Configuration precedence

`CLI argument > environment variable > .env file > built-in default`

Environment variables: `CATALOG_URL`, `PRODUCTS_URL`, `OUTPUT`, `MATCHER`, `THRESHOLD`.
The `.env` file is looked up next to the application entry point, so it is found
regardless of the current working directory. `.env` is optional; its absence is not
an error.

## 2. Input validation (seller feed)

Each entry is validated one at a time with Pydantic v2 (`model_validate`); no Pydantic
list is materialized.

- The document root must be a JSON array of objects. `[]` is valid.
- `Id`, `SellerName`, `Name`: required, non-empty strings after trimming.
- `Brand`, `Category`: optional string or `null`.
- `Id` is treated as an opaque string. It is **not** parsed as a number and **not**
  required to be a UUID.
- Unknown fields are ignored.
- A validation failure aborts the run (rollback, previous output preserved) and is
  logged with the record index and the offending field — never the record contents.

## 3. Identity resolution

### Normalization

One function, applied identically to catalog names, feed names, and brand values:

1. Unicode NFKD, drop combining marks (accent folding).
2. Lowercase.
3. Replace every run of non-alphanumeric characters with a single space.
4. Collapse whitespace, trim.

Digits and decimal separators inside numbers are preserved (step 3 keeps `12.9` as
`12 9`, which is stable across `12.9"`, `12.9''`, `12.9`).

### Matching stages

1. **Exact**: look up the normalized feed name in the normalized-name index of the
   catalog. Catalog names are unique after normalization, so this yields 0 or 1.
2. **Fuzzy** (only if stage 1 misses): retrieve candidates from the `CandidateSource`,
   score each with the selected `Similarity`, and keep those that pass the gate:
   - both brands, after normalization, are equal — when both are present;
   - same number of whitespace-separated tokens;
   - the multiset of tokens that contain a digit is equal;
   - `score >= threshold`.

### Outcomes

| Situation | Action |
| --- | --- |
| Exactly one product (exact or one eligible fuzzy candidate) | insert only the `SellerProduct` link |
| No product and no eligible candidate | insert a new `Product` (fields from the feed entry), then the link |
| The link already exists for this identity | no change |
| Two or more eligible candidates, or a brand conflict on an otherwise-matching name | skip the entry, record it in the report, continue |

- Attributes of existing products are never enriched or overwritten.
- `Category` never affects identity. A category difference between a linked entry and
  its product is logged at `WARNING` and otherwise ignored.

## 4. Persistence

- One transaction per import. **Not** one transaction per entry.
- All statements that touch external data use parameterized SQL.
- `SellerProduct` uniqueness: `(SellerName, SellerProductId)`. A second entry for the
  same pair is a no-op; a pair that would map to a different product is a conflict and
  is skipped and reported.
- On any failure (network, JSON, validation, database), the transaction is rolled back,
  the temp database is discarded, and the previous output file is left untouched.
- A failure during the final atomic replacement also preserves the previous output.

## 5. Schema changes (on the downloaded copy)

- Rebuild `SellerProduct` so that `SellerProductId` has type `TEXT NOT NULL`.
- Add `UNIQUE(SellerName, SellerProductId)`.
- Keep the `ProductId` foreign key to `Product(Id)`; enable `PRAGMA foreign_keys = ON`.
- `Product` is not altered.
- WAL is not used; the published output is a self-contained database written after the
  connection is closed.

## 6. Matcher interface

```python
class Similarity(Protocol):
    name: str
    suggested_threshold: float
    def score(self, a: str, b: str) -> float: ...   # inputs already normalized; returns [0, 1]

class CandidateSource(Protocol):
    def candidates(self, normalized_name: str) -> Iterable[ProductRow]: ...
```

- Both `Similarity` implementations must satisfy the same contract and pass the same
  test suite.
- The backend is chosen in the CLI and injected into `consolidate(...)` as a keyword
  argument. The default `CandidateSource` is a full table scan.
- `rapidfuzz` is imported lazily; running with `--matcher difflib` must not require it.

## 7. Exit codes

- `0` only after the output has been successfully published.
- Non-zero on any failure (configuration, download, parsing, validation, persistence,
  publication).

## 8. Logging

- Uniform format: `HH:MM:SS [LEVEL] message key=value ...`.
- `INFO`: effective configuration (no secrets), each stage, the first record, progress
  every 1000 records, commit, publication, final summary.
- `WARNING`: use of an approximate match, tolerated category divergence, replacement of
  an existing output, non-TLS URL.
- `ERROR`: download, parsing, validation, persistence, publication failures.
- Never logged: full records, `.env` contents, signed URLs, values rejected by Pydantic,
  percentages when the total is unknown.
- Pending changes, a completed commit, and an actually-published output are reported as
  distinct events.
