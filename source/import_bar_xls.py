"""Import the current bar spreadsheets into the universal JSON catalog.

The utility is intentionally kept outside the runtime application.  It parses
the two owner-provided XLS exports, skips known accounting duplicates, updates
existing cards by their stable technical-card number (or a curated alias), and
uses the regular safe catalog storage only when ``--write`` is supplied.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import xlrd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.models.catalog import Catalog
from app.models.catalog_item import (
    MAX_STABLE_ID_LENGTH,
    CatalogItem,
    Ingredient,
)
from app.models.category import Category
from app.models.subcategory import Subcategory
from app.services.stable_id import generate_stable_id
from app.storage.json_storage import JsonCatalogStorage

PF_FILE = ROOT / "Технологическая_карта_полуфабрикати_бар.xls"
BAR_FILE = ROOT / "Технологическая карта бар блюда.xls"
CATALOG_FILE = ROOT / "data" / "catalog.json"

ORIGINAL_SIGNATURE_IDS = {
    "gin-tonic-yuzu",
    "kamanin-tigress",
    "pornstar-girl",
    "pornstar-boy",
    "pornstar-strawb",
    "cryptocurrency",
    "kiss-me",
    "kiwi-punch",
    "black-hive",
    "mango-sayer",
    "basil-smash",
    "butterfly",
    "allure",
    "huddart-smoky",
    "ultimo-turno",
}

ORIGINAL_CLASSIC_IDS = {
    "cosmopolitan",
    "whiskey-sour",
    "gin-sour",
    "old-fashioned",
    "negroni",
    "boulevardier",
    "margarita",
    "aperol-spritz",
}

# These recipes originated in source/tech_cards.txt and contain precise syrup,
# liqueur, puree, and spirit variants that the later XLS export generalizes as
# "в ассортименте".  XLS metadata is still refreshed, but these ingredient
# lists are deliberately authoritative and must not be overwritten.
TEXT_SOURCE_RECIPE_IDS = (
    ORIGINAL_SIGNATURE_IDS
    | ORIGINAL_CLASSIC_IDS
    | {"berry-bloom-na"}
)


@dataclass(frozen=True)
class SourceCard:
    """Normalized data extracted from one spreadsheet card."""

    index: int
    title: str
    number: str
    date: str
    ingredients: list[Ingredient]
    yield_text: str


def _text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip().replace("\n", " ")


def _decimal(value: str) -> Decimal:
    return Decimal(value.replace(",", ".").strip())


def _clean_amount(value: Decimal) -> int | float:
    normalized = value.normalize()
    if normalized == normalized.to_integral():
        return int(normalized)
    return float(normalized)


def _clean_ingredient_name(raw_name: str) -> tuple[str, str | None]:
    name = re.sub(r"\s+", " ", raw_name).strip()
    suffix = re.search(r",\s*(л|кг|шт|порц)\.?$", name, re.IGNORECASE)
    unit_hint = suffix.group(1).casefold() if suffix else None
    name = re.sub(r"\s*/?\s*\d{4}(?:\s+\d{2}){2,4}\b", "", name)
    name = re.sub(
        r",\s*(л|кг|шт|порц)\.?$",
        "",
        name,
        flags=re.IGNORECASE,
    )
    name = name.replace("п\\ф", "П/Ф").replace("б\\а", "б/а")
    name = re.sub(
        r"\bв\s+ассортим\.?\b",
        "в ассортименте",
        name,
        flags=re.IGNORECASE,
    )
    name = name.replace("украшенияв", "для украшения в")
    name = re.sub(r"\s+", " ", name).strip(" ,.;")
    if name:
        name = name[0].upper() + name[1:]
    return name, unit_hint


def _convert_ingredient(raw_name: str, raw_unit: str, raw_amount: str) -> Ingredient:
    name, unit_hint = _clean_ingredient_name(raw_name)
    source_unit = (unit_hint or raw_unit.strip().casefold()).rstrip(".")
    amount = _decimal(raw_amount)
    if source_unit == "л":
        amount *= 1000
        unit = "мл"
    elif source_unit == "кг":
        amount *= 1000
        unit = "г"
    elif source_unit in {"шт", "штука"}:
        unit = "шт."
    elif source_unit == "порц":
        unit = "порция"
    else:
        unit = raw_unit.strip()
    return Ingredient(name=name, amount=_clean_amount(amount), unit=unit)


def _format_yield(raw_yield: str) -> str:
    match = re.fullmatch(
        r"\s*([0-9]+(?:[.,][0-9]+)?)\s*(\S+)\s*",
        raw_yield,
    )
    if match is None:
        return raw_yield.strip()
    amount = _decimal(match.group(1))
    unit = match.group(2).casefold().rstrip(".")
    if unit == "л":
        return f"{_clean_amount(amount * 1000)} мл"
    if unit == "кг":
        return f"{_clean_amount(amount * 1000)} г"
    if unit.startswith("порц"):
        return f"{_clean_amount(amount)} порция"
    return f"{_clean_amount(amount)} {match.group(2)}"


def parse_cards(path: Path) -> list[SourceCard]:
    """Parse every technological card from one XLS report sheet."""

    sheet = xlrd.open_workbook(path).sheet_by_index(0)
    cards: list[SourceCard] = []
    for row in range(sheet.nrows):
        marker = _text(sheet.cell_value(row, 0))
        marker_match = re.fullmatch(
            r"Технологическая карта №\s*(.+)",
            marker,
            re.IGNORECASE,
        )
        if marker_match is None:
            continue

        header_row = next(
            (
                candidate
                for candidate in range(row + 1, min(sheet.nrows, row + 16))
                if _text(sheet.cell_value(candidate, 1)).casefold()
                == "наименование продукта"
            ),
            None,
        )
        if header_row is None:
            raise RuntimeError(
                f"Не найдена таблица ингредиентов после строки {row + 1}"
            )

        ingredients: list[Ingredient] = []
        cursor = header_row + 1
        total_row: int | None = None
        while cursor < sheet.nrows:
            first_cell = _text(sheet.cell_value(cursor, 0))
            if first_cell.casefold() == "итого":
                total_row = cursor
                break
            if re.fullmatch(r"\d+", first_cell):
                raw_name = _text(sheet.cell_value(cursor, 1))
                raw_unit = _text(sheet.cell_value(cursor, 5))
                raw_amount = (
                    _text(sheet.cell_value(cursor, 12))
                    or _text(sheet.cell_value(cursor, 10))
                    or _text(sheet.cell_value(cursor, 8))
                    or _text(sheet.cell_value(cursor, 6))
                )
                if raw_name and raw_amount:
                    ingredients.append(
                        _convert_ingredient(raw_name, raw_unit, raw_amount)
                    )
            cursor += 1

        yield_text = ""
        if total_row is not None:
            for candidate in range(
                total_row + 1,
                min(sheet.nrows, total_row + 7),
            ):
                if "вес готового блюда" in _text(
                    sheet.cell_value(candidate, 0)
                ).casefold():
                    yield_text = _format_yield(
                        _text(sheet.cell_value(candidate, 9))
                    )
                    break

        cards.append(
            SourceCard(
                index=len(cards) + 1,
                title=_text(sheet.cell_value(row - 1, 0)),
                number=marker_match.group(1).strip(),
                date=_text(sheet.cell_value(row + 2, 0)),
                ingredients=ingredients,
                yield_text=yield_text,
            )
        )
    return cards


def _normalized(value: str) -> str:
    value = value.casefold().replace("ё", "е")
    value = value.translate(
        str.maketrans({"і": "и", "ї": "и", "є": "е", "ґ": "г"})
    )
    value = value.replace("n/a", "ба").replace("б\\а", "ба").replace("б/а", "ба")
    value = re.sub(r"^\s*(коктейль|шот)\s+", "", value)
    value = re.sub(r"\bп[\\/]?ф\b", "", value)
    return re.sub(r"[^a-zа-я0-9]+", "", value)


def _display_name(card: SourceCard, kind: str) -> str:
    name = re.sub(r"\s+", " ", card.title).strip()
    if kind in {"cocktail", "shot"}:
        name = re.sub(
            r"^(?:Коктейль|Шот)\s+",
            "",
            name,
            flags=re.IGNORECASE,
        )
    if kind == "wine":
        name = re.sub(r"^Вино\s+", "", name, flags=re.IGNORECASE)
    if kind == "pf":
        name = name.replace("п\\ф", "").replace("п/ф", "")
        name = re.sub(r"\bпф\b", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\b20(?:24|25|26)\b", "", name)
        name = re.sub(r"\bиюль\b", "", name, flags=re.IGNORECASE)
        name = re.sub(r",\s*(?:кг|л)\s*$", "", name, flags=re.IGNORECASE)
        name = re.sub(r"\s+", " ", name).strip(" ,.-")

    name = {
        "Hirosima": "Hiroshima",
        "Фільтр кава": "Фильтр-кофе",
        "Еспресо": "Эспрессо",
        "Айс латте": "Айс-латте",
        "Еспресо-тонік": "Эспрессо-тоник",
    }.get(name, name)
    name = re.sub(r"(?i)(\d)\s*ml\b", r"\1 мл", name)
    name = re.sub(r"(?i)(\d)\s*l\b", r"\1 л", name)
    name = name.replace("0,25мл", "250 мл").replace("025l", "250 мл")
    name = re.sub(r"\s+", " ", name).strip()
    return name[0].upper() + name[1:] if name else name


def _bar_section(card: SourceCard) -> tuple[str, str | None] | None:
    """Return the catalog location for a useful bar card.

    Row ranges reflect stable sections in the owner's 1C-style report.  Cards
    150-153 and most of 363-407 are accounting copies, component write-offs,
    or older duplicate recipes and are deliberately not published.
    """

    index = card.index
    if 1 <= index <= 100:
        if index >= 69:
            return "wine", "wine_red"
        title_key = _normalized(card.title)
        ingredient_key = "".join(_normalized(item.name) for item in card.ingredients)
        sparkling = (
            "prosecco",
            "cava",
            "champagne",
            "игрист",
            "lambrusco",
            "rieslingtrocken",
        )
        rose = ("rose", "рожев", "whitezinfandel")
        if any(token in title_key for token in sparkling):
            return "wine", "wine_sparkling"
        if any(token in title_key or token in ingredient_key for token in rose):
            return "wine", "wine_rose"
        return "wine", "wine_white"
    if 101 <= index <= 105 or index in {109, 111, 112, 113, 114}:
        return "signature_tea", None
    if index in {106, 107}:
        return "cocktails", "hot_cocktails"
    if index in {108, 110}:
        return "classic_tea", None
    if 115 <= index <= 133 and index != 125:
        target = "coffee_special" if index in {115, 131} else None
        return "coffee", target
    if 134 <= index <= 139:
        return "cocktails", "hot_cocktails"
    if 140 <= index <= 149:
        return "bar_alcohol", "infusions"
    if 154 <= index <= 240 and index != 192:
        classic = {
            "aperolsour",
            "aperolspritz",
            "bloodymary",
            "boulevardier",
            "cosmopolitan",
            "daiquiri",
            "espressomartini",
            "ginsour",
            "hugo",
            "lynchburglemonade",
            "maitai",
            "margarita",
            "negroni",
            "oldfashion",
            "penicillin",
            "piscosour",
            "royalmojito",
            "sangriared",
            "sangriawhite",
            "tequilasour",
            "whiskeysour",
            "вискикола",
            "кубалибре",
        }
        target = (
            "old_classics"
            if _normalized(card.title) in classic
            else "old_cocktails"
        )
        return "cocktails", target
    if 241 <= index <= 249:
        return "cocktails", "non_alcoholic"
    if 250 <= index <= 252:
        return "soft_drinks", "smoothies"
    if 253 <= index <= 257:
        return "cocktails", "shots"
    if 258 <= index <= 339 or 394 <= index <= 396:
        key = _normalized(card.title)
        if key.startswith("виски"):
            subcategory = "whisky"
        elif key.startswith("горилка"):
            subcategory = "vodka"
        elif key.startswith("джин"):
            subcategory = "gin_spirits"
        elif key.startswith(("ром", "текила", "писко")):
            subcategory = "rum_tequila"
        elif key.startswith(("бренди", "коньяк")):
            subcategory = "brandy"
        else:
            subcategory = "liqueurs"
        return "bar_alcohol", subcategory
    if 340 <= index <= 348 or index == 408:
        return "desserts", None
    if 349 <= index <= 361 or 399 <= index <= 401 or index == 405:
        return "bar_alcohol", "beer_cider"
    if index == 362:
        return "cocktails", "signature"
    if 409 <= index <= 423 or index in {460, 462, 463}:
        return "soft_drinks", "lemonades"
    if 424 <= index <= 435:
        return "soft_drinks", "fresh_servings"
    if 436 <= index <= 451:
        return "soft_drinks", "packaged_drinks"
    if index in {452, 455, 467, 468}:
        return "coffee", "coffee_special"
    if index in {
        453,
        454,
        456,
        457,
        458,
        459,
        469,
        470,
        471,
        472,
        473,
        474,
        475,
        476,
    }:
        return "soft_drinks", "homemade_drinks"
    return None


def _pf_subcategory(card: SourceCard) -> str:
    key = _normalized(card.title)
    if any(word in key for word in ("морож", "морозиво", "крошка")):
        return "dessert_bases"
    if "фреш" in key or "зачистка" in key:
        return "fresh_juices"
    if any(word in key for word in ("сироп", "кордиал", "раствор", "сахар")):
        return "syrups_cordials"
    if any(
        word in key
        for word in (
            "лимонад",
            "сангри",
            "морс",
            "чай",
            "компот",
            "сангрита",
            "мерри",
        )
    ):
        return "lemonade_bases"
    if any(
        word in key
        for word in ("пюре", "кули", "нектарин", "малиновиноград", "киви")
    ):
        return "purees_bases"
    return "infusions_mixes"


class CatalogImporter:
    """Build an updated in-memory catalog while preserving all current IDs."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.categories = {category.id: category for category in catalog.categories}
        self._reconcile_previous_import()
        self.items = [item for category in catalog.categories for item in category.items]
        self.items += [
            item
            for category in catalog.categories
            for subcategory in category.subcategories
            for item in subcategory.items
        ]
        self.old_ids = {item.id for item in self.items}
        self.all_ids = set(self.old_ids)
        self.all_ids.update(category.id for category in catalog.categories)
        self.all_ids.update(
            subcategory.id
            for category in catalog.categories
            for subcategory in category.subcategories
        )
        self.by_id = {item.id: item for item in self.items}
        self.by_tech = {
            str(item.fields["tech_card_number"]): item
            for item in self.items
            if item.fields.get("tech_card_number")
        }
        self.by_name: dict[str, list[CatalogItem]] = defaultdict(list)
        for item in self.items:
            self.by_name[_normalized(item.name)].append(item)
        self.matched: list[tuple[str, str, str]] = []
        self.created: list[tuple[str, str, str, str | None]] = []
        self.selected_numbers: set[str] = set()

    def _reconcile_previous_import(self) -> None:
        """Merge a known alias and normalize two legacy text-only recipes."""

        cocktails = self.categories["cocktails"]
        non_alcoholic = next(
            subcategory
            for subcategory in cocktails.subcategories
            if subcategory.id == "non_alcoholic"
        )
        non_alcoholic.items = [
            item
            for item in non_alcoholic.items
            if item.id != "berry_blum_n_a"
        ]

        semifinished = self.categories["polufabrikaty_p_f"]
        legacy_items = {
            item.id: item
            for subcategory in semifinished.subcategories
            for item in subcategory.items
        }
        berry_base = legacy_items.get("yagodnyy_p_f")
        if berry_base is not None and not berry_base.ingredients:
            berry_base.ingredients = [
                Ingredient(name="Смородина", amount=250, unit="г"),
                Ingredient(name="Клубника", amount=250, unit="г"),
                Ingredient(name="Сахар", amount=250, unit="г"),
            ]
            berry_base.short_description = (
                "Ягодная основа из смородины и клубники для фирменного чая."
            )
        compote = legacy_items.get("polunichno")
        if compote is not None and not compote.ingredients:
            compote.ingredients = [
                Ingredient(name="Клубника", amount=1500, unit="г"),
                Ingredient(name="Смородина", amount=1500, unit="г"),
                Ingredient(name="Яблоки", amount=2000, unit="г"),
                Ingredient(name="Сахар", amount=1500, unit="г"),
                Ingredient(name="Вода", amount=4, unit="кулера"),
            ]
            compote.short_description = "Клубнично-яблочная основа для компота."

    def ensure_structure(self) -> None:
        cocktails = self.categories["cocktails"]
        methods = next(
            (
                subcategory
                for subcategory in cocktails.subcategories
                if subcategory.id == "methods"
            ),
            None,
        )
        if methods is not None and methods.items:
            raise RuntimeError(
                "Нельзя удалить непустой раздел методов приготовления"
            )
        cocktails.subcategories = [
            subcategory
            for subcategory in cocktails.subcategories
            if subcategory.id != "methods"
        ]

        classic_tea = self._ensure_category(
            "classic_tea",
            "Классические чаи",
            "🍵",
            35,
            "Классический листовой и гречишный чай.",
        )
        alcohol = self._ensure_category(
            "bar_alcohol",
            "Барный алкоголь",
            "🥃",
            45,
            "Крепкий алкоголь, ликёры, настойки и пиво.",
        )
        soft = self._ensure_category(
            "soft_drinks",
            "Безалкогольные напитки",
            "🧃",
            50,
            "Лимонады, фреши, воды и прохладительные напитки.",
        )
        self._ensure_category(
            "desserts",
            "Десерты",
            "🍨",
            55,
            "Барные десерты и мороженое.",
        )

        wine = self.categories["wine"]
        for spec in (
            ("wine_white", "Белые вина", "🥂", 10, "Белые вина по бокалам и бутылкам."),
            ("wine_rose", "Розовые вина", "🌸", 20, "Розовые вина по бокалам и бутылкам."),
            ("wine_sparkling", "Игристые вина", "🍾", 30, "Игристые вина и шампанское."),
            ("wine_red", "Красные вина", "🍷", 40, "Красные вина по бокалам и бутылкам."),
        ):
            self._ensure_subcategory(wine, *spec)

        hot_cocktails = self._ensure_subcategory(
            self.categories["cocktails"],
            "hot_cocktails",
            "Горячие коктейли",
            "♨️",
            45,
            "Глинтвейны, пунши и горячие смешанные напитки.",
        )
        old_cocktails = self._ensure_subcategory(
            self.categories["cocktails"],
            "old_cocktails",
            "Старые фирменные коктейли",
            "🕰",
            15,
            "Коктейли из прежних технологических карт бара.",
        )
        old_classics = self._ensure_subcategory(
            self.categories["cocktails"],
            "old_classics",
            "Старые классические коктейли",
            "📜",
            25,
            "Классические коктейли из прежних технологических карт.",
        )
        self._order_cocktail_subcategories()
        self._separate_imported_signature_cocktails(old_cocktails)
        self._separate_imported_classic_cocktails(old_classics)
        self._separate_tea_cards(classic_tea, hot_cocktails)
        for spec in (
            ("whisky", "Виски", "🥃", 10, "Виски порционной подачи."),
            ("vodka", "Водка", "🧊", 20, "Водка порционной подачи."),
            ("gin_spirits", "Джин", "🌿", 30, "Джин порционной подачи."),
            ("rum_tequila", "Ром, текила и писко", "🌵", 40, "Ром, текила, мескаль и писко."),
            ("liqueurs", "Ликёры и вермуты", "🍸", 50, "Ликёры, вермуты и аперитивы."),
            ("brandy", "Бренди и коньяк", "🪵", 60, "Бренди и коньяк."),
            ("infusions", "Настойки и лимончелло", "🍊", 70, "Фирменные настойки и лимончелло."),
            ("beer_cider", "Пиво и сидр", "🍺", 80, "Пиво и сидр в ассортименте."),
        ):
            self._ensure_subcategory(alcohol, *spec)
        for spec in (
            ("lemonades", "Лимонады", "🍋", 10, "Авторские и классические лимонады."),
            ("fresh_servings", "Фреши", "🍊", 20, "Свежевыжатые соки."),
            ("smoothies", "Смузи и милкшейки", "🥤", 30, "Смузи и молочные коктейли."),
            ("packaged_drinks", "Напитки в бутылках", "🧊", 40, "Газированные напитки, соки и энергетики."),
            ("homemade_drinks", "Вода, компоты и морсы", "💧", 50, "Вода и напитки собственного приготовления."),
        ):
            self._ensure_subcategory(soft, *spec)
        self._ensure_subcategory(
            self.categories["polufabrikaty_p_f"],
            "dessert_bases",
            "Десертные заготовки",
            "🍨",
            60,
            "Мороженое, крошка и десертные полуфабрикаты.",
        )

    def _order_cocktail_subcategories(self) -> None:
        cocktails = self.categories["cocktails"]
        order = {
            "signature": 10,
            "classic": 20,
            "shots": 30,
            "old_cocktails": 40,
            "old_classics": 50,
            "non_alcoholic": 60,
            "hot_cocktails": 70,
        }
        for subcategory in cocktails.subcategories:
            if subcategory.id in order:
                subcategory.sort_order = order[subcategory.id]
        cocktails.subcategories.sort(
            key=lambda subcategory: (
                subcategory.sort_order,
                subcategory.name.casefold(),
                subcategory.id,
            )
        )

    def _separate_tea_cards(
        self,
        classic_tea: Category,
        hot_cocktails: Subcategory,
    ) -> None:
        signature_tea = self.categories["signature_tea"]
        classic_ids = {"chay_grechaniy", "chay_klasichniy"}
        mulled_wine_ids = {"glintveyn_b_a", "glintveyn_b_a_2"}

        classic_items = [
            item for item in signature_tea.items if item.id in classic_ids
        ]
        mulled_wines = [
            item for item in signature_tea.items if item.id in mulled_wine_ids
        ]
        moved_ids = classic_ids | mulled_wine_ids
        signature_tea.items = [
            item for item in signature_tea.items if item.id not in moved_ids
        ]

        classic_existing_ids = {item.id for item in classic_tea.items}
        classic_tea.items.extend(
            item for item in classic_items if item.id not in classic_existing_ids
        )
        hot_existing_ids = {item.id for item in hot_cocktails.items}
        hot_cocktails.items.extend(
            item for item in mulled_wines if item.id not in hot_existing_ids
        )

        classic_names = {
            "chay_grechaniy": "Гречишный чай — 500 мл",
            "chay_klasichniy": "Классический чай — 500 мл",
        }
        for sort_order, item in enumerate(classic_tea.items, start=1):
            item.name = classic_names.get(item.id, item.name)
            item.sort_order = sort_order * 10
            item.tags = ["чай", "классический"]
        for sort_order, item in enumerate(signature_tea.items, start=1):
            item.sort_order = sort_order * 10
        for item in hot_cocktails.items:
            if item.id in mulled_wine_ids:
                item.tags = ["горячий", "безалкогольный", "глинтвейн"]

    def _separate_imported_signature_cocktails(
        self,
        old_cocktails: Subcategory,
    ) -> None:
        signature = next(
            subcategory
            for subcategory in self.categories["cocktails"].subcategories
            if subcategory.id == "signature"
        )
        moved_items = [
            item
            for item in signature.items
            if item.id not in ORIGINAL_SIGNATURE_IDS
            and item.fields.get("tech_card_number")
        ]
        moved_ids = {item.id for item in moved_items}
        signature.items = [
            item for item in signature.items if item.id not in moved_ids
        ]
        old_existing_ids = {item.id for item in old_cocktails.items}
        old_cocktails.items.extend(
            item for item in moved_items if item.id not in old_existing_ids
        )

        for sort_order, item in enumerate(signature.items, start=1):
            item.sort_order = sort_order * 10
        for sort_order, item in enumerate(old_cocktails.items, start=1):
            item.sort_order = sort_order * 10
            item.tags = [
                "старые фирменные коктейли"
                if tag in {"фирменные коктейли", "старые кокты"}
                else tag
                for tag in item.tags
            ]

    def _separate_imported_classic_cocktails(
        self,
        old_classics: Subcategory,
    ) -> None:
        classic = next(
            subcategory
            for subcategory in self.categories["cocktails"].subcategories
            if subcategory.id == "classic"
        )
        moved_items = [
            item
            for item in classic.items
            if item.id not in ORIGINAL_CLASSIC_IDS
            and item.fields.get("tech_card_number")
        ]
        moved_ids = {item.id for item in moved_items}
        classic.items = [
            item for item in classic.items if item.id not in moved_ids
        ]
        old_existing_ids = {item.id for item in old_classics.items}
        old_classics.items.extend(
            item for item in moved_items if item.id not in old_existing_ids
        )

        for sort_order, item in enumerate(classic.items, start=1):
            item.sort_order = sort_order * 10
        for sort_order, item in enumerate(old_classics.items, start=1):
            item.sort_order = sort_order * 10
            item.tags = [
                "старые классические коктейли"
                if tag == "классические коктейли"
                else tag
                for tag in item.tags
            ]

    def _ensure_category(
        self,
        category_id: str,
        name: str,
        emoji: str,
        sort_order: int,
        description: str,
    ) -> Category:
        category = self.categories.get(category_id)
        if category is None:
            category = Category(
                id=category_id,
                name=name,
                emoji=emoji,
                description=description,
                sort_order=sort_order,
            )
            self.catalog.categories.append(category)
            self.categories[category_id] = category
            self.all_ids.add(category_id)
        return category

    def _ensure_subcategory(
        self,
        category: Category,
        subcategory_id: str,
        name: str,
        emoji: str,
        sort_order: int,
        description: str,
    ) -> Subcategory:
        subcategory = next(
            (
                entry
                for entry in category.subcategories
                if entry.id == subcategory_id
            ),
            None,
        )
        if subcategory is None:
            subcategory = Subcategory(
                id=subcategory_id,
                name=name,
                emoji=emoji,
                description=description,
                sort_order=sort_order,
            )
            category.subcategories.append(subcategory)
            self.all_ids.add(subcategory_id)
        return subcategory

    def _locate(self, card: SourceCard, kind: str) -> CatalogItem | None:
        item = self.by_tech.get(card.number)
        if item is not None:
            return item
        key = _normalized(_display_name(card, kind))
        aliases = {
            "mangosour": "mango-sayer",
            "berryblumба": "berry-bloom-na",
            "oldfashion": "old-fashioned",
            "hirosima": "hiroshima",
            "кивипанч": "kiwi-punch",
            "каманинськатигриця": "kamanin-tigress",
            "strawberrypornstar": "pornstar-strawb",
        }
        alias = aliases.get(key)
        if alias is not None and alias in self.by_id:
            return self.by_id[alias]
        candidates = self.by_name.get(key, [])
        return candidates[0] if len(candidates) == 1 else None

    def _container(
        self,
        category_id: str,
        subcategory_id: str | None,
    ) -> list[CatalogItem]:
        category = self.categories[category_id]
        if subcategory_id is None:
            return category.items
        return next(
            subcategory.items
            for subcategory in category.subcategories
            if subcategory.id == subcategory_id
        )

    @staticmethod
    def _source_fields(card: SourceCard) -> dict[str, str]:
        fields = {
            "tech_card_number": card.number,
            "tech_card_date": card.date,
        }
        if card.yield_text:
            fields["yield"] = card.yield_text
        return fields

    @staticmethod
    def _source_description(card: SourceCard) -> str:
        description = (
            f"Состав перенесён из технологической карты № {card.number} "
            f"от {card.date}."
        )
        if card.yield_text:
            description += f" Расчётный выход: {card.yield_text}."
        return description

    def apply(
        self,
        card: SourceCard,
        kind: str,
        category_id: str,
        subcategory_id: str | None,
        tags: list[str],
    ) -> None:
        if card.number in self.selected_numbers:
            raise RuntimeError(f"Повтор номера техкарты в импорте: {card.number}")
        self.selected_numbers.add(card.number)
        item = self._locate(card, kind)
        if item is not None:
            if item.id not in TEXT_SOURCE_RECIPE_IDS:
                item.ingredients = card.ingredients
            item.fields = {**item.fields, **self._source_fields(card)}
            if item.description and item.description.startswith(
                "Состав перенесён из технологической карты"
            ):
                item.description = self._source_description(card)
            self.by_tech[card.number] = item
            self.matched.append((card.title, item.id, card.number))
            return

        name = _display_name(card, kind)
        item_id = generate_stable_id(
            name,
            self.all_ids,
            MAX_STABLE_ID_LENGTH,
        )
        self.all_ids.add(item_id)
        container = self._container(category_id, subcategory_id)
        item = CatalogItem(
            id=item_id,
            name=name,
            short_description="Позиция из актуальной технологической карты бара.",
            description=self._source_description(card),
            ingredients=card.ingredients,
            fields=self._source_fields(card),
            steps=[],
            tags=tags,
            sort_order=(len(container) + 1) * 10,
        )
        container.append(item)
        self.by_id[item_id] = item
        self.by_tech[card.number] = item
        self.by_name[_normalized(name)].append(item)
        self.created.append((name, item_id, category_id, subcategory_id))

    def import_all(self) -> Catalog:
        self.ensure_structure()
        bar_cards = parse_cards(BAR_FILE)
        for card in bar_cards:
            section = _bar_section(card)
            if section is None:
                continue
            category_id, subcategory_id = section
            kind = (
                "wine"
                if category_id == "wine"
                else "cocktail"
                if category_id == "cocktails"
                else "shot"
                if subcategory_id == "shots"
                else "bar"
            )
            tags = [self.categories[category_id].name.casefold()]
            if subcategory_id is not None:
                tags.append(
                    next(
                        subcategory.name.casefold()
                        for subcategory in self.categories[
                            category_id
                        ].subcategories
                        if subcategory.id == subcategory_id
                    )
                )
            self.apply(card, kind, category_id, subcategory_id, tags)

        for card in parse_cards(PF_FILE):
            self.apply(
                card,
                "pf",
                "polufabrikaty_p_f",
                _pf_subcategory(card),
                ["п/ф", "полуфабрикат", "барная заготовка"],
            )

        new_ids = {
            item.id
            for category in self.catalog.categories
            for item in category.items
        }
        new_ids.update(
            item.id
            for category in self.catalog.categories
            for subcategory in category.subcategories
            for item in subcategory.items
        )
        if not self.old_ids <= new_ids:
            raise RuntimeError("Импорт потерял существующий стабильный ID")
        return Catalog.model_validate(self.catalog.model_dump(mode="python"))

    def print_summary(self, bar_total: int, pf_total: int) -> None:
        total_items = sum(
            len(category.items)
            + sum(len(subcategory.items) for subcategory in category.subcategories)
            for category in self.catalog.categories
        )
        print(f"Исходные карточки: бар={bar_total}, п/ф={pf_total}")
        print(f"Использовано: {len(self.selected_numbers)}")
        print(f"Обновлено существующих: {len(self.matched)}")
        print(f"Создано новых: {len(self.created)}")
        print(f"Итого карточек в каталоге: {total_items}")
        print("Распределение:")
        for category in sorted(
            self.catalog.categories,
            key=lambda entry: entry.sort_order,
        ):
            direct_count = len(category.items)
            print(f"- {category.name}: напрямую {direct_count}")
            for subcategory in sorted(
                category.subcategories,
                key=lambda entry: entry.sort_order,
            ):
                print(f"  - {subcategory.name}: {len(subcategory.items)}")
        duplicate_names = Counter(
            item.name.casefold()
            for category in self.catalog.categories
            for item in category.items
        )
        duplicate_names.update(
            item.name.casefold()
            for category in self.catalog.categories
            for subcategory in category.subcategories
            for item in subcategory.items
        )
        print(
            "Повторяющихся отображаемых названий: "
            f"{sum(count > 1 for count in duplicate_names.values())}"
        )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--write",
        action="store_true",
        help="атомарно записать итоговый catalog.json; без флага только проверка",
    )
    args = parser.parse_args()

    storage = JsonCatalogStorage(CATALOG_FILE)
    importer = CatalogImporter(storage.load())
    catalog = importer.import_all()
    if args.write:
        storage.save(catalog)
        print("Каталог сохранён атомарно с резервной копией.")
    else:
        print("Проверочный режим: catalog.json не изменён.")
    importer.print_summary(
        bar_total=len(parse_cards(BAR_FILE)),
        pf_total=len(parse_cards(PF_FILE)),
    )


if __name__ == "__main__":
    main()
