#!/usr/bin/env python3
import argparse
import os
import pathlib
import shutil
import sys
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_ENV_VAR = "DJMAX_CLOUDFLARE_BASE_URL"


def _build_url(target: str, base_url: str | None) -> str:
    if urllib.parse.urlparse(target).scheme:
        return target
    if not base_url:
        raise ValueError(
            "A base URL is required for relative targets. Set --base-url or DJMAX_CLOUDFLARE_BASE_URL."
        )
    return urllib.parse.urljoin(base_url.rstrip("/") + "/", target.lstrip("/"))


def _default_output_path(url: str) -> pathlib.Path:
    name = pathlib.Path(urllib.parse.urlparse(url).path).name
    if not name:
        raise ValueError("Could not infer output filename from URL. Pass --output.")
    return pathlib.Path(name)


def download_file(url: str, output: pathlib.Path, force: bool) -> None:
    if output.exists() and not force:
        raise FileExistsError(f"Output already exists: {output}. Use --force to overwrite.")
    output.parent.mkdir(parents=True, exist_ok=True)

    with urllib.request.urlopen(url, timeout=60) as response:
        status = getattr(response, "status", None)
        if status is not None and status >= 400:
            raise urllib.error.HTTPError(url, status, "HTTP error", response.headers, None)
        with output.open("wb") as destination:
            shutil.copyfileobj(response, destination)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="djmax-cli", description="Download DJMAX files from Cloudflare")
    parser.add_argument("--base-url", default=os.getenv(DEFAULT_ENV_VAR), help=f"Cloudflare base URL (defaults to {DEFAULT_ENV_VAR})")

    subparsers = parser.add_subparsers(dest="command", required=True)
    download_parser = subparsers.add_parser("download", help="Download a file")
    download_parser.add_argument("target", help="File path relative to base URL, or full URL")
    download_parser.add_argument("-o", "--output", help="Output path (defaults to URL filename)")
    download_parser.add_argument("-f", "--force", action="store_true", help="Overwrite output if it exists")

    args = parser.parse_args(argv)

    if args.command == "download":
        try:
            url = _build_url(args.target, args.base_url)
            output = pathlib.Path(args.output) if args.output else _default_output_path(url)
            download_file(url, output, args.force)
        except (ValueError, FileExistsError, urllib.error.URLError, urllib.error.HTTPError) as exc:
            print(f"Error: {exc}", file=sys.stderr)
            return 1

        print(f"Downloaded {url} -> {output}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
