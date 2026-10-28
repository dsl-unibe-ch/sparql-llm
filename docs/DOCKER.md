# Self-hosting the chat assistant with Docker

This guide is for curators who run the chat assistant on their own machine. You need a terminal and Docker. You don't need Python or Node.js.

One machine runs one instance with this `compose.yml`. A second copy of the folder would share the same data volumes, so don't start one.

## Requirements

- Docker with Docker Compose (Docker Desktop on Windows and macOS, Docker Engine on Linux).
- About 4 GB of free RAM.
- About 5 GB of free disk space.
- Internet access on the first start. The app downloads an embedding model of about 2 GB. Allow up to 30 minutes on first start, depending on your connection. Later starts reuse the download.

## Start

1. Copy the example settings file:

   ```sh
   cp .env.example .env
   ```

2. Open `.env` in a text editor and fill in three values:

   - `AUTH_SECRET`: a long random string that signs login sessions. Generate one with `python -c "import secrets; print(secrets.token_urlsafe(48))"`, or type any long random text (at least 32 characters).
   - `ADMIN_EMAIL`: the email of the first administrator.
   - `ADMIN_PASSWORD`: the password of the first administrator.

3. Start the app:

   ```sh
   docker compose up -d
   ```

4. Open <http://localhost:8000> in your browser.

5. Check that the app is ready:

   ```sh
   docker compose ps
   ```

   The `STATUS` column should show `healthy`.

On the first start the model download takes a long time. During that time `docker compose ps` can show `unhealthy` on a slow connection. This is expected. It turns `healthy` when the download ends. To follow the progress:

```sh
docker compose logs -f app
```

Press Ctrl+C to stop following the logs. The app keeps running.

The app listens on `127.0.0.1:8000`, so only this machine can reach it. See "Exposing it publicly" below to change that.

## First login and inviting users

1. Open <http://localhost:8000> and sign in with `ADMIN_EMAIL` and `ADMIN_PASSWORD`.
2. Open the admin page at <http://localhost:8000/admin>.
3. In the **Add user** card, enter the person's email and a starting password, choose a role and press **Add user**. Send them the address and the password. Admins can add users and curators; curators can add plain users.

Roles:

- **Admin**: everything, including publishing datasets and managing users.
- **Curator**: uploads, checks and rebuilds the files of datasets; adds plain users.
- **User**: chats with the published datasets.

## Adding a dataset

A new install has no dataset of your own until you add one. You do this on the admin page, with three files you write yourself. The steps are in [ONBOARDING.md](ONBOARDING.md).

## Adding your LLM key

Without a language model the chat answers: "No language model is set up for your account yet. Add your API key and endpoint in Settings, then pick a model."

1. Open the API Settings page (<http://localhost:8000/api-settings>, or the **API Key** button in the chat header).
2. Enter the base URL and API key of any OpenAI-compatible service. Save.
3. Go back to the chat and pick a model.

Each user can save their own key this way. Saved keys are encrypted with `AUTH_SECRET`.

To give every user one shared key, set `OPENAI_BASE_URL`, `OPENAI_API_KEY` and `DEFAULT_LLM_MODEL` in `.env`, then run `docker compose up -d` again.

Settings an admin saves in the UI (for example the retrieval settings) are stored in `/app/data/settings.runtime.json` inside the data volume. A value set in `.env` always wins over a value saved in the UI.

## Exposing it publicly

By default only your own machine can reach the app. To let others reach it:

1. Put a reverse proxy with HTTPS in front of the app (for example Caddy or nginx). It must forward requests to port 8000. Getting a certificate and a domain name is up to you.
2. In `compose.yml`, change the port binding `"127.0.0.1:8000:8000"`. If the proxy runs on the same machine, keep it as is and point the proxy at `127.0.0.1:8000`. If the proxy runs elsewhere, bind a reachable address instead, and block direct access to port 8000 with your firewall.
3. In `.env`, set the public name of your site, without `https://`:

   ```sh
   APP_PUBLIC_HOST=chat.example.org
   ```

4. Apply the change:

   ```sh
   docker compose up -d
   ```

Without `APP_PUBLIC_HOST`, login and the MCP endpoints fail when the site is opened under its public name.

## Backup and restore

Everything you create lives in the Docker volume `sparql-llm-data`: users, datasets, search indexes, saved chats and settings. The model download in `sparql-llm-cache` doesn't need a backup.

**Back up:**

```sh
docker compose stop
docker run --rm -v sparql-llm-data:/d -v "${PWD}:/b" alpine tar czf /b/sparql-llm-data.tgz -C /d .
docker compose start
```

Keep your `.env`, or at least `AUTH_SECRET`, together with the archive. Saved provider keys are encrypted with `AUTH_SECRET`. Without it they can't be read, and users have to enter their keys again.

**Restore** into an empty volume (on a new machine, or after `docker compose down` and `docker volume rm sparql-llm-data`):

```sh
docker compose stop
docker run --rm -v sparql-llm-data:/d -v "${PWD}:/b" alpine tar xzf /b/sparql-llm-data.tgz -C /d
docker run --rm -v sparql-llm-data:/d alpine chown -R 1000:1000 /d
docker compose up -d
```

Use the same `AUTH_SECRET` as when the backup was made.

The commands work as written in PowerShell (Windows) and in a macOS or Linux terminal. Run them in the folder that holds `compose.yml`; the archive is written there.

If you prefer a folder on your disk to a named volume, replace the volume in `compose.yml` with a bind mount. The folder must be owned by user id 1000.

## Updating

```sh
docker compose pull
docker compose up -d
```

Your data stays in the volumes. To stay on a release instead of the newest image, set the version in `.env`:

```sh
SPARQL_LLM_VERSION=<release tag>
```

Replace `<release tag>` with a version tag from the image registry, then run the two commands above.

## Build from source

If you changed the code, build the image yourself instead of pulling it:

```sh
docker compose up -d --build
```

## Warning

Don't restart, stop or update the app while an index rebuild is running. A rebuild is running when the Search index card on the admin page says so. Wait until it ends. An interrupted rebuild is marked as failed after the restart and changes nothing; start it again.

## Checking the app

- <http://localhost:8000/healthz> answers when the app is alive.
- <http://localhost:8000/readyz> answers when the app is ready to serve (it fails while the data folder is unreadable).
