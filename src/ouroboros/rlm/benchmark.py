"""Benchmark fixtures for the isolated RLM MVP dogfood run."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID = "rlm-mvp-src-dogfood-v1"


@dataclass(frozen=True, slots=True)
class RLMBenchmarkQuestion:
    """One evidence-grounded question required by an RLM benchmark fixture."""

    question_id: str
    title: str
    prompt: str
    required_evidence: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the benchmark question for Hermes prompt context."""
        return {
            "question_id": self.question_id,
            "title": self.title,
            "prompt": self.prompt,
            "required_evidence": list(self.required_evidence),
        }


@dataclass(frozen=True, slots=True)
class RLMBenchmarkTargetCorpus:
    """Repository corpus selected for an evidence-grounded RLM benchmark."""

    corpus_id: str
    root: str
    description: str
    include_globs: tuple[str, ...]
    exclude_globs: tuple[str, ...] = ()
    required_paths: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Serialize the target corpus into the Hermes benchmark context."""
        return {
            "corpus_id": self.corpus_id,
            "root": self.root,
            "description": self.description,
            "include_globs": list(self.include_globs),
            "exclude_globs": list(self.exclude_globs),
            "required_paths": list(self.required_paths),
        }


@dataclass(frozen=True, slots=True)
class RLMBenchmarkExecutionConfig:
    """Runtime knobs that make a benchmark exercise recursive Hermes calls."""

    chunk_line_limit: int
    max_atomic_chunks: int
    min_nested_inner_lm_calls: int

    def to_dict(self) -> dict[str, Any]:
        """Serialize benchmark execution settings for prompt and test evidence."""
        return {
            "chunk_line_limit": self.chunk_line_limit,
            "max_atomic_chunks": self.max_atomic_chunks,
            "min_nested_inner_lm_calls": self.min_nested_inner_lm_calls,
        }


@dataclass(frozen=True, slots=True)
class RLMBenchmarkFixture:
    """Stable dogfood benchmark definition embedded in RLM prompt envelopes."""

    benchmark_id: str
    target: str
    target_corpus: RLMBenchmarkTargetCorpus
    execution_config: RLMBenchmarkExecutionConfig
    root_question: str
    questions: tuple[RLMBenchmarkQuestion, ...]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the fixture into the RLM Hermes input envelope."""
        return {
            "benchmark_id": self.benchmark_id,
            "target": self.target,
            "target_corpus": self.target_corpus.to_dict(),
            "execution_config": self.execution_config.to_dict(),
            "root_question": self.root_question,
            "questions": [question.to_dict() for question in self.questions],
        }


RLM_MVP_SRC_ROOT_QUESTION = (
    "Analyze the Ouroboros src/ tree and report whether the current RLM MVP "
    "satisfies the dual-layer recursive language model constraints, using only "
    "supplied source chunks and citing evidence from at least three source files."
)

RLM_MVP_SRC_DOGFOOD_TARGET_CORPUS = RLMBenchmarkTargetCorpus(
    corpus_id="ouroboros-src",
    root="src",
    description="The Ouroboros repository source tree used by the RLM MVP dogfood benchmark.",
    include_globs=("src/ouroboros/**/*.py",),
    exclude_globs=(
        "src/**/__pycache__/**",
        "src/**/*.pyc",
    ),
    required_paths=(
        "src/ouroboros/cli/commands/rlm.py",
        "src/ouroboros/cli/main.py",
        "src/ouroboros/rlm/loop.py",
        "src/ouroboros/orchestrator/hermes_runtime.py",
        "src/ouroboros/core/ac_tree.py",
        "src/ouroboros/rlm/trace.py",
        "src/ouroboros/persistence/event_store.py",
        "src/ouroboros/evolution/wonder.py",
        "src/ouroboros/evolution/reflect.py",
        "src/ouroboros/evolution/loop.py",
    ),
)

RLM_MVP_SRC_DOGFOOD_EXECUTION_CONFIG = RLMBenchmarkExecutionConfig(
    chunk_line_limit=1,
    max_atomic_chunks=6,
    min_nested_inner_lm_calls=1,
)

