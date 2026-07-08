// tl-report — minimal JS client for the ThroughLog agent endpoint (Node 18+ / fetch).
//
// Same contract as throughlog/agent_sdk.py: build a schema-v2 AGENT_REPORT and POST it to
// the ThroughLog endpoint (local capture endpoint or a cloud relay). One call and your
// agent / CI job / n8n node writes itself into the work journal.
//
//   import { AgentReporter } from "./tl-report.mjs";
//   const r = new AgentReporter({ identity: "agent:ci", token: process.env.SAL_TOKEN });
//   await r.report("deployed v1.2.3", { repo: "github.com/me/app", status: "success" });

export const SCHEMA_VERSION = 2;
export const DEFAULT_ENDPOINT = "http://127.0.0.1:8787/report";

/** Build the report object the /report endpoint validates. Pure — no network. */
export function buildReport({
  summary, identity, tool = "", project = null, repo = null,
  files = null, status = "", sessionId = "", tsWall = null,
  eventType = "AGENT_REPORT", extra = null,
} = {}) {
  const payload = { summary: String(summary), tool };
  if (files && files.length) payload.files = files.map(String);
  if (repo) payload.repo = String(repo);
  if (project) payload.project_hint = String(project);
  if (status) payload.status = String(status);
  if (extra) Object.assign(payload, extra);
  return {
    schema_version: SCHEMA_VERSION,
    type: eventType,
    source: { kind: "agent", adapter: tool || "agent_sdk", identity, session_id: sessionId },
    ts_wall: tsWall || new Date().toISOString(),
    payload,
  };
}

export class AgentReporter {
  constructor({ identity, endpoint = DEFAULT_ENDPOINT, token = null,
                tool = "", sessionId = "", timeoutMs = 5000 } = {}) {
    this.identity = identity;
    this.endpoint = endpoint;
    this.token = token;
    this.tool = tool;
    this.sessionId = sessionId;
    this.timeoutMs = timeoutMs;
  }

  async report(summary, opts = {}) {
    const report = buildReport({
      summary, identity: this.identity,
      tool: opts.tool ?? this.tool, sessionId: opts.sessionId ?? this.sessionId,
      project: opts.project, repo: opts.repo, files: opts.files,
      status: opts.status, extra: opts.extra,
    });
    return this.send(report);
  }

  async send(report) {
    const headers = { "Content-Type": "application/json" };
    if (this.token) headers["Authorization"] = `Bearer ${this.token}`;
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), this.timeoutMs);
    try {
      const resp = await fetch(this.endpoint, {
        method: "POST", headers, body: JSON.stringify(report), signal: ctrl.signal,
      });
      return { ok: resp.ok, transport: "http", status: resp.status };
    } catch (err) {
      // Never throw out of a reporter — an agent's main job must not crash here.
      return { ok: false, transport: "failed", error: String(err) };
    } finally {
      clearTimeout(t);
    }
  }
}
