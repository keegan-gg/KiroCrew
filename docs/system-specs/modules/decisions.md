# Jev Decision Seam

Owners: `src/kiro_crew/decisions/`, `src/kiro_crew/context.py`, `src/kiro_crew/config/sections.py`, and the Decisions card in `website/src/pages/settings/FeaturePreviewsSection.tsx`.

## 1. Scope

The foundation sends typed questions to Jev and validates its answers. `skills.select` is the only business integration: an enabled, sampled session can use Jev to select one eligible skill, or no skill, instead of the word-overlap result. Cron delivery and skill deduplication remain unchanged. There is no shadow mode, LLM comparison implementation, calibration report, or decisions report command.

## 2. Configuration and sampling

`decisions.enabled` defaults to false. Only a literal boolean true enables requests; strings and numbers are not consent. `decisions.bucket` is an integer percentage from 0 to 100. Sampling uses the session key's SHA-256 digest, so a session remains in the same group rather than drawing again on each message. Zero samples none; 100 samples every otherwise eligible session.

The provider configuration contains endpoint, API-key reference, model and `timeout_ms`. A default timeout is 1000 milliseconds. The enabled flag and sampling percentage use the existing live config snapshot, not a disk read per decision.

Legacy `preview`, `points`, `arm`, `shadow`, and `live` values do not enable this behavior. Enabling an observation-only configuration must never silently authorize business decisions. The new enabled flag is the explicit opt-in.

## 3. Core contract

`decide(point, state, questions, session_key=...)` returns validated answers or `None`. Disabled, unknown-point, unsampled, scrubbed, timed-out, transport-error and invalid-answer paths return `None`. Cancellation propagates so a caller can abandon a pending request. `is_enabled` provides the same cheap preflight before a caller builds its candidate menu; `decide` checks the gates again.

Jev is the sole provider implementation. Requests use its documented wire format with bounded response reads and no retry. Answer identifiers and values must match the questions, and probabilities must be finite and within their domain. Provider failures do not fabricate a selection. The response mapping is tested against a loopback server; those tests do not prove a real TypeSafe account round-trip.

Before transmission, the request is scanned with the existing credential patterns, canonical credential redactor and exfiltration-URL detector. A match or scanner failure refuses the whole request. No partially scrubbed request is sent. A `secret://` API-key reference resolves only for the default endpoint; a custom endpoint cannot use configuration changes to retrieve a vault credential. Configuration is not an independent authorization boundary: an actor allowed to change it can enable requests.

## 4. Skill selection

The normal word-overlap result remains the fallback. The Jev candidate menu uses skills available through the existing loader and its project-confinement rules. Always-loaded skills are not candidates; negative triggers and `skills.max_triggered` remain effective. A zero trigger cap means no automatic selection and no Jev request. Skill identifiers are preserved, not shortened into different names.

A validated choice replaces the triggered list before the existing body/pointer split. The explicit no-skill answer produces an empty list; `None` means retain the original list. Custom-agent and minimal-context paths do not acquire automatic skill selection.

`ContextBuilder.build_message` is synchronous. On its worker thread, it submits the decision to the captured running event loop and waits with a finite budget. It must not block the event-loop thread itself. Missing or closed loops, same-loop calls, invalid answers and expired budgets fall back. An abandoned future is cancelled; its eventual answer cannot change a context already assembled.

## 5. Operational log

The basic JSONL log records call metadata rather than an A/B score: timestamp, point, hashed session, latency, scrubbed boolean, bounded answer data and an error category. Raw messages, candidate descriptions, API keys and provider exception text do not belong in rows. No claimed price, baseline or agreement curve is emitted.

Disabled and unsampled paths do not write logs. Log failure cannot turn a valid decision into an application exception. JSON serialization keeps control characters encoded as data; this feature has no terminal report renderer. Logging is best effort, not proof that a selection reached the assembled context.

## 6. Settings and validation

The Decisions card reads and writes `decisions.enabled`, not localStorage. The backend editable-field validation accepts the boolean switch and bounded integer bucket; sensitive provider credentials remain masked in GET output. A backend exposing only legacy `preview` is unsupported, not an enabled gateway. A literal `configKey` is required so the settings extractor connects the control to the schema.

Tests cover strict opt-in and legacy non-migration; sampling; Jev success, malformed replies and failure; credential refusal; log shape; real PATCH/GET behavior; actual skill-selection consumption and fallback from an executor thread; negative triggers and cap limits; frontend field support, writes, refused writes and generated key parity. These are local/loopback tests. Live provider verification requires a real key and separate consent to send test data.
