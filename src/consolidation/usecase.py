"""Application layer — use cases that orchestrate the domain and the repositories.

Two use cases:

* :class:`ConsolidateEntryUseCase` — apply one validated seller submission
  (screen, resolve identity, create-or-link, record the outcome). Pure policy; all
  persistence goes through repositories.
* :func:`run` — the end-to-end run: download, migrate, stream the feed, drive the
  per-entry use case inside one transaction each, publish atomically.

Depends on: :mod:`consolidation.domain`, :mod:`consolidation.services`,
:mod:`consolidation.repository`, :mod:`consolidation.schema`,
:mod:`consolidation.infrastructure`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from sqlalchemy import create_engine
from sqlalchemy.engine import Connection

from consolidation import schema
from consolidation.domain import Product, Submission, new_uuid, normalize
from consolidation.infrastructure import (
    FeedValidationError,
    alembic_config,
    build_similarity,
    download_to,
    iter_feed,
    screen_entry,
    verify_sqlite_header,
)
from consolidation.repository import (
    BrandRepository,
    CategoryRepository,
    ProductRepository,
    SellerListingRepository,
    SellerRepository,
    load_catalog,
)
from consolidation.services import ProductIdentityResolver, Similarity

logger = logging.getLogger("consolidation")


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
    """Apply one validated feed entry inside the caller's transaction."""

    def __init__(
        self,
        conn: Connection,
        catalog,
        similarity: Similarity,
        threshold: float,
    ) -> None:
        self.conn = conn
        self.catalog = catalog
        self.resolver = ProductIdentityResolver(similarity, threshold)
        self.brands = BrandRepository(conn)
        self.categories = CategoryRepository(conn)
        self.sellers = SellerRepository(conn)
        self.products = ProductRepository(conn)
        self.listings = SellerListingRepository(conn)

    # -- steps ------------------------------------------------------------- #
    def _create_product(self, submission: Submission) -> Product:
        brand_id = self.brands.get_or_create(submission.Brand)
        product = Product(new_uuid(), submission.Name, submission.Brand)
        self.products.add(product, brand_id)
        self._attach_category(product, submission.Category)
        self.catalog.add(product)
        return product

    def _attach_category(self, product: Product, raw_category: str | None) -> bool:
        """Persist the membership and update the aggregate. Returns divergence."""
        category_id = self.categories.get_or_create(raw_category)
        if not category_id or not normalize(raw_category):
            return False
        self.products.add_category_membership(product.id, category_id)
        return product.record_category(raw_category)

    def _link(
        self,
        seller_id: str,
        product: Product,
        submission: Submission,
        record_index: int,
        report: Report,
    ) -> None:
        bound_product = self.listings.product_for_sku(seller_id, submission.Id)
        if bound_product and bound_product != product.id:
            report.skipped += 1
            report.skipped_entries.append(
                {"record_index": record_index, "reason": "external SKU conflict"}
            )
            logger.warning("event=skip record=%d reason=external_sku_conflict", record_index)
            return

        existing_sku = self.listings.sku_for_pair(seller_id, product.id)
        if existing_sku is not None:
            if existing_sku != submission.Id:
                logger.info(
                    "event=duplicate_listing record=%d seller_id=%s product_id=%s",
                    record_index,
                    seller_id,
                    product.id,
                )
            return

        if self.listings.link(seller_id, product.id, submission.Id):
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
        seller_id = self.sellers.id_for(submission.SellerName)

        if product is None:
            bound_product = (
                self.listings.product_for_sku(seller_id, submission.Id) if seller_id else None
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

        seller_id = seller_id or self.sellers.get_or_create(submission.SellerName)
        self._link(seller_id, product, submission, record_index, report)


# --------------------------------------------------------------------------- #
# Use case 2: the full consolidation run.
# --------------------------------------------------------------------------- #
def _publish(tmp: Path, output: Path) -> None:
    """Atomically replace ``output`` with ``tmp`` (only after a fully successful run)."""
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        logger.warning("replacing existing output path=%s", output)
    tmp.replace(output)
    logger.info("published output path=%s", output)


def _new_entry_use_case(conn, similarity, threshold: float) -> ConsolidateEntryUseCase:
    """Rebuild in-memory indexes after an item transaction is rolled back."""
    use_case = ConsolidateEntryUseCase(conn, load_catalog(conn), similarity, threshold)
    conn.commit()
    return use_case


def _merge_item_report(report: Report, item_report: Report) -> None:
    report.new += item_report.new
    report.linked += item_report.linked
    report.skipped += item_report.skipped
    report.threat += item_report.threat
    report.skipped_entries.extend(item_report.skipped_entries)
    report.threats.extend(item_report.threats)


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


def run(
    *,
    catalog_url: str,
    products_url: str,
    output: str | Path,
    matcher: str,
    threshold: float,
) -> int:
    """Execute one consolidation run. Returns a process exit code."""
    logger.info(
        "run config products_url=%s matcher=%s threshold=%s",
        products_url,
        matcher,
        threshold,
    )
    output = Path(output).resolve()
    tmp: Path | None = None
    report: Report | None = None
    try:
        tmp = download_to(catalog_url, output.parent)
        verify_sqlite_header(tmp)

        engine = create_engine(f"sqlite:///{tmp}")
        try:
            with engine.connect() as conn:
                trans = conn.begin()
                try:
                    source = schema.classify_source(conn)
                    logger.info("source classified as=%s", source)
                    if source == "unrecognized":
                        raise RuntimeError("unrecognized catalog schema; aborting before any write")

                    command.upgrade(alembic_config(conn), "head")
                    trans.commit()
                    logger.info("schema refactor committed")

                    conn.exec_driver_sql("PRAGMA foreign_keys = ON")
                    if conn.exec_driver_sql("PRAGMA foreign_keys").scalar() != 1:
                        raise RuntimeError("foreign key enforcement could not be enabled")
                    conn.commit()
                    logger.info("foreign key enforcement enabled")

                    similarity = build_similarity(matcher)
                    use_case = _new_entry_use_case(conn, similarity, threshold)
                    report = Report()
                    for record_index, entry in enumerate(iter_feed(products_url)):
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
                            use_case = _new_entry_use_case(conn, similarity, threshold)
                            continue

                        _merge_item_report(report, item_report)
                        if report.processed % 1000 == 0:
                            logger.info("feed progress processed=%d", report.processed)

                    _log_feed_summary(report)
                    logger.info("feed processing complete")
                except Exception:
                    if trans.is_active:
                        trans.rollback()
                    logger.error("setup or feed stream failed; previous output preserved")
                    raise
        finally:
            engine.dispose()

        _publish(tmp, output)
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
