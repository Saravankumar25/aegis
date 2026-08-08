import type { NextConfig } from "next";

const nextConfig: NextConfig = {
  reactStrictMode: true,
  // Emits `.next/standalone` with a self-contained server and only the node_modules actually
  // reached by the build, so the runtime image carries neither the dev toolchain nor the full
  // dependency tree it was compiled with.
  output: "standalone",
};

export default nextConfig;
