"""Inspect runtime voice aliases and fuzzy scores without Telegram or Vosk."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.models.catalog_item import CatalogItem  # noqa: E402
from app.services.catalog_service import CatalogService  # noqa: E402
from app.services.search_service import SearchResult  # noqa: E402
from app.services.voice_alias_service import (  # noqa: E402
    VoiceAliasService,
    contains_latin,
)
from app.services.voice_matcher import VoiceCatalogMatcher  # noqa: E402
from app.storage.json_storage import JsonCatalogStorage  # noqa: E402

CATALOG_PATH = PROJECT_ROOT / "data" / "catalog.json"


def _active_results(service: CatalogService) -> Iterator[SearchResult]:
    for category in service.get_active_categories():
        for item in service.get_active_items(category.id):
            yield SearchResult(category=category, item=item)
        for subcategory in service.get_active_subcategories(category.id):
            for item in service.get_active_items(category.id, subcategory.id):
                yield SearchResult(category, item, subcategory)


def _location(result: SearchResult) -> str:
    if result.subcategory is None:
        return result.category.name
    return f"{result.category.name} / {result.subcategory.name}"


def _format_values(values: set[str] | list[str]) -> str:
    return ", ".join(sorted(values)) if values else "—"


def _print_alias_report(
    results: list[SearchResult],
    alias_service: VoiceAliasService,
) -> None:
    latin_count = 0
    print("Display name → generated aliases | manual aliases")
    print("=" * 78)
    for result in results:
        item: CatalogItem = result.item
        automatic = alias_service.build_automatic_aliases(item.name)
        if contains_latin(item.name):
            latin_count += 1
        print(f"[{_location(result)}] {item.name}")
        print(f"  generated: {_format_values(automatic)}")
        print(f"  manual:    {_format_values(item.voice_aliases)}")
    print("=" * 78)
    print(
        f"Active items: {len(results)}; names containing Latin: {latin_count}"
    )


def _print_top_results(
    query: str,
    matcher: VoiceCatalogMatcher,
) -> None:
    outcome = matcher.match(query)
    print(f"Query: {outcome.query}")
    print(f"Confidence: {outcome.confidence.value}")
    print("TOP-10 matcher results")
    print("=" * 78)
    for position, match in enumerate(matcher.rank(query, limit=10), start=1):
        print(
            f"{position:>2}. {match.score:.4f} | "
            f"{match.result.item.name} | {_location(match.result)} | "
            f"alias: {match.matched_text}"
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inspect BarBot runtime voice aliases and fuzzy scores.",
    )
    parser.add_argument(
        "query",
        nargs="?",
        help="Optional normalized or raw phrase for TOP-10 matching.",
    )
    arguments = parser.parse_args(argv)

    catalog = JsonCatalogStorage(CATALOG_PATH).load()
    service = CatalogService(catalog)
    results = list(_active_results(service))
    alias_service = VoiceAliasService()
    if arguments.query:
        _print_top_results(
            arguments.query,
            VoiceCatalogMatcher(catalog, alias_service),
        )
    else:
        _print_alias_report(results, alias_service)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
