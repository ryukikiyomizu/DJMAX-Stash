# DJMAX Stash

A small desktop app that pulls songs, assets and whole DLCs out of a Cloudflare
R2 bucket — with a menu built around one specific layout:

```
djmax/                                    <- root prefix
└── By_DLC/                               <- DLC folder
    └── Arcaea/                           <- [ DLC Name ]
        ├── Songs/                        <- song container
        │   └── AI[UE]OON [737]/          <- [ Song Title ]
        │       ├── Chart and OGG/        <- .ogg keysound + .pt charts
        │       └── MV/                   <- .mp4 music video
        ├── Gears/
        └── Other Assets/
```

## The menu

| Menu item | What it downloads |
|---|---|
| **Download → Songs** | `<Song Title>/Chart and OGG/**` — the `.ogg` keysound, every `.pt` chart, and anything else inside that folder. MV excluded. |
| **Download → Assets** | `Gears/`, `Other Assets/` — everything in a DLC *except* the `Songs/` folder. |
| **Download → DLC Assets (including songs)** | The entire `<DLC Name>/` folder: assets **and** every song (charts, keysounds, MV). |
| Additional: *whole song folder* | `<Song Title>/**` including `MV/` — for when you want the video too. |
| Additional: *selected folder* | Any folder node you click, e.g. just `Gears/`. |

Paths are preserved on disk, so `djmax/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/`
lands as `<your save folder>/By_DLC/Arcaea/Songs/AI[UE]OON [737]/Chart and OGG/`.

## Why friends can't just grab your bucket

You never hand out R2 keys. Everything goes through the Worker in
[`worker/worker.js`](worker/worker.js), which holds the credentials server-side:

| | R2 S3 keys in the app | This Worker + app token |
|---|---|---|
| Read your music | yes | yes |
| Read anything outside `ALLOWED_PREFIX` | yes | **no** (403) |
| Delete or overwrite your stash | yes | **no** (`ALLOW_DELETE=false`) |
| Credentials visible in `config.json` | plain text | obfuscated token |
| Cut one person off | rotate everyone's keys | rotate `APP_TOKEN` |

If someone extracts the token, the worst they can do is download the prefix you
already shared with them.

---

## Connecting the app to your Cloudflare (2 minutes)

You need three things, then it's a copy-paste:

**1. Deploy the Worker** (this is the only part with a terminal):

```bash
npm install -g wrangler
wrangler login
cd worker
cp wrangler.toml.example wrangler.toml     # set bucket_name + ALLOWED_PREFIX
wrangler deploy                            # prints your Worker URL
wrangler secret put APP_TOKEN              # invent a long random token
```

**2. Copy the two values** `wrangler` gave you:

| What | Where it comes from | Looks like |
|---|---|---|
| API URL | the URL `wrangler deploy` printed | `https://djmax-stash-api.you.workers.dev` |
| Token | whatever you typed at `wrangler secret put APP_TOKEN` | `k7f2...` (your choice) |

**3. Paste them into the app**: click **Connection** at the top left → fill in
API URL and Token → **Test & connect** → **Save to config**. The bar collapses
and the DLC list loads. That's it — the values persist, so it's a one-time step.

**4. If your folders aren't called `djmax/By_DLC/…`**, click **Detect layout** in
the same bar: it reads the bucket, fills in the root prefix / DLC folder / song
folder names, saves them and reloads. Nothing to edit by hand. (For a bucket
whose files sit at the very top, or one that uses `Chart & OGG`, that's the
button to press.) Still stuck? `python djmax_stash_cli.py doctor --api-url … --token …`
prints what it found.

Check it worked: the status dot at the bottom goes green and says
`Connected - N DLC(s)`. If not, the Activity tab says why (401 = token typo,
403 = prefix outside `ALLOWED_PREFIX`, "could not reach" = wrong URL).

Prefer the command line? Same two values:

```bash
python djmax_stash_cli.py doctor --api-url https://your-worker.workers.dev --token YOUR_TOKEN
```

## Setup (you, once)

```bash
npm install -g wrangler
wrangler login
wrangler r2 bucket create djmax-stash        # skip if you already have one

cd worker
cp wrangler.toml.example wrangler.toml       # edit bucket_name + ALLOWED_PREFIX
wrangler deploy                              # prints your Worker URL
wrangler secret put APP_TOKEN                # invent a long random token
```

Then either put the URL + token in the app's **Connection** bar (it saves them),
or bake them into a build for your friends:

```bash
python tools/build_release.py --api-url https://djmax-stash-api.you.workers.dev --token YOUR_TOKEN --zip
# -> dist/DJMAX-Stash/  (run.bat, run.sh, config.json with the token baked in)
# -> dist/DJMAX-Stash.zip

python tools/build_release.py --api-url ... --token ... --exe   # needs: pip install pyinstaller
```

---

## Running the app

```bash
python djmax_stash.py                # the GUI
```

Nothing to install: standard library only, Python 3.8+. Tkinter ships with the
python.org installers; on Linux you may need `sudo apt install python3-tk`.

**Your friends' copy:** unzip, then double-click `run.bat` (Windows) or
`./run.sh` (macOS/Linux). The server URL and token are already in `config.json`.

