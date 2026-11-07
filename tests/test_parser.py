from datetime import datetime

import pytest

from bot.models import WorkType
from bot.services.parser import detect_work_type, parse_task

NOW = datetime(2026, 11, 7, 10, 0)  # суббота


def test_reference_phrase_is_exact():
    """Эталон из ТЗ — сохраняется сразу, без подтверждения."""
    p = parse_task("Покраска, заказ 123, срок пятница, ответственный Иван", NOW)
    assert p.work_type is WorkType.PAINTING
    assert p.order_no == "123"
    assert p.due == datetime(2026, 11, 13, 18, 0)
    assert p.assignee == "Иван"
    assert p.exact and not p.needs_confirmation

    task = p.to_task(chat_id=42)
    assert (task.chat_id, task.is_outdoor) == (42, False)


def test_mounting_with_number_sign_and_full_name():
    p = parse_task("Монтаж окон, заказ №45, срок 15.11.2026, ответственный Пётр Сидоров.", NOW)
    assert p.work_type is WorkType.MOUNTING
    assert (p.order_no, p.assignee) == ("45", "Пётр Сидоров")
    assert p.due == datetime(2026, 11, 15, 18, 0)
    assert not p.needs_confirmation
    assert p.to_task(1).is_outdoor


def test_lowercase_semicolons_genitive():
    p = parse_task("демонтаж; заказ 7-А; срок до среды; ответственная Мария", NOW)
    assert p.work_type is WorkType.DISMANTLING
    assert p.order_no == "7-А"
    assert p.due == datetime(2026, 11, 11, 18, 0)
    assert p.assignee == "Мария"
    assert p.exact


def test_synonym_and_date_without_year():
    p = parse_task("Установка, заказ 12, срок 20.11, ответственный Анна", NOW)
    assert p.work_type is WorkType.MOUNTING
    assert p.due == datetime(2026, 11, 20, 18, 0)
    assert p.exact


def test_no_commas_parsed_softly_needs_confirmation():
    p = parse_task("Обслуживание заказ 88 срок завтра ответственный Олег", NOW)
    assert p.work_type is WorkType.MAINTENANCE
    assert (p.order_no, p.assignee) == ("88", "Олег")
    assert p.due == datetime(2026, 11, 8, 18, 0)
    assert not p.exact and p.needs_confirmation


def test_typo_in_work_type_asks_user():
    p = parse_task("Монтж, заказ 5, срок пятница, ответственный Иван", NOW)
    assert p.work_type is None
    assert p.missing_fields() == ["work_type"]
    assert p.needs_confirmation
    with pytest.raises(ValueError):
        p.to_task(1)


def test_partial_text_reports_missing_fields():
    p = parse_task("Покраска ворот срок понедельник", NOW)
    assert p.work_type is WorkType.PAINTING
    assert p.due == datetime(2026, 11, 9, 18, 0)
    assert p.missing_fields() == ["order_no", "assignee"]


def test_due_without_keyword_in_template():
    p = parse_task("Монтаж, заказ 45, 20.11.2026, ответственный Сеня", NOW)
    assert p.due == datetime(2026, 11, 20, 18, 0)
    assert (p.order_no, p.assignee) == ("45", "Сеня")
    assert p.exact and not p.needs_confirmation


def test_due_without_keyword_weekday():
    p = parse_task("Покраска, заказ 1, пятница, ответственный Иван", NOW)
    assert p.due == datetime(2026, 11, 13, 18, 0) and p.exact


def test_due_without_keyword_in_past_asks_confirmation():
    """Дата из прошлого (вчера) распознаётся, но требует подтверждения."""
    p = parse_task("Монтаж, заказ 45, 06.11.2026, ответственный Сеня", NOW)
    assert p.due == datetime(2026, 11, 6, 18, 0)
    assert p.due_in_past and p.needs_confirmation


def test_due_without_keyword_soft_mode():
    p = parse_task("Покраска заказ 5 ответственный Иван, 20.11", NOW)
    assert p.due == datetime(2026, 11, 20, 18, 0)
    assert p.assignee == "Иван"


def test_short_format_without_keywords():
    p = parse_task("Покраска, заказ 321, 20.11 12:00, Алекс", NOW)
    assert p.work_type is WorkType.PAINTING
    assert p.order_no == "321"
    assert p.due == datetime(2026, 11, 20, 12, 0)
    assert p.assignee == "Алекс"
    assert p.exact and not p.needs_confirmation


def test_assignee_keyword_still_supported():
    for text in ("Покраска, заказ 1, пятница, ответственный Иван",
                 "Покраска, заказ 1, пятница, исполнитель Иван",
                 "Покраска, заказ 1, пятница, Ответственный: Иван"):
        assert parse_task(text, NOW).assignee == "Иван", text


def test_swapped_fields_not_mixed_up():
    """Имя не должно попасть в срок, а «срок пятница» — в ответственного."""
    p = parse_task("Покраска, заказ 1, ответственный Иван, срок пятница", NOW)
    assert p.assignee == "Иван"
    assert p.due == datetime(2026, 11, 13, 18, 0)
    assert p.needs_confirmation  # не по шаблону — подтверждаем


def test_bad_due_in_short_format_keeps_assignee():
    p = parse_task("Покраска, заказ 1, когда-нибудь, Алекс", NOW)
    assert p.due is None and p.assignee == "Алекс"
    assert p.missing_fields() == ["due"]


def test_word_starting_with_do_is_not_deadline():
    p = parse_task("Обслуживание доводчика заказ 3 срок завтра ответственный Иван", NOW)
    assert p.due == datetime(2026, 11, 8, 18, 0)


def test_same_day_after_16_needs_choice():
    friday_evening = datetime(2026, 11, 13, 17, 0)
    p = parse_task("Покраска, заказ 1, срок пятница, ответственный Иван", friday_evening)
    assert p.due == datetime(2026, 11, 13, 16, 0)
    assert p.due_alternative == datetime(2026, 11, 20, 18, 0)
    assert p.exact and p.needs_confirmation


def test_past_date_needs_confirmation():
    p = parse_task("Покраска, заказ 1, срок 01.11.2026, ответственный Иван", NOW)
    assert p.due_in_past and p.needs_confirmation


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ТО кондиционера", WorkType.MAINTENANCE),
        ("сервисное обслуживание", WorkType.MAINTENANCE),
        ("окраска фасада", WorkType.PAINTING),
        ("снятие старых окон", WorkType.DISMANTLING),
        ("Демонтаж", WorkType.DISMANTLING),
        ("сделать то, что просили", None),  # «то» не путаем с ТО
        ("ремонт", None),
    ],
)
def test_detect_work_type(text, expected):
    assert detect_work_type(text) is expected
