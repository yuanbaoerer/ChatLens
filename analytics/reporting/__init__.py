"""Report narrative composition for ChatLens analytics."""
from analytics.reporting.summary import (
    SummaryComposer,
    SummaryConfig,
    SummaryGroundingError,
    SummaryInputError,
    SummaryResult,
    build_facts,
)

__all__ = [
    "SummaryComposer",
    "SummaryConfig",
    "SummaryGroundingError",
    "SummaryInputError",
    "SummaryResult",
    "build_facts",
]