Common flags:

```bash
python djmax_stash.py --demo              # use the local mock server (below)
python djmax_stash.py --output "D:/DJMAX Stash"
python djmax_stash.py --api-url URL --token XXX --no-autoconnect
python djmax_stash.py --selftest          # headless check, no window
python djmax_stash.py --setup-help
```

In the window:

* Tick songs or whole DLCs in the tree (multi-select works), then use the
  buttons on the right or the Download menu.
* The **Filter** box hides anything that doesn't match (it searches DLC names,
  folders and song titles).
* **Transfers** shows live progress, speed and ETA; **Activity** is a log;
  **Options** holds re-download / checksum / resume / open-folder preferences.
* Cancel is always available — partial files are kept as `.part` and resume
  automatically next time.
* Every download is MD5-verified against the bucket, so a truncated file is
  caught and re-fetched instead of silently kept.

## Settings that describe your layout

Set these in `config.json` (see [`config.example.json`](config.example.json)) if
your bucket differs from the default:

| Key | Default | Meaning |
|---|---|---|
| `root_prefix` | `"djmax/"` | everything shared lives under this prefix |
| `dlc_dir` | `"By_DLC"` | folder holding one folder per DLC |
| `song_dir` | `"Songs"` | song container inside each DLC |
| `chart_dirs` | `["Chart and OGG", "Chart & OGG", "Chart+OGG", "Chart_OGG"]` | all accepted spellings; matching ignores case, spaces, `&` and the word "and" |
| `workers` | `4` | parallel download threads (1–16) |
| `output_dir` | `~/DJMAX Stash` | where files land |

Run `python djmax_stash_cli.py doctor` and it will guess your layout by looking
at the bucket, then tell you what to put in the config.

---

## Command line (same engine)

```bash
python djmax_stash_cli.py doctor                       # diagnose + detect layout
python djmax_stash_cli.py list                         # DLCs and sizes
python djmax_stash_cli.py list --dlc Arcaea --songs    # songs inside a DLC
python djmax_stash_cli.py songs --dlc Arcaea           # chart + keysounds
python djmax_stash_cli.py songs --all --song Halcyon   # filter by title
python djmax_stash_cli.py assets --all                 # gears/assets only
python djmax_stash_cli.py dlc --dlc Deemo              # assets + songs
python djmax_stash_cli.py folder "djmax/By_DLC/Arcaea/Gears"
```

Useful for a cron job or a "grab everything overnight" run.

## Trying it without Cloudflare

```bash
python tools/make_demo_bucket.py     # builds tools/demo_bucket/ with the layout above
python tools/mock_worker.py          # serves it on http://127.0.0.1:8787, token: dev-token
python djmax_stash.py --demo         # GUI pointed at the mock
```

The demo bucket contains placeholder files (valid OggS headers, random `.pt`
data) so you can watch a real transfer — including a 2.5 MB MV to make the
speed/ETA readouts move.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Not authorised (401)` | token typo, or you rotated `APP_TOKEN` — re-copy it |
| `Access denied (403)` | the prefix you asked for is outside `ALLOWED_PREFIX` in `wrangler.toml` |
| `Could not reach ...` | wrong URL, or the Worker isn't deployed |
| "Nothing to download" | the layout is wrong — run `djmax_stash_cli.py doctor` |
| Empty song list for a DLC | `song_dir` doesn't match your bucket (`Songs`) |
| Tkinter missing message | `sudo apt install python3-tk` (Linux); Windows/macOS: reinstall Python from python.org |
| A song shows "no chart folder" | that song has no `Chart and OGG`; the app downloads the whole song folder instead |

Run `python djmax_stash.py --selftest` to check the whole chain without opening
a window (it uses the same config, so `--selftest --api-url ... --token ...`
works as a connection test), and check the Cloudflare Worker logs (`wrangler tail`) if the app says
the connection failed.

## Layout of this repo

```
djmax_stash.py          GUI (Tkinter) - thin: widgets + event rendering
stash_tasks.py          background tasks, download plans, event protocol
stash_core.py           everything else: HTTP client, bucket model, downloader
djmax_stash_cli.py      command line interface
worker/worker.js        Cloudflare Worker (R2 binding, token auth, read-only)
worker/wrangler.toml.example
tools/mock_worker.py    local Worker stand-in, same API
tools/make_demo_bucket.py
tools/build_release.py  package a build for friends (folder / zip / .exe)
tests/                  75 python tests + 29 worker checks
```

## Tests

```bash
python -m unittest discover -s tests -t .   # python: core + GUI (75 tests)
node tests/test_worker.mjs                  # the Worker against a fake R2 binding
```

Covers path sanitising, config loading, the HTTP client, auto-detected bucket
layouts, resumable/verified downloads and every GUI menu action. The worker
suite exercises token auth, prefix scoping, range/resume requests and the
delete gate. The GUI tests
run the real `djmax_stash.py` against a virtual-Tk stub
([`tests/tk_stub.py`](tests/tk_stub.py)), so they work on a headless machine and
also assert that the GUI never calls a Tk method that doesn't exist.