RLM_WONDER_REFLECT_ONTOLOGY_MIGRATION_QUESTION = RLMBenchmarkQuestion(
    question_id="wonder-reflect-generation-ontology-migration",
    title="Wonder/Reflect generation-level ontology migration",
    prompt=(
        "Analyze whether Wonder and Reflect preserve generation-level ontology "
        "migration: show how Wonder derives unanswered questions or ontology "
        "tensions from generation N evidence, how Reflect turns those findings "
        "into generation N+1 ontology mutations or seed changes, and whether the "
        "evolution loop records enough lineage or delta evidence to audit that "
        "migration."
    ),
    required_evidence=(
        "src/ouroboros/evolution/wonder.py",
        "src/ouroboros/evolution/reflect.py",
        "src/ouroboros/evolution/loop.py",
    ),
)

RLM_MVP_SRC_DOGFOOD_FIXTURE = RLMBenchmarkFixture(
    benchmark_id=RLM_MVP_SRC_DOGFOOD_BENCHMARK_ID,
    target=RLM_MVP_SRC_DOGFOOD_TARGET_CORPUS.root,
    target_corpus=RLM_MVP_SRC_DOGFOOD_TARGET_CORPUS,
    execution_config=RLM_MVP_SRC_DOGFOOD_EXECUTION_CONFIG,
    root_question=RLM_MVP_SRC_ROOT_QUESTION,
    questions=(
        RLMBenchmarkQuestion(
            question_id="command-isolation",
            title="Command isolation",
            prompt=(
                "Show that rlm is registered as its own command and constructs "
                "RLMRunConfig directly instead of invoking run or evolve code."
            ),
            required_evidence=(
                "src/ouroboros/cli/commands/rlm.py",
                "src/ouroboros/cli/main.py",
            ),
        ),
        RLMBenchmarkQuestion(
            question_id="hermes-inner-lm-boundary",
            title="Hermes inner-LM boundary",
            prompt=(
                "Show that RLM uses HermesCliRuntime through "
                "AgentRuntime.execute_task_to_result() and passes a system "
                "prompt that forbids recursive ooo or Ouroboros calls."
            ),
            required_evidence=(
                "src/ouroboros/rlm/loop.py",
                "src/ouroboros/orchestrator/hermes_runtime.py",
            ),
        ),
        RLMBenchmarkQuestion(
            question_id="ac-rlm-recursion-guardrails",
            title="AC and RLM recursion guardrails",
            prompt=(
                "Show that the loop carries max_ac_depth = 5, ambiguity "
                "threshold <= 0.2, RLM node IDs, AC node IDs, and chunk child calls."
            ),
            required_evidence=(
                "src/ouroboros/rlm/loop.py",
                "src/ouroboros/core/ac_tree.py",
            ),
        ),
        RLMBenchmarkQuestion(
            question_id="trace-replay-readiness",
            title="Trace and replay readiness",
            prompt=(
                "Show that generated envelopes include selected chunk IDs, call "
                "IDs, parent call IDs, child results, and enough AC/RLM linkage "
                "to replay causality."
            ),
            required_evidence=(
                "src/ouroboros/rlm/loop.py",
                "src/ouroboros/rlm/trace.py",
                "src/ouroboros/persistence/event_store.py",
            ),
        ),
        RLMBenchmarkQuestion(
            question_id="context-scaling",
            title="Context scaling",
            prompt=(
                "Show that a target larger than one Hermes context is split into "
                "bounded chunks and synthesized by a parent RLM node."
            ),
            required_evidence=("src/ouroboros/rlm/loop.py",),
        ),
        RLM_WONDER_REFLECT_ONTOLOGY_MIGRATION_QUESTION,
    ),
)


RLM_BENCHMARK_FIXTURES: tuple[RLMBenchmarkFixture, ...] = (
    RLM_MVP_SRC_DOGFOOD_FIXTURE,
)


def _normalize_target(target: str) -> str:
    """Return the stable benchmark target spelling for path-like invocations."""
    normalized = target.strip().replace("\\", "/").rstrip("/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def benchmark_fixture_for_id(benchmark_id: str) -> RLMBenchmarkFixture | None:
    """Return the built-in RLM benchmark fixture for a stable benchmark ID."""
    normalized = benchmark_id.strip()
    for fixture in RLM_BENCHMARK_FIXTURES:
        if normalized == fixture.benchmark_id:
            return fixture
    return None


def benchmark_fixture_for_target(target: str) -> RLMBenchmarkFixture | None:
    """Return the dogfood benchmark fixture for the default RLM source target."""
    normalized = _normalize_target(target)
    if normalized == RLM_MVP_SRC_DOGFOOD_FIXTURE.target:
        return RLM_MVP_SRC_DOGFOOD_FIXTURE
    return None
