# Connecting to your Cloudflare storage

The app needs **two values**. Both come from the Worker you deploy — not from
R2's own settings:

| Value | What it is | Where you get it |
|---|---|---|
| **API URL** | the address Cloudflare gives your Worker | `https://djmax-stash-api.<your-subdomain>.workers.dev` — shown on the Worker's page after you deploy it |
| **Token** | a password **you invent**, stored in the Worker as the `APP_TOKEN` secret | you type it yourself; Cloudflare never generates one |

> **The one thing that surprises everyone:** secret values are *write-only*.
> Once you save `APP_TOKEN`, neither the dashboard nor `wrangler` will ever show
> it again. Keep a copy somewhere. If you lose it, just save a new one — nothing
> breaks, the old token simply stops working (which is also how you cut someone
> off later).

Make a strong one with:

```bash
python djmax_stash_cli.py new-token
```

---

## Which storage service? R2, not KV

Cloudflare has several lookalike entries in the sidebar. For this app the files
must live in **R2 Object Storage**:

| Service | What it's for | Use it here? |
|---|---|---|
| **R2 Object Storage** | files and blobs of any size, no egress fees | **yes — this is where your songs go** |
| Workers KV | small key-value data (config, flags); 25 MB per value | no — cannot hold songs |
| D1 / PostgreSQL | SQL databases | no |
| Images & Stream | Cloudflare's image/video products | no |

If you don't see your `djmax/…` files listed under **R2 Object Storage**, the app
has nothing to read yet — upload the tree there first (drag and drop works, or
`npx wrangler r2 object put`).

## Route A — dashboard only (no terminal)

You need: a Cloudflare account with R2 activated, and the name of your bucket.

**1. Find your bucket name**
Dashboard → **R2 object storage** → the bucket holding your `djmax/…` files.
Write down its exact name.

**2. Create the Worker**
**Workers & Pages** → **Create** → **Worker** → **Create Worker** → give it a
name (e.g. `djmax-stash-api`) → **Deploy**.

**3. Paste in the code**
On the Worker's page → **Edit code** → select everything in the editor and
delete it → paste the whole contents of [`worker/worker.js`](worker/worker.js)
→ **Deploy** (button top right).

**4. Give it access to the bucket**
Worker → **Settings** → **Bindings** → **Add** → **R2 bucket**

* **Variable name**: `BUCKET` ← must be exactly this, the code looks for it
* **R2 bucket**: pick your bucket from the dropdown

→ **Save** → **Deploy**.

*Careful:* the R2 option is **not** in the "Variables and Secrets" list. If you
only see Text/JSON there, you're in the wrong panel — go to **Bindings**.

**5. Add your token**
Worker → **Settings** → **Variables and Secrets** → **Add**

* **Type**: **Secret**
* **Variable name**: `APP_TOKEN` ← must be exactly this
* **Value**: the token you generated above

→ **Save** → **Deploy**.

**6. Copy the URL**
Back on the Worker's **Overview** page, copy the `https://…workers.dev` address.
If it says the workers.dev subdomain isn't enabled, click through the prompt to
pick a subdomain first (it's free).

**7. Paste both into the app**
**Connection** (top-left) → API URL + Token → **Test & connect** → **Save to
config**. Status dot goes green: `Connected - N DLC(s)`.

---

## Route B — terminal (wrangler)

```bash
python djmax_stash_cli.py new-token          # copy the token it prints
npm install -g wrangler
wrangler login

cd DJMAX-Stash/worker
cp wrangler.toml.example wrangler.toml       # edit bucket_name + ALLOWED_PREFIX
wrangler deploy                              # prints your API URL
wrangler secret put APP_TOKEN                # paste the token
```

Then paste the URL + token into the app exactly as in step 7 above.

---

## Which values go in `wrangler.toml` (or the dashboard vars)

| Setting | Default | Must match |
|---|---|---|
| `bucket_name` | — | your R2 bucket's name, exactly |
| binding name | `BUCKET` | must stay `BUCKET` (only in the dashboard route do you type this) |
| `ALLOWED_PREFIX` | `djmax/` | the folder your files live under. Everything outside it returns 403. |

Doing Route A and want to change `ALLOWED_PREFIX`? Add it under **Settings →
Variables and Secrets → Add → Text** with the value `djmax/` (or your folder),
then Deploy. Leaving it out is fine — `djmax/` is the default.

---

## Verifying without the GUI

```bash
# full check: connection + detected folder layout + DLC list
python djmax_stash_cli.py doctor --api-url https://YOUR-WORKER.workers.dev --token YOUR_TOKEN

# or the GUI's own headless check
python djmax_stash.py --selftest --api-url https://YOUR-WORKER.workers.dev --token YOUR_TOKEN
```

A good run ends with `Connection : OK`, a detected layout, and your DLC names.

## If something goes wrong

| Message | Meaning | Fix |
|---|---|---|
| `Not authorised (401)` | token mismatch | it's what you saved as `APP_TOKEN`, exactly, no spaces. Check it didn't get a newline when pasted |
| `Access denied (403)` | the request is outside `ALLOWED_PREFIX` | set `ALLOWED_PREFIX` to your folder, e.g. `djmax/` |
| `Could not reach …` | wrong URL or not deployed | open the URL in a browser; a Worker that's live answers `{"error":"Bad or missing token"}` (that's healthy — it means it's deployed and guarding correctly) |
| `Bucket unreachable` | binding missing or misnamed | the binding must be called exactly `BUCKET` |
| `Connected - 0 DLC(s)` | URL and token fine, folder layout different | click **Detect layout** in the Connection bar |

## What about R2 API tokens / S3 keys?

You don't need them for this app — that's the point of the Worker: the R2
credentials stay in Cloudflare and your friends only ever hold a read-only,
prefix-scoped token. (If you want S3 keys for some *other* tool, they're under
**R2 → Manage R2 API Tokens** — but don't ship those to anyone.)
