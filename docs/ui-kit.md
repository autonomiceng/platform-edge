# platform-ui: the shared UI kit

One stylesheet, `docker/console/platform.css`, gives the Edge console, the stack landing
pages and the Backplane dashboard the same colours, type, spacing and components. It is
plain CSS with custom properties: no build step, no web fonts, no JavaScript. Its first line
states the version (`platform-ui v1`).

- [Adopt it](#adopt-it)
- [Tokens](#tokens)
- [Components](#components)
- [Badge states](#badge-states)
- [Platform home link](#platform-home-link)
- [What stays local](#what-stays-local)

## Adopt it

Stacks copy the file verbatim; nobody edits a copy. From a platform-edge checkout:

```sh
scripts/sync-ui.sh ../llm-gateway-stack ../observability-stack ../agent-backplane
scripts/sync-ui.sh --check ../llm-gateway-stack ../observability-stack ../agent-backplane
```

The copy goes to the sibling's console directory: `docker/caddy/console/platform.css`
(Gateway, Observability) or `apps/web/platform.css` (Backplane). Its first line records the
source commit and the SHA-256 of the rest of the file:

```css
/* vendored from platform-edge@<commit> sha256:<hex> ; do not edit here */
```

`--check` exits 1 when a copy's header is missing, its checksum does not match, it differs
from the canonical file, or the named commit did not hold that content. Without a
platform-edge checkout, a sibling can still verify its copy against its own header:

```sh
f=docker/caddy/console/platform.css
[ "$(tail -n +2 "$f" | sha256sum | cut -d ' ' -f 1)" = "$(head -n 1 "$f" | sed 's/.*sha256:\([0-9a-f]*\).*/\1/')" ]
```

Change the kit only here, commit, then sync every sibling. Load it before the page's own
stylesheet and opt in with `<body class="pk-page">`, which sets the background, text, font,
link colours and focus rings. Add `<meta name="color-scheme" content="dark light">`.

## Tokens

Dark is the default; `prefers-color-scheme: light` switches every colour token to a light
value with the same role. Every text colour meets WCAG AA (4.5:1) on `bg`, `panel`, `card`
and `input` in both schemes, including badge text on its own tint.

| Token | Dark | Light | Use |
| --- | --- | --- | --- |
| `--pk-bg` | `#0d1525` | `#f3f6fa` | page background |
| `--pk-panel` | `#162235` | `#ffffff` | `.pk-card`, drawer |
| `--pk-card` | `#101b2c` | `#f7f9fc` | `.pk-app`, lists, notices |
| `--pk-input` | `#1d2c42` | `#e9eef5` | buttons, inputs |
| `--pk-line` | `#2a3a50` | `#d3dbe6` | every 1 px border |
| `--pk-text` | `#e4edf7` | `#142033` | body text |
| `--pk-muted` | `#96a9c1` | `#4f5f75` | descriptions, labels, meta |
| `--pk-code` | `#bdd5ed` | `#1d3a5c` | URLs, image references |
| `--pk-accent`, `--pk-accent-hover` | `#8ccaff`, `#b4e2ff` | `#0b5bb0`, `#07458a` | links, focus ring |
| `--pk-ok` | `#61dfb1` | `#06673f` | healthy |
| `--pk-warn` | `#ffb67b` | `#9a4a05` | degraded, attention |
| `--pk-danger` | `#ff8a8a` | `#b3242b` | unreachable, destructive |
| `--pk-info` | `#9faad7` | `#46519c` | configured, starting |

Radii: `--pk-radius-lg` 14 px (sections), `--pk-radius` 10 px (app cards, lists),
`--pk-radius-sm` 8 px (buttons, inputs, notices), `--pk-radius-pill` 20 px (pills, badges).
Spacing: `--pk-space-1` to `--pk-space-7` are 4, 8, 12, 16, 20, 24 and 32 px;
`--pk-gutter` is the page side padding (32, 24 under 1100 px, 16 under 700 px) and
`--pk-page-max` the content width (1440 px). Type: `--pk-font` (system UI stack) and
`--pk-mono`; sizes `--pk-size-base` 15, `-brand` 18 (weight 650), `-h2` 19, `-small` 13,
`-meta` 12, `-badge` 11 and `-label` 10 px (uppercase, 1.4 px tracking). Borders are 1 px
`--pk-line`; the drawer is the only element with a shadow.

## Components

Every class starts with `pk-`. Decorative icons take `alt=""`; the adjacent text names the
control.

**Header** (`.pk-header`): brand mark and product name, an optional pill, links on the right.

```html
<header class="pk-header">
  <a class="pk-brand" href="/"><img src="/icon.svg" alt="">LLM Gateway</a>
  <span class="pk-pill">HTTPS</span>
  <nav class="pk-header-links" aria-label="Links">
    <a href="https://platform.example.com/">Platform</a>
    <a href="https://github.com/autonomiceng/llm-gateway-stack">GitHub ↗</a>
  </nav>
</header>
```

**Card** (`.pk-card`): a panel grouping a project, a form or a table.
**Section label** (`.pk-section-label`): the small uppercase heading inside one.

```html
<section class="pk-card"><h3 class="pk-section-label">Applications</h3>…</section>
```

**App card** (`.pk-app`): 28 px icon, the title as the browser link with `↗`, a badge, one
line of description, endpoint rows, and a footer with the configured version left and
Details right. S3 and other API endpoints are endpoint rows, never links.

```html
<article class="pk-app">
  <div class="pk-app-head">
    <img src="/icons/litellm.png" alt="">
    <h3><a href="https://litellm.example.com/ui/" target="_blank" rel="noreferrer">LiteLLM ↗</a></h3>
    <span class="pk-badge" data-state="healthy">Healthy</span>
  </div>
  <p class="pk-app-desc">Choose models, manage API keys and set budgets.</p>
  <div class="pk-endpoints">…endpoint rows…</div>
  <div class="pk-app-foot">
    <span>Configured v1.101.0</span>
    <button class="pk-button pk-plain" type="button">Details</button>
  </div>
</article>
```

**Endpoints** (`.pk-endpoints` with `.pk-endpoint` rows): label, value and an optional
`.pk-copy` button; the label column aligns across the rows of one list. A row without a
button lets the value span the last column, which suits detail rows (Image, Configured at).

```html
<div class="pk-endpoints">
  <div class="pk-endpoint">
    <span>S3</span><code>https://s3.example.com</code>
    <button class="pk-copy" type="button" aria-label="Copy S3 endpoint">…copy icon…</button>
  </div>
  <div class="pk-endpoint"><span>Image</span><code>rustfs/rustfs:1.0.0</code></div>
</div>
```

Set `data-copied` on `.pk-copy` briefly after a copy, and announce it in a
`role="status"` region (`.pk-sr-only` hides it visually).

**Badge** (`.pk-badge`) and **pill** (`.pk-pill`): the badge carries a state (below); the pill
is a neutral label such as the access mode.

**Notice** (`.pk-notice`): one bordered message; `data-state="warn"` or `"danger"` colours
its left edge.

```html
<p class="pk-notice" data-state="warn">Alert delivery is not configured.</p>
```

**Button and input** (`.pk-button`, `.pk-input`): `.pk-plain` makes a button look like a
link (Details), `.pk-danger` marks a destructive action.

```html
<button class="pk-button" type="button">Refresh</button>
<button class="pk-button pk-danger" type="button">Revoke</button>
<input class="pk-input" aria-label="Workspace name">
```

**List** (`.pk-list`): compact rows for supporting components or datastores: icon, text,
then a version or badge.

```html
<ul class="pk-list">
  <li><img src="/icons/postgres.svg" alt=""><span>PostgreSQL</span>
    <span class="pk-badge" data-state="configured">Configured</span></li>
</ul>
```

**Drawer** (`.pk-drawer`): a `<dialog>` opened with `showModal()`, pinned to the right edge
over a shaded backdrop. Escape closes it natively. Put the focus on its close button when it
opens, keep Tab inside it, and return focus to the control that opened it on `close`.

```html
<dialog class="pk-drawer" aria-labelledby="drawer-title">
  <div class="pk-drawer-head">
    <img src="/icons/litellm.png" alt=""><h2 id="drawer-title">LiteLLM</h2>
    <span class="pk-badge" data-state="healthy">Healthy</span>
    <button class="pk-button pk-close" type="button" aria-label="Close details">×</button>
  </div>
  <div class="pk-drawer-body"><section>…</section></div>
</dialog>
```

Focus rings (2 px accent outline) come from `.pk-page :focus-visible`. Under
`prefers-reduced-motion: reduce` the kit removes its transitions; it has no animations.
Under 700 px, cards, the header and the drawer tighten their padding; the drawer is 440 px
wide and never wider than the viewport.

## Badge states

Set `data-state`; the label text is the page's. Consoles use exactly these words.

| `data-state` | Colour | Label | Meaning |
| --- | --- | --- | --- |
| `healthy` (alias `ok`) | ok | Healthy | The Status Document lists it enabled and its Health Path answered 200. |
| `degraded` (alias `warn`) | warn | Degraded | Part of the application answers: one of its Health Paths is 200, another is unreachable, or a required feature is off. |
| `unreachable` (alias `danger`) | danger | Unreachable | The Health Path answered 502, 503 or 504, or timed out. |
| `unknown` | muted | Unknown | No evidence yet: not checked, another status code, or no Status Document. Never shown as a failure. |
| `configured` (alias `info`) | info | Configured | The Status Document lists it as enabled; nothing probes it. |
| `disabled` | muted outline | Disabled | The Status Document lists it with `enabled: false`. It is not probed. |
| `starting` | info | Starting | The page knows the component is starting. |

Reachability comes only from Health Paths and configuration only from the Status Document
(see the [status contract](operations/status-contract.md)); a badge never turns
configuration into Healthy.

## Platform home link

Every stack landing page and the Backplane dashboard header show a "Platform" link to the
Edge console when a platform origin is configured, as the first entry of
`.pk-header-links`. The origin comes from the stack's optional `*_PLATFORM_URL` setting
(`LG_PLATFORM_URL`, `OB_PLATFORM_URL`, `BP_PLATFORM_URL`). A standalone stack leaves it
empty and shows no link. The Edge console is the platform home and has no such link.

## What stays local

The kit holds tokens and components only. Each page keeps its own stylesheet, loaded after
the kit, for page layout (grids, columns, breakpoints beyond the kit's), tables, forms and
application-specific pieces. Local styles use the `--pk-*` tokens instead of literal
colours and do not restyle `pk-` classes; a need that several pages share goes into the kit
here instead.
