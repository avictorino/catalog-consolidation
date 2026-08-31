"""Application layer — use cases that orchestrate the domain and the repositories.

Two use cases:

* :class:`ConsolidateEntryUseCase` — apply one validated seller submission
  (screen, resolve identity, create-or-link, record the outcome). Pure policy; all
  persistence goes through the injected repository bundle.
* :class:`ConsolidateCatalogUseCase` — the end-to-end run: download, migrate,
  stream the feed, drive the per-entry use case inside one transaction each,
  publish atomically.

Both collaborators the run needs — the ``Similarity`` backend and the
``CatalogRepositories`` factory — are injected, so the use cases never build them.

Depends on: :mod:`consolidation.domain`, :mod:`consolidation.services`,
:mod:`consolidation.repository`, :mod:`consolidation.infrastructure`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from consolidation.domain import Catalog, Product, Submission, new_uuid, normalize
from consolidation.infrastructure import (
    FeedValidationError,
    alembic_config,
    classify_source,
    download_to,
    iter_feed,
    screen_entry,
    verify_sqlite_header,
)
from consolidation.repository import CatalogRepositories
from consolidation.services import ProductIdentityResolver, Similarity

logger = logging.getLogger("consolidation")

RepositoriesFactory = Callable[[Connection], CatalogRepositories]


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
# Use case 1: consolidate one seller submission.
# --------------------------------------------------------------------------- #
class ConsolidateEntryUseCase:
    """Apply one validated feed entry inside the caller's transaction.

    Receives the working :class:`~consolidation.domain.Catalog` and a
    :class:`~consolidation.repository.CatalogRepositories` bundle — every database
    access already instantiated against the run's connection.
    """

    def __init__(
        self,
        catalog: Catalog,
        repositories: CatalogRepositories,
        similarity: Similarity,
        threshold: float,
    ) -> None:
        self.catalog = catalog
        self.repositories = repositories
        self.resolver = ProductIdentityResolver(similarity, threshold)

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
                    "event=duplicate_listing record=%d seller_id=%s product_id=%s",
                    record_index,
                    seller_id,
                    product.id,
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
                "event=approximate_match record=%d product_id=%s score=%.3f",
                record_index,
                product.id,
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
                "event=category_divergence record=%d product_id=%s", record_index, product.id
            )

        seller_id = seller_id or self.repositories.sellers.get_or_create(submission.SellerName)
        self._link(seller_id, product, submission, record_index, report)


# --------------------------------------------------------------------------- #
# Use case 2: the full consolidation run.
# --------------------------------------------------------------------------- #
class ConsolidateCatalogUseCase:
    """End-to-end run: download the catalog, refactor its schema, stream the seller
    feed through :class:`ConsolidateEntryUseCase` (one transaction per entry), and
    publish the output atomically only on success.

    Injected collaborators:

    * ``similarity`` — an already-constructed ``Similarity`` backend (the
      composition root picks it via ``infrastructure.build_similarity``);
    * ``repositories`` — a factory ``(Connection) -> CatalogRepositories`` that
      builds the whole database-access bundle for the run's connection. A test
      can pass a fake here; the default is the real bundle.

    Construct with the resolved configuration and call :meth:`execute`, which
    returns a process exit code (``0`` ok, ``1`` on any failure or item failure).
    """

    def __init__(
        self,
        *,
        catalog_url: str,
        products_url: str,
        output: str | Path,
        similarity: Similarity,
        threshold: float,
        repositories: RepositoriesFactory = CatalogRepositories,
    ) -> None:
        self.catalog_url = catalog_url
        self.products_url = products_url
        self.output = Path(output).resolve()
        self.similarity = similarity
        self.threshold = threshold
        self.repositories = repositories

    # -- public entry point --------------------------------------------- #
    def execute(self) -> int:
        logger.info(
            "run config products_url=%s matcher=%s threshold=%s",
            self.products_url,
            self.similarity.name,
            self.threshold,
        )
        tmp: Path | None = None
        report: Report | None = None
        try:
            tmp = download_to(self.catalog_url, self.output.parent)
            verify_sqlite_header(tmp)

            engine = create_engine(f"sqlite:///{tmp}")
            try:
                with engine.connect() as conn:
                    report = self._process_connection(conn)
            finally:
                engine.dispose()

            self._publish(tmp)
            tmp = None
            return 1 if report and report.failed else 0
        except FeedValidationError as exc:
            logger.error("feed validation failed record=%d fields=%s", exc.record_index, exc.fields)
            logger.exception("run failed")
            return 1
        except Exception:
            logger.exception("run failed")
            return 1
        finally:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    # -- steps --------------------------------------------------------- #
    def _process_connection(self, conn: Connection) -> Report:
        trans = conn.begin()
        try:
            self._refactor_schema(conn, trans)
            self._enable_foreign_keys(conn)

            report = self._consume_feed(conn)
            logger.info("feed processing complete")
            return report
        except Exception:
            if trans.is_active:
                trans.rollback()
            logger.error("setup or feed stream failed; previous output preserved")
            raise

    @staticmethod
    def _refactor_schema(conn: Connection, trans) -> None:
        source = classify_source(conn)
        logger.info("source classified as=%s", source)
        if source == "unrecognized":
            raise RuntimeError("unrecognized catalog schema; aborting before any write")
        command.upgrade(alembic_config(conn), "head")
        trans.commit()
        logger.info("schema refactor committed")

    @staticmethod
    def _enable_foreign_keys(conn: Connection) -> None:
        conn.exec_driver_sql("PRAGMA foreign_keys = ON")
        if conn.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
            raise RuntimeError("foreign key enforcement could not be enabled")
        conn.commit()
        logger.info("foreign key enforcement enabled")

    def _new_entry_use_case(self, conn: Connection) -> ConsolidateEntryUseCase:
        """Build the per-entry use case, priming its in-memory indexes from the DB.
        Also used to rebuild them after an item transaction is rolled back."""
        repositories = self.repositories(conn)
        use_case = ConsolidateEntryUseCase(
            repositories.load_catalog(), repositories, self.similarity, self.threshold
        )
        conn.commit()
        return use_case

    def _consume_feed(self, conn: Connection) -> Report:
        use_case = self._new_entry_use_case(conn)
        report = Report()
        for record_index, entry in enumerate(iter_feed(self.products_url)):
            report.processed += 1
            if record_index == 0:
                logger.info("first feed record received")

            item_report = Report()
            item_trans = conn.begin()
            try:
                use_case.process(entry, record_index, item_report)
                item_trans.commit()
            except Exception as exc:
                if item_trans.is_active:
                    item_trans.rollback()
                report.failed += 1
                detail = str(exc).splitlines()[0][:200] or type(exc).__name__
                report.failures.append(
                    {
                        "record_index": record_index,
                        "error": f"{type(exc).__name__}: {detail}",
                    }
                )
                use_case = self._new_entry_use_case(conn)
                continue

            self._merge_item_report(report, item_report)
            if report.processed % 1000 == 0:
                logger.info("feed progress processed=%d", report.processed)

        self._log_feed_summary(report)
        return report

    def _publish(self, tmp: Path) -> None:
        """Atomically replace the output with ``tmp`` (only after a successful run)."""
        self.output.parent.mkdir(parents=True, exist_ok=True)
        if self.output.exists():
            logger.warning("replacing existing output path=%s", self.output)
        tmp.replace(self.output)
        logger.info("published output path=%s", self.output)

    # -- report helpers ---------------------------------------------- #
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
