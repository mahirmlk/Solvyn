# Agent evaluations

Solvyn includes a small, versioned evaluation set for the system designer, backend engineer, QA engineer, and security auditor. It sends one task to each registered agent, checks the returned structure and a short list of explicit rubric signals, then stores the scores for the configured tenant.

```bash
solvyn eval datasets
solvyn eval run --report ./evaluation-report.json
solvyn eval history --limit 10
```

`eval run` uses the default provider and model from the Solvyn configuration. Pass `--provider` and `--model` to override them for that run. The command needs a working LLM provider, a migrated database, and a valid default tenant. It makes one model call for each case. The optional JSON report contains the dataset version, model identity, per-case check results, and aggregate scores.

Evaluation history is tenant-scoped and append-only. Solvyn stores case IDs, check outcomes, scores, provider/model metadata, and aggregate metrics. It does not store prompts or raw model responses in the history table or report. The packaged dataset contains the prompts used by the runner.

Agent or provider errors are recorded as execution failures and excluded from the quality pass-rate denominator. The run also reports execution success separately. This prevents a timeout or missing credential from looking like a poor model answer.

These checks measure whether a response follows the requested JSON shape and mentions selected rubric concepts. They do not establish that generated code works, that a security finding is correct, or that the model is reliable across repeated runs. One case per role is a starting point, not a calibrated benchmark. Fixture-based tests validate the scorer and are not model evaluation results. No model-quality score should be published until the cases have been reviewed and run against a configured provider; repeat runs are needed to measure consistency.

Evaluation uses the normal LLM runtime and trace settings. Prompts and model responses can appear in in-memory traces and may be sent to Langfuse when that integration is configured. Avoid sensitive task examples unless the configured provider and tracing destinations are approved for that data.
