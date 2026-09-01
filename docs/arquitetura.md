# Arquitetura — DDD simplificado

Versão educacional para demonstração. O objetivo é mostrar *linguagem ubíqua*,
*camadas*, *use cases* e *repositories* — não é uma arquitetura de produção.

## Camadas e arquivos

| Arquivo | Camada DDD | Responsabilidade |
| --- | --- | --- |
| `src/consolidation/domain.py` | **Domínio** | Value objects (`normalize`, `new_uuid`, `brands_compatible`), entidades ricas (`Product`, `Catalog`), contrato `Submission`. Não importa nada do projeto nem bibliotecas de I/O. |
| `src/consolidation/services.py` | **Domain services / portas** | `resolve_product` (as regras de identidade: nome exato → multiset → fuzzy) e a fachada `ProductIdentityResolver` que embrulha `Similarity` + `threshold`; a porta `Similarity`; a porta `ByteSource` (transporte de bytes para o download e o feed). |
| `src/consolidation/repository.py` | **Repositories** | Único código que toca a conexão. `BrandRepository`, `CategoryRepository`, `SellerRepository`, `ProductRepository`, `SellerListingRepository`, e o pacote `CatalogRepositories` que os agrupa, lê o catálogo (`load_catalog`) e dona a transação por entrada (`entry_transaction()`) + o `reload()` pós-rollback. A conexão é privada (`_conn`). A porta `CatalogRepository` fica em `usecase.py`; a implementação SQLite (`SqliteCatalogRepository`) em `infrastructure.py`. |
| `src/consolidation/usecase.py` | **Aplicação (use cases)** | `PrepareCatalogDatabaseUseCase` (baixa + migra o banco), `ConsolidateFeedUseCase` (feed → dedupe + insert), `ConsolidateEntryUseCase` (uma submissão), o coordenador fino `ConsolidateCatalogUseCase`, o read model `Report`, e a **definição** da porta `CatalogRepository`. Recebe por injeção um `ProductIdentityResolver` pronto e essa porta. |
| `src/consolidation/infrastructure.py` | **Infraestrutura** | Adapters e **todo acesso ao banco fora dos repositories**: transportes `HttpByteSource`/`S3ByteSource` + `build_source` (`boto3` importado só dentro de `S3ByteSource.open`), `download_to` / `verify_sqlite_header`, feed em streaming (`iter_feed`) + ACL (`ProductEntry`), `screen_entry` (SQL injection), backends `DifflibSimilarity`/`RapidFuzzSimilarity` + `build_similarity`, wiring do Alembic (`alembic_config`, `MIGRATIONS_DIR`), os passos do refactor (`classify_source`, `create_staging_tables`, `rebuild_*`, `swap_tables`, `foreign_key_check`), e o adapter `SqliteCatalogRepository`. |
| `src/consolidation/schema.py` | **Schema declarativo** | Só os metadados das tabelas (SQLAlchemy Core). Nenhuma função recebe `Connection`. |
| `src/consolidation/cli.py` | **Interface / composition root** | `argparse` + `.env`; constrói o `ProductIdentityResolver` (backend + threshold), o `ByteSource` (`--source http\|s3`) e o `SqliteCatalogRepository`, e roda os use cases em ordem: `PrepareCatalogDatabaseUseCase` → `ConsolidateCatalogUseCase`. |

## Regra de dependência

```
cli ─▶ usecase ─▶ services ─▶ domain
   │       ├──▶ repository ──▶ schema + domain
   │       └──▶ infrastructure ──▶ repository + services + domain
   └──▶ services (monta ProductIdentityResolver + ByteSource) + infrastructure (adapters)

domain.py  → não importa nada do projeto, nenhuma lib de I/O
schema.py  → só SQLAlchemy Core, nenhuma função recebe Connection
conn crua (begin/commit/rollback/execute) → só em repository.py e no SqliteCatalogRepository
```

## Injeção de dependência dos use cases

- **Um único `repository`** (a porta `CatalogRepository`) é injetado em
  `PrepareCatalogDatabaseUseCase(repository, source)` e em
  `ConsolidateCatalogUseCase(repository, resolver, source)`. Ele prepara o banco
  (migração) **e** entrega o `CatalogRepositories` via
  `repository.catalog_repositories()`.
