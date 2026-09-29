# Catalog Summary

Scope: JSON catalog and spreadsheet import state
Trust: Confirmed
Lifecycle: Fresh
Last verified: 2026-09-29
Evidence: `data/catalog.json`, `source/import_bar_xls.py`, catalog tests, commit `a57cc389c4aafcf5e840b67b0481a168c032fe3b`
Depends on: catalog schema and owner spreadsheet sources
Importance: High

- The catalog is validated JSON with stable category/subcategory/item IDs.
- Current committed state: 10 top-level categories and 513 cards.
- `Новые лимонады` contains 6 active cards imported from
  `Технологическая карта лимонади.xls`, which is stored in the repository.
- Seven pre-existing cocktails contain owner-confirmed manual recipe/card
  refinements and must remain intact.
- Five lemonade recipes contain owner-curated syrup variants. The importer
  treats those recipes as authoritative while refreshing spreadsheet metadata.
- Lemonade dry-run import is idempotent; existing stable IDs and photographs
  are preserved. Commit `a57cc389c4aafcf5e840b67b0481a168c032fe3b`
  completed the work with a confirmed full test result of `797 passed`.

Do not regenerate or rewrite the catalog merely to satisfy formatting. Compare
semantic IDs and owner-curated exceptions before any future import.
