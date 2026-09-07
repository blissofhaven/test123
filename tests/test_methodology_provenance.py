# -*- coding: utf-8 -*-
"""Происхождение коэффициентов, статус профиля и снимок применённых значений.

Этап A2. Проверяется не «число равно 1,2», а то, что число нельзя выдать за
более достоверное, чем оно есть:

* статус профиля — заявка, и профиль обязан ей отвечать;
* у каждого применённого коэффициента есть происхождение, и оно доходит до
  протокола;
* результат расчёта хранит снимок коэффициентов, а не ссылку на файл, который
  завтра может быть другим.
"""
import copy
import json
from pathlib import Path

import pytest

from rza_calc.core.engine import run
from rza_calc.core.methodology import (CONFIRMED, ORIGINS, PLACEHOLDER,
                                       PROJECT, TYPICAL, UNCONFIRMED,
                                       Methodology, MethodologyError)
from rza_calc.io.project import load

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROFILE = ROOT / "rza_calc" / "data" / "methodology_default.json"
PROJECT_TEMPLATE = ROOT / "rza_calc" / "data" / "methodology_project_template.json"
EXAMPLE = ROOT / "tests" / "fixtures" / "legacy_projects" / "ps_severnaya.json"


def _default() -> Methodology:
    return Methodology.load(DEFAULT_PROFILE)


# ── статус как заявка ─────────────────────────────────────────────────────
def test_shipped_profile_supports_the_status_it_claims():
    """Профиль в поставке заявляет ТИПОВОЙ и отвечает этой заявке."""
    methodology = _default()
    assert methodology.status == TYPICAL
    assert methodology.status_claim_errors() == []
    assert methodology.blocking_errors() == []


def test_placeholder_claims_nothing_and_is_never_blocked_for_provenance():
    """ЗАГЛУШКА ничего не утверждает, поэтому происхождение с неё не спрашивают.

    Иначе заглушку нельзя было бы использовать по прямому назначению — как
    заведомо неготовый профиль, с которым всё же можно посчитать и увидеть
    предупреждение.
    """
    methodology = _default()
    methodology.data["status"] = PLACEHOLDER
    for section in ("mtz", "to", "ozz"):
        for node in methodology.data[section].values():
            if isinstance(node, dict):
                node.pop("origin", None)
                node.pop("source", None)
    assert methodology.status_claim_errors() == []
    assert methodology.is_placeholder


def test_typical_status_requires_a_named_source_for_every_coefficient():
    """Убрать источник, оставив статус ТИПОВОЙ, — заявить несуществующее."""
    methodology = _default()
    methodology.data["mtz"]["k_ots"]["source"] = "   "
    problems = methodology.status_claim_errors()
    assert any("mtz.k_ots" in text and "источник" in text for text in problems)
    assert any("понизьте статус" in text for text in problems), (
        "сообщение обязано называть законный выход — понизить статус, — иначе "
        "единственным способом пройти проверку останется выдумать источник"
    )
    assert methodology.blocking_errors(), "ложная заявка обязана блокировать расчёт"


def test_unknown_origin_is_rejected_instead_of_being_accepted_as_free_text():
    methodology = _default()
    methodology.data["mtz"]["k_ots"]["origin"] = "откуда-то"
    problems = methodology.status_claim_errors()
    assert any("mtz.k_ots" in text and "происхождение" in text for text in problems)


def test_project_status_requires_confirmed_sources_and_a_named_approver():
    """ПРОЕКТНЫЙ — самая сильная заявка, и она проверяется полностью."""
    methodology = _default()
    methodology.data["status"] = PROJECT
    problems = methodology.status_claim_errors()
    assert any(UNCONFIRMED in text for text in problems)
    assert any("approval.approved_by" in text for text in problems)
    assert any("approval.approved_on" in text for text in problems)
    assert any("approval.object" in text for text in problems)


def test_filled_project_profile_passes():
    """Заполненный проектный профиль проходит — иначе статус недостижим."""
    methodology = _default()
    methodology.data["status"] = PROJECT
    methodology.data["approval"] = {
        "approved_by": "Иванов И. И., главный специалист РЗА",
        "approved_on": "2026-09-01",
        "object": "ПС 110/10 кВ «Северная»",
    }
    for path in methodology.DOCUMENTED:
        node = methodology._node(path)
        target = node if "source" in node else node.get("meta", node)
        target["source_status"] = CONFIRMED
    assert methodology.status_claim_errors() == []
    assert methodology.blocking_errors() == []
    assert not methodology.requires_approval


def test_shipped_project_template_refuses_to_run_until_it_is_filled():
    """Шаблон проектной методики намеренно не проходит проверку.

    Шаблон, который считается «из коробки», — это приглашение поставить
    статус ПРОЕКТНЫЙ, ничего не сделав.
    """
    template = Methodology.load(PROJECT_TEMPLATE)
    assert template.status == PROJECT
    problems = template.blocking_errors()
    assert problems
    assert any("approval.approved_by" in text for text in problems)


