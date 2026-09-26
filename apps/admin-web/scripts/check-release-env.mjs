const issuer = (process.env.VITE_OIDC_ISSUER || "").trim();
const client = (process.env.VITE_OIDC_CLIENT_ID || "").trim();
const api = (process.env.VITE_API_BASE_URL || "/api").trim();

if (!issuer.startsWith("https://") || !client) {
  throw new Error("Release build requires HTTPS VITE_OIDC_ISSUER and VITE_OIDC_CLIENT_ID");
}
if (process.env.VITE_API_TOKEN) {
  throw new Error("Release build must not embed VITE_API_TOKEN");
}
if (api !== "/api" && !api.startsWith("https://")) {
  throw new Error("Release API base must be same-origin /api or HTTPS");
}
console.log("Release identity and API settings are present.");
