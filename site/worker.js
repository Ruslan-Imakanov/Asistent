// Cloudflare Worker "neuro-office": serves the public pages from this repository (folder site/).
// New page = new file site/<name>.html → available at /<name> after push (cached 5 min).
const BASE = "https://raw.githubusercontent.com/Ruslan-Imakanov/Asistent/claude/friendly-dirac-yz3iti/site/";
export default {
  async fetch(req) {
    const path = new URL(req.url).pathname.replace(/\/+$/, "") || "/index";
    const name = path.slice(1);
    if (!/^[a-z0-9-]{1,60}$/.test(name)) return new Response("Не найдено", { status: 404 });
    const r = await fetch(BASE + name + ".html", { cf: { cacheTtl: 300, cacheEverything: true } });
    if (!r.ok) return new Response("Страница не найдена", { status: 404, headers: { "content-type": "text/plain; charset=utf-8" } });
    return new Response(r.body, { headers: { "content-type": "text/html; charset=utf-8", "cache-control": "public, max-age=300" } });
  }
};