- **Um único `source`** (a porta `ByteSource` — `HttpByteSource` ou `S3ByteSource`)
  é injetado nos dois use cases e serve tanto `download_to` quanto `iter_feed`.
  Nenhum `requests`/`boto3` é threaded camada a camada.
- **Um único `resolver`** (`ProductIdentityResolver`, já com o `Similarity` e o
  `threshold` dentro) desce por injeção: `cli` → `ConsolidateCatalogUseCase` →
  `ConsolidateFeedUseCase` → `ConsolidateEntryUseCase`. Nenhum `similarity` nem
  `threshold` solto sendo passado camada a camada.
- `ConsolidateFeedUseCase(repositories, resolver).execute(feed)` — `repositories` é
  uma instância de `CatalogRepositories`.
- `ConsolidateEntryUseCase(repositories, resolver)` — lê o `Catalog` de trabalho
  via `repositories.load_catalog()`; nunca cria repository nem toca a conexão.
- Quem orquestra (roda `PrepareCatalogDatabaseUseCase` e passa o `Path` pronto para
  `ConsolidateCatalogUseCase.execute`) é o `cli`, não um use case.

## Use cases

### `ConsolidateEntryUseCase.process(submission, record_index, report)`

Pipeline por entrada do feed (política pura; toda persistência via repository):

1. `screen_entry` (infra) — SQL injection → `threat`, encerra.
2. `ProductIdentityResolver.resolve` (domain service) — nome exato → multiset de
   palavras → fuzzy com gate → `Resolution(match | skip | novo)`.
3. `skip` → registra motivo no `Report` e encerra.
4. Match fuzzy → log `event=approximate_match`. Sem produto → cria via
   `ProductRepository.add` (+ `BrandRepository.get_or_create`, membership de categoria)
   **ou** reaproveita o produto já ligado ao `ExternalSku` daquele seller.
5. Categoria → `CategoryRepository.get_or_create` + `ProductRepository.add_category_membership`;
   `Product.record_category` sinaliza divergência (log `event=category_divergence`).
6. Vínculo (`SellerListingRepository`) → `SellerRepository.get_or_create` do seller,
   depois: `(SellerId, ExternalSku)` já aponta para outro produto → `skip`; par
   `(seller, produto)` já existe → nada (SKU novo → log `event=duplicate_listing`);
   senão `INSERT OR IGNORE` do link e `linked += 1`.

### `PrepareCatalogDatabaseUseCase(repository, source).execute(catalog_url, dest_dir) -> Path`

Use case 1 — deixa o banco pronto para consolidar. O construtor recebe o
`CatalogRepository` (porta) e o `ByteSource`; `execute` recebe os parâmetros do
run. **Não conhece SQLite / SQLAlchemy / Alembic / requests / boto3**:

`download_to(catalog_url, dest_dir, source)` → `repository.verify_database(tmp)` → `repository.connect(tmp)` →
`repository.begin()` → `repository.classify_source()` (aborta se `unrecognized`) →
`repository.upgrade()` → `repository.commit()` → `repository.enable_foreign_keys()`
→ devolve o `Path`. **Em sucesso o repository fica conectado** (FKs ligadas) para o
use case seguinte consumir na mesma conexão; quem fecha é o coordenador. Em erro:
`repository.rollback()` + `repository.close()`, apaga o arquivo parcial, propaga.

`SqliteCatalogRepository` (infra) implementa a porta: `create_engine`, header
check do SQLite, `command.upgrade(alembic_config(conn), "head")`, o
`PRAGMA foreign_keys = ON`, a transação SQLAlchemy, e `catalog_repositories()` que
devolve `CatalogRepositories(conn)`. Trocar `url_template` (ou escrever outro
adapter) aponta para outro banco sem tocar no use case.

### `ConsolidateFeedUseCase(repositories, resolver).execute(feed) -> Report`

Use case 2 — construtor recebe o `CatalogRepositories` e o `ProductIdentityResolver`
prontos; `execute` recebe só o feed (iterável de `Submission`). Cria o
`ConsolidateEntryUseCase` e roda o loop dentro de `repositories.entry_transaction()`
(**uma transação por entrada**, commit/rollback dentro do repository); entrada que
falha é registrada em `report.failures`, `repositories.reload()` recarrega os caches,
um novo `ConsolidateEntryUseCase` é construído, e o loop segue. Devolve o `Report`.
**Nenhuma `conn` aparece aqui.**

