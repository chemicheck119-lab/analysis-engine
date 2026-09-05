# AGENTS.md

## Scope

These instructions apply to the entire AI/model API repository.

## Validation

- Run the repository's documented test and build checks for code changes.
- Keep evaluation inputs, manifests, source versions, and split boundaries reproducible.
- Do not replace deterministic safety gates with prompt-only instructions.

## Code Review Rules

- Flag any path where an LLM directly decides chemical compatibility, hazard severity, or operational safety. Parsers, resolvers, retrievers, and agents may structure, rank, retrieve, and coordinate; deterministic CAMEO rules may run only after two CAS values are separately confirmed.
- Flag conflation of the 419-record Resolver locked evaluation with the 442-record Parser external evaluation, target metrics with measured results, historical facility candidates with current inventory, or CAMEO ordinal outputs with probabilities.
- Flag evaluation leakage, overlap between train/tuning and locked test data, unversioned source artifacts, non-reproducible metrics, or claims that generalize beyond the evaluated population.
- Flag resolver behavior that silently converts unseen or ambiguous expressions into a single confirmed chemical. Preserve ranked candidates, confidence/abstention behavior, and explicit confirmation requirements.
- Flag fabricated citations, unsupported summaries, or fallback that invents evidence. Retrieval and generation failures must fail closed or use a source-bounded extractive fallback.
- Flag secrets, personal/incident data, copyrighted source dumps, or large raw datasets committed to Git or emitted to ordinary logs.
- Flag changes to API contracts, rule tables, data transformations, or metric calculations without proportional regression tests and provenance updates.

## Fix Guidance

- Prefer the smallest evidence-backed fix that preserves separation of roles and reproducibility.
- When asked to fix a review finding, add or update a regression test and state which claim remains unverified by real field data.
