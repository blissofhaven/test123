# -*- coding: utf-8 -*-
"""Adapters at the boundary of the canonical domain model."""

from .legacy_calculation import (
    AdaptationResult,
    AdapterDiagnostic,
    CalculationTrace,
    LegacyCalculationAdapterError,
    LegacyImportError,
    adapt_to_calculation,
    import_legacy_network,
    legacy_equipment_types,
)

__all__ = [
    "AdaptationResult",
    "AdapterDiagnostic",
    "CalculationTrace",
    "LegacyCalculationAdapterError",
    "LegacyImportError",
    "adapt_to_calculation",
    "import_legacy_network",
    "legacy_equipment_types",
]
