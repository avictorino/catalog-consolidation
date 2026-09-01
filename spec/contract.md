# Specification — contract

Normative contract for the consolidation tool. Acceptance criteria are in
[`acceptance.md`](acceptance.md); the data profile is in [`data-profile.md`](data-profile.md).

## 1. Command-line interface

Entry point: `python -m consolidation.cli`.

| Option | `.env` key | `.env.example` value | Meaning |
| --- | --- | --- | --- |
| `--catalog-url` | `CATALOG_URL` | `https://engineering-hiring-process.s3.us-east-1.amazonaws.com/catalog.db` | HTTP(S) URL of the base SQLite catalog |
| `--products-url` | `PRODUCTS_URL` | `https://engineering-hiring-process.s3.us-east-1.amazonaws.com/ProductEntry.json` | URL of the seller feed |
| `--output` | `OUTPUT` | `catalog_output.db` (in the current working directory) | destination path for the consolidated database |
| `--source` | `SOURCE` | `http` | byte-stream transport for the **seller feed only**: `http` or `s3` |
| `--matcher` | `MATCHER` | `rapidfuzz` | similarity backend: `rapidfuzz` (default) or `difflib` |

There is no `--threshold` CLI option. `THRESHOLD` in `.env` is optional and read
directly by the similarity backend — see §7.

- `--catalog-url` is always fetched over HTTP(S) with `requests`; only HTTP(S)
  URLs are accepted and a non-TLS `http://` URL is allowed but logged as a warning.
- `--source http` (default) also reads `--products-url` over HTTP(S).
- `--source s3` streams the **feed** with `boto3` `get_object` **unsigned** (the
  object must be publicly readable, no credentials resolved). `--products-url`
  must then be `s3://bucket/key` or an `…amazonaws.com` HTTP(S) URL; the latter is
  rewritten to `s3://bucket/key` at config time (logged at `INFO`). The catalog
  URL is unaffected by `--source`.
- Local files are not supported in this version.

### Configuration resolution

Configuration is deliberately small: for each option, a **CLI flag overrides the value
in `.env`**. There is no environment-variable layer and there are no built-in fallbacks.

- `.env` (copied from `.env.example`) is looked up next to the `consolidation` package,
  so it is found regardless of the current working directory.
- If an option is set in **neither** the CLI nor `.env`, the run is invalid: an
  `ERROR` is logged and the process exits non-zero before any work starts. A missing
  `.env` file is treated the same as an empty one.
- Invalid values (unknown `--matcher`, unknown `--source`, a URL whose scheme
  does not match the selected source) are also logged as `ERROR` and abort the
  run before any work starts. A non-float or out-of-range `THRESHOLD` is **not**
  caught here — see §7.

## 2. Input validation (seller feed)

Each entry is validated one at a time with Pydantic v2 (`model_validate`); no Pydantic
list is materialized.

- The document root must be a JSON array of objects. `[]` is valid.
- `Id`, `SellerName`, `Name`: required, non-empty strings after trimming.
- `Brand`, `Category`: optional string or `null`.
- `Id` is treated as an opaque string. It is **not** parsed as a number and **not**
  required to be a UUID.
- Unknown fields are ignored.
- A schema validation failure aborts the run (rollback, previous output preserved) and
  is logged with the record index and the offending field — never the record contents.

### SQL injection screening

After schema validation, every string field of the entry (`Id`, `SellerName`, `Name`,
`Brand`, `Category`) is screened with `libinjection` (`libinjection-python`), the
tokenizer/fingerprint engine used by WAFs such as ModSecurity.

- If `libinjection.is_sql_injection(value)["is_sqli"]` is true for any field, the entry
  is a **threat**: it is **not imported** (no product, no seller, no link), a `WARNING`
  is logged (`event=sqli_attempt`, record index, field name, libinjection fingerprint,
  and the value truncated to 120 characters), and the `threat` counter is incremented.
- Processing continues with the next entry — a threat does not abort the import.
- libinjection is chosen over a regex because it distinguishes real payloads
  (`"TestBrand'; SELECT 1; --"`) from benign apostrophes and quotes in legitimate data
  (`"Levi's"`, `12.9''`). Residual false-positive risk is accepted; the `threat` list
  in the final report lets an operator review every rejected entry.
- Parameterized SQL (§5) remains the actual defense; this screen is defense in depth
  and alerting.

## 3. Identity resolution

### Normalization

One function, applied identically to catalog names, feed names, brand values, and
category values:

1. Unicode NFKD, drop combining marks (accent folding).
2. Lowercase.
3. Remove quote and apostrophe marks with no replacement (straight `'` `` ` `` `"`,
   curly single/double, prime / double-prime, acute, modifier apostrophe).
4. Replace every run of remaining non-alphanumeric characters with a single space.
5. Collapse whitespace, trim.

Digits and decimal separators inside numbers become separate tokens (step 4 keeps
`12.9` as `12 9`, stable across `12.9"`, `12.9''`, `12.9`). Step 3 is what makes feed
`Levi's` normalize to `levis` and match catalog `Levis`.

