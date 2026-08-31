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

### Why this model is compromised

- `SellerName` is denormalized free text — the seller is an entity, not a string
  repeated per row.
- `Product.Brand` and `Product.Category` are denormalized the same way; `Brand` is
  already inconsistent in the catalog.
- `SellerProductId` is typed `INTEGER`, but the feed's identifier is a UUID string
  (some not even valid UUIDs), reused across sellers, so it cannot be a key.
- The surrogate `Id` on a link table is pointless; the natural key is
  `(SellerId, ProductId)`.
- Nothing enforces uniqueness of a seller/product pair.
- `AUTOINCREMENT` integer keys are enumerable and need database coordination to mint.

## Refactored database

The published `catalog.db` reports `PRAGMA user_version = 0` and has the legacy tables,
so every normal run migrates it. The migration is conditional (guarded by
`user_version`): a database already at `user_version = 1` with the target tables skips
it, which keeps incremental re-runs against a previous output possible.

Both given tables **are really replaced** — dropped and recreated — via SQLAlchemy Core
(declarative `Table` definitions + DDL + `insert()` statements; no ORM, no Alembic),
inside the import transaction. Every primary key is a `uuid4` stored as `TEXT`, minted
in Python; no `AUTOINCREMENT`.

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
    BrandId    TEXT REFERENCES Brand (Id),
    CategoryId TEXT REFERENCES Category (Id)
);

CREATE TABLE Seller (
    Id   TEXT PRIMARY KEY,                -- uuid4
    Name TEXT NOT NULL UNIQUE
);

CREATE TABLE SellerProduct (
    SellerId    TEXT NOT NULL REFERENCES Seller (Id),
    ProductId   TEXT NOT NULL REFERENCES Product (Id),
    ExternalSku TEXT NOT NULL,           -- the seller's product id (feed entry Id)
    PRIMARY KEY (SellerId, ProductId),
    UNIQUE (SellerId, ExternalSku)
);
```

### Kept / replaced / deleted

- **`Product`** — dropped and recreated. `Name` carried over for all 975 rows; `Id`
  becomes a fresh `uuid4` (old integer id discarded); `Brand` / `Category` text columns
  replaced by `BrandId` / `CategoryId` FKs (`NULL` for the 119 brand-less and 34
  category-less rows).
- **`SellerProduct`** — dropped and recreated: `Id` removed, `SellerName` → `Seller`,
  `SellerProductId INTEGER` → `ExternalSku TEXT`, `ProductId` becomes a UUID FK; new
  composite PK and `UNIQUE (SellerId, ExternalSku)`.
- **`Brand`**, **`Category`**, **`Seller`** — created with UUID PKs.

### Migrations (staged tables, then swapped in)

1. **`Product.Brand` → `Brand`**: `(uuid4(), name)` per distinct normalized non-empty
   brand (**637**); keep the brand map.
2. **`Product.Category` → `Category`**: same (**43**); keep the category map.
3. **`Product` rebuild**: fresh `uuid4` per row, `Name` carried, `BrandId` / `CategoryId`
   from the maps; keep `{old_int_id: new_uuid}`.
4. **`SellerProduct.SellerName` → `Seller`**: `(uuid4(), name)` per distinct name (base
   table empty → 0 rows).
5. **`SellerProduct` rebuild**: rows remapped through the seller and product maps,
   `SellerProductId` → `ExternalSku` as text (base table empty → 0 rows).
6. `foreign_key_check` passes; `PRAGMA user_version = 1`.

`Brand` and `Category` are **reference tables** (a product has 0..1 of each → nullable
FK), not junctions. A `BrandProduct` / `CategoryProduct` junction was rejected — it
would allow a product to have two brands or two categories.

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

- **Whitespace**: 60 entries contain a double space (`"Smartphone  Galaxy S23"`).
- **Accents**: `"Câmera Canon EOS R6"` vs `"Camera Canon EOS R6"`.
- **Inch marks**: `12.9"`, `12.9''`, `12.9`; `55"`, `55` — same product.
- **Brand spelling**: catalog `BLACK+DECKER` vs `Black+Decker`; feed `"Levi's"` vs
  catalog `"Levis"`. Normalizing the brand before comparison (and before building the
  `Brand` table) resolves both.
- **Category disagreement**: `Camera Canon EOS R6` appears with `Photography` (catalog)
  and `Photo` (feed) — category cannot be part of identity. The two become distinct
  `Category` rows; `Photo` is never written because the entry links to the existing
  product and existing products are not enriched.
- **SQL injection probe**: one entry has `Brand = "TestBrand'; SELECT 1; --"`,
  `Name = "Security Test Product"`, `SellerName = "MegaStore"`, and one of the malformed
  `Id`s. `libinjection` flags the brand; the entry is rejected and counted as `threat`.
  `libinjection` does **not** flag `"Levi's"` or `12.9''`.

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
