# LAE Compose golden application

This fixture is intentionally small, but it exercises the complete Compose
product contract:

- `web` and `admin` are separate public HTTP services;
- `worker` is an internal long-running process;
- `postgres` is a datastore with a required secret;
- `app-data` and `pg-data` are retained named volumes;
- every source-built service has its own build context.

The repository contains no Luma manifest. LAE Agent must generate and persist
the deployment plan, stable route references, volume declarations, and Luma
runtime manifest.

