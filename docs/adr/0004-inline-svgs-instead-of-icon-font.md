# ADR-0004: Inline SVGs instead of icon font

## Status

Accepted (superseded v1 approach in v2.0)

## Context

v1 used a custom icon font (`static/icons.woff2`, ~34 KB) with Unicode codepoints referenced in templates (`<span class="se-icon">&#xe914;</span>`). Problems:

- A single `@font-face` declaration blocks initial render until the font loads
- Icons appear as squares (`□`) during the FOUT / FOIT window
- Color, size, and stroke-width are tied to CSS font properties — no per-icon styling
- Adding or modifying icons requires re-generating the font
- Unicode codepoints like `&#xE975;` are opaque in the source

Options considered:
1. **Stick with the icon font** — status quo, no work
2. **Switch to a third-party icon set** (Font Awesome, Material Icons) — adds a CDN dependency or bulk imports hundreds of unused icons
3. **Inline SVGs in templates** — each icon is an `<svg>` element with visible path data

## Decision

Replace the icon font with inline SVGs. All icons use a consistent format:

```html
<svg class="header-icon" viewBox="0 0 24 24"
     fill="none" stroke="currentColor"
     stroke-width="2" stroke-linecap="round" stroke-linejoin="round">
  <!-- path data -->
</svg>
```

Icons use Feather-style outline design and inherit color via `currentColor`.

## Consequences

**Positive:**
- Zero extra HTTP requests, no font loading
- Icons render immediately with the HTML
- Color, stroke, size adjustable per-instance via CSS
- Source is grep-able — `<path d="M3 9l9-7..."` is visible
- No build step, no font generator
- Works with CSP `font-src: 'self'` without additional rules

**Negative:**
- Templates are more verbose — each icon is ~10 lines of SVG markup
- Duplicated SVG code across templates (no shared icon library)
- SVGs are rendered by the browser's SVG engine, which has more surface than font rendering

**Neutral:**
- If icon reuse becomes painful, we can introduce Jinja2 macros (`{% macro icon_home() %}...{% endmacro %}`) or a central icon include file
