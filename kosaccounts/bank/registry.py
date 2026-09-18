"""Bank statement parser registry.

Each parser module exposes a class implementing BankParser. `parse_statement` tries registered parsers
in order; the first whose can_parse() returns True wins. If none claim the file, the model fallback is
used and flagged=True is returned so the ledger records "Flagged" and the summary asks for a sample.

Adding a bank = one new module + one entry in PARSERS.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Protocol

from kosaccounts.claude_client import ClaudeClient, ClaudeError
from kosaccounts.config import Config
from kosaccounts.models import BankTxn

from kosaccounts.bank.csv_generic import CsvGenericParser
from kosaccounts.bank.html_generic import HtmlGenericParser
from kosaccounts.bank.model_fallback import extract_with_model

logger = logging.getLogger(__name__)


class BankParser(Protocol):
    name: str

    def can_parse(self, path: Path) -> bool: ...

    def parse(self, path: Path, source_file: str) -> list[BankTxn]: ...


PARSERS: list[BankParser] = [HtmlGenericParser(), CsvGenericParser()]


def parse_statement(
    path: Path,
    source_file: str,
    cfg: Config,
    client: Optional[ClaudeClient] = None,
) -> tuple[list[BankTxn], bool]:
    """Returns (transactions, flagged). flagged=True when the model fallback was used or no parser
    and no client were available (then transactions is empty)."""
    for parser in PARSERS:
        try:
            if not parser.can_parse(path):
                continue
        except Exception:
            logger.warning("bank parser %s: can_parse raised", getattr(parser, "name", parser), exc_info=True)
            continue

        try:
            return parser.parse(path, source_file), False
        except Exception:
            logger.warning("bank parser %s: parse raised, trying next parser", getattr(parser, "name", parser), exc_info=True)
            continue

    if client is None:
        return [], True

    try:
        return extract_with_model(path, source_file, client), True
    except ClaudeError:
        logger.warning("bank model fallback failed for %s", source_file, exc_info=True)
        return [], True
