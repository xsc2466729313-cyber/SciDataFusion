"""M10 native table parsing adapters and orchestration."""

from scidatafusion.tables.csv import CsvTableAdapter
from scidatafusion.tables.projection import table_to_polars, table_to_rows

__all__ = ["CsvTableAdapter", "table_to_polars", "table_to_rows"]
