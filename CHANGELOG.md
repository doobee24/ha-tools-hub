# HA Tools Hub — Changelog

## v0.02a — 2026-03-24

### First public release

#### Core
- Python backend with Supervisor API proxy — all hub pages work without a user token via ingress
- Generic REST proxy (`/api/proxy`) and WebSocket registry bridge (`/api/registry`) for entity, device, area and floor data
- IP restriction — port 8099 accepts connections from the Supervisor ingress proxy only
- Addon-mode auto-connect — no credential popup when opened via HA ingress

#### Pages
- **Health Score** — full dual-mode analysis (addon backend + standalone HAConn), auto-runs on load in addon mode
- **Device Explorer** — Home Passport, Integration DNA, Power Map, 15 panels, brand icons
- **System Analyser** — 18 live-data panels, device/integration/domain breakdown
- **Automation Manager** — 57 templates across 14 categories, live HA tab
- **Config Manager** — visual capability gallery, snapshots, YAML validator, Settings tab with Enable Themes + config check + HA restart
- **Dashboard Card Library** — Extract, Built-in, Generate, HACS, Icons and Saved tabs, live HA preview dashboard
- **Project Tracker** — Issues, Milestones, Roadmap, Tasks and Costs with dual-layer HA entity persistence

#### Design
- 28 themes across Standard, Premium Glow and Bitcoin Branded groups
- Brand icons for ~2000 integrations via HA Brands CDN with local API upgrade on HA 2026.3+
- Responsive nav with hamburger menu and sidebar drawer
- Custom icon and logo

---

## v0.01 — 2026-03-21

### Initial proof of concept
- Standalone web app running from `/config/www/`
- HAConn WebSocket module, OAuth 2.0 + PKCE authentication
- First 5 pages: Integration Audit, Entity Exporter, Dashboard Composer, Device Card Generator, Card Library
- 16 themes, shared CSS design system
