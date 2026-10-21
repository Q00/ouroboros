"""Tests for the pure-Python BM25 ranker."""

from __future__ import annotations

import pytest

from ouroboros.core.bm25 import rank, tokenize


class TestTokenize:
    def test_lowercases_and_splits_on_punctuation(self) -> None:
        assert tokenize("Hello, World! foo-bar_baz") == ["hello", "world", "foo", "bar", "baz"]

    def test_splits_camel_case_and_keeps_whole_word(self) -> None:
        assert tokenize("BrownfieldHandler") == ["brownfield", "handler", "brownfieldhandler"]
        assert tokenize("HTTPServer") == ["http", "server", "httpserver"]

    def test_drops_stopwords_and_single_ascii_chars(self) -> None:
        assert tokenize("the a of x payments in go") == ["payments", "go"]

    def test_keeps_non_latin_words(self) -> None:
        assert tokenize("결제 모듈") == ["결제", "모듈"]

    def test_hangul_words_add_syllable_bigrams(self) -> None:
        assert tokenize("결제를 서비스") == ["결제를", "결제", "제를", "서비스", "서비", "비스"]

    def test_ascii_tokens_get_no_bigrams(self) -> None:
        assert tokenize("Payment gateway v2") == ["payment", "gateway", "v2"]


class TestRank:
    def test_returns_only_matching_documents_best_first(self) -> None:
        docs = [
            "frontend react dashboard",
            "payments service stripe billing",
            "billing reports",
        ]
        ranked = rank("stripe billing", docs)
        assert [r.index for r in ranked] == [1, 2]
        assert ranked[0].score > ranked[1].score > 0

    def test_rare_term_outweighs_common_term(self) -> None:
        docs = ["api common", "api common", "api common", "api rare"]
        ranked = rank("api rare", docs)
        assert ranked[0].index == 3

    def test_term_frequency_saturates_and_length_normalizes(self) -> None:
        short = "auth"
        long_padded = "auth " + " ".join(f"word{i}" for i in range(40))
        ranked = rank("auth", [long_padded, short])
        assert ranked[0].index == 1

    def test_ties_break_by_tie_key_then_index(self) -> None:
        docs = ["shared token", "shared token", "shared token"]
        ranked = rank("token", docs, tie_keys=["charlie", "alpha", "bravo"])
        assert [r.index for r in ranked] == [1, 2, 0]
        assert len({r.score for r in ranked}) == 1

        without_keys = rank("token", docs)
        assert [r.index for r in without_keys] == [0, 1, 2]

    def test_is_deterministic(self) -> None:
        docs = [f"repo{i} service api client" for i in range(30)]
        first = rank("api client", docs)
        assert all(rank("api client", docs) == first for _ in range(5))

    @pytest.mark.parametrize("query", ["", "   ", "the of a"])
    def test_empty_or_stopword_query_returns_nothing(self, query: str) -> None:
        assert rank(query, ["anything at all"]) == []

    def test_empty_corpus_returns_nothing(self) -> None:
        assert rank("anything", []) == []

    def test_korean_particle_query_matches_bare_noun(self) -> None:
        docs = [
            "로그 수집 파이프라인",
            "결제 서비스 README",
            "회원 관리 화면",
        ]
        ranked = rank("결제를 처리하는 레포", docs)
        assert ranked[0].index == 1
        assert rank("결제를", ["결제 모듈"])[0].index == 0

    def test_english_ranking_unchanged_by_hangul_support(self) -> None:
        docs = ["payments service stripe billing", "billing reports", "결제 서비스"]
        ranked = rank("stripe billing", docs)
        assert [r.index for r in ranked] == [0, 1]

    def test_repeated_query_terms_count_once(self) -> None:
        docs = ["alpha beta", "alpha"]
        assert rank("alpha alpha alpha", docs) == rank("alpha", docs)
