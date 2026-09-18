# One edge per host

A host running several stacks cannot give each stack the same public ports. We use one shared Caddy to own ports 80 and 443, obtain and renew certificates, and route hostnames over the Platform Network; separate port numbers for public applications would leak deployment details into login URLs, API clients and presigned links.

Each stack retains its standalone gateway and uses HTTP behind the Edge with its public HTTPS origin configured separately. Route Files use stable Upstream Aliases, resolved at request time, so an absent stack affects only its own hostnames. The Edge is a shared point of failure; its certificate volumes and network aliases are operational interfaces. Authentication stays with each application.
