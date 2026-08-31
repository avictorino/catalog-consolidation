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
3. Replace every run of non-alphanumeric characters with a single space.
4. Collapse whitespace, trim.

Digits and decimal separators inside numbers are preserved (step 3 keeps `12.9` as
`12 9`, which is stable across `12.9"`, `12.9''`, `12.9`).

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
  `insert()` / `select()` / `insert().from_select()`). No ORM, no Alembic.
- One transaction per import — covering the schema refactor and all feed processing.
  **Not** one transaction per entry.
- All statements that carry external data are parameterized (Core does this by construction).
- `SellerProduct` identity is `(SellerId, ProductId)`; `UNIQUE (SellerId, ExternalSku)`
  is also enforced. Re-inserting the same pair is a no-op (`INSERT OR IGNORE`).
- `Brand` and `Seller` rows are created on demand, keyed by their `UNIQUE` `Name`.
- On any failure (network, JSON, validation, database), the transaction is rolled back,
  the temp database is discarded, and the previous output file is left untouched.
- A failure during the final atomic replacement also preserves the previous output.

## 5. Schema refactor (on the downloaded copy, before feed processing)

The given model is compromised: denormalized `SellerName`, `Product.Brand`,
`Product.Category`; mistyped `SellerProductId`; a pointless surrogate key; no
uniqueness. It **is really altered**, not shadowed.

### Conditional migration

Guarded by `PRAGMA user_version` so the tool can run incrementally against a database
it has already produced:

| Source | Classified as | Action |
| --- | --- | --- |
| `user_version = 0` and legacy tables present (`Product.Brand` + `Product.Category` text columns, `SellerProduct.SellerName`, `SellerProduct.SellerProductId`, no `Brand`/`Category` tables) | legacy | run the migrations, then `PRAGMA user_version = 1` |
| `user_version = 1` and target tables present (`Brand`, `Category`, `Seller`; `SellerProduct.ExternalSku`) | already migrated | skip the migrations |
| neither | unrecognized | abort before any write, non-zero exit |

The published `catalog.db` is always legacy. Running against a previous output re-applies
the feed with idempotent writes and is a no-op when the feed is unchanged.

### Target schema

```sql
CREATE TABLE Brand (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE            -- normalized brand string
);

CREATE TABLE Category (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE            -- normalized category string
);

CREATE TABLE Product (
    Id         INTEGER PRIMARY KEY AUTOINCREMENT,
    Name       TEXT NOT NULL,
    BrandId    INTEGER REFERENCES Brand (Id),
    CategoryId INTEGER REFERENCES Category (Id)
);

CREATE TABLE Seller (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE
);

CREATE TABLE SellerProduct (
    SellerId    INTEGER NOT NULL REFERENCES Seller (Id),
    ProductId   INTEGER NOT NULL REFERENCES Product (Id),
    ExternalSku TEXT NOT NULL,
    PRIMARY KEY (SellerId, ProductId),
    UNIQUE (SellerId, ExternalSku)
);
```

### Kept / altered / deleted

| Table | Change |
| --- | --- |
| `Product` | altered in place: `Id` (values + `sqlite_sequence` preserved) and `Name` kept; `BrandId`, `CategoryId` nullable FKs added; the `Brand` and `Category` text columns dropped after their data is migrated |
| `SellerProduct` | dropped and recreated: `Id` gone, `SellerName` → `Seller`, `SellerProductId` (INTEGER) → `ExternalSku` (TEXT), `ProductId` kept; new composite PK and `UNIQUE (SellerId, ExternalSku)` |
| `Brand`, `Category`, `Seller` | created |

### Migration steps (all inside the import transaction, `foreign_keys = OFF`)

Each extraction reads distinct values, folds them with the shared Python normalizer, and
inserts via SQLAlchemy Core (not a pure `INSERT ... SELECT`).

1. **`Product.Brand` → `Brand`**: `CREATE TABLE Brand`; insert distinct normalized
   non-empty brands; `ALTER TABLE Product ADD COLUMN BrandId INTEGER REFERENCES Brand (Id)`;
   `UPDATE Product SET BrandId = <lookup>` (`NULL` when brand null/empty);
   `ALTER TABLE Product DROP COLUMN Brand`.
2. **`Product.Category` → `Category`**: same shape, producing `CategoryId` and dropping
   `Product.Category`.
3. **`SellerProduct` rebuild** (PK changes → staged table, not `ALTER`): `CREATE TABLE Seller`;
   insert distinct `SellerName`; `CREATE TABLE SellerProduct_new (...)`; copy rows via the
   seller-name join, `CAST(SellerProductId AS TEXT)` into `ExternalSku`; drop the old table;
   rename. Base table empty → the row copy is a no-op but is written to work with data.
4. `PRAGMA foreign_keys = ON`; `PRAGMA foreign_key_check`; `PRAGMA user_version = 1`.

- Steps 1–4 are skipped entirely when the source is already `user_version = 1`.
- `Brand.Name` / `Category.Name` hold the normalized form; raw spelling is not preserved.
- WAL is not used; the published output is a self-contained database written after the
  engine is disposed.

## 6. Matcher interface

```python
class Similarity(Protocol):
    name: str
    suggested_threshold: float
    def score(self, a: str, b: str) -> float: ...   # inputs already normalized; returns [0, 1]
```

- Both `Similarity` implementations must satisfy the same contract and pass the same
  test suite.
- The backend is chosen in the CLI and injected into `consolidate(...)` as a keyword
  argument.
- `rapidfuzz` is imported lazily; running with `--matcher difflib` must not require it.
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
