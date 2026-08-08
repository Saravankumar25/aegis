import type { NextConfig } from "next";

// Origin of the Aegis API for same-origin proxying. Overridable so that repointing the
// dashboard at a different backend is a deployment setting rather than a commit; the default
// is the production host, which is a public address and not a secret.
const API_ORIGIN = process.env.AEGIS_API_ORIGIN ?? "http://13.203.209.44";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // The browser must reach the API on the dashboard's own origin, not directly.
  //
  // Session auth rides on httpOnly cookies flagged `Secure` + `SameSite=Strict` (ESD §8,
  // api/security.py). Served from an HTTPS host (Vercel) against a plain-HTTP API on another
  // address, all three of those fail independently: a `Secure` cookie delivered over http is
  // discarded, `SameSite=Strict` withholds it on a cross-site request, and the browser blocks
  // the fetch as active mixed content before CORS is ever consulted. Proxying through this
  // origin makes every API call same-origin, which resolves all three without weakening a
  // single cookie flag.
  //
  // Only reached where the page itself is HTTPS — `lib/api.ts` addresses the API host directly
  // when served over plain HTTP, so an EC2-hosted dashboard still talks to its own backend and
  // this rewrite cannot silently cross environments.
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${API_ORIGIN}/api/v1/:path*` }];
  },
  // Emits `.next/standalone` with a self-contained server and only the node_modules actually
  // reached by the build, so the runtime image carries neither the dev toolchain nor the full
  // dependency tree it was compiled with.
  output: "standalone",
};

export default nextConfig;
