# LAE Next.js standalone fixture

This fixture is the source of the pinned Next.js catalog template. It uses an
`npm ci` lockfile and Next.js standalone output so the Builder never selects a
moving package-manager version. The runtime is a non-root Node user and exposes
one HTTP service on port 3000.
