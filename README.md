# Model 1 Studio

Dashboard, job queue, and image gallery for an external Model 1 worker.

## Deploy

In Arcane, register this repository and create a Git Sync for `compose.yaml`. Provide these values in the deployment configuration:

- `STUDIO_PASSWORD` is the dashboard password for the `studio` account.
- `STUDIO_WORKER_KEY` is the shared key for the worker and server.
- `STUDIO_SESSION_KEY` signs browser sessions.

Use independent randomly generated values of at least 24 characters. For example, generate each value with `openssl rand -hex 32` a separate time.

Connect the service to your reverse proxy and route your HTTPS domain to container port `8080`. For Caddy Docker Proxy, select `compose.override.yaml` as the override and set `STUDIO_DOMAIN` and `STUDIO_PROXY_NETWORK`. The base Compose file publishes no host port. Run one server replica and keep the `studio-data` volume across deployments.

On the Mac, set the worker endpoint to `https://YOUR_STUDIO_DOMAIN/backend.php` and set its `worker_key` to the same value as `STUDIO_WORKER_KEY`. Start the existing worker. The dashboard stays online while the worker is offline. Queued jobs expire after 45 minutes.

See [the worker protocol](site/MODEL1_SITE_SPEC.md) for the API.
