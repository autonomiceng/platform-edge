# Use one Tailscale machine name with separate application ports

For optional Tailscale access, give each application an HTTPS port on the machine's existing Tailscale name. This is an exception to ADR-0001's preference for public application subdomains: the machine certificate does not cover invented subdomains, and application path prefixes would require fragile rewriting of login URLs and signed S3 requests. Edge preserves the original hostname and port, while each application receives an explicit browser URL; public-domain and standalone installations retain their existing routes.

The setup helper pins Edge's current platform-network address because sibling gateways trust that exact peer. (Note, 2026-09-23: pinning moved to the fixed Edge address `PE_EDGE_IP` on the platform contract's network allocation; the helper no longer writes `PE_TAILSCALE_EDGE_IP` or `compose.tailscale.yaml`, and sibling trust uses that fixed address.) Application URLs require setup before they can be advertised as usable remote links. Missing stacks remain optional and do not prevent Edge or other stacks from running.

Tailscale supplements local access: Edge keeps loopback HTTP and self-signed HTTPS, and each Tailscale listener created by this setup forwards to the same loopback HTTP listener. Sibling gateways still use HTTP behind Edge. Applications retain one configured browser origin for login and generated links.

Native Backplane and Observability storage consoles use optional ports 8450 and
8451. Their client allowlists require Edge to preserve Tailscale's verified client
address. The helper therefore trusts only the current host bridge gateway at
Edge's loopback listener and sends one validated address to each console gateway.
Caddy computes client addresses globally; existing application routes explicitly
retain their previous forwarding behavior. Host and Docker administrators remain
trusted. Native console authentication is still required.
