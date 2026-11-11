/** Static export served by the FastAPI backend (single Railway service).
 *  In `next dev`, /api is proxied to the local backend instead. */
const dev = process.env.NODE_ENV === "development";
const config = dev
  ? { async rewrites() { return [{ source: "/api/:path*", destination: `${process.env.MERIDIAN_API ?? "http://localhost:8000"}/api/:path*` }]; } }
  : { output: "export", trailingSlash: true, images: { unoptimized: true } };
export default config;
