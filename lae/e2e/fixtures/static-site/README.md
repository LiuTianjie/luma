# Static upload acceptance fixture

This fixture exercises both supported file paths:

- upload `index.html` directly for the single-file HTML flow;
- archive `index.html` and `assets/site.css` as a ZIP for the packaged static
  artifact flow.

The page contains the stable marker `lae-static-e2e-v1` so a live deployment
can be checked without depending on presentation text.
