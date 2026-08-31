# PRD — Catalog consolidation

## Context

A traditional e-commerce company is becoming a marketplace. It already has a product
catalog; now it must ingest product feeds from many sellers. The same product is often
sold by several sellers, each registering it slightly differently. Duplicating products
is undesirable, but every seller offering a product must be recorded.

Input is a SQLite `catalog.db` with two tables and a JSON file of seller product
entries. For a duplicate product the system must **not** insert into the product table;
it must link the existing product to the seller in the link table.

The challenge explicitly values demonstrated mastery of the problem and its
ambiguities over a scalable, production-ready implementation, and it permits changing
the database.

## Data (profiled from the published sources)

- `Product`: 975 rows. `Id INTEGER PRIMARY KEY AUTOINCREMENT`, `Name NOT NULL`,
  `Brand` and `Category` nullable. Names are unique after normalization (no collisions).
- `SellerProduct`: **empty**. `SellerName`, `ProductId` (FK → `Product.Id`),
  `SellerProductId INTEGER NOT NULL`. No index, no uniqueness constraint.
- `ProductEntry.json`: 269 entries, all five fields always present. `Brand` is null in 3.
  `Id` is a text UUID (3 are malformed), reused across sellers — it is not a product
  identity. One `(SellerName, Id)` pair is repeated.
- Expected result: **976 products** (one new: `Security Test Product`) and
  **268 links** (269 entries minus 1 duplicated pair). Portuguese→English name
  variants resolve as matches, not as new products.

See [`spec/data-profile.md`](spec/data-profile.md) for the full profile.

## Identity model

Product identity = **normalized `Name`**. `Brand` (normalized) is only a tie-break
gate. `Category` never participates. `Id` never participates (it is a seller SKU).

Per-entry pipeline:

```
normalize (shared)
  -> exact lookup by normalized name (shared)
  -> CandidateSource (injected; default: full scan)
  -> Similarity.score (injected; difflib | rapidfuzz)
  -> identity policy + threshold (shared)
  -> outcome: link | insert + link | skip and report
```

Normalization (one function, applied to catalog names, feed names, and brands):
lowercase, strip accents (NFKD), collapse whitespace, remove punctuation and quote
marks, keep digits. SQLite `lower()` is ASCII-only, so normalization is done in Python.

Fuzzy gate (only when the exact lookup misses): brands equal after normalization when
both are present, same word count, same numeric tokens, `score >= backend threshold`.

Outcomes: exactly 1 candidate -> link; 0 candidates -> new product + link;
2+ candidates or a brand conflict -> **skip the row and report it at the end**
(do not abort the whole import).

## Ambiguity register (every class occurs in the real data)

| Class | Example | Decision |
| --- | --- | --- |
| Double space | `"Smartphone  Galaxy S23"` | normalization resolves |
| Accents | `"Câmera Canon EOS R6"` vs `"Camera Canon EOS R6"` | strip accents |
| Inch marks | `12.9"` / `12.9''` / `12.9` ; `55"` / `55` | remove punctuation |
| Brand apostrophe | feed `"Levi's"` vs catalog `"Levis"` | normalize brand before comparing |
| Category disagreement | feed `Photo` vs catalog `Photography` | category is not identity; link and log |
| PT<->EN translation | `"Roteador WiFi 6 TP-Link"` <-> `"Router WiFi 6 TP-Link"` (difflib 0.909) | fuzzy resolves; fragile at the 0.90 cutoff — documented |
| SQL injection probe | `Brand = "TestBrand'; SELECT 1; --"` | parameterized SQL; legitimate new product |
| Invalid / reused `Id` | 3 non-UUID, 14 reused across sellers | `Id` is an opaque, seller-scoped string |
| Repeated `(seller, Id)` | `GardenStore` + Câmera/Camera | a single link |
| Null `Brand` | `"Cable Organizer Kit"`, `"Bed Frame Wood King"`, `"Round Rug 6 Feet"` | absent brand does not block a name match |

## Matcher layer (parameter injection)

Two injection points, constructed at the CLI edge and passed as keyword arguments.
No DI container, no framework.

### Similarity — pure scorer

```python
from typing import Protocol

class Similarity(Protocol):
    name: str
    suggested_threshold: float
    def score(self, a: str, b: str) -> float: ...   # a, b already normalized -> [0, 1]
```

- `DifflibSimilarity` — `SequenceMatcher(None, a, b, autojunk=False).ratio()`, stdlib.
- `RapidFuzzSimilarity` — `fuzz.ratio(a, b) / 100.0`, external.
- `fuzz.WRatio` / `token_set_ratio` / `token_sort_ratio` are disallowed — they hide
  extra terms that matter for identity (capacity, model).
- `fuzz.ratio` and `difflib.ratio` are both `2M/T`; on this data both give `0.909`
  for `Roteador/Router`, so the same `0.90` threshold works. Not guaranteed in
  general, hence each backend carries its own `suggested_threshold`.

Factory with lazy imports (the `difflib` path never imports `rapidfuzz`):

```python
def build_similarity(name: str) -> Similarity:
    if name == "difflib":
        from .difflib_impl import DifflibSimilarity
        return DifflibSimilarity()
    if name == "rapidfuzz":
        from .rapidfuzz_impl import RapidFuzzSimilarity
        return RapidFuzzSimilarity()
    raise SystemExit(f"unknown matcher: {name!r} (options: difflib, rapidfuzz)")
```

### CandidateSource — retrieval, where SQLite could plug in

SQLite cannot implement `score()` without an extension: `editdist3()` needs
`spellfix1` (not installed), and FTS5 returns a `bm25()` rank, not a `[0, 1]` score.
SQLite's place is candidate retrieval — a separate seam, orthogonal to the scorer:

```python
class CandidateSource(Protocol):
    def candidates(self, normalized_name: str) -> Iterable[ProductRow]: ...
```

- `FullScanCandidates(conn)` — default and the only shipped implementation; fine for
  975 rows.
- `FtsCandidates(conn)` — future work. Documented in the README with the FTS5 probe:
  the phrase `Roteador WiFi 6 TP-Link` does not retrieve `Router ...`; `iPhone 15 Pro`
  also retrieves `Pro Max`; `bm25()` is not comparable to the similarity cutoff.

## Schema changes (applied to the downloaded DB on every run)

- Rebuild `SellerProduct` with `SellerProductId TEXT NOT NULL`.
- Add `UNIQUE(SellerName, SellerProductId)`.
- `PRAGMA foreign_keys = ON`.
- `Product` keeps its original schema; the normalized-name index is built in memory at
  load time (documented trade-off versus a persisted column).

## Execution flow

1. Resolve and validate configuration (CLI plus optional `.env` next to the entry point).
2. Download `catalog.db` in chunks to a temp file in the output directory (fresh every run).
3. Verify the SQLite header and the expected schema.
4. Open the connection, `foreign_keys = ON`, apply schema migrations, begin **one** transaction.
5. Stream `ProductEntry.json` with `requests` + `ijson`; validate each object with
   Pydantic (`model_validate`, one at a time).
6. For each entry, run the identity pipeline; accumulate counters and the skipped list.
7. Consume the entire document before committing.
8. Commit -> close the connection -> atomically replace the output with the temp file.

Any failure (network, JSON, validation, database) -> rollback, discard the temp file,
previous output preserved. No partial resume.

## Out of scope for the first iteration

FTS5 candidate source, persisted normalized columns, global product identity,
labeled-data evaluation, processing resume.