### Matching stages

Only entries that pass both schema validation and the SQL injection screen (§2) reach
this point.

1. **Exact**: look up the normalized feed name in the normalized-name index of the
   catalog. Catalog names are unique after normalization, so this yields 0 or 1.
2. **Same words, different order** (only if stage 1 misses): compare the non-empty
   multisets of normalized name tokens, preserving repeated words and numeric/model
   tokens. Filter matches by brand compatibility (equal when both are present).
   Exactly one compatible candidate is linked without a similarity score or threshold.
   Multiple compatible candidates are skipped as ambiguous; if all word matches have
   conflicting brands, skip the entry as a brand conflict. These outcomes are final;
   do not fall back to fuzzy matching for an ambiguity or a brand conflict.
3. **Fuzzy** (only if neither earlier stage finds a name match): scan the catalog,
   score each product with the selected `Similarity`, and keep those that pass the gate:
   - both brands, after normalization, are equal — when both are present;
   - same number of whitespace-separated tokens;
   - the multiset of tokens that contain a digit is equal;
   - `score >= similarity.threshold` (§7 — the backend's own resolved cutoff).

### Outcomes

| Situation | Action |
| --- | --- |
| Exactly one product (exact name, same words, or one eligible fuzzy candidate) | `get_or_create` the seller, then `INSERT OR IGNORE` the `(SellerId, ProductId, ExternalSku)` link |
| No product and no eligible candidate | `get_or_create` the brand and the category, insert a new `Product`, then the link |
| The `(SellerId, ProductId)` link already exists | no change; if the incoming `ExternalSku` differs from the stored one, log `event=duplicate_listing` |
| The incoming `(SellerId, ExternalSku)` already maps to a different product | skip the entry, record it (no silent re-association) |
| Two or more eligible candidates, or a brand conflict on an otherwise-matching name | skip the entry, record it in the report, continue |

- Existing product attributes, including `BrandId`, are never enriched or overwritten.
- Category never affects identity. A linked entry adds its category to
  `ProductCategory` when necessary; a category difference is logged at `WARNING`.
- `ExternalSku` stores the feed `Id` of the entry that first created the link.

## 4. Input transport

- The **catalog** is always downloaded with plain `requests` (`download_to`). It
  writes a temp file the schema refactor then largely rewrites, so that transport
  is deliberately not swappable.
- The **seller feed** is read through one `ByteSource` (in
  `consolidation.services`), selected by `--source` at the composition root and
  injected into `ConsolidateCatalogUseCase` (and on to `iter_feed`). The use case
  and `iter_feed` never import `requests` or `boto3`.
- `HttpByteSource` (`--source http`) streams with `requests` and is the default.
  `S3ByteSource` (`--source s3`) streams with `boto3` `get_object`, signature
  version `UNSIGNED` — no credential chain is consulted and the object must be
  public. `boto3` is imported lazily inside `S3ByteSource.open`, so
  `--source http` must not require it.
- `parse_s3_ref` accepts `s3://bucket/key`, virtual-hosted
  `https://bucket.s3.<region>.amazonaws.com/key`, and path-style
  `https://s3.<region>.amazonaws.com/bucket/key`. When `--source s3` is set, the
  CLI normalizes `--products-url` to the `s3://bucket/key` form up front.
- Both transports satisfy the identical `open(ref) -> context-managed binary
  stream` contract and pass the same feed test suite.

## 5. Persistence

- Database access is through **SQLAlchemy Core** — declarative `Table` metadata
  (`consolidation.schema`), `insert()` / `select()` in the repositories
  (`consolidation.repository`), and parameterized `text()` statements for the
  migration steps (`consolidation.infrastructure`). No ORM. The refactor is a single
  Alembic revision (`0001`); no revision chain, no autogenerate, no offline mode.
- The whole run uses **one connection**, opened by `PrepareCatalogDatabaseUseCase`
  and handed to the consumption use case still open. The schema refactor runs in its
  setup transaction (Alembic invoked with that connection injected,
  `config.attributes["connection"]`). Feed processing then uses one transaction per
  entry (`CatalogRepositories.entry_transaction()`): a successful entry is committed
  immediately, an entry failure is rolled back in isolation, recorded, and the
  in-memory caches are reloaded before the next entry is attempted.
- All statements that carry external data are parameterized (Core does this by construction).
- `SellerProduct` identity is `(SellerId, ProductId)`; `UNIQUE (SellerId, ExternalSku)`
  is also enforced. Re-inserting the same pair is a no-op (`INSERT OR IGNORE`).
- `Brand`, `Category`, and `Seller` rows are created on demand, keyed by their `UNIQUE`
  `Name` (id = a Python-minted `uuid4`). `ProductCategory` rows are created on demand
  with an idempotent composite `(ProductId, CategoryId)` key.
- On a global failure (network, JSON parsing, schema validation, database setup), the
  temp database is discarded and the previous output file is left untouched. A feed-item
  persistence failure rolls back only that item, is logged at the end, and later items
  continue; the successfully processed temp database is published with a non-zero exit
  code.
- A failure during the final atomic replacement also preserves the previous output.

## 6. Schema refactor (on the downloaded copy, before feed processing)

The full target schema, the `alembic_version` guard, the kept/replaced/deleted
breakdown, and the migration steps are defined in
[`data-profile.md#refactored-database`](data-profile.md#refactored-database). Normative
requirements on top of that:

- The refactor runs **inside the setup transaction** (§5), before the first
  feed entry is processed — Alembic is invoked with that connection injected.
- It is **conditional**: `classify_source` rejects an unrecognized schema before any
  write with a non-zero exit; otherwise `alembic upgrade head` runs — revision `0001`
  for a legacy source, a no-op for a source already at `0001`.
- Every primary key is a Python-minted `uuid4` `TEXT`; there is no `AUTOINCREMENT`, and
  `sqlite_sequence` carries no counters after the refactor.
- FK enforcement stays off during the rebuild (SQLite makes `PRAGMA foreign_keys` a
  no-op inside a transaction); `PRAGMA foreign_key_check` must be empty and
  `alembic_version` must read `0001` before feed processing begins.
- After the setup transaction commits, enable `PRAGMA foreign_keys = ON` on the
  import connection and verify that it reads `1` before consuming the JSON. Apply
  this to already-migrated sources too; fail before feed processing/publication if
  enforcement cannot be enabled. Keep it enabled throughout all item transactions,
  including after rollbacks. A foreign key violation is an item persistence failure.
- WAL is not used; the published output is self-contained, written after the engine is
  disposed.
- No Alembic offline (`--sql`) mode and no autogenerate; the single revision is
  hand-written.

## 7. Matcher interface

The `Similarity` protocol (in `consolidation.services`) and its two implementations
(in `consolidation.infrastructure`) are described in
[`prd.md#injected-seams-dependency-injection`](../prd.md#injected-seams-dependency-injection).
Normative requirements:

- Both implementations satisfy the identical `score(a, b) -> float` (`[0, 1]`) contract
  and pass the same test suite.
- The backend is chosen in the CLI (`--matcher`), wrapped in a `ProductIdentityResolver`
  with **no threshold argument**, and that single resolver is injected into the use
  cases. `rapidfuzz` is imported lazily — `--matcher difflib` must not require it.
- **Threshold resolution is the backend's own responsibility, not a parameter passed
  in.** Each backend exposes `threshold: float`, a `functools.cached_property`
  resolved on first access: an explicit constructor value if given (tests only), else
  `THRESHOLD` read from `.env`, else `suggested_threshold` (`0.90` for both backends).
  Resolution happens the first time the fuzzy stage runs, not at startup — a malformed
  `THRESHOLD` (non-float or outside `[0, 1]`) surfaces as a `ValueError` at that point,
  not during CLI configuration resolution (§1). Once resolved, the value is cached for
  the life of the backend instance — editing `.env` mid-run has no effect.
- `fuzz.WRatio` / `token_set_ratio` / `token_sort_ratio` are disallowed.
- Candidate retrieval for the fuzzy stage is a plain `select(Product)` scan.
- The `ByteSource` transport (§4) follows the same port/adapter/lazy-import shape,
  but is chosen via `--source`/`SOURCE`, unlike the threshold.

## 8. Report and exit codes

The final summary reports: `processed`, `new`, `linked`, `skipped`, `threat`, `failed`.

- `processed` — entries read from the feed.
- `new` — products inserted.
- `linked` — `(SellerId, ProductId)` links inserted.
- `skipped` — entries dropped for ambiguity or a brand conflict (§3).
- `threat` — entries rejected by the SQL injection screen (§2).
- `failed` — entries whose transaction rolled back on a persistence error; each is
  listed afterwards with its record index and captured reason.

Exit codes:

- `0` only after the output has been successfully published. Contained threats and
  skips do not change this — the import still succeeded.
- Non-zero on any failure (configuration, download, parsing, schema validation,
  item persistence, publication). Item failures are reported after the feed is
  exhausted and do not prevent successful items from being published.

## 9. Logging

- Uniform format: `HH:MM:SS [LEVEL] message key=value ...`. On a TTY, `WARNING` and
  `ERROR` lines are coloured red; output to a pipe or file carries no ANSI.
- `INFO`: effective configuration (no secrets; includes `source=`), each stage
  (the download and feed lines carry `via=<source>`), the first record, progress
  every 1000 records, commit, publication, final summary (with all six counters).
- `WARNING`: SQL injection attempt (`event=sqli_attempt`), use of an approximate match,
  tolerated category divergence, replacement of an existing output, non-TLS URL. When
  `threat > 0`, a summary `WARNING` repeats the count.
- `ERROR`: download, parsing, schema validation, persistence, publication failures.
  Feed item persistence failures are printed after the feed is exhausted with the
  record index and the captured reason.
- Never logged: full records, `.env` contents, signed URLs, values rejected by Pydantic.
  A threat log includes the offending value truncated to 120 characters (needed for
  forensics), never the whole record.
- Pending changes, a completed commit, and an actually-published output are reported as
  distinct events.
