import http from "node:http";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

const root = path.dirname(fileURLToPath(import.meta.url));
const port = Number(process.env.PORT || 8080);
const apiHost = process.env.MEDIA_API_HOST || "127.0.0.1";
const apiPort = Number(process.env.MEDIA_API_PORT || 8081);
const domain = (process.env.DOMAIN || "").trim().toLowerCase().replace(/^https?:\/\//, "").replace(/\.$/, "");

const contentTypes = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
};

// ---------- mDNS (Bonjour / Zeroconf) so diwan.local works on any device ----------
/** @type {import("multicast-dns").MulticastDNS | null} */
let mdns = null;

function lanIPv4() {
  const nets = os.networkInterfaces();
  for (const iface of Object.values(nets)) {
    for (const addr of iface || []) {
      if (addr.family === "IPv4" && !addr.internal) return addr.address;
    }
  }
  return null;
}

function startMdns() {
  if (!domain || !domain.endsWith(".local")) return;
  const ip = lanIPv4();
  if (!ip) return;

  // Dynamic import  -  multicast-dns is ESM-only in recent versions
  import("multicast-dns").then(({ default: createMdns }) => {
    mdns = createMdns();
    mdns.on("query", (query) => {
      for (const q of query.questions) {
        if (q.type === "A" && q.name === domain) {
          mdns.respond({ answers: [{ name: domain, type: "A", data: ip, ttl: 120 }] });
        }
      }
    });
    mdns.on("error", () => { /* mDNS port may be in use  -  silently ignore */ });
    console.log(`mDNS: ${domain} -> ${ip}`);
  }).catch(() => { /* multicast-dns not installed  -  skip */ });
}

function stopMdns() {
  if (mdns) { try { mdns.destroy(); } catch {} mdns = null; }
}

function proxy(request, response) {
  const upstream = http.request({
    hostname: apiHost,
    port: apiPort,
    method: request.method,
    path: request.url,
    headers: { ...request.headers, host: `${apiHost}:${apiPort}` },
  }, upstreamResponse => {
    response.writeHead(upstreamResponse.statusCode || 502, upstreamResponse.headers);
    upstreamResponse.pipe(response);
  });
  upstream.on("error", error => {
    response.writeHead(502, { "Content-Type": "application/json" });
    response.end(JSON.stringify({ detail: `Media server unavailable: ${error.message}` }));
  });
  request.pipe(upstream);
}

function handleRequest(request, response) {
  if (request.url.startsWith("/api/")) return proxy(request, response);
  const pathname = decodeURIComponent(new URL(request.url, "http://localhost").pathname);
  const candidate = path.resolve(root, `.${pathname}`);
  const safe = candidate.startsWith(root + path.sep) && fs.existsSync(candidate) && fs.statSync(candidate).isFile();
  const file = safe ? candidate : path.join(root, "index.html");
  response.writeHead(200, { "Content-Type": contentTypes[path.extname(file)] || "application/octet-stream", "Cache-Control": "no-cache, no-store, must-revalidate", "Pragma": "no-cache", "Expires": "0" });
  fs.createReadStream(file).pipe(response);
}

http.createServer(handleRequest).listen(port, "0.0.0.0", () => {
  if (domain) startMdns();
  const addrs = [];
  if (domain) addrs.push(`http://${domain}${port === 80 ? '' : ':' + port}`);
  addrs.push(`http://localhost:${port}`);
  const nets = os.networkInterfaces();
  for (const iface of Object.values(nets)) {
    for (const addr of iface || []) {
      if (addr.family === "IPv4" && !addr.internal) addrs.push(`http://${addr.address}:${port}`);
    }
  }
  console.log(`Media portal: ${addrs.join("  |  ")}`);

  // If a custom domain is set, also serve directly on port 80 (no port in URL).
  if (domain && port !== 80) {
    const server80 = http.createServer(handleRequest);
    server80.on("error", error => { console.warn(`Could not listen on port 80 for ${domain}: ${error.message}. Use http://${domain}:${port}`); });
    server80.listen(80, "0.0.0.0", () => {
      console.log(`Also serving on http://${domain}`);
    });
  }
});

process.on("SIGINT", () => { stopMdns(); process.exit(); });
process.on("SIGTERM", () => { stopMdns(); process.exit(); });
