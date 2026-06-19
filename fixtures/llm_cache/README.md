# Replay fixtures

Bundled responses have `source: synthetic`: they were generated from known invoice inputs, not recorded from Claude. They test deterministic auditing and workflow behavior, not live LLM extraction quality. Cache keys cover the exact PDF text, model, versioned prompt and schema.

`LLM_MODE=record` stores actual Claude responses with `source: recorded`. Re-running the generator preserves recorded entries. Missing replay entries raise CacheMissError; there is no model, parser or truth-label fallback at runtime.
