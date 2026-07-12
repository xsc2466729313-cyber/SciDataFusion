"""M06 coverage evaluation and deterministic source selection."""

from scidatafusion.selection.integrity import (
    calculate_coverage_report_hash,
    calculate_search_gap_set_hash,
    calculate_selected_source_set_hash,
    calculate_selection_input_hash,
    calculate_source_selection_output_hash,
    verify_selection_request_integrity,
    verify_source_selection_integrity,
)
from scidatafusion.selection.projection import (
    applicable_categories,
    assess_license,
    candidate_claims,
    candidate_download_locators,
    claim_coverage_state,
)
from scidatafusion.selection.service import SourceSelectionService

__all__ = [
    "SourceSelectionService",
    "applicable_categories",
    "assess_license",
    "calculate_coverage_report_hash",
    "calculate_search_gap_set_hash",
    "calculate_selected_source_set_hash",
    "calculate_selection_input_hash",
    "calculate_source_selection_output_hash",
    "candidate_claims",
    "candidate_download_locators",
    "claim_coverage_state",
    "verify_selection_request_integrity",
    "verify_source_selection_integrity",
]
