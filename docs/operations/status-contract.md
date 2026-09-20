# Public stack status, version 1

This contract describes bounded observations for the Edge console and independent stack
consoles. It is an informational interface, not a deployment, authentication or readiness
control API. Producers and consumers ship independently. The examples under
[`status-fixtures/`](status-fixtures/) are compatibility inputs for their implementations.
This document establishes the interface; it does not claim a producer is deployed.

## Transport and limits

A stack serves `GET /status.json` through its gateway. Edge exposes the corresponding
same-origin document at `/stack-status/{gateway,backplane,observability,edge}`. Missing
producers may return 404; older `/versions.json` documents remain separate interfaces.
Routes strip request credentials and cookies, accept only GET/HEAD, and return no
upstream diagnostic body on proxy errors. Responses use `application/json` and
`Cache-Control: no-store`. The document is unauthenticated in every access mode, including
public internet access. Edge needs no Docker socket or administrative credential. Consumers
require `application/json`; a successful HTML fallback is unavailable metadata.

A consumer permits at most 64 KiB and 32 components per document, with a four-second
request deadline. It fetches stacks independently, refreshes on request and periodically
while visible, and bounds concurrent requests. One failed or incompatible producer
must not prevent other cards or the console itself from rendering.

Fresh configuration fields from a supported status document take precedence over legacy
`/versions.json` values for the same ID. Legacy values may be shown as explicitly dated
configuration when status is absent, unsupported or its configuration observation expires;
they never establish current health. The legacy gateway `langfuse` version maps to both
`langfuse-web` and `langfuse-worker`.

## Document

| Field | Meaning |
| --- | --- |
| `schemaVersion` | Integer `1`. An unsupported version is unavailable metadata. |
| `stack` | One of `edge`, `gateway`, `backplane`, `observability`; must match the requested stack. |
| `generatedAt` | UTC RFC 3339 timestamp when this document was assembled. This does not renew its observations. |
| `configurationObservedAt` | UTC timestamp of the configuration inspection, or null if unknown. |
| `configurationValidForSeconds` | Integer from 1 to 300; maximum age of configuration and telemetry observations. |
| `telemetry` | `configured`, `disabled` or `unknown`. Configuration alone never proves successful collection. |
| `components` | Array of component records with unique IDs within this stack. |

Each component has these required fields:

| Field | Meaning |
| --- | --- |
| `id` | Stable identifier from the table below. No installation names or dynamic container IDs. |
| `kind` | `service`, `capability` or `task`. |
| `configured` | Boolean, or null when the installed configuration is unknown. |
| `state` | One of the states below, supported by a component-specific observation. |
| `observedAt` | UTC timestamp of that observation, or null when none exists. |
| `validForSeconds` | Integer from 1 to 300; maximum age for treating this observation as current. |

Tasks additionally require nullable `lastExecutionAt`, the UTC start time of their latest
execution. Their `observedAt` is when the execution record was inspected. A task with no
record has `lastExecutionAt: null` and `state: unknown`; an explicitly disabled task may
have no execution. Execution time does not age out, but the record inspection does.

Optional version fields are nullable strings. Omission means unknown:

| Field | Evidence and presentation |
| --- | --- |
| `configuredVersion` | A recognized upstream release version, or `custom` for an arbitrary operator tag. Label as configured. |
| `observedVersion` | A release version parsed from a bounded, service-specific runtime probe. Never copy the configured version here. |
| `configuredDigest` | Complete `sha256:` registry digest from effective configuration, if present. |
| `observedImageId` | Complete `sha256:` local image content ID from a trusted host observer, if available. |

A registry manifest digest and Docker image content ID identify different objects;
consumers must not compare them as if equal strings proved deployment convergence.
An observation can have a known version and unknown readiness, or vice versa. Both version
labels match `^[A-Za-z0-9._+-]{1,128}$`; both digest fields match
`^sha256:[0-9a-f]{64}$`. Invalid optional values are unknown. Producers may omit version
and digest fields to limit public disclosure.

## Stable identifiers

These names belong to this interface; existing console element IDs may map to them.
Only the listed capability/task IDs use the corresponding kind. All other IDs are services.

| Stack | Service IDs | Capability IDs | Task IDs |
| --- | --- | --- | --- |
| edge | `caddy` | none | `bootstrap` |
| gateway | `caddy`, `litellm`, `langfuse-web`, `langfuse-worker`, `postgres`, `clickhouse`, `valkey`, `rustfs`, `postgres-exporter`, `valkey-exporter` | none | `bootstrap`, `rustfs-init` |
| backplane | `server`, `postgres`, `caddy`, `rustfs`, `workerd` | `files`, `functions` | `bootstrap`, `migrate`, `data-init`, `blob-bootstrap` |
| observability | `caddy`, `grafana`, `alloy`, `loki`, `mimir`, `tempo`, `rustfs` | none | `bootstrap`, `rustfs-init` |