# ── происхождение доходит до протокола ────────────────────────────────────
def test_every_documented_coefficient_has_its_own_provenance():
    """Происхождение берётся у самого коэффициента, а не одно на весь профиль."""
    methodology = _default()
    sources = set()
    for path in methodology.DOCUMENTED:
        prov = methodology.provenance(path)
        assert prov.origin in ORIGINS, path
        assert prov.source.strip(), path
        sources.add(prov.source)
    assert len(sources) > 1, (
        "если у всех коэффициентов один источник, поле «источник» ничего не "
        "различает и его наличие вводит в заблуждение"
    )


def test_citation_names_the_coefficient_and_its_confirmation_state():
    methodology = _default()
    citation = methodology.cite("sensitivity.kch_mtz_main")
    assert "sensitivity.kch_mtz_main" in citation
    assert "нормативное требование" in citation
    assert "ПУЭ" in citation
    assert UNCONFIRMED in citation, (
        "несверенная ссылка обязана выглядеть несверенной прямо в протоколе"
    )


def test_no_source_field_pretends_to_cite_an_exact_clause():
    """Ни один источник не содержит выдуманного номера пункта.

    Программа не сверяла тексты документов. Ссылка вида «ПУЭ п. 3.2.26»
    выглядела бы проверенной и была бы опаснее честного «ПУЭ, глава 3.2»,
    потому что её никто не стал бы перепроверять.
    """
    import re

    methodology = _default()
    pattern = re.compile(r"\b(п\.|пункт|таблиц\w*\s*№?\s*\d)", re.IGNORECASE)
    offenders = [
        path for path in methodology.DOCUMENTED
        if pattern.search(methodology.provenance(path).source)
    ]
    assert not offenders, (
        "источники с номерами пунктов, не сверенными с документом: "
        + ", ".join(offenders)
    )


def test_protocol_line_carries_the_provenance_of_the_coefficient_used():
    """Строка «источник» в шаге протокола относится к своему коэффициенту."""
    net, methodology, _ = load(EXAMPLE)
    result = run(net, methodology)
    for protection in result.all_results():
        for step in protection.steps:
            if step.source and "k_ots" in step.source:
                assert "профиль" in step.source
                return
    pytest.fail("ни один шаг протокола не сослался на коэффициент отстройки")


# ── снимок применённых коэффициентов ──────────────────────────────────────
def test_result_keeps_an_immutable_snapshot_of_applied_coefficients():
    net, methodology, _ = load(EXAMPLE)
    result = run(net, methodology)
    snapshot = result.calculation_case.methodology_snapshot

    assert snapshot is not None
    assert snapshot.value("mtz.k_ots") == methodology.k("mtz.k_ots")
    assert snapshot.entry("mtz.k_ots").provenance.origin == "типовая практика"
    with pytest.raises(Exception):
        snapshot.entries = ()          # frozen dataclass


def test_snapshot_survives_a_later_edit_of_the_profile_file():
    """Главное, ради чего снимок существует.

    Файл методики внешний. Если после расчёта его отредактировать, протокол
    обязан продолжать показывать те коэффициенты, по которым он получен, а не
    новые.
    """
    net, methodology, _ = load(EXAMPLE)
    result = run(net, methodology)
    snapshot = result.calculation_case.methodology_snapshot
    before = snapshot.value("mtz.k_ots")

    methodology.data["mtz"]["k_ots"]["value"] = 9.99

    assert snapshot.value("mtz.k_ots") == before
    assert methodology.k("mtz.k_ots") == 9.99
    assert not result.is_current_for(net, methodology), (
        "изменённый профиль обязан помечать прежний результат как устаревший"
    )


def test_snapshot_covers_everything_that_influences_the_numbers():
    """Снимок без части применённых величин не воспроизводит расчёт."""
    methodology = _default()
    snapshot = methodology.snapshot()
    paths = {item.path for item in snapshot.entries}
    for required in methodology.REQUIRED + methodology.REQUIRED_CHOICES:
        assert required in paths, required
    assert snapshot.u_avg, "таблица средних напряжений — часть базиса расчёта"
    assert snapshot.ct_scale, "шкала уставок влияет на принятое значение"
    assert snapshot.ct_round_mode in ("up", "nearest")


def test_snapshot_is_serialisable_without_losing_provenance():
    snapshot = _default().snapshot()
    restored = json.loads(json.dumps(snapshot.as_dict(), ensure_ascii=False))
    entry = next(item for item in restored["entries"] if item["path"] == "mtz.k_v")
    assert entry["origin"] == "паспорт оборудования"
    assert entry["source_status"] in (UNCONFIRMED, CONFIRMED)


# ── параметр-выбор ────────────────────────────────────────────────────────
def test_choice_parameter_rejects_an_unknown_option():
    """Неизвестный вариант — ошибка, а не повод молча взять умолчание."""
    methodology = _default()
    methodology.data["to"]["i_nom_basis"]["value"] = "как-нибудь"
    with pytest.raises(MethodologyError, match="допустимых"):
        methodology.text("to.i_nom_basis")
    assert methodology.blocking_errors()


def test_numeric_reader_refuses_a_choice_parameter():
    """`k()` не должна возвращать float от строки-варианта."""
    methodology = _default()
    with pytest.raises(MethodologyError):
        methodology.k("to.i_nom_basis")
