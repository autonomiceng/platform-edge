# Platform Edge

The terms this repository uses, one sentence each. Use these words in code, docs and
commits; avoid the listed alternatives.

**Edge**: the host's shared public entry point (one Caddy on ports 80 and 443) for every
installed platform stack.
_Avoid_: stack gateway, load balancer

**Route File**: the file under `routes.d/` holding the hostname-to-upstream routes owned
by one stack at the Edge.
_Avoid_: service discovery, registry

**Platform Network**: the external Docker network `platform`, with the fixed allocation
`172.30.0.0/24` and Edge at `172.30.0.2`, through which Edge and the installed stacks reach
each other.
_Avoid_: public network, application network

**Upstream Alias**: the stable network name of a stack's ingress target on the Platform
Network (`lg-gateway:80`, `ob-gateway:80`, `bp-server:3000`), independent of container
instances.
_Avoid_: container name, public hostname

**Checkpoint**: a recoverable copy of Edge's certificate and configuration volumes taken
at one stopped moment, with evidence of its integrity and CA identity.
_Avoid_: snapshot, certificate export

**Platform Contract**: the versioned table of network, ingress, TLS, status, secrets and
bootstrap interfaces shared by the four stacks, canonical in `docs/conventions.md` here
and vendored into the siblings.
_Avoid_: shared config, spec sheet

**Status Document**: a stack's public `/status.json` under contract 2, listing its
configured components and features without any claim about what is running.
_Avoid_: health report, inventory

**Health Path**: a stack's same-origin `/health/<component>` route that answers with a
status code and an empty body.
_Avoid_: healthcheck endpoint, readiness probe

**Issuer**: the source of a stack's HTTPS certificates, selected by `*_TLS_ISSUER`
independently of the access mode: the stack's internal CA, an ACME directory, or operator
certificate files.
_Avoid_: certificate mode, TLS provider

**Bundle**: the sibling stacks installed behind Edge by one bootstrap run with `--with`,
each receiving the Platform Contract's bundle settings and running its own bootstrap.
_Avoid_: orchestration, selected installation

**Tailnet Origin**: an application's `https://<name>.<tailnet>.ts.net` browser origin,
served by that hostname's own Tailscale node inside the Edge project and routed by Edge
over the Platform Network.
_Avoid_: Tailscale port, machine URL
