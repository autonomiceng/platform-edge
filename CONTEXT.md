# Platform Edge

The shared ingress for independent application stacks on one host.

## Language

**Edge**:
The host's shared public entry point for all installed platform stacks.
_Avoid_: Stack Gateway, load balancer

**Route File**:
The set of hostname-to-upstream routes owned by one stack at the Edge.
_Avoid_: Service discovery, registry

**Platform Network**:
The trusted shared network through which the Edge and installed stacks reach each other.
_Avoid_: Public network, application network

**Upstream Alias**:
A stable network name identifying a stack's ingress target independently of its individual instances.
_Avoid_: Container name, public hostname

**Checkpoint**:
A recoverable copy of the Edge's certificate and configuration state at one stopped moment, with evidence of its integrity and CA identity.
_Avoid_: Snapshot, certificate export

**Platform Contract**:
The versioned table of network, ingress, TLS, status, secrets and bootstrap interfaces shared by the four stacks, canonical in this repo's `docs/conventions.md` and vendored into the siblings.
_Avoid_: Shared config, spec sheet

**Status Document**:
A stack's public `/status.json` under contract 2, listing its configured components and features without any claim about what is running.
_Avoid_: Health report, inventory

**Issuer**:
The source of a stack's HTTPS certificates, selected by `*_TLS_ISSUER` independently of the access mode: the stack's internal CA, an ACME directory, or operator certificate files.
_Avoid_: Certificate mode, TLS provider

**Health Path**:
A stack's same-origin `/health/<component>` route that answers with a status code only.
_Avoid_: Healthcheck endpoint, readiness probe

**Bundle**:
The sibling stacks installed behind Edge by one bootstrap run with `--with`, each receiving the Platform Contract's bundle settings and running its own bootstrap.
_Avoid_: Orchestration, selected installation

**Tailnet Origin**:
An application's `https://<name>.<tailnet>.ts.net` browser origin, served by that hostname's own Tailscale node inside the Edge project and routed by Edge over the Platform Network.
_Avoid_: Tailscale port, machine URL
