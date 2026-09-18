"""Supplier normalisation and matching.

Files (all in data/):
- suppliers.csv: Supplier, Default category, Other categories (";"-separated), Count, Last date (YYYY-MM-DD)
- supplier_aliases.csv: Alias, Supplier  (alias is the raw string as seen; matched via normalise())
- new_suppliers_pending.csv: Supplier, Suggested category, First seen (ISO datetime), Source file, Context, Occurrences

Matching tiers (thresholds from config.suppliers):
1. normalise(name) equals normalise(alias)  -> method "alias", score 100
2. normalise(name) equals normalise(supplier) -> "exact", 100
3. rapidfuzz token_set_ratio best score >= auto_match_threshold -> "fuzzy"
4. best score in [model_review_threshold, auto) and client is not None -> ask model to pick among
   top-5 candidates or answer NEW -> "model" (or "new")
5. otherwise -> "new"; recorded in pending file (dedup by normalise(), bump Occurrences)
Successful fuzzy/model matches are written back as aliases so the model is never asked twice.

CONTRACT (implement; do not change signatures):
"""

from __future__ import annotations

import csv
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Optional

from rapidfuzz import fuzz, process

from kosaccounts.categories import Categories
from kosaccounts.claude_client import ClaudeClient, ClaudeError
from kosaccounts.config import Config
from kosaccounts.models import SupplierMatch

# Kept in sync with SuppliersConfig.legal_suffixes' default factory.
DEFAULT_LEGAL_SUFFIXES = ["inc", "llc", "ltd", "limited", "corp", "co", "company", "plc"]


def normalise(name: str, legal_suffixes: Optional[list[str]] = None) -> str:
    """Lowercase; strip punctuation to spaces; drop legal suffixes (inc, llc, ltd, ...);
    collapse whitespace. 'C&C Button Inc' -> 'c c button'; 'T-Mobile' -> 't mobile'."""
    if name is None:
        return ""
    suffixes = set(s.lower() for s in (legal_suffixes if legal_suffixes is not None else DEFAULT_LEGAL_SUFFIXES))

    lowered = name.lower()
    tokens: list[str] = []
    current: list[str] = []
    for ch in lowered:
        if ch.isalnum():
            current.append(ch)
        else:
            if current:
                tokens.append("".join(current))
                current = []
    if current:
        tokens.append("".join(current))

    if not tokens:
        return ""

    filtered = [t for t in tokens if t not in suffixes]
    if filtered:
        tokens = filtered
    # else: dropping suffixes would leave the name empty, so keep the original tokens.

    if len(tokens) > 1 and tokens[0] == "the":
        tokens = tokens[1:]

    return " ".join(tokens)


# Leading tokens that card processors / banks prepend to merchant names in statement descriptors.
PROCESSOR_PREFIXES = {
    "sq", "tst", "pp", "paypal", "pos", "dbt", "crd", "chkcard", "checkcard", "purchase",
    "debit", "card", "ach", "pmt", "pymt", "recurring", "ext", "int",
    "recur", "online", "web", "intl", "chk", "ck", "eft", "pre", "auth", "authorized",
    "visa", "mc", "mastercard", "discover", "amex",
}


def strip_processor_prefix(tokens: list[str]) -> list[str]:
    """Drop leading processor tokens ('sq mood fabrics' -> ['mood', 'fabrics']); never drop all."""
    i = 0
    while i < len(tokens) - 1 and tokens[i] in PROCESSOR_PREFIXES:
        i += 1
    return tokens[i:]


def _contains_token_sequence(haystack: list[str], needle: list[str]) -> bool:
    """True if `needle` appears as a contiguous run inside `haystack`."""
    n, m = len(haystack), len(needle)
    if m == 0 or m > n:
        return False
    for i in range(n - m + 1):
        if haystack[i : i + m] == needle:
            return True
    return False


