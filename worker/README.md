# DJMAX Stash Worker

Cloudflare Worker that exposes a **read-only, prefix-scoped** view of an R2
bucket. This is what stops your friends from getting direct bucket access.

## Why this exists

If you hand out R2 S3 credentials, whoever gets them can list, download and
**delete** everything in your bucket. With this Worker in front:

| | R2 keys handed out | This Worker + app token |
|---|---|---|
| Read your music | yes | yes |
| Read *other* buckets/prefixes | yes | no - one prefix only |
| Delete / overwrite | yes | no (`ALLOW_DELETE=false`) |
| Revoke for one person | no | yes - rotate `APP_TOKEN` |
| Credentials visible in the app | plain text | token can't do anything destructive |

## Deploy

```bash
npm install -g wrangler
wrangler login

# 1. create the bucket (skip if it exists)
wrangler r2 bucket create djmax-stash

# 2. go to the worker folder
cd worker
cp wrangler.toml.example wrangler.toml
#    -> edit bucket_name / ALLOWED_PREFIX

# 3. deploy
wrangler deploy

# 4. set the token your friends will use (pick something long and random)
wrangler secret put APP_TOKEN
```

`wrangler deploy` prints the URL, e.g.
`https://djmax-stash-api.<your-subdomain>.workers.dev` — put that in the GUI
(Settings) or in `config.json` as `api_url`.

## Endpoints

| Route | Purpose |
|---|---|
| `GET /api/ping` | Health check: token valid, bucket reachable, shows scope |
| `GET /api/list?prefix=&delimiter=/&cursor=` | List objects / sub-folders |
| `GET /api/stats?prefix=` | File count + total bytes for a folder |
| `GET /api/search?q=&depth=` | Name search across the tree |
| `GET /api/file?key=` | Stream an object (supports `Range`, resumable) |
| `POST /api/delete` | Only if `ALLOW_DELETE=true` |

All routes require `Authorization: Bearer <APP_TOKEN>`.

## Locking it down further

* `ALLOWED_PREFIX` - trim it to only what you share, e.g. `djmax/By_DLC/`.
* `ALLOWED_ORIGINS` - set to your friend's origin if you ever build a web UI;
  desktop clients send no `Origin` and are unaffected.
* Cloudflare dashboard -> Security -> WAF -> Rate limiting rules: cap requests
  per IP to blunt scraping.
* Rotate `APP_TOKEN` (`wrangler secret put APP_TOKEN`) whenever you want to cut
  someone off. Everyone using the old token gets a 401 and a clear message.
* Cloudflare Zero Trust Access can gate the Worker behind an email allowlist -
  great if your friends are on Gmail. The GUI then needs the Access JWT cookie,
  so only do this if you're comfortable editing `HttpSession` headers.