### `ConsolidateCatalogUseCase(repository, resolver, source).execute(prepared_database, products_url, output) -> int`

Recebe o **mesmo** `repository` (ainda conectado, FKs ligadas), o `resolver`, o
`source` e o `Path` do banco preparado. `repository.catalog_repositories()` (não
cria engine) → `iter_feed(products_url, source)` → `ConsolidateFeedUseCase` →
`repository.close()` → publicação atômica em `output` só em sucesso; em falha apaga
o arquivo preparado. Devolve o exit code.

## Injeção de dependência: dois seams

### Matcher

- **Porta:** `services.Similarity` (contrato do domínio).
- **Adapters:** `infrastructure.DifflibSimilarity`, `infrastructure.RapidFuzzSimilarity`
  (`rapidfuzz` importado só dentro de `.score`).
- **Fábrica:** `infrastructure.build_similarity(name)`.

### Transporte de bytes

- **Porta:** `services.ByteSource` — `open(ref) -> context manager de stream`.
- **Adapters:** `infrastructure.HttpByteSource` (`requests`, comportamento
  original) e `infrastructure.S3ByteSource` (`boto3` `get_object` **anônimo** —
  `import boto3` só dentro de `.open`; objeto tem que ser público).
- **Fábrica:** `infrastructure.build_source(name)` (`http` | `s3`), escolhida por
  `--source`. `parse_s3_ref` aceita `s3://bucket/key` e URLs `…amazonaws.com`.
- **Motivo:** demonstrar o padrão adapter aplicado a I/O externo além do matcher.
  Os dois consumidores (`download_to`, `iter_feed`) passam a receber o `source`.
- **Composition root (`cli`):** `ProductIdentityResolver(build_similarity(matcher), threshold)`
  — o resolver já embrulha backend + threshold e é a **única** coisa de matching
  que desce pelos use cases. O use case nunca toca na fábrica.
- Nos testes o resolver é montado direto (`ProductIdentityResolver(DifflibSimilarity(), 0.90)`),
  sem passar pela fábrica — esse é o ganho do seam.

## De / para (código anterior → DDD)

| Antes | Agora |
| --- | --- |
| `util.normalize`, `db_upgrade.new_uuid` | `domain` |
| `util.download_to`, `verify_sqlite_header` | `infrastructure` |
| `db_upgrade.classify_source` / `rebuild_*` / `swap_tables` (tocam o banco) | `infrastructure` |
| `create_engine` / `verify_sqlite_header` / `alembic upgrade` no use case | porta `CatalogRepository` + `SqliteCatalogRepository` (infra) |
| `importer.CatalogProduct` / `CatalogIndex` | `domain.Product` / `domain.Catalog` (com comportamento) |
| `importer.resolve_product` | `services.resolve_product` + `services.ProductIdentityResolver` |
| `importer.FeedImporter` (SQL + orquestração) | `usecase.ConsolidateEntryUseCase` + `repository.CatalogRepositories` |
| uso solto de `conn.begin/commit/rollback` no loop do feed | `CatalogRepositories.entry_transaction()` / `reload()` (dentro do repository) |
| `similarity` + `threshold` passados camada a camada | um `ProductIdentityResolver` pronto, injetado do `cli` |
| `pipeline.run` (download+migra+consome) | `PrepareCatalogDatabaseUseCase` + `ConsolidateFeedUseCase`, sob `ConsolidateCatalogUseCase` |
| `feed.ProductEntry` / `iter_feed` / `screen_entry` | `infrastructure` (ACL + source + screen) |
| `requests` embutido em `iter_feed` / `download_to` | porta `ByteSource` em `services`, adapters `HttpByteSource`/`S3ByteSource` + `build_source` em `infrastructure` |
| `feed.Report` | `usecase.Report` (read model) |
| `similarity.py` | porta em `services`, backends + fábrica em `infrastructure` |
| `db_upgrade.py` | tabelas → `schema.py`; funções que executam SQL → `infrastructure.py` |
| `pipeline.py` | `usecase.ConsolidateCatalogUseCase` + `cli` (composition root) |
