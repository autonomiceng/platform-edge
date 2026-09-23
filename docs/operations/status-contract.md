# Public stack status, contract 2

Each stack publishes one public document describing what it was configured with, and one
health path per component describing whether that component answers now. This is an
informational interface for the Edge console and each stack's own console, not a
deployment, authentication or readiness control API. Producers and consumers ship
independently. The examples under [`status-fixtures/`](status-fixtures/) are compatibility
inputs for implementations; they are synthetic and prove nothing about an installed stack.

The contract is frozen here before any producer or consumer changes (plan v2, slice C1).
Producers arrive in S1 (Gateway), S2 (Observability), S3 (Backplane) and S4 (Edge);
version 1 is retired in S5.

## Transport

| Path | Served by | Meaning |
| --- | --- | --- |
| `GET /status.json` | each stack's gateway (Backplane: the server) | the Status Document below |
| `GET /health/<component>` | same | 200 when the component's documented bounded probe passes, 503 when it fails, 404 for an unknown or disabled component; empty body publicly |
| `GET /stack-status/<stack>` | Edge, same-origin | proxies that stack's `/status.json` |
| `GET /stack-status/<stack>/health/<component>` | Edge, same-origin | proxies that stack's `/health/<component>` |

`<stack>` is one of `edge`, `gateway`, `backplane`, `observability`. Edge routes strip
request credentials and cookies, accept only GET and HEAD, apply a four-second deadline,
limit responses to 64 KiB and 32 components, set `Cache-Control: no-store`, and return no
upstream diagnostic body on proxy errors. Documents are unauthenticated in every access
mode, including public internet access. Edge needs no Docker socket or administrative
credential. Consumers require `application/json`; a successful HTML fallback is
unavailable metadata.

Gateway, Observability and Edge write the document as a static file at bootstrap and serve
it through Caddy. Backplane assembles it in the server from its configuration through an
explicit public projection; the operations document is never serialized. No producer uses
host observers, timers or `docker exec`.

## Document

| Field | Meaning |
| --- | --- |
| `contract` | Integer `2`. Any other value is unavailable metadata. |
| `stack` | One of `edge`, `gateway`, `backplane`, `observability`; must match the requested stack. |
| `configuredAt` | UTC RFC 3339 timestamp when the producer rendered its configuration: bootstrap time for static files, configuration load time for Backplane. Informational; it does not age out. |
| `components` | Array of component records, unique `id` within the stack, at most 32. |
| `features` | Object with the optional keys `backups` and `alerts`. |

Each component has these fields and no others:

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier from the table below, matching `^[a-z][a-z0-9-]{0,31}$`. |
| `name` | Display name, 1 to 64 characters, rendered as text. |
| `kind` | `app`, `datastore`, `gateway`, `collector` or `runtime`. |
| `enabled` | Boolean: the component is selected by the configured Compose profiles and overlays. |
| `image` | The configured image reference (registry, repository, tag) without digest, 1 to 256 characters. |
| `version` | The release version parsed from the configured tag with a component-specific allowlist, matching `^[A-Za-z0-9._+-]{1,128}$`, or null when the tag is not a recognized release. |
| `health` | Same-origin path `/health/<id>`. Required for every component; a disabled component's path answers 404 and is never probed. |
| `url` | Optional. The component's configured browser or API origin. Omitted when the component has none. |

`features.backups` is `{"configured": boolean, "lastCheckpointAt": timestamp or null}`;
`lastCheckpointAt` is the newest Checkpoint the producer knew of when it rendered the
document, not a live value.
`features.alerts` is `{"configured": boolean}`. A missing feature key is unknown.

The field set is closed. Producers emit exactly these fields; a consumer treats a document
with any other envelope, component or feature field as malformed. This is the public
disclosure boundary: adding a field is a contract bump, reviewed as such.

## Stable identifiers

| Stack | Component IDs |
| --- | --- |
| edge | `caddy` |
| gateway | `caddy`, `litellm`, `langfuse-web`, `langfuse-worker`, `postgres`, `clickhouse`, `valkey`, `rustfs`, `postgres-exporter`, `valkey-exporter` |
| backplane | `server`, `postgres`, `rustfs`, `workerd`, `caddy` |
| observability | `caddy`, `grafana`, `alloy`, `loki`, `mimir`, `tempo`, `rustfs` |

Consumers ignore unknown IDs. A component whose required field is missing or invalid is
discarded and renders unknown; malformed JSON, a wrong `contract`, a stack mismatch,
duplicate IDs, unknown fields, or size and count limit violations reject the whole
document. Malformed data never turns into a healthy state.

## Meaning

- Everything in the document is configuration. Consumers label `version` and `image` as
  "configured", never "running" or "deployed". Observed digests, worker, task and restart
  states are not part of this contract (red team finding 7, Owner decision D6).
- Liveness comes only from `health`. A consumer probes each enabled component's `health`
  path with the same bounds as the document fetch and shows healthy on 200, unhealthy on
  503, unknown otherwise. Disabled components are shown as off and never probed.
- An absent or unreachable producer means unknown, never unhealthy, and never prevents
  another stack's card or the console itself from rendering.
- Consumers render `name` as text and treat `url` as data: they link it only when its host
  is one of the consumer's own configured hostnames, otherwise they show it as text.
- Producers publish complete documents atomically and never serialize Compose
  environments, Docker inspect output, connection strings, credentials, container names,
  host paths, internal URLs or free-form errors.

## Fixtures

One version 2 example per stack: [`v2-gateway.json`](status-fixtures/v2-gateway.json),
[`v2-observability.json`](status-fixtures/v2-observability.json),
[`v2-backplane.json`](status-fixtures/v2-backplane.json) and
[`v2-edge.json`](status-fixtures/v2-edge.json). Consumer tests should exercise a valid
document per stack, an unknown field, a duplicate ID, a stack mismatch, a missing producer,
a disabled component and credential-free proxying. `current.json`, `stale.json`,
`forward-compatible.json`, `malformed-component.json`, `duplicate.json` and
`unsupported.json` are the version 1 fixtures and stay until S5.

## Legacy v1

Until slice S5, Edge still accepts a version 1 document from a stack that has not shipped
version 2, recognized by `schemaVersion: 1`. The fields Edge reads from it are
`schemaVersion`, `stack`, `generatedAt`, `configurationObservedAt`,
`configurationValidForSeconds`, `telemetry`, and per component `id`, `kind`
(`service`, `capability`, `task`), `configured`, `state` (`healthy`, `degraded`, `starting`,
`unavailable`, `disabled`, `absent`, `unknown`), `observedAt`, `validForSeconds`,
`lastExecutionAt`, `configuredVersion`, `observedVersion`, `configuredDigest` and
`observedImageId`, with the freshness and clock rules of the version 1 document. S5
removes the version 1 consumer path, its fixtures, the host observers and timers that
produced it, and this appendix. The full version 1 text is in git history before C1.