class SupplierBook:
    def __init__(
        self,
        cfg: Config,
        categories: Categories,
        client: Optional[ClaudeClient] = None,
    ) -> None:
        """Loads suppliers.csv and supplier_aliases.csv from cfg.paths. `client=None` disables tier 4
        (grey-zone names become new)."""
        self.cfg = cfg
        self.categories = categories
        self.client = client
        self._legal_suffixes = cfg.suppliers.legal_suffixes

        self._suppliers: dict[str, dict[str, str]] = {}
        suppliers_path = cfg.paths.suppliers
        if suppliers_path.exists():
            with suppliers_path.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    supplier = (row.get("Supplier") or "").strip()
                    if not supplier or supplier in self._suppliers:
                        continue
                    self._suppliers[supplier] = row

        # normalised alias -> (raw alias text, canonical)
        self._alias_index: dict[str, tuple[str, str]] = {}
        aliases_path = cfg.paths.aliases
        if aliases_path.exists():
            with aliases_path.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    alias = (row.get("Alias") or "").strip()
                    canonical = (row.get("Supplier") or "").strip()
                    if not alias or not canonical:
                        continue
                    norm_alias = self._norm(alias)
                    if norm_alias not in self._alias_index:
                        self._alias_index[norm_alias] = (alias, canonical)

        # normalised canonical name -> pending row, keyed for dedup
        self._pending: dict[str, dict[str, object]] = {}
        pending_path = cfg.paths.pending_suppliers
        if pending_path.exists():
            with pending_path.open("r", newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                for row in reader:
                    supplier = (row.get("Supplier") or "").strip()
                    if not supplier:
                        continue
                    norm = self._norm(supplier)
                    try:
                        occurrences = int(row.get("Occurrences") or "1")
                    except ValueError:
                        occurrences = 1
                    self._pending[norm] = {
                        "Supplier": supplier,
                        "Suggested category": (row.get("Suggested category") or "").strip(),
                        "First seen": row.get("First seen") or "",
                        "Source file": row.get("Source file") or "",
                        "Context": row.get("Context") or "",
                        "Occurrences": occurrences,
                    }

        # precomputed (normalised name, canonical) pairs for fuzzy matching
        self._fuzzy_pairs: list[tuple[str, str]] = [
            (self._norm(canonical), canonical) for canonical in self._suppliers.keys()
        ]
        self._canon_by_norm: dict[str, str] = {}
        for norm, canonical in self._fuzzy_pairs:
            if norm not in self._canon_by_norm:
                self._canon_by_norm[norm] = canonical

        # compact (no-space) forms, for descriptors that squash names: "TMOBILE*AUTO PAY".
        # Canonicals and aliases alike; minimum 5 chars so short words can't collide.
        self._canon_by_compact: dict[str, str] = {}
        for norm, canonical in self._fuzzy_pairs:
            compact = norm.replace(" ", "")
            if len(compact) >= 5 and compact not in self._canon_by_compact:
                self._canon_by_compact[compact] = canonical
        for norm_alias, (_raw, canonical) in self._alias_index.items():
            compact = norm_alias.replace(" ", "")
            if len(compact) >= 5 and compact not in self._canon_by_compact:
                self._canon_by_compact[compact] = canonical

    def _count(self, canonical: str) -> int:
        """Purchase count from suppliers.csv; used to break ties toward the usual supplier."""
        try:
            return int((self._suppliers.get(canonical) or {}).get("Count") or 0)
        except ValueError:
            return 0

    def _norm(self, name: str) -> str:
        return normalise(name, self._legal_suffixes)

    @property
    def names(self) -> list[str]:
        return list(self._suppliers.keys())

    def default_category(self, canonical: str) -> Optional[str]:
        row = self._suppliers.get(canonical)
        if not row:
            return None
        value = (row.get("Default category") or "").strip()
        return value or None

    def match(self, name: str, context: str = "", source_file: str = "") -> SupplierMatch:
        """Resolve `name`. `context` (e.g. receipt line summary or bank description) is passed to the
        model in tier 4 and stored in the pending file for new suppliers. For new suppliers the model
        (if available) suggests a category restricted to categories.names; else default_category=None."""
        if name is None or not str(name).strip():
            return SupplierMatch(
                input_name=name,
                canonical=None,
                default_category=None,
                score=0,
                method="new",
                is_new=True,
            )

        norm = self._norm(name)

        # Tier 1: alias
        alias_hit = self._alias_index.get(norm)
        if alias_hit is not None:
            canonical = alias_hit[1]
            return SupplierMatch(
                input_name=name,
                canonical=canonical,
                default_category=self.default_category(canonical),
                score=100.0,
                method="alias",
                is_new=False,
            )

        # Tier 2: exact
        exact_canonical = self._canon_by_norm.get(norm)
        if exact_canonical is not None:
            return SupplierMatch(
                input_name=name,
                canonical=exact_canonical,
                default_category=self.default_category(exact_canonical),
                score=100.0,
                method="exact",
                is_new=False,
            )

        # Tier 3: fuzzy (+ whole-token containment bonus)
        candidates: list[tuple[str, float]] = []
        if self._fuzzy_pairs:
            name_tokens = norm.split()
            lead_tokens = strip_processor_prefix(name_tokens)
            norm_by_canonical = {c: n for n, c in self._fuzzy_pairs}

            def _leads(canon_tokens: list[str]) -> bool:
                return lead_tokens[: len(canon_tokens)] == canon_tokens

            choices = [p[0] for p in self._fuzzy_pairs]
            extracted = process.extract(norm, choices, scorer=fuzz.token_set_ratio, limit=10)
            for _choice_str, score, idx in extracted:
                canonical_name = self._fuzzy_pairs[idx][1]
                canon_tokens = self._fuzzy_pairs[idx][0].split()
                # token_set_ratio gives 100 whenever every token of the canonical appears in the
                # input. For a one-word supplier ("Mood", "Delta") that is too weak: "GOOD MOOD
                # CAFE" is not Mood. A one-word supplier must lead the descriptor (after processor
                # prefixes); otherwise fall back to the strict whole-string ratio.
                if len(canon_tokens) == 1 and len(name_tokens) > 1 and not _leads(canon_tokens):
                    score = fuzz.ratio(norm, self._fuzzy_pairs[idx][0])
                candidates.append((canonical_name, float(score)))

            # Compact-form lead match: "tmobile auto pay" -> T-Mobile ("t mobile" -> "tmobile").
            compact_hit = self._canon_by_compact.get(lead_tokens[0]) if lead_tokens else None
            if compact_hit is not None:
                candidates = [(c, s) for c, s in candidates if c != compact_hit]
                candidates.append((compact_hit, 96.0))

            best_containment: Optional[tuple[str, str]] = None  # (canonical, norm_canonical)
            for norm_canon, canonical_name in self._fuzzy_pairs:
                if len(norm_canon) < 4:
                    continue
                canon_tokens = norm_canon.split()
                if len(canon_tokens) == 1 and not _leads(canon_tokens):
                    continue
                if _contains_token_sequence(name_tokens, canon_tokens):
                    if best_containment is None or len(norm_canon) > len(best_containment[1]):
                        best_containment = (canonical_name, norm_canon)

            if best_containment is not None:
                bc_canonical, _ = best_containment
                merged = False
                boosted: list[tuple[str, float]] = []
                for c_name, c_score in candidates:
                    if c_name == bc_canonical:
                        boosted.append((c_name, max(c_score, 95.0)))
                        merged = True
                    else:
                        boosted.append((c_name, c_score))
                if not merged:
                    boosted.append((bc_canonical, 95.0))
                candidates = boosted

            # Ties (e.g. "Amazon" vs "Amazon.com" both at 100) go to the more frequent supplier,
            # then to the longer name.
            candidates.sort(
                key=lambda c: (-c[1], -self._count(c[0]), -len(norm_by_canonical.get(c[0], "")))
            )

        best_canonical = candidates[0][0] if candidates else None
        best_score = candidates[0][1] if candidates else 0.0

        auto_threshold = self.cfg.suppliers.auto_match_threshold
        model_threshold = self.cfg.suppliers.model_review_threshold

        if best_canonical is not None and best_score >= auto_threshold:
            self.record_alias(name, best_canonical)
            return SupplierMatch(
                input_name=name,
                canonical=best_canonical,
                default_category=self.default_category(best_canonical),
                score=best_score,
                method="fuzzy",
                is_new=False,
            )

        if (
            best_canonical is not None
            and self.client is not None
            and model_threshold <= best_score < auto_threshold
        ):
            top5 = candidates[:5]
            chosen = self._ask_model_for_choice(name, context, top5)
            if chosen is not None:
                self.record_alias(name, chosen)
                chosen_score = next((s for c, s in top5 if c == chosen), best_score)
                return SupplierMatch(
                    input_name=name,
                    canonical=chosen,
                    default_category=self.default_category(chosen),
                    score=chosen_score,
                    method="model",
                    is_new=False,
                )
            # choice was 0 (none) or the model errored: fall through to "new"

        return self._handle_new(name, norm, context, source_file, fallback_score=best_score)

    def _ask_model_for_choice(
        self, name: str, context: str, top5: list[tuple[str, float]]
    ) -> Optional[str]:
        lines = "\n".join(f"{i + 1}. {canonical}" for i, (canonical, _score) in enumerate(top5))
        prompt = (
            "You are matching a raw supplier/vendor string (from a receipt or bank statement) to a "
            "list of known suppliers for a bookkeeping pipeline.\n"
            f"Raw name: {name}\n"
            f"Context: {context}\n"
            "Candidates:\n"
            f"{lines}\n"
            "Reply with ONLY a JSON object of the exact form "
            '{"choice": <candidate number 1-' + str(len(top5)) + ', or 0 if none match>, '
            '"reason": "<short reason>"}. No prose, no markdown fences.'
        )
        try:
            response = self.client.run_json(prompt)
        except ClaudeError:
            return None

        choice = response.get("choice") if isinstance(response, dict) else None
        try:
            choice_i = int(choice)
        except (TypeError, ValueError):
            return None
        if choice_i < 1 or choice_i > len(top5):
            return None
        return top5[choice_i - 1][0]

    def _ask_model_for_category(self, name: str, context: str) -> Optional[str]:
        category_names = self.categories.names
        lines = "\n".join(f"{i + 1}. {c}" for i, c in enumerate(category_names))
        prompt = (
            "A new, unrecognised supplier/vendor was found while processing bookkeeping records.\n"
            f"Supplier name: {name}\n"
            f"Context: {context}\n"
            "Choose the single best-fitting expense category from this list, or null if none fit "
            "well. Use the EXACT spelling from the list.\n"
            f"{lines}\n"
            'Reply with ONLY a JSON object of the exact form {"category": "<exact category name>"} '
            'or {"category": null}. No prose, no markdown fences.'
        )
        try:
            response = self.client.run_json(prompt)
        except ClaudeError:
            return None

        category = response.get("category") if isinstance(response, dict) else None
        if category and self.categories.is_valid(category):
            return self.categories.canonical(category)
        return None

    def _handle_new(
        self,
        name: str,
        norm: str,
        context: str,
        source_file: str,
        fallback_score: float = 0.0,
    ) -> SupplierMatch:
        existing = self._pending.get(norm)
        if existing is not None:
            existing["Occurrences"] = int(existing.get("Occurrences", 1)) + 1  # type: ignore[arg-type]
            suggested = existing.get("Suggested category") or None
            return SupplierMatch(
                input_name=name,
                canonical=None,
                default_category=suggested,  # type: ignore[arg-type]
                score=fallback_score,
                method="new",
                is_new=True,
            )

        suggested_category: Optional[str] = None
        if self.client is not None:
            suggested_category = self._ask_model_for_category(name, context)

        self._pending[norm] = {
            "Supplier": str(name).strip(),
            "Suggested category": suggested_category or "",
            "First seen": datetime.now().isoformat(timespec="seconds"),
            "Source file": source_file or "",
            "Context": context or "",
            "Occurrences": 1,
        }
        return SupplierMatch(
            input_name=name,
            canonical=None,
            default_category=suggested_category,
            score=fallback_score,
            method="new",
            is_new=True,
        )

    def record_alias(self, alias: str, canonical: str) -> None:
        if self._norm(alias) == self._norm(canonical):
            return
        norm_alias = self._norm(alias)
        if norm_alias in self._alias_index:
            return
        self._alias_index[norm_alias] = (alias, canonical)

    def save(self) -> None:
        """Persist aliases and pending files (atomic write: temp + rename)."""
        alias_rows = sorted(self._alias_index.values(), key=lambda pair: pair[0].lower())
        self._write_csv_atomic(self.cfg.paths.aliases, ["Alias", "Supplier"], alias_rows)

        pending_rows = [
            (
                row["Supplier"],
                row["Suggested category"],
                row["First seen"],
                row["Source file"],
                row["Context"],
                row["Occurrences"],
            )
            for row in self._pending.values()
        ]
        self._write_csv_atomic(
            self.cfg.paths.pending_suppliers,
            ["Supplier", "Suggested category", "First seen", "Source file", "Context", "Occurrences"],
            pending_rows,
        )

    @staticmethod
    def _write_csv_atomic(path: Path, header: list[str], rows) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(header)
                writer.writerows(rows)
            os.replace(tmp_path, path)
        except BaseException:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
