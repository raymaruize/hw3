# GitHub Pages (Static Demo)

This `docs/` folder is intended for **GitHub Pages**.

Because PRT TrueTime does **not** send CORS headers, a browser-only (static) page cannot call the PRT API directly.

This static UI therefore:
- renders with **mock data** by default, and
- optionally can fetch live data from a backend endpoint you provide at runtime (stored in localStorage):
  - `{API_BASE}/api/next`

If you want a fully-working public deployment (live data + secrets protected), deploy the backend (Flask) to a serverless host (Render/Fly/Railway) and point the GitHub Pages UI at it.
