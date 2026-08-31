# Arquitetura — DDD simplificado

Versão educacional para demonstração. O objetivo é mostrar *linguagem ubíqua*,
*camadas*, *use cases* e *repositories* — não é uma arquitetura de produção.

## Camadas e arquivos

| Arquivo | Camada DDD | Responsabilidade |
| --- | --- | --- |
| `src/consolidation/domain.py` | **Domínio** | Value objects (`normalize`, `new_uuid`, `brands_compatible`), entidades ricas (`Product`, `Catalog`), contrato `Submission`. Não importa nada do projeto nem bibliotecas de I/O. |
| `src/consolidation/services.py` | **Domain services / portas** | `ProductIdentityResolver` (regra de identidade do produto) e a porta `Similarity`. |
| `src/consolidation/repository.py` | **Repositories** | Único código que lê/escreve o banco: `BrandRepository`, `CategoryRepository`, `SellerRepository`, `ProductRepository`, `SellerListingRepository`. |
| `src/consolidation/usecase.py` | **Aplicação (use cases)** | `ConsolidateEntryUseCase` (uma submissão do feed) e `run()` (a execução ponta a ponta). Orquestra domínio + repositories. |
| `src/consolidation/infrastructure.py` | **Infraestrutura** | Adapters: download HTTP, feed em streaming + ACL (`ProductEntry`), screen de SQL injection, backends `DifflibSimilarity`/`RapidFuzzSimilarity`, wiring do Alembic. |
| `src/consolidation/schema.py` | **Persistência** | Metadados das tabelas (SQLAlchemy Core) e os passos da migração one-way. |
| `src/consolidation/cli.py` | **Interface / composition root** | `argparse` + `.env`, monta a configuração e chama `usecase.run`. |

## Regra de dependência

```
cli ─▶ usecase ─▶ services ─▶ domain
          │           └──────▶ domain
          ├─▶ repository ─▶ schema ─▶ domain
          └─▶ infrastructure ─▶ services, domain

domain.py  → não importa nada do projeto
```

## Use cases

### `ConsolidateEntryUseCase.process(submission, record_index, report)`

Pipeline por entrada do feed (política pura; toda persistência via repository):

1. `screen_entry` (infra) — SQL injection → `threat`, encerra.
2. `ProductIdentityResolver.resolve` (domain service) — nome exato → multiset de
   palavras → fuzzy com gate → `Resolution(match | skip | novo)`.
3. `skip` → registra motivo no `Report` e encerra.
4. Sem produto → cria via `ProductRepository.add` (+ `BrandRepository.get_or_create`,
   membership de categoria) **ou** reaproveita o produto já ligado ao `ExternalSku`.
5. Categoria → `CategoryRepository.get_or_create` + `ProductRepository.add_category_membership`;
   `Product.record_category` sinaliza divergência (log).
6. Vínculo → `SellerRepository.get_or_create` + `SellerListingRepository.link`
   (idempotente; respeita `UNIQUE (SellerId, ExternalSku)`).

### `run(...)`

Download → `verify_sqlite_header` → `classify_source` → `alembic upgrade head`
(refactor do schema) → habilita FKs → stream do feed, **uma transação por entrada**,
falha isolada e reportada → publicação atômica do output.

## Injeção de dependência do matcher

- **Porta:** `services.Similarity` (contrato do domínio).
- **Adapters:** `infrastructure.DifflibSimilarity`, `infrastructure.RapidFuzzSimilarity`
  (`rapidfuzz` importado só dentro de `.score`).
- **Fábrica:** `infrastructure.build_similarity(name)`.
- **Composition root:** `usecase.run` escolhe pelo nome e injeta a instância em
  `ConsolidateEntryUseCase` → `ProductIdentityResolver`.
- Nos testes o backend é injetado direto no construtor, sem passar pela fábrica.

## De / para (código anterior → DDD)

| Antes | Agora |
| --- | --- |
| `util.normalize`, `db_upgrade.new_uuid` | `domain` |
| `util.download_to`, `verify_sqlite_header` | `infrastructure` |
| `importer.CatalogProduct` / `CatalogIndex` | `domain.Product` / `domain.Catalog` (com comportamento) |
| `importer.resolve_product` | `services.resolve_product` + `services.ProductIdentityResolver` |
| `importer.FeedImporter` (SQL + orquestração) | `usecase.ConsolidateEntryUseCase` + `repository.*` |
| `feed.ProductEntry` / `iter_feed` / `screen_entry` | `infrastructure` (ACL + source + screen) |
| `feed.Report` | `usecase.Report` (read model) |
| `similarity.py` | porta em `services`, backends + fábrica em `infrastructure` |
| `db_upgrade.py` | `schema.py` |
| `pipeline.py` | `usecase.run` + `cli` (composition root) |
