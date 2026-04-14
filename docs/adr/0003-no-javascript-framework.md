# ADR-0003: No JavaScript framework

## Status

Accepted

## Context

The UI needs interactive features — sortable tables, live sync progress polling, lightbox dialogs, theme toggle, language switcher, restore test forms. A common pattern would be to use React / Vue / Svelte with a build step.

Options considered:
1. **Single-page app (React / Vue)** — modern DX, component reuse, virtual DOM. Requires a build pipeline, bundler, and a second backend (the API).
2. **HTMX** — server-driven interactivity without a SPA. Good fit but adds a dependency and changes the mental model.
3. **Alpine.js** — lightweight reactivity sprinkled onto server-rendered HTML.
4. **Plain HTML + vanilla JS** — no build step, no dependencies, 100% server-rendered with progressive enhancement.

## Decision

Use plain HTML templates (Jinja2) with vanilla JavaScript (`static/app.js`) for the small number of interactive features. No build step, no bundler, no framework.

## Consequences

**Positive:**
- Zero build step — edit a file, reload the browser, done
- No npm / node dependency — Python-only toolchain
- Small payload (~20 KB of JS), no framework runtime
- Server-rendered HTML works without JavaScript for most views (accessibility, search engines)
- Code stays simple and readable for Python-focused maintainers
- Source maps, transpilation, polyfills are all non-issues

**Negative:**
- Interactive features are more verbose than with declarative frameworks
- No component reuse — duplicated patterns (lightboxes, sync dots) would need manual refactoring
- No type checking on the client side
- Polling-based progress updates are less elegant than WebSockets / SSE would be

**Neutral:**
- As the app grows, individual interactive pages (e.g. the cluster detail view) may hit a complexity ceiling. At that point introducing HTMX or Alpine for specific pages is more appealing than migrating the whole UI to a SPA.
