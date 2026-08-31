# Specification — data profile

Profile of the published inputs and the shape of the refactored database. Numbers are a
snapshot; the remote content may change.

## Base catalog as given (`catalog.db`)

```sql
CREATE TABLE Product (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL,
    Brand TEXT,
    Category TEXT
);

CREATE TABLE SellerProduct (
    Id INTEGER PRIMARY KEY AUTOINCREMENT,
    SellerName TEXT NOT NULL,
    ProductId INTEGER NOT NULL CONSTRAINT FK_Product_Id REFERENCES Product (Id),
    SellerProductId INTEGER NOT NULL
);
```

- `Product`: **975 rows**. `Name` is **unique after normalization** (zero collisions) —
  an exact normalized-name lookup returns at most one product.
  - `Brand`: **119 rows** null. **639** distinct raw strings; **637** after normalization
    — the 2 that merge: `BLACK+DECKER` / `Black+Decker` and `Simplehuman` / `simplehuman`.
  - `Category`: **34 rows** null. **43** distinct strings; **43** after normalization
    (no merges).
- `SellerProduct`: **0 rows**. No index, no uniqueness constraint.

Why this model is compromised, the design decisions, and the accepted risks are in
[`prd.md`](../prd.md#database-refactor). This file is the **single source of truth for
the schema and the migration**; other documents reference this section rather than
restating it.

## Refactored database

Before any feed processing, the downloaded copy is refactored — both given tables
dropped and recreated, three reference tables (`Brand`, `Category`, `Seller`) added —
via SQLAlchemy Core (declarative `Table` metadata + DDL + `insert()`; no ORM, no
Alembic), inside the single import transaction. Every primary key is a `uuid4` stored
as `TEXT`, minted in Python; no `AUTOINCREMENT`.

### Conditional migration

Guarded by `PRAGMA user_version`, so the tool can also run against a database it has
already produced.

| Detected source | Classified as | Action |
| --- | --- | --- |
| `user_version = 0` **and** legacy tables present: `Product` with integer `Id` + `Brand` + `Category` columns, `SellerProduct` with `SellerName` + `SellerProductId`, no `Brand`/`Category`/`Seller` tables | legacy | run the migration steps, then `PRAGMA user_version = 1` |
| `user_version = 1` **and** target tables present: `Brand`, `Category`, `Seller`; `Product.Id` is `TEXT`; `SellerProduct` has `ExternalSku` | already migrated | skip the migration |
| neither | unrecognized | abort before any write, non-zero exit |

The published `catalog.db` is always legacy. Re-running against a previous output
(`user_version = 1`) re-applies the feed with idempotent writes and is a no-op when the
feed is unchanged.

### Target schema

```sql
CREATE TABLE Brand (
    Id   TEXT PRIMARY KEY,                -- uuid4
    Name TEXT NOT NULL UNIQUE             -- normalized brand string
);

CREATE TABLE Category (
    Id   TEXT PRIMARY KEY,                -- uuid4
    Name TEXT NOT NULL UNIQUE             -- normalized category string
);

CREATE TABLE Product (
    Id         TEXT PRIMARY KEY,          -- uuid4
    Name       TEXT NOT NULL,
    BrandId    TEXT REFERENCES Brand (Id),      -- nullable (119 base rows)
    CategoryId TEXT REFERENCES Category (Id)    -- nullable (34 base rows)
);

CREATE TABLE Seller (
    Id   TEXT PRIMARY KEY,                -- uuid4
    Name TEXT NOT NULL UNIQUE
);

CREATE TABLE SellerProduct (             -- many-to-many link + the seller's own id
    SellerId    TEXT NOT NULL REFERENCES Seller (Id),
    ProductId   TEXT NOT NULL REFERENCES Product (Id),
    ExternalSku TEXT NOT NULL,            -- the feed entry's Id (opaque)
    PRIMARY KEY (SellerId, ProductId),
    UNIQUE (SellerId, ExternalSku)
);
```

`Brand` and `Category` are reference tables (0..1 per product → nullable FK), not
`BrandProduct` / `CategoryProduct` junctions. `SellerProduct` is a junction because a
product genuinely has many sellers.

### Kept / replaced / deleted

- **`Product`** — dropped and recreated. `Name` carried over for all 975 rows; `Id`
  becomes a fresh `uuid4` (old integer id discarded); `Brand` / `Category` text columns
  replaced by `BrandId` / `CategoryId` FKs (`NULL` for the 119 brand-less and 34
  category-less rows).
- **`SellerProduct`** — dropped and recreated: `Id` removed, `SellerName` → `Seller`,
  `SellerProductId INTEGER` → `ExternalSku TEXT`, `ProductId` becomes a UUID FK; new
  composite PK and `UNIQUE (SellerId, ExternalSku)`.
- **`Brand`**, **`Category`**, **`Seller`** — created with UUID PKs.

### Migration steps

`PRAGMA foreign_keys = OFF` for the whole block; staged tables built alongside the
originals then swapped in. Normalization and every `uuid4` are computed in Python, so
each extraction reads distinct values and builds an in-memory map (not a pure
`INSERT ... SELECT`).

1. **`Product.Brand` → `Brand`**: `(uuid4(), name)` per distinct normalized non-empty
   brand (**637** rows); keep `{normalized_brand: id}`.
2. **`Product.Category` → `Category`**: same (**43** rows); keep `{normalized_category: id}`.
3. **`Product` rebuild**: `Product_new`; per old row `(uuid4(), Name, brand_map.get(...),
   category_map.get(...))`; keep `{old_int_id: new_uuid}`.
4. **`SellerProduct.SellerName` → `Seller`**: `(uuid4(), name)` per distinct name (base
   table empty → 0 rows; sellers are created later from the feed).
5. **`SellerProduct` rebuild**: `SellerProduct_new`; each old row remapped through the
   seller and product maps, `CAST(SellerProductId AS TEXT)` → `ExternalSku` (base table
   empty → 0 rows).
6. `DROP` the two old tables; rename `Product_new` / `SellerProduct_new` into place.
7. `PRAGMA foreign_keys = ON`; `PRAGMA foreign_key_check`; `PRAGMA user_version = 1`.

Steps 1–7 are skipped entirely for an already-migrated source. WAL is not used; the
output is a self-contained database written after the engine is disposed.

## Seller feed (`ProductEntry.json`)

- **269 entries**. Every entry has all five keys: `Id`, `SellerName`, `Name`, `Brand`,
  `Category`.
- `Brand` is `null` in 3 entries (`Cable Organizer Kit`, `Bed Frame Wood King`,
  `Round Rug 6 Feet`). `Category` always present (28 distinct normalized values; `photo`
  is the only one not in the catalog). `Name`, `SellerName` never empty.
- **20** distinct seller names.
- `Id`: 255 distinct of 269.
  - **3 are not valid UUIDs**: `ddddeee-ffff-4000-1111-222233334444` (7-char group),
    `09835342345-4678-9abc-def012345678` (4 groups, oversized first),
    `uddd0000-eeee-4111-ffff-aaaa22223333` (`u` is not hex).
  - **14 `Id` strings are reused**, always across *different* sellers — so
    `UNIQUE (SellerId, ExternalSku)` is never violated, and `Id` cannot identify a
    product.
  - `Id` is kept as `SellerProduct.ExternalSku`, opaque text.
- **1 `(SellerName, Id)` pair repeats**: `GardenStore`, same `Id`, once
  `"Câmera Canon EOS R6"` and once `"Camera Canon EOS R6"` — both resolve to the same
  product, so one link.

## Match outcomes (with the shipped rules)

Screening → normalization → exact lookup → gated fuzzy.

| Outcome | Count |
| --- | --- |
| Rejected by the SQL injection screen (`threat`) | 1 |
| Resolves to exactly one catalog product (exact normalized name) | 266 |
| Resolves via one gated fuzzy candidate | 2 |
| No match, new product | 0 (the only candidate is the threat) |
| Ambiguous / brand conflict (`skipped`) | 0 |

The 3 entries without an exact normalized-name match:

| Feed name | Nearest catalog name | `difflib.ratio` | Decision |
| --- | --- | --- | --- |
| `Roteador WiFi 6 TP-Link` | `Router WiFi 6 TP-Link` | **0.909** | fuzzy match (passes the 0.90 gate) |
| `Processador AMD Ryzen 9 7950X` | `Processor AMD Ryzen 9 7950X` | **0.964** | fuzzy match |
| `Security Test Product` | `Security Camera Nest` | 0.585 | would be new — but rejected as a threat |

`rapidfuzz.fuzz.ratio` uses the same `2M/T` formula as `difflib.ratio` and also yields
`0.909` for `Roteador/Router`; the shared `0.90` threshold holds for both backends.

## Field-level ambiguities observed

The full list with the decision taken for each class is the
[ambiguity register in `prd.md`](../prd.md#ambiguity-register-every-class-occurs-in-the-real-data).
Data specifics found here:

- **Whitespace**: 60 feed entries contain a double space (`"Smartphone  Galaxy S23"`).
- **Brand spelling**: catalog `BLACK+DECKER` vs `Black+Decker` and `Simplehuman` vs
  `simplehuman` (merge on normalization); feed `"Levi's"` vs catalog `"Levis"`.
- **Category disagreement**: `Camera Canon EOS R6` — `Photography` in the catalog,
  `Photo` in the feed → two distinct `Category` rows.
- **SQL injection probe**: the `MegaStore` / `"Security Test Product"` entry has
  `Brand = "TestBrand'; SELECT 1; --"` and one of the malformed `Id`s. `libinjection`
  flags the brand (rejected, `threat`) but does **not** flag `"Levi's"` or `12.9''`.

## Resulting row counts

| Table | Rows | Note |
| --- | --- | --- |
| `Brand` | **637** | distinct normalized catalog brands; no new brands (the one new-product candidate is a threat) |
| `Category` | **43** | distinct normalized catalog categories; no new categories |
| `Product` | **975** | same count, rebuilt with fresh `uuid4` ids; `Brand`/`Category` replaced by `BrandId`/`CategoryId`; 119 rows `BrandId IS NULL`, 34 rows `CategoryId IS NULL` |
| `Seller` | **20** | distinct feed seller names |
| `SellerProduct` | **256** | distinct `(SellerId, ProductId)`; 12 feed entries collapse onto an existing pair (11 log `duplicate_listing`), 1 entry is a threat |

Local SQLite build note: `spellfix1` is unavailable; FTS5 and the trigram tokenizer are
present. Indexed candidate reduction is out of scope regardless.
