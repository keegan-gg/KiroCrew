# Jev skill selection

Jev can choose an automatic skill for a sampled conversation. When enabled, it receives a short message excerpt and a menu of eligible skill names and descriptions. Its valid answer changes the selected skill; a timeout or failed request keeps the normal trigger-matching result. The feature is off by default.

## What changes

Only automatic skill selection uses Jev. Skill deduplication and scheduled notifications do not change. Mandatory skills, custom-agent exclusions, project access rules and the automatic skill limit still apply.

| State | Skill selection |
|---|---|
| Disabled | Normal trigger matching |
| Enabled, outside the sample | Normal trigger matching |
| Enabled, sampled, valid answer | Jev's selection |
| Timeout, refusal or invalid answer | Normal trigger matching |

A valid answer can choose one skill or explicitly choose none. Choosing none is not a failed request. There is no shadow mode that asks Jev only to discard its answer.

## Configure before enabling

Use Settings > Developer > Feature Previews for the Decisions switch. It saves `decisions.enabled` on the gateway, so it applies across devices. An older backend that lacks this field keeps the switch disabled.

The configuration shape is:

```json
{
  "decisions": {
    "enabled": false,
    "bucket": 10,
    "provider": {
      "endpoint": "https://api.typesafe.ai/v1/systemone",
      "api_key": "secret://YOUR_JEV_SECRET",
      "model": "jev-latest",
      "timeout_ms": 1000
    }
  }
}
```

Create the API-key secret through the existing [secrets vault](secrets-vault.md), then use its reference. The reference above is a placeholder, not a working key. Vault references are only resolved for the default Jev endpoint.

`bucket` chooses a percentage of sessions. It is a fixed sample, not a random draw per message. A session stays selected or unselected while its key and bucket remain unchanged. `0` samples none and `100` samples all otherwise eligible sessions.

`skills.max_triggered` must be greater than zero to allow automatic selection. Its default is zero, which disables automatic selection even when the Decisions switch is on. Jev selects at most one skill and does not raise that limit.

After setting the provider and sampling values, enable the switch only if the data transfer below is acceptable. Turn it off to return to normal trigger matching. Old `preview` and per-point mode values do not enable this new behavior.

## Data and waiting time

Enabling Jev allows the message excerpt and candidate skill descriptions to leave the machine. Credential and suspicious-URL checks refuse matching requests, but they are not a guarantee that all private content is detected. Do not enable the feature for content that must stay local.

A sampled selection waits for a bounded answer. `timeout_ms` controls the provider budget; its default is 1000 milliseconds. A missing key, unavailable provider or short budget can make the feature fall back without changing the selected skills. There is no automatic retry.

## Basic logs

Operational records are JSONL day-files under the gateway's data home, in the `decisions` directory. They contain the point name, hashed session identifier, elapsed time, bounded answer data and error categories. They do not contain the message body, candidate descriptions or credentials.

These are diagnostic records, not an agreement score or a billing report. This feature does not provide a decisions report command or automatic log retention. A missing row alone is not proof that an answer was applied.

The provider mapping is tested locally against a loopback server. A real Jev call requires your API key; local tests do not establish real service latency or account compatibility.
