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
