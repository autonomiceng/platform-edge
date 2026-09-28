// The platform's projects and applications, not a runtime inventory. Status Documents supply
// configured versions and enabled state; Edge's Health Paths supply reachability.
// Apps: `status` is the contract component ID, `health` the Edge probes under /health/,
// `open` the browser [hostname prefix, path] ("" is the console) and `endpoints` extra
// [label, hostname prefix, path] rows. Components: the project's remaining contract IDs.
const DATA = {
  projects: [
    {
      id: "edge",
      name: "Platform Edge",
      description: "Private access to your applications.",
      icon: "edge.svg",
      repo: "platform-edge",
      apps: [
        { id: "caddy", status: "caddy", name: "Caddy", icon: "caddy.svg", source: "caddyserver/caddy",
          description: "Routes every hostname and serves this console.",
          health: ["caddy"], open: ["", "/"], endpoints: [] },
      ],
      components: [],
    },
    {
      id: "backplane",
      name: "Agent Backplane",
      description: "Shared state and work for your agents.",
      icon: "backplane.svg",
      repo: "agent-backplane",
      apps: [
        { id: "backplane", status: "server", name: "Backplane", icon: "backplane.svg", source: "autonomiceng/agent-backplane",
          description: "Manage workspaces, agent access and approvals.",
          health: ["backplane"], open: ["backplane", "/dashboard/"], endpoints: [["API", "backplane", "/api/v1"]] },
      ],
      components: [
        { id: "postgres", name: "PostgreSQL", icon: "postgres.svg", description: "Workspace data, queues and audit history." },
        { id: "rustfs", name: "RustFS", icon: "rustfs.svg", description: "Optional object storage for agent files." },
        { id: "workerd", name: "workerd", icon: "workerd.svg", description: "Runs agent functions in isolated workers." },
        { id: "caddy", name: "Caddy", icon: "caddy.svg", description: "Standalone ingress; not used behind Edge." },
      ],
    },
    {
      id: "observability",
      name: "Observability",
      description: "Logs, metrics and traces in one place.",
      icon: "observability.svg",
      repo: "observability-stack",
      apps: [
        { id: "grafana", status: "grafana", name: "Grafana", icon: "grafana.svg", source: "grafana/grafana",
          description: "Explore dashboards, logs, metrics and traces.",
          health: ["observability"], open: ["grafana", "/"], endpoints: [] },
      ],
      components: [
        { id: "caddy", name: "Caddy", icon: "caddy.svg", description: "Routes requests to Grafana." },
        { id: "alloy", name: "Alloy", icon: "alloy.svg", description: "Collects telemetry and sends it to the stores." },
        { id: "loki", name: "Loki", icon: "loki.svg", description: "Stores and searches logs." },
        { id: "mimir", name: "Mimir", icon: "mimir.svg", description: "Stores and queries metrics." },
        { id: "tempo", name: "Tempo", icon: "tempo.svg", description: "Stores and searches traces." },
        { id: "rustfs", name: "RustFS", icon: "rustfs.svg", description: "Optional object storage for telemetry." },
      ],
    },
    {
      id: "gateway",
      name: "LLM Gateway",
      description: "Models, tracing and usage.",
      icon: "gateway.svg",
      repo: "llm-gateway-stack",
      apps: [
        { id: "litellm", status: "litellm", name: "LiteLLM", icon: "litellm.png", source: "BerriAI/litellm",
          description: "Choose models, manage API keys and set budgets.",
          health: ["litellm"], open: ["litellm", "/ui/"], endpoints: [["API", "litellm", ""]] },
        { id: "langfuse", status: "langfuse-web", name: "Langfuse", icon: "langfuse.svg", source: "langfuse/langfuse",
          description: "Explore traces, prompts and evaluations.",
          health: ["langfuse"], open: ["langfuse", "/"], endpoints: [["OTLP", "langfuse", "/api/public/otel"]] },
        // The S3 API is an endpoint for clients, never a browser link.
        { id: "rustfs", status: "rustfs", name: "RustFS", icon: "rustfs.svg", source: "rustfs/rustfs",
          description: "Object storage for Langfuse events, uploads and exports.",
          health: ["s3", "rustfs"], open: ["rustfs", "/rustfs/console/"], endpoints: [["S3", "s3", ""]] },
      ],
      components: [
        { id: "caddy", name: "Caddy", icon: "caddy.svg", description: "Routes requests to Gateway applications." },
        { id: "langfuse-worker", name: "Langfuse worker", icon: "langfuse.svg", description: "Processes queued events and exports." },
        { id: "postgres", name: "PostgreSQL", icon: "postgres.svg", description: "Model settings, accounts and project metadata." },
        { id: "clickhouse", name: "ClickHouse", icon: "clickhouse.svg", description: "Trace and analytics data." },
        { id: "valkey", name: "Valkey", icon: "valkey.svg", description: "Response cache and background queues." },
        { id: "postgres-exporter", name: "Postgres exporter", icon: "prometheus.svg", description: "Database metrics for Alloy." },
        { id: "valkey-exporter", name: "Valkey exporter", icon: "prometheus.svg", description: "Queue and cache metrics for Alloy." },
      ],
    },
  ],
};
