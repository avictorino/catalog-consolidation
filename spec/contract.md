# Specification — contract

Normative contract for the consolidation tool. Acceptance criteria are in
[`acceptance.md`](acceptance.md); the data profile is in [`data-profile.md`](data-profile.md).

## 1. Command-line interface

Entry point: `python -m consolidation.cli`.

| Option | `.env` key | Shipped default | Meaning |
| --- | --- | --- | --- |
| `--catalog-url` | `CATALOG_URL` | `https://engineering-hiring-process.s3.us-east-1.amazonaws.com/catalog.db` | HTTP(S) URL of the base SQLite catalog |
| `--products-url` | `PRODUCTS_URL` | `https://engineering-hiring-process.s3.us-east-1.amazonaws.com/ProductEntry.json` | HTTP(S) URL of the seller feed |
| `--output` | `OUTPUT` | `catalog_output.db` (in the current working directory) | destination path for the consolidated database |
| `--matcher` | `MATCHER` | `difflib` | similarity backend: `difflib` or `rapidfuzz` |
| `--threshold` | `THRESHOLD` | `0.90` | float in `[0, 1]`; the fuzzy cutoff |

- Only HTTP(S) URLs are accepted for `--catalog-url` and `--products-url`. Local files
  are not supported in this version.
- A non-TLS `http://` URL is allowed but logged as a warning.

### Configuration precedence

`CLI argument > environment variable > .env file > built-in fallback`

Every option has its default set in `.env` (copied from `.env.example`), so a plain
`python -m consolidation.cli` with the shipped `.env` runs against the S3 sources with
`difflib` at `0.90`. The built-in fallback constants match those values and apply only
when `.env` and the environment are both silent.

- Environment variables: `CATALOG_URL`, `PRODUCTS_URL`, `OUTPUT`, `MATCHER`, `THRESHOLD`.
- `.env` is looked up next to the application entry point, so it is found regardless of
  the current working directory. `.env` is optional; its absence is not an error.
- A `THRESHOLD` in `.env` or the environment overrides each backend's
  `suggested_threshold`; both shipped backends suggest `0.90`, so the shipped `.env`
  is consistent with either.

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
- Parameterized SQL (§4) remains the actual defense; this screen is defense in depth
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
2. **Fuzzy** (only if stage 1 misses): scan the catalog, score each product with the
   selected `Similarity`, and keep those that pass the gate:
   - both brands, after normalization, are equal — when both are present;
   - same number of whitespace-separated tokens;
   - the multiset of tokens that contain a digit is equal;
   - `score >= threshold`.

### Outcomes

| Situation | Action |
| --- | --- |
| Exactly one product (exact match or one eligible fuzzy candidate) | `get_or_create` the seller, then `INSERT OR IGNORE` the `(SellerId, ProductId, ExternalSku)` link |
| No product and no eligible candidate | `get_or_create` the brand and the category, insert a new `Product`, then the link |
| The `(SellerId, ProductId)` link already exists | no change; if the incoming `ExternalSku` differs from the stored one, log `event=duplicate_listing` |
| The incoming `(SellerId, ExternalSku)` already maps to a different product | skip the entry, record it (no silent re-association) |
| Two or more eligible candidates, or a brand conflict on an otherwise-matching name | skip the entry, record it in the report, continue |

- Attributes of existing products (including `BrandId` and `CategoryId`) are never
  enriched or overwritten.
- `CategoryId` never affects identity. A category difference between a linked entry and
  its product is logged at `WARNING` and otherwise ignored.
- `ExternalSku` stores the feed `Id` of the entry that first created the link.

## 4. Persistence

- Database access is through **SQLAlchemy Core** (declarative `Table` metadata,
  `insert()` / `select()` / `insert().from_select()`). No ORM. The schema refactor is a
  single Alembic revision (`0001`); no revision chain, no autogenerate, no offline mode.
- One transaction per import — covering the schema refactor and all feed processing.
  **Not** one transaction per entry. Alembic runs with the import connection injected
  (`config.attributes["connection"]`) so revision `0001` executes inside that same
  transaction.
