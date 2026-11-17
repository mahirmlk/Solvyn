# Agent evaluations

Solvyn includes versioned evaluation dataset `solvyn-engineering-teammates` version `1.1.0`. It contains 12 cases: three for each of the system designer, backend engineer, QA engineer, and security auditor. Each role has boundary-focused and adversarial-input coverage alongside a core engineering task. The runner sends each case to its registered agent, checks the returned structure and explicit rubric signals, then stores the scores for the configured tenant.

```bash
solvyn eval datasets
solvyn eval run --report ./evaluation-report.json
solvyn eval history --limit 10
solvyn eval compare --baseline ./baseline.json --candidate ./evaluation-report.json --output ./comparison.json
```

`eval run` uses the default provider and model from the Solvyn configuration. Pass `--provider` and `--model` to override them for that run. The command needs a working LLM provider, a migrated database, and a valid default tenant. It makes one model call per case (12 calls for version 1.1.0). The optional JSON report contains the dataset version, model identity, per-case check results, and aggregate scores.

Evaluation history is tenant-scoped and append-only. Solvyn stores case IDs, check outcomes, scores, provider/model metadata, and aggregate metrics. It does not store prompts or raw model responses in the history table or report. The packaged dataset contains the prompts used by the runner.

Agent or provider errors are recorded as execution failures and excluded from the quality pass-rate denominator. The run also reports execution success separately. This prevents a timeout or missing credential from looking like a poor model answer.

The cases broaden role coverage and include boundary and adversarial inputs, but the scorer remains deliberately simple: it checks JSON shape, required keys, and whether selected phrases appear. It does not execute generated code, verify the correctness of a security finding, measure false positives, or establish a product-quality guarantee. Three cases per role are still a small starting dataset, not a calibrated benchmark. Fixture-based tests validate the scorer and comparison logic; they are not model evaluation results.

`eval compare` reads two saved JSON reports without contacting a provider or database. It requires matching dataset/version, provider/model, case IDs, and role assignments, then reports per-case, per-role, and overall changes. Score and pass-rate deltas use only cases that completed in both runs; execution failure changes are reported separately. Run multiple reports with the same reviewed provider/model and dataset to measure consistency and regressions. The comparison command does not choose or certify a baseline. Do not publish a model-quality score until the cases and baseline have been reviewed and the reports were produced by configured-provider runs.

Evaluation uses the normal LLM runtime and trace settings. Prompts and model responses can appear in in-memory traces and may be sent to Langfuse when that integration is configured. Avoid sensitive task examples unless the configured provider and tracing destinations are approved for that data.
