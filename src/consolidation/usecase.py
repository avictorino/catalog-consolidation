"""Application layer — use cases that orchestrate the domain and the repositories.

Three use cases, smallest to largest:

* :class:`ConsolidateEntryUseCase` — apply one validated seller submission
  (screen, resolve identity, create-or-link, record the outcome). Pure policy;
  all persistence goes through the injected repository bundle.
* :class:`PrepareCatalogDatabaseUseCase` — download ``catalog.db``, classify it,
  run the schema migration; hand back a database file *ready to be consolidated*.
* :class:`ConsolidateFeedUseCase` — given a ``CatalogRepositories`` bundle (which
  carries the live connection to a prepared database) and the seller feed, resolve
  every listing (exact / word-order / fuzzy) and persist the links, creating
  products only when genuinely new. Returns a Report.

:class:`ConsolidateCatalogUseCase` ties the feed consumption to atomic publishing;
the composition root (:mod:`consolidation.cli`) runs
:class:`PrepareCatalogDatabaseUseCase` first and passes it the prepared file.

Injected collaborators (a ready ``ProductIdentityResolver``, the
``CatalogRepository`` port) are passed in, never built by the use cases.

Depends on: :mod:`consolidation.domain`, :mod:`consolidation.services`,
:mod:`consolidation.repository`, :mod:`consolidation.infrastructure`.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from consolidation.domain import Product, Submission, new_uuid, normalize
from consolidation.infrastructure import (
    FeedValidationError,
    download_to,
    iter_feed,
    screen_entry,
)
from consolidation.repository import CatalogRepositories
from consolidation.services import ByteSource, ProductIdentityResolver

logger = logging.getLogger("consolidation")


class CatalogRepository(Protocol):
    """Port: the one repository a run needs — prepare the catalog database, then
    hand out per-aggregate access to it.

    Implemented per database engine in :mod:`consolidation.infrastructure`
    (``SqliteCatalogRepository``). The use cases depend only on this contract —
    they never import SQLAlchemy or Alembic and do not know the file is SQLite.
    """

    # -- preparation (migration) --------------------------------------- #
    def verify_database(self, path: Path) -> None:
        """Raise if the file at ``path`` is not a valid database of this kind."""

    def connect(self, path: Path) -> None:
        """Open a connection to the database at ``path``."""

    def classify_source(self) -> str:
        """``'legacy'`` | ``'migrated'`` | ``'unrecognized'``."""

    def begin(self) -> None:
        """Start the migration transaction."""

    def upgrade(self) -> None:
        """Run the schema migration to head."""

    def commit(self) -> None: ...

    def rollback(self) -> None: ...

    def enable_foreign_keys(self) -> None:
        """Turn on and verify referential-integrity enforcement for the run."""

    # -- consumption --------------------------------------------------- #
    def catalog_repositories(self) -> CatalogRepositories:
        """A ``CatalogRepositories`` bundle bound to the live connection."""

    def close(self) -> None:
        """Release the connection and any engine-level resources."""


@dataclass
class Report:
    """Counters and review details accumulated during one import (a read model)."""

    processed: int = 0
    new: int = 0
    linked: int = 0
    skipped: int = 0
    threat: int = 0
    failed: int = 0
    skipped_entries: list[dict[str, object]] = field(default_factory=list)
    threats: list[dict[str, object]] = field(default_factory=list)
    failures: list[dict[str, object]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Use case: consolidate one seller submission.
# --------------------------------------------------------------------------- #
class ConsolidateEntryUseCase:
    """Apply one validated feed entry inside the caller's transaction.

    Receives the :class:`~consolidation.repository.CatalogRepositories` bundle
    (which reads the working :class:`~consolidation.domain.Catalog` for us) and a
    ready :class:`~consolidation.services.ProductIdentityResolver`.
    """

    def __init__(
        self,
        repositories: CatalogRepositories,
        resolver: ProductIdentityResolver,
    ) -> None:
        self.repositories = repositories
        self.catalog = repositories.load_catalog()
        self.resolver = resolver

    # -- steps ------------------------------------------------------------- #
    def _create_product(self, submission: Submission) -> Product:
        brand_id = self.repositories.brands.get_or_create(submission.Brand)
        product = Product(new_uuid(), submission.Name, submission.Brand)
        self.repositories.products.add(product, brand_id)
        self._attach_category(product, submission.Category)
        self.catalog.add(product)
        return product

    def _attach_category(self, product: Product, raw_category: str | None) -> bool:
        """Persist the membership and update the aggregate. Returns divergence."""
        category_id = self.repositories.categories.get_or_create(raw_category)
        if not category_id or not normalize(raw_category):
            return False
        self.repositories.products.add_category_membership(product.id, category_id)
        return product.record_category(raw_category)

    def _link(
        self,
        seller_id: str,
        product: Product,
        submission: Submission,
        record_index: int,
        report: Report,
    ) -> None:
        listings = self.repositories.listings
        bound_product = listings.product_for_sku(seller_id, submission.Id)
        if bound_product and bound_product != product.id:
            report.skipped += 1
            report.skipped_entries.append(
                {"record_index": record_index, "reason": "external SKU conflict"}
            )
            logger.warning("event=skip record=%d reason=external_sku_conflict", record_index)
            return

        existing_sku = listings.sku_for_pair(seller_id, product.id)
        if existing_sku is not None:
            if existing_sku != submission.Id:
                logger.info(
                    "event=duplicate_listing record=%d seller=%s product=%s",
                    record_index,
                    submission.SellerName,
                    product.name,
                )
            return

        if listings.link(seller_id, product.id, submission.Id):
            report.linked += 1

    # -- entry point ----------------------------------------------------- #
    def process(self, submission: Submission, record_index: int, report: Report) -> None:
        finding = screen_entry(submission, record_index)
        if finding is not None:
            report.threat += 1
            report.threats.append(
                {
                    "record_index": record_index,
                    "field": finding.field,
                    "fingerprint": finding.fingerprint,
                    "value": finding.value,
                }
            )
            return

        resolution = self.resolver.resolve(self.catalog, submission)
        if resolution.is_skip:
            report.skipped += 1
            report.skipped_entries.append(
                {"record_index": record_index, "reason": resolution.reason}
            )
            logger.warning(
                "event=skip record=%d reason=%s",
                record_index,
                resolution.reason.replace(" ", "_"),
            )
            return

        product = resolution.product
        seller_id = self.repositories.sellers.id_for(submission.SellerName)

        if product is None:
            bound_product = (
                self.repositories.listings.product_for_sku(seller_id, submission.Id)
                if seller_id
                else None
            )
            if bound_product:
                product = next((p for p in self.catalog.products if p.id == bound_product), None)
                if product is None:
                    report.skipped += 1
                    report.skipped_entries.append(
                        {"record_index": record_index, "reason": "external SKU conflict"}
                    )
                    logger.warning(
                        "event=skip record=%d reason=external_sku_conflict", record_index
                    )
                    return
            else:
                product = self._create_product(submission)
                report.new += 1
        elif resolution.score is not None:
            logger.warning(
                "event=approximate_match record=%d seller=%s product=%s score=%.3f",
                record_index,
                submission.SellerName,
                product.name,
                resolution.score,
            )

        incoming_category = normalize(submission.Category)
        diverges = (
            bool(product.categories)
            and bool(incoming_category)
            and not product.has_category(incoming_category)
        )
        self._attach_category(product, submission.Category)
        if diverges:
            logger.warning(
                "event=category_divergence record=%d seller=%s product=%s",
                record_index,
                submission.SellerName,
                product.name,
            )

        seller_id = seller_id or self.repositories.sellers.get_or_create(submission.SellerName)
        self._link(seller_id, product, submission, record_index, report)


# --------------------------------------------------------------------------- #
# Use case 1: prepare the database.
# --------------------------------------------------------------------------- #
class PrepareCatalogDatabaseUseCase:
    """Download ``catalog.db``, classify the source, run the one-way schema
    migration, and turn on integrity enforcement. Returns the path to a database
    file ready to be consolidated.

    Every database-specific step goes through the injected
    :class:`CatalogRepository`; this use case does not know it is SQLite. The
    catalog is always downloaded over plain HTTP(S) (`requests`) — the swappable
    transport is the seller feed's, not this. On success the repository is left
    **connected** (with foreign keys enabled) so the next use case consumes on the
    same connection — the caller owns closing it. On any failure the repository is
    closed and the partial file deleted.
    """

    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository

    def execute(self, catalog_url: str, dest_dir: str | Path) -> Path:
        tmp = download_to(catalog_url, dest_dir)
        try:
            self.repository.verify_database(tmp)
            self.repository.connect(tmp)
            self._migrate()
            self.repository.enable_foreign_keys()
        except Exception:
            self.repository.close()
            Path(tmp).unlink(missing_ok=True)
            raise
        logger.info("catalog database prepared path=%s", tmp)
        return tmp

    def _migrate(self) -> None:
        self.repository.begin()
        try:
            source = self.repository.classify_source()
            logger.info("source classified as=%s", source)
            if source == "unrecognized":
                raise RuntimeError("unrecognized catalog schema; aborting before any write")
            self.repository.upgrade()
            self.repository.commit()
            logger.info("schema refactor committed")
        except Exception:
            self.repository.rollback()
            raise


# --------------------------------------------------------------------------- #
# Use case 2: consolidate the seller feed into a prepared database.
# --------------------------------------------------------------------------- #
class ConsolidateFeedUseCase:
    """Given a :class:`~consolidation.repository.CatalogRepositories` bundle and the
    seller feed, resolve each listing and persist links (one transaction per
    entry via ``repositories.entry_transaction()``), creating products only when
    genuinely new. A failed entry rolls back in isolation and is reported; the
    in-memory view is then rebuilt (``repositories.reload()``) and later entries
    continue. Returns the :class:`Report`.
    """

    def __init__(
        self,
        repositories: CatalogRepositories,
        resolver: ProductIdentityResolver,
    ) -> None:
        self.repositories = repositories
        self.resolver = resolver

    def execute(self, feed: Iterable[Submission]) -> Report:
        repositories = self.repositories
        entry_use_case = ConsolidateEntryUseCase(repositories, self.resolver)

        report = Report()
        for record_index, entry in enumerate(feed):
            report.processed += 1
            if record_index == 0:
                logger.info("first feed record received")

            item_report = Report()
            try:
                with repositories.entry_transaction():
                    entry_use_case.process(entry, record_index, item_report)
            except Exception as exc:
                report.failed += 1
                detail = str(exc).splitlines()[0][:200] or type(exc).__name__
                report.failures.append(
                    {
                        "record_index": record_index,
                        "error": f"{type(exc).__name__}: {detail}",
                    }
                )
                repositories.reload()
                entry_use_case = ConsolidateEntryUseCase(repositories, self.resolver)
                continue

            self._merge_item_report(report, item_report)
            if report.processed % 1000 == 0:
                logger.info("feed progress processed=%d", report.processed)

        self._log_feed_summary(report)
        return report

    @staticmethod
    def _merge_item_report(report: Report, item_report: Report) -> None:
        report.new += item_report.new
        report.linked += item_report.linked
        report.skipped += item_report.skipped
        report.threat += item_report.threat
        report.skipped_entries.extend(item_report.skipped_entries)
        report.threats.extend(item_report.threats)

    @staticmethod
    def _log_feed_summary(report: Report) -> None:
        logger.info(
            "feed summary processed=%d new=%d linked=%d skipped=%d threat=%d failed=%d",
            report.processed,
            report.new,
            report.linked,
            report.skipped,
            report.threat,
            report.failed,
        )
        if report.threat:
            logger.warning("feed threats rejected=%d", report.threat)
        if report.failures:
            logger.error("feed item failures count=%d; details follow", report.failed)
            for failure in report.failures:
                logger.error(
                    "feed item failure record=%d reason=%s",
                    failure["record_index"],
                    failure["error"],
                )


# --------------------------------------------------------------------------- #
# Coordinator: run the two use cases and publish.
# --------------------------------------------------------------------------- #
class ConsolidateCatalogUseCase:
    """Consume the seller feed into an **already prepared** database and publish the
    output atomically only on a fully successful run. Returns a process exit code
    (``0`` ok, ``1`` on any failure or item failure).

    Preparing the database (``PrepareCatalogDatabaseUseCase``) is the caller's job
    — the composition root (:mod:`consolidation.cli`) runs it, then hands this use
    case the **same** ``repository`` (still connected, foreign keys on) plus the
    prepared file path.

    Injected collaborators: ``repository`` (the ``CatalogRepository`` — source of
    both the live connection and the per-aggregate bundle), ``resolver`` (a ready
    ``ProductIdentityResolver``, already carrying the similarity backend, which
    resolves its own threshold) and ``source`` (the ``ByteSource`` transport the
    feed streams through).
    """

    def __init__(
        self,
        repository: CatalogRepository,
        resolver: ProductIdentityResolver,
        source: ByteSource,
    ) -> None:
        self.repository = repository
        self.resolver = resolver
        self.source = source

    def execute(
        self,
        prepared_database: str | Path,
        products_url: str,
        output: str | Path,
    ) -> int:
        prepared_database = Path(prepared_database)
        output = Path(output).resolve()
        logger.info(
            "run config products_url=%s source=%s matcher=%s threshold=%s",
            products_url,
            self.source.name,
            self.resolver.similarity.name,
            self.resolver.similarity.threshold,
        )
        published = False
        try:
            report = self._consume_feed(products_url)
            self.repository.close()  # release the file before moving it
            self._publish(prepared_database, output)
            published = True
            return 1 if report.failed else 0
        except FeedValidationError as exc:
            logger.error("feed validation failed record=%d fields=%s", exc.record_index, exc.fields)
            logger.exception("run failed")
            return 1
        except Exception:
            logger.exception("run failed")
            return 1
        finally:
            self.repository.close()
            if not published:
                prepared_database.unlink(missing_ok=True)

    def _consume_feed(self, products_url: str) -> Report:
        repositories = self.repository.catalog_repositories()
        feed = iter_feed(products_url, self.source)
        report = ConsolidateFeedUseCase(repositories, self.resolver).execute(feed)
        logger.info("feed processing complete")
        return report

    @staticmethod
    def _publish(tmp: Path, output: Path) -> None:
        """Atomically replace ``output`` with ``tmp`` (only after a successful run)."""
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.exists():
            logger.warning("replacing existing output path=%s", output)
        tmp.replace(output)
        logger.info("published output path=%s", output)
