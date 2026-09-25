# DJMAX-Stash

A tiny `djmax-cli` helper to download files from Cloudflare-hosted URLs.

## Usage

```bash
./djmax-cli download <target> [--base-url <url>] [-o <output>] [--force]
```

- `<target>` can be either:
  - a full URL (`https://...`), or
  - a path relative to a Cloudflare base URL.
- Base URL is read from `--base-url` or `DJMAX_CLOUDFLARE_BASE_URL`.

## Examples

```bash
# Use env-based Cloudflare base URL
export DJMAX_CLOUDFLARE_BASE_URL="https://example.cloudflare.com/files"
./djmax-cli download songs/track.zip

# Use a full URL directly
./djmax-cli download https://example.cloudflare.com/files/songs/track.zip -o track.zip
```
