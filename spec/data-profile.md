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

- `Product`: **975 rows**. `Category` populated for every row. `Name` is **unique after
  normalization** (zero collisions) — an exact normalized-name lookup returns at most
  one product.
  - `Brand`: **119 rows** have no brand. **639** distinct raw brand strings; **637**
    after normalization. The 2 that merge: `BLACK+DECKER` / `Black+Decker` and
    `Simplehuman` / `simplehuman`.
- `SellerProduct`: **0 rows**. No index, no uniqueness constraint.

### Why this model is compromised

- `SellerName` is denormalized free text — the seller is an entity, not a string
  repeated per row.
- `Product.Brand` is denormalized the same way, and already inconsistent in the catalog.
- `SellerProductId` is typed `INTEGER`, but the feed's identifier is a UUID string
  (some not even valid UUIDs), reused across sellers, so it cannot be a key.
- The surrogate `Id` on a link table is pointless; the natural key is
  `(SellerId, ProductId)`.
- Nothing enforces uniqueness of a seller/product pair.

## Refactored database

The published `catalog.db` reports `PRAGMA user_version = 0` and has the legacy tables,
so every normal run migrates it. The migration is conditional (guarded by
`user_version`): a database already at `user_version = 1` with the target tables skips
it, which keeps incremental re-runs against a previous output possible.

Rebuilt via SQLAlchemy Core (declarative `Table` definitions + `insert()` statements;
no ORM, no Alembic), inside the import transaction.

```sql
CREATE TABLE Brand (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE            -- normalized brand string
);

CREATE TABLE Product (
    Id       INTEGER PRIMARY KEY AUTOINCREMENT,
    Name     TEXT NOT NULL,
    BrandId  INTEGER REFERENCES Brand (Id),
    Category TEXT
);

CREATE TABLE Seller (
    Id   INTEGER PRIMARY KEY AUTOINCREMENT,
    Name TEXT NOT NULL UNIQUE
);

CREATE TABLE SellerProduct (
    SellerId    INTEGER NOT NULL REFERENCES Seller (Id),
    ProductId   INTEGER NOT NULL REFERENCES Product (Id),
    ExternalSku TEXT NOT NULL,           -- the seller's product id (feed entry Id)
    PRIMARY KEY (SellerId, ProductId),
    UNIQUE (SellerId, ExternalSku)
);
```

What the refactor does:

1. Extract `Brand`: distinct non-empty `Product.Brand`, normalized and deduped → **637 rows**.
2. Rebuild `Product` preserving `Id`, `Name`, `Category`; set `BrandId` from the
   normalized brand lookup (`NULL` for the 119 brand-less rows). Raw brand spelling is
   not preserved.
3. Extract `Seller`: distinct `SellerProduct.SellerName` (base table empty → 0 rows
   created here; sellers are created later from the feed).
4. Rebuild `SellerProduct` as the link table, carrying `SellerProductId` into
   `ExternalSku` as text (base table empty → no rows).
5. `foreign_key_check` passes.

`Brand` is a **reference table** (a product has 0..1 brands → nullable FK), not a
junction. A `BrandProduct` junction was rejected — it would allow a product to have two
brands.

## Seller feed (`ProductEntry.json`)

- **269 entries**. Every entry has all five keys: `Id`, `SellerName`, `Name`, `Brand`,
  `Category`.
- `Brand` is `null` in 3 entries (`Cable Organizer Kit`, `Bed Frame Wood King`,
  `Round Rug 6 Feet`). `Category` always present. `Name`, `SellerName` never empty.
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
- **Category disagreement**: `Camera Canon EOS R6` appears with `Photography` and
  `Photo` — category cannot be part of identity.
- **SQL injection probe**: one entry has `Brand = "TestBrand'; SELECT 1; --"`,
  `Name = "Security Test Product"`, `SellerName = "MegaStore"`, and one of the malformed
  `Id`s. `libinjection` flags the brand; the entry is rejected and counted as `threat`.
  `libinjection` does **not** flag `"Levi's"` or `12.9''`.

## Resulting row counts

| Table | Rows | Note |
| --- | --- | --- |
| `Brand` | **637** | distinct normalized catalog brands; no new brands (the one new-product candidate is a threat) |
| `Product` | **975** | unchanged count; `Brand` replaced by `BrandId`; 119 rows have `BrandId IS NULL` |
| `Seller` | **20** | distinct feed seller names |
| `SellerProduct` | **256** | distinct `(SellerId, ProductId)`; 12 feed entries collapse onto an existing pair (11 log `duplicate_listing`), 1 entry is a threat |

Local SQLite build note: `spellfix1` is unavailable; FTS5 and the trigram tokenizer are
present. Indexed candidate reduction is out of scope regardless.
