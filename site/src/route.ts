/**
 * Where a request goes. The data host serves the bucket at `/` (release
 * bundle views hard-code `https://data.bioconice.cancerdatasci.org/...` URLs,
 * so it must never redirect); every other host serves the bucket under
 * `/data/` and the pages everywhere else.
 */
export type Route =
  | { kind: "data"; key: string; base: string }
  | { kind: "docs" };

export function route(url: URL): Route {
  const path = decodeURIComponent(url.pathname);
  if (url.hostname.startsWith("data.")) {
    return { kind: "data", key: path.slice(1), base: "" };
  }
  if (path === "/data" || path.startsWith("/data/")) {
    return { kind: "data", key: path.slice("/data/".length), base: "/data" };
  }
  return { kind: "docs" };
}

/**
 * Workers static assets answer old pretty URLs (`/guides/x/`) with a 307 to
 * the canonical path. Those moves are permanent, so send a 301: search
 * engines pass link equity and clients may cache it.
 */
export function permanentRedirect(res: Response): Response {
  if (res.status !== 307) return res;
  return new Response(null, { status: 301, headers: res.headers });
}
