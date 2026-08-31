# Specification — acceptance criteria

## Expected result

Against the currently published sources, with either matcher backend:

| Metric | Value | Derivation |
| --- | --- | --- |
| `Brand` rows after import | **637** | distinct normalized catalog brands (`BLACK+DECKER`/`Black+Decker` and `Simplehuman`/`simplehuman` each merge); no new brands from the feed |
| `Category` rows after import | **43** | distinct normalized catalog categories (no merges); no new categories from the feed |
| `Product` rows after import | **975** | base 975 rebuilt with fresh `uuid4` ids; the only new-product candidate (`Security Test Product`) is rejected as a threat. 119 rows have `BrandId IS NULL`, 34 have `CategoryId IS NULL` |
| `Seller` rows after import | **20** | distinct `SellerName` values in the feed |
| `SellerProduct` rows after import | **256** | distinct `(SellerId, ProductId)`; 12 feed entries re-offer a product the seller already has (11 log `duplicate_listing`), 1 entry is a threat |
| `new` | 0 | PT→EN name variants match existing products; the one genuine new product is a threat |
| `skipped` | 0 | no ambiguous multi-candidate, brand-conflict, or SKU-conflict case in the current data |
| `threat` | 1 | `MegaStore` / `"Security Test Product"` — `Brand = "TestBrand'; SELECT 1; --"` |

Treated as a check, not a guarantee: if the remote content changes, the numbers change.
The tool must still be correct; only this table becomes stale.

## Acceptance scenarios

### Configuration

- With the shipped `.env`, a bare `python -m consolidation.cli` resolves to the S3
  sources, `catalog_output.db`, `difflib`, threshold `0.90`.
- Every option has a key in `.env`; precedence holds: CLI > env var > `.env` > built-in
  fallback.
- Removing `.env` entirely still resolves the same values from the built-in fallbacks.
- A `THRESHOLD` in `.env` or the environment overrides the backend's suggested value.
- `.env` is located relative to the entry point even when the process runs from another
  directory.
- A non-TLS `http://` source URL produces a `WARNING` and still runs.

### Download and refactor

- Every run downloads the base catalog again; the previous output is never read as input.
- The base catalog is downloaded in chunks to a temp file, not held whole in memory.
- A corrupt or non-SQLite download is rejected before any write.
- After the refactor the database has `Brand (Id, Name UNIQUE)`, `Category (Id, Name UNIQUE)`,
  `Product (Id, Name, BrandId, CategoryId)`, `Seller (Id, Name UNIQUE)`, and
  `SellerProduct (SellerId, ProductId, ExternalSku)` with `PRIMARY KEY (SellerId, ProductId)`
  and `UNIQUE (SellerId, ExternalSku)`; `foreign_key_check` passes; `user_version = 1`.
- Every `Id` / FK column is a 36-char `uuid4` string; no table uses `AUTOINCREMENT` and
  `sqlite_sequence` is empty or absent.
- `Product` no longer has `Brand` or `Category` text columns; all 975 base rows are
  present with their `Name` and a fresh `uuid4` `Id`, plus a `BrandId` and `CategoryId`
  (each possibly `NULL`); no product row is lost.
- `Brand` holds 637 rows (the two case/punctuation pairs merge); `Category` holds 43.
- The refactor is conditional: a legacy source (`user_version = 0`, integer `Product.Id`,
  `Product.Brand` + `Product.Category` text columns, `SellerProduct.SellerName`) is
  migrated; a source already at `user_version = 1` skips the migrations; an unrecognized
  schema aborts before any write.
- Running the tool a second time against its own previous output (an already-migrated
  DB) with the same feed produces logically identical tables and reports 0 `new`.
- The refactor runs inside the same transaction as feed processing; a later failure
  rolls back the new tables too.
- Database access is via SQLAlchemy Core; no Alembic migration files exist in the repo.

### Streaming

- The feed is consumed incrementally: no `response.json()`, no `response.content`, no
  `list(iterator)`, no local copy of the JSON.
- The feed arriving in many small chunks — including chunk boundaries that split a
  multi-byte UTF-8 character — is parsed correctly.

### Validation and transactions

- An invalid root (object instead of array) aborts before any write.
- A JSON document truncated mid-stream aborts; the previous output is preserved.
- An invalid record after several pending inserts triggers a full rollback; the previous
  output is preserved (including the schema refactor).
- `[]` as the feed is valid and produces the refactored base catalog with no links.

### Identity

- Exact duplicate (whitespace / accent / punctuation variant) → link only, no new product.
- PT→EN translation variant within the fuzzy gate → link only.
- Missing brand on one side → still matches on name.
- Category difference between entry and product → link, `WARNING` logged.
- A model/capacity difference (`128GB` vs `256GB`) → not matched, new product.
- A constructed two-candidate case → entry skipped and reported, import continues.

### Brands, categories, sellers and links

- A brand name new to the database creates exactly one `Brand` row; brand names that
  normalize equally share a row. Same for `Category`.
- A new product with `Brand = null` is inserted with `BrandId IS NULL`; the migration
  leaves the 119 brand-less and 34 category-less base rows with `NULL` FKs.
- A seller name new to the database creates exactly one `Seller` row; the same name
  reuses it.
- The same product offered by two different sellers produces two links, each with its
  own `ExternalSku`.
- A seller offering the same product via several feed entries (name variants, distinct
  feed `Id`s) produces one `(SellerId, ProductId)` link; the first entry's `Id` is
  stored as `ExternalSku`, the rest log `duplicate_listing`.
- An entry whose `(SellerId, ExternalSku)` already maps to a different product is
  skipped and reported.
- Re-running the whole import produces the same table contents (no duplicate links).
- `ExternalSku` holds the feed `Id` verbatim, including the malformed ones.

### Matcher backends

- The same identity test suite passes with `--matcher difflib` and `--matcher rapidfuzz`.
- `--matcher difflib` runs without `rapidfuzz` installed.
- `--threshold` overrides the backend default and changes outcomes at the boundary
  (`Roteador/Router` at `0.909`: included at `0.90`, excluded at `0.91`).

### Security

- An entry with a `libinjection`-detected SQL injection payload in any string field
  (`"TestBrand'; SELECT 1; --"`) is **rejected**: no product, no seller, no link. A
  `WARNING` with `event=sqli_attempt`, the field name, the libinjection fingerprint,
  and the value truncated to 120 chars is logged, and `threat` is incremented.
- The import is not aborted by a threat; subsequent entries are still processed.
- Benign quotes and apostrophes in legitimate data are **not** rejected: `"Levi's"` as
  a brand and `12.9''` in a name pass the screen and are imported normally.
- Every DB statement that carries external data uses parameterized SQL; a raw payload
  that somehow reached persistence would still be stored as an inert string, never
  executed.
- Referential integrity holds after the refactor; every base `Product` survives with its
  `Name` (new `uuid4` `Id`, `Brand` → `BrandId`, `Category` → `CategoryId`), and any
  pre-existing `SellerProduct` rows survive as `(SellerId, ProductId, ExternalSku)` links
  with the remapped UUID foreign keys.

### Observability

- Exit code `0` only after publication; non-zero on any failure. Contained threats and
  skips keep the exit code at `0`.
- The final summary reports `processed`, `new`, `linked`, `skipped`, `threat`.
- When `threat > 0`, a summary `WARNING` restates the count.
- Two runs from the same sources produce logically equivalent tables.
