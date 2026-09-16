"""
test_skills.py — tests for canonical().

Pure function — no DB, no network, no config file needed.
Run with: pytest tests/test_skills.py -v
"""

from __future__ import annotations

import pytest

from edgedash.skills import canonical, canonicalise_list

_ALIASES = {
    "k8s": "kubernetes",
    "postgres": "postgresql",
    "ml": "machine learning",
    "nodejs": "node.js",
    "powerbi": "power bi",
}


class TestCanonical:

    def test_lowercase(self):
        assert canonical("Python", _ALIASES) == "python"

    def test_strips_outer_whitespace(self):
        assert canonical("  sql  ", _ALIASES) == "sql"

    def test_collapses_internal_whitespace(self):
        assert canonical("machine  learning", _ALIASES) == "machine learning"

    def test_strips_parenthetical_qualifier(self):
        assert canonical("kubernetes (eks)", _ALIASES) == "kubernetes"

    def test_strips_parenthetical_with_spaces(self):
        assert canonical("python (3.11+)", _ALIASES) == "python"

    def test_alias_applied(self):
        assert canonical("k8s", _ALIASES) == "kubernetes"

    def test_alias_applied_case_insensitive(self):
        # normalise to lowercase first, then alias
        assert canonical("K8S", _ALIASES) == "kubernetes"

    def test_alias_applied_after_paren_strip(self):
        # "postgres (rds)" -> "postgres" -> "postgresql"
        assert canonical("postgres (rds)", _ALIASES) == "postgresql"

    def test_no_alias_returns_normalised(self):
        assert canonical("apache spark", _ALIASES) == "apache spark"

    def test_empty_string_returns_empty(self):
        assert canonical("", _ALIASES) == ""

    def test_only_whitespace_returns_empty(self):
        assert canonical("   ", _ALIASES) == ""

    def test_strips_leading_punctuation(self):
        assert canonical("-python", _ALIASES) == "python"

    def test_strips_trailing_punctuation(self):
        assert canonical("python.", _ALIASES) == "python"

    def test_no_alias_map_needed(self):
        # calling with empty alias map should still normalise
        assert canonical("  PostgreSQL ", {}) == "postgresql"


class TestCanonicaliseList:

    def test_deduplicates_after_canonicalisation(self):
        # "k8s" and "K8S" both map to "kubernetes"
        result = canonicalise_list(["k8s", "K8S", "docker"], _ALIASES)
        assert result.count("kubernetes") == 1
        assert "docker" in result

    def test_preserves_order(self):
        result = canonicalise_list(["sql", "python", "docker"], _ALIASES)
        assert result == ["sql", "python", "docker"]

    def test_empty_list(self):
        assert canonicalise_list([], _ALIASES) == []

    def test_filters_empty_strings(self):
        result = canonicalise_list(["", "  ", "python"], _ALIASES)
        assert result == ["python"]
