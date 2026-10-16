# Website maintenance

The public GitHub Pages website lives in `site/`. It is a static bilingual site: `index.html` is English and `zh.html` is Chinese. Keep their content and anchors aligned. Both pages share `styles.css` and `app.js`.

## Design and product previews

Use the console's neutral background, white surfaces, thin borders, 8px card corners and blue primary actions. The homepage contains an interactive, clearly labeled illustration of applications, routes, metrics, delivery and observability. Its `example.com` domains and resource values are sample data, not a live cluster or a screenshot. Keep route-relationship descriptions distinct from connectivity or health checks.

The illustration uses native HTML and SVG and works without remote images. All 25 sidebar menus and submenus switch to distinct illustrative screens and synchronize the breadcrumb and selected state. The five shortcut tabs share that navigation state and support arrow keys, Home and End. Keep the menu targets and sample screens aligned between both languages. JavaScript also provides the mobile menu and command copying; core page content and documentation remain readable without JavaScript.

The Geist font is self-hosted under `site/assets/fonts/` with its license. Chinese text uses the platform's CJK fonts. Existing brand assets remain available; the old decorative hero graphics are not loaded.

## Build and preview

Use Python 3.12, matching CI:

```bash
python -m venv /tmp/luma-site-venv
/tmp/luma-site-venv/bin/python -m pip install Markdown==3.8.2
/tmp/luma-site-venv/bin/python scripts/build-site.py
python scripts/check-site.py
python -m http.server 5182 --directory _site
```

Open `http://localhost:5182/` or `http://localhost:5182/zh.html`. To emulate a project Pages prefix, copy the artifact into a `luma/` directory and serve its parent. All local URLs must work under that prefix.

The build copies static assets and the raw documentation, then renders `docs/**/*.md` files (excluding internal `docs/research/` notes) into an adjacent HTML page. Markdown tables, fenced code and page outlines are supported. Relative links to Markdown documentation are rewritten to HTML; links to repository code and directories point to GitHub. Mermaid fences remain code examples rather than executing a remote diagram renderer.

Edit Markdown source, not generated HTML. Add high-priority guides to `GUIDES` in `scripts/build-site.py` and update both homepage document links when appropriate. Do not commit `_site/`. Generated output carries a `.luma-site-output` marker; subsequent builds replace only marked output directories, and refuse to overwrite an unrelated nonempty directory.

## Publishing

`.github/workflows/pages.yml` builds and deploys the site when relevant changes reach `main`, or when manually dispatched. It installs the pinned Markdown renderer and publishes the `_site` artifact using GitHub Pages.

A local build is not a public release. Review the English and Chinese pages, narrow-screen navigation, preview tabs, command-copy behavior and documentation links before merging and publishing.

## Chinese documentation

Every public English guide has a sibling `*.zh-CN.md` translation. Documents originally written in Chinese keep their existing paths. Keep translations synchronized when updating prose; preserve commands, configuration keys and fenced examples exactly. The generated CLI help remains the literal terminal output in both languages. Run the CLI reference generator first, then copy its updated help blocks into the Chinese reference.

Chinese pages localize navigation and resolve document links to Chinese counterparts. Explicit heading IDs (`{#original-heading-id}`) preserve existing English section links and language-switch anchors. Add matching IDs when translating new sections. English and Chinese homepage links should point to the corresponding language. `scripts/check-site.py` checks Chinese coverage, translated code blocks, heading IDs and local links before publication.