- All statements that carry external data are parameterized (Core does this by construction).
- `SellerProduct` identity is `(SellerId, ProductId)`; `UNIQUE (SellerId, ExternalSku)`
  is also enforced. Re-inserting the same pair is a no-op (`INSERT OR IGNORE`).
- `Brand`, `Category`, and `Seller` rows are created on demand, keyed by their `UNIQUE`
  `Name` (id = a Python-minted `uuid4`).
- On any failure (network, JSON, validation, database), the transaction is rolled back,
  the temp database is discarded, and the previous output file is left untouched.
- A failure during the final atomic replacement also preserves the previous output.

## 5. Schema refactor (on the downloaded copy, before feed processing)

The full target schema, the `alembic_version` guard, the kept/replaced/deleted
breakdown, and the migration steps are defined in
[`data-profile.md#refactored-database`](data-profile.md#refactored-database). Normative
requirements on top of that:

- The refactor runs **inside the single import transaction** (§4), before the first
  feed entry is processed — Alembic is invoked with that connection injected.
- It is **conditional**: `classify_source` rejects an unrecognized schema before any
  write with a non-zero exit; otherwise `alembic upgrade head` runs — revision `0001`
  for a legacy source, a no-op for a source already at `0001`.
- Every primary key is a Python-minted `uuid4` `TEXT`; there is no `AUTOINCREMENT`, and
  `sqlite_sequence` carries no counters after the refactor.
- FK enforcement stays off during the rebuild (SQLite makes `PRAGMA foreign_keys` a
  no-op inside a transaction); `PRAGMA foreign_key_check` must be empty and
  `alembic_version` must read `0001` before feed processing begins.
- WAL is not used; the published output is self-contained, written after the engine is
  disposed.
- No Alembic offline (`--sql`) mode and no autogenerate; the single revision is
  hand-written.

## 6. Matcher interface

The `Similarity` protocol and its two implementations are defined in
[`prd.md#matcher-layer-parameter-injection`](../prd.md#matcher-layer-parameter-injection).
Normative requirements:

- Both implementations satisfy the identical `score(a, b) -> float` (`[0, 1]`) contract
  and pass the same test suite.
- The backend is chosen in the CLI and injected into `consolidate(...)` as a keyword
  argument. `rapidfuzz` is imported lazily — `--matcher difflib` must not require it.
- `fuzz.WRatio` / `token_set_ratio` / `token_sort_ratio` are disallowed.
- Candidate retrieval for the fuzzy stage is a plain `select(Product)` scan.

## 7. Report and exit codes

The final summary reports: `processed`, `new`, `linked`, `skipped`, `threat`.

- `processed` — entries read from the feed.
- `new` — products inserted.
- `linked` — `(SellerId, ProductId)` links inserted.
- `skipped` — entries dropped for ambiguity or a brand conflict (§3).
- `threat` — entries rejected by the SQL injection screen (§2).

Exit codes:

- `0` only after the output has been successfully published. Contained threats and
  skips do not change this — the import still succeeded.
- Non-zero on any failure (configuration, download, parsing, schema validation,
  persistence, publication).

## 8. Logging

- Uniform format: `HH:MM:SS [LEVEL] message key=value ...`.
- `INFO`: effective configuration (no secrets), each stage, the first record, progress
  every 1000 records, commit, publication, final summary (with all five counters).
- `WARNING`: SQL injection attempt (`event=sqli_attempt`), use of an approximate match,
  tolerated category divergence, replacement of an existing output, non-TLS URL. When
  `threat > 0`, a summary `WARNING` repeats the count.
- `ERROR`: download, parsing, schema validation, persistence, publication failures.
- Never logged: full records, `.env` contents, signed URLs, values rejected by Pydantic.
  A threat log includes the offending value truncated to 120 characters (needed for
  forensics), never the whole record.
- Pending changes, a completed commit, and an actually-published output are reported as
  distinct events.
