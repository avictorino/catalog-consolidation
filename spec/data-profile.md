# Specification — data profile

Profile of the published inputs, taken while designing the identity model. Numbers are
a snapshot; the remote content may change.

## Base catalog (`catalog.db`)

Two tables, as given:

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

- `Product`: **975 rows**. `Brand` and `Category` are populated for every row in the
  sample. Names are **unique after normalization** — zero collisions — so an exact
  normalized-name lookup returns at most one product.
- `SellerProduct`: **0 rows**. No index, no uniqueness constraint. Because it is empty,
  there are no pre-existing links to preserve.
- `SellerProductId` is declared `INTEGER`, but the feed's `Id` values are UUID strings.
  This is the schema change the challenge hints at: the column must become `TEXT`.

## Seller feed (`ProductEntry.json`)

- **269 entries**. Every entry has all five keys: `Id`, `SellerName`, `Name`, `Brand`,
  `Category`.
- `Brand` is `null` in 3 entries (`Cable Organizer Kit`, `Bed Frame Wood King`,
  `Round Rug 6 Feet`). `Category` is always present. `Name` and `SellerName` are never
  empty.
- `Id`: 255 distinct of 269.
  - **3 are not valid UUIDs**: `ddddeee-ffff-4000-1111-222233334444` (7-char group),
    `09835342345-4678-9abc-def012345678` (4 groups, oversized first),
    `uddd0000-eeee-4111-ffff-aaaa22223333` (`u` is not hex).
  - **14 `Id` strings are reused** across different products / sellers — so `Id` alone
    cannot identify a product; it is at best a seller-scoped SKU.
- **1 `(SellerName, Id)` pair is repeated**: `GardenStore` with the same `Id`, once as
  `"Câmera Canon EOS R6"` and once as `"Camera Canon EOS R6"`. This is why 269 entries
  become 268 links.

## Match outcomes with normalization only

Normalization = accent fold + lowercase + punctuation to space + whitespace collapse.

| Outcome | Count |
| --- | --- |
| Resolves to exactly one catalog product | 266 |
| Normalized name matches multiple catalog rows (ambiguous) | 0 |
| No normalized-name match (new-product candidate) | 3 |

The 3 without an exact match:

| Feed name | Nearest catalog name | `difflib.ratio` | Decision |
| --- | --- | --- | --- |
| `Roteador WiFi 6 TP-Link` | `Router WiFi 6 TP-Link` (id 21) | **0.909** | fuzzy match (passes the 0.90 gate) |
| `Processador AMD Ryzen 9 7950X` | `Processor AMD Ryzen 9 7950X` (id 28) | **0.964** | fuzzy match |
| `Security Test Product` | `Security Camera Nest` | 0.585 | genuinely new product |

`rapidfuzz.fuzz.ratio` uses the same `2M/T` formula as `difflib.ratio` and also yields
`0.909` for `Roteador/Router`, so the shared `0.90` threshold holds for both backends.

## Field-level ambiguities observed

- **Whitespace**: 60 entries contain a double space (`"Smartphone  Galaxy S23"`).
- **Accents**: `"Câmera Canon EOS R6"` vs `"Camera Canon EOS R6"`.
- **Inch marks**: `12.9"`, `12.9''`, and `12.9`; `55"` and `55` — all for the same product.
- **Brand punctuation**: feed `"Levi's"` vs catalog `"Levis"` for `Belt Leather
  Reversible` — a false conflict unless the brand is normalized before comparison.
- **Category disagreement**: `Camera Canon EOS R6` appears with `Category` `Photography`
  and `Photo` — same product, so category cannot be part of identity.
- **SQL injection probe**: one entry has `Brand = "TestBrand'; SELECT 1; --"`,
  `Name = "Security Test Product"`. It is a legitimate new product; the point is that
  parameterized SQL must be used everywhere.

## FTS5 probe (why it is not the shipped matcher)

Tested in an in-memory FTS5 table over the catalog names:

| Query | Result |
| --- | --- |
| `Running Shoes  Nike Air Zoom` | finds the single-space name |
| `Câmera Canon EOS R6` | finds `Camera Canon EOS R6` |
| phrase `Roteador WiFi 6 TP-Link` | does **not** find `Router WiFi 6 TP-Link` |
| terms `WiFi`, `6`, `TP-Link` | finds the router |
| phrase `iPhone 15 Pro` | also matches `iPhone 15 Pro Max` |

So the top FTS5 hit cannot be accepted blindly, and `bm25()` is not comparable to the
`0.90` similarity cutoff. FTS5 belongs to candidate retrieval, not scoring, and is left
as documented future work.

`spellfix1` (`editdist3`) is not available in the local SQLite build; FTS5 and the
trigram tokenizer are.
