# Arquitetura — DDD simplificado

Versão educacional para demonstração. O objetivo é mostrar *linguagem ubíqua*,
*camadas*, *use cases* e *repositories* — não é uma arquitetura de produção.

## Camadas e arquivos

| Arquivo | Camada DDD | Responsabilidade |
| --- | --- | --- |
| `src/consolidation/domain.py` | **Domínio** | Value objects (`normalize`, `new_uuid`, `brands_compatible`), entidades ricas (`Product`, `Catalog`), contrato `Submission`. Não importa nada do projeto nem bibliotecas de I/O. |
| `src/consolidation/services.py` | **Domain services / portas** | `ProductIdentityResolver` (regra de identidade do produto) e a porta `Similarity`. |
| `src/consolidation/repository.py` | **Repositories** | Acesso ao banco orientado a agregado para os use cases: `BrandRepository`, `CategoryRepository`, `SellerRepository`, `ProductRepository`, `SellerListingRepository`, e o pacote `CatalogRepositories` que instancia os cinco contra uma conexão. |
| `src/consolidation/usecase.py` | **Aplicação (use cases)** | `ConsolidateEntryUseCase` (uma submissão do feed) e `ConsolidateCatalogUseCase` (a execução ponta a ponta). Recebe `Similarity` e o factory de `CatalogRepositories` por injeção. |
| `src/consolidation/infrastructure.py` | **Infraestrutura** | Adapters e **todo acesso ao banco fora dos repositories**: download HTTP, feed em streaming + ACL (`ProductEntry`), screen de SQL injection, backends `DifflibSimilarity`/`RapidFuzzSimilarity`, wiring do Alembic, e os passos do refactor de schema (`classify_source`, `create_staging_tables`, `rebuild_*`, `swap_tables`, `foreign_key_check`). |
| `src/consolidation/schema.py` | **Schema declarativo** | Só os metadados das tabelas (SQLAlchemy Core). Nenhuma função recebe `Connection`. |
| `src/consolidation/cli.py` | **Interface / composition root** | `argparse` + `.env`, monta a configuração, constrói o `Similarity` e chama `ConsolidateCatalogUseCase(...).execute()`. |

## Regra de dependência

```
cli ─▶ usecase ─▶ services ─▶ domain
          ├─▶ repository ─▶ schema (metadados) + domain
          └─▶ infrastructure ─▶ services + domain

domain.py  → não importa nada do projeto
schema.py  → só SQLAlchemy Core, nenhuma Connection
infrastructure → executa SQL cru (migração); repository → acesso por agregado
```

## Injeção de dependência dos use cases

- `ConsolidateCatalogUseCase` recebe **dois colaboradores por parâmetro de construtor**:
  - `similarity` — instância pronta de `Similarity` (ver seção do matcher);
  - `repositories` — factory `(Connection) -> CatalogRepositories`; ao ser chamada
    entrega o pacote com os cinco repositories já instanciados contra a conexão do run.
    Default é o real; um teste passa um fake.
- `ConsolidateEntryUseCase` recebe o `CatalogRepositories` já montado (nunca cria
  repository) mais o `Catalog` de trabalho.

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

### `ConsolidateCatalogUseCase.execute()`

Download → `verify_sqlite_header` → `classify_source` → `alembic upgrade head`
(refactor do schema) → habilita FKs → stream do feed, **uma transação por entrada**,
falha isolada e reportada → publicação atômica do output.

## Injeção de dependência do matcher

- **Porta:** `services.Similarity` (contrato do domínio).
- **Adapters:** `infrastructure.DifflibSimilarity`, `infrastructure.RapidFuzzSimilarity`
  (`rapidfuzz` importado só dentro de `.score`).
- **Fábrica:** `infrastructure.build_similarity(name)`.
- **Composition root (`cli`):** chama `build_similarity` e passa a **instância pronta**
  como parâmetro do construtor de `ConsolidateCatalogUseCase`, que a repassa para
  `ConsolidateEntryUseCase` → `ProductIdentityResolver`. O use case nunca toca na fábrica.
- Nos testes o backend é injetado direto no construtor (`similarity=DifflibSimilarity()`),
  sem passar pela fábrica — esse é o ganho do seam.

## De / para (código anterior → DDD)

| Antes | Agora |
| --- | --- |
| `util.normalize`, `db_upgrade.new_uuid` | `domain` |
| `util.download_to`, `verify_sqlite_header` | `infrastructure` |
| `db_upgrade.classify_source` / `rebuild_*` / `swap_tables` (tocam o banco) | `infrastructure` |
| `importer.CatalogProduct` / `CatalogIndex` | `domain.Product` / `domain.Catalog` (com comportamento) |
| `importer.resolve_product` | `services.resolve_product` + `services.ProductIdentityResolver` |
| `importer.FeedImporter` (SQL + orquestração) | `usecase.ConsolidateEntryUseCase` + `repository.CatalogRepositories` |
| `feed.ProductEntry` / `iter_feed` / `screen_entry` | `infrastructure` (ACL + source + screen) |
| `feed.Report` | `usecase.Report` (read model) |
| `similarity.py` | porta em `services`, backends + fábrica em `infrastructure` |
| `db_upgrade.py` | tabelas → `schema.py`; funções que executam SQL → `infrastructure.py` |
| `pipeline.py` | `usecase.ConsolidateCatalogUseCase` + `cli` (composition root) |