The `files` capability covers the selected filesystem or S3 backend through the Files API.
`functions` covers the configured deployment and invocation path. `bootstrap` is the host
installation preparation task; `migrate` applies Backplane database migrations; `data-init`
prepares Backplane filesystem permissions; `blob-bootstrap` prepares the Backplane S3
bucket and scoped credentials. Gateway and observability call their equivalent S3 preparation
task `rustfs-init`. Task IDs preserve each owning stack's Compose service names.

Components can be omitted by older producers. Omission is unknown, not disabled or absent.
Consumers ignore unknown component IDs and additive fields. Malformed JSON, unsupported
schema versions, a stack mismatch, invalid required envelope fields, duplicate IDs, or size
and count limit violations reject the whole document. Invalid required component fields,
wrong kinds for known IDs and malformed component timestamps discard that component,
which renders unknown. They never turn malformed data into healthy state. New IDs or optional fields are additive changes.
A changed field meaning or removal requires a new schema version.

## States and freshness

| State | Required evidence |
| --- | --- |
| `healthy` | The service or capability passed its documented bounded probe. For a task, its most recent recorded execution succeeded. |
| `degraded` | A documented partial-success condition, distinct from healthy. |
| `starting` | Startup or task execution was actually observed in progress. |
| `unavailable` | A configured component failed its probe or a recorded task failed. |
| `disabled` | A current configuration inspection explicitly selected the component off (`configured: false`). |
| `absent` | A trusted installation inventory explicitly found no resource for the configured component. |
| `unknown` | No sufficient observation exists. |

`healthy`, `degraded`, `starting`, `unavailable` and `absent` require `configured: true`.
`unknown` may have any configured value. Every state other than `unknown` requires
`observedAt`; disabled observations refer to the configuration inspection time.
For `disabled`, `observedAt` equals `configurationObservedAt` and effective validity is the
minimum of the component and configuration validity windows. Task success reports the last
execution, not continuous service readiness; present its execution time separately.

A failed HTTP request cannot establish absence. Consumers display unavailable metadata
when the producer cannot be reached, preserving any previous observation only with its
original timestamp and an explicit stale label. They must not extend freshness on a
failed refresh or reuse an old healthy result as the current answer.

Age is measured against the consumer's current UTC clock. Consumers require a synchronized
clock; when clock agreement cannot be established, report unknown rather than relax age
checks. An observation older than `validForSeconds` is stale, regardless of `generatedAt`.
Re-fetching an unchanged document must never reset its age. Missing observation time is
unknown. Timestamps over five seconds in the future relative to the consumer are invalid;
observation and execution timestamps also cannot exceed `generatedAt` by over five seconds.
Consumers may advance a validated age with a monotonic clock between refreshes. Stale and invalid observations cannot produce a current healthy indicator.
`configuredVersion`, `configuredDigest`, `configured` and `telemetry` use
`configurationObservedAt` and `configurationValidForSeconds`. Missing or expired
configuration time makes those facts unknown or explicitly stale. `observedVersion`
and `observedImageId` share their component's observation time and must be omitted when
their evidence came from a different observation. A recent health probe must not make
an older configuration snapshot appear newly inspected. Null configuration time requires
`telemetry: unknown` and null configured fields.

## Producer responsibilities

Each producer documents what its probes prove and their limits. An application returning
200 does not establish health of its databases, object store, workers or telemetry path.
Files and Functions require capability-specific evidence. Docker health is usable only
when its actual healthcheck and freshness support the advertised claim; running is not
healthy. Missing dependencies cannot be represented as a healthy capability.

Probes have bounded deadlines, output sizes and concurrency, and avoid user data or
irreversible actions. Producers publish complete documents atomically. Failed probes
record their new outcome and timestamp; they do not preserve success with a new timestamp.
Static configuration observations must age out unless an observer refreshes them.

The response is a closed public allowlist. Never serialize Compose environments, Docker
inspect documents, connection strings, credentials, container names, host paths, internal
URLs, probe bodies or free-form errors. Parse upstream versions with a component-specific
allowlist; arbitrary operator tags become `custom`. Digest fields contain only lowercase
SHA-256 identifiers. Consumers render text as text and derive links from their own trusted
configuration, never from this document. Installation task results expose only the listed
task ID, state and time; diagnostic details remain in protected operator interfaces.

## Compatibility fixtures

`current.json` contains mixed current observations, including a completed task and custom
image configuration. `stale.json` deliberately has a fresh document timestamp and old
component timestamp. `forward-compatible.json` adds unknown fields and a component.
`malformed-component.json` leaves its valid sibling usable; `duplicate.json` rejects the
whole document. `unsupported.json` must not be consumed as version 1. Tests should
use a fixed clock of `2026-09-20T12:00:00Z`, then exercise expiry, unknown IDs/fields,
duplicate IDs, malformed fields, a missing producer and credential-free proxying. Fixtures
are synthetic; they are not acceptance evidence for an installed stack.
