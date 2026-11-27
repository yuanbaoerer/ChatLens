"""Concrete deterministic analyzers registered by the analysis service."""
from analytics.analyzers.media_duplicates import MediaDuplicateAnalyzer
from analytics.analyzers.text_links import TextLinkDuplicateAnalyzer

__all__ = ["MediaDuplicateAnalyzer", "TextLinkDuplicateAnalyzer"]
