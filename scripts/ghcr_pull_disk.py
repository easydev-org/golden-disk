"""Rebuild a golden qcow2 from its ghcr OCI image.

    ghcr_pull_disk.py <windows|macos> <out.qcow2>

The disk is published as ONE tag whose LAYERS are consecutive pieces of the
qcow2 (`golden-windows:base` has 8, `golden-macos:base` has 9). Concatenating the
layer payloads in manifest order reproduces the original file exactly, and the
result is verified against the sha256 recorded in the manifest annotation before
it is moved into place.

Why not skopeo: it is not installed in the sandbox images, and the whole point of
this script is that a plain machine with python3 can produce the embedded disk.

Auth: the packages are PRIVATE, and ghcr's /token endpoint echoes back
base64(PAT) rather than a registry JWT — that base64 string is what the /v2
endpoints accept as a Bearer token. (Sending the PAT as Basic, or decoding the
base64 first, both give 401.)

Environment: GHCR_TOKEN (a PAT with read:packages).
"""

import base64
import json
import os
import ssl
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REG = "ghcr.io"
OWNER = "silvermelon233"
USER = "SilverMelon233"

PLAN = {
    "windows": ("golden-windows", "base"),
    "macos": ("golden-macos", "base"),
    # The Linux desktop disks: same idea (a tag whose layers are pieces of the
    # qcow2) but one file per layer — disks/vNN.qcow2 — and tags named after the
    # desktop they carry rather than "base".
    "gnome": ("golden-linux-desktop", "linux-desktop-gnome-v17"),
    "kde": ("golden-linux-desktop", "linux-desktop-kde-v27"),
}

ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE


def token(repo, pat):
    basic = base64.b64encode(f"{USER}:{pat}".encode()).decode()
    url = f"https://{REG}/token?scope=repository:{OWNER}/{repo}:pull&service={REG}"
    r = urllib.request.urlopen(
        urllib.request.Request(url, headers={"Authorization": "Basic " + basic}),
        timeout=30, context=ctx)
    return json.load(r)["token"]


def get(url, tok, accept=None):
    h = {"Authorization": "Bearer " + tok}
    if accept:
        h["Accept"] = accept
    return urllib.request.urlopen(urllib.request.Request(url, headers=h),
                                  timeout=120, context=ctx)


def main():
    which, out = sys.argv[1], sys.argv[2]
    repo, tag = PLAN[which]
    pat = os.environ.get("GHCR_TOKEN")
    if not pat:
        sys.exit("set GHCR_TOKEN (PAT with read:packages)")

    tok = token(repo, pat)
    man = json.load(get(f"https://{REG}/v2/{OWNER}/{repo}/manifests/{tag}", tok,
                        "application/vnd.oci.image.manifest.v1+json"))
    layers = man["layers"]
    want = (man.get("annotations") or {}).get("disk.sha256", "")
    print(f"{repo}:{tag}  layers={len(layers)}  expected sha256={want[:16] or '(none)'}")

    os.makedirs(os.path.dirname(out) or ".", exist_ok=True)
    tmp = out + ".part"
    with open(tmp, "wb") as fh:
        for i, l in enumerate(layers):
            dg = l["digest"]
            sz = l["size"]
            print(f"  layer {i + 1}/{len(layers)}  {sz / 1024 ** 2:8.1f} MB  {dg[:22]}", flush=True)
            with tempfile.NamedTemporaryFile(suffix=".blob", delete=False) as blob:
                bpath = blob.name
            try:
                # Download with curl, not urllib.
                #
                # urllib truncated a multi-hundred-MB layer mid-stream over the
                # egress proxy and the error only surfaced later, when tar tried
                # to read the file:
                #   EOFError: Compressed file ended before the end-of-stream
                #   marker was reached
                # curl retries and resumes, and it reports a short read itself.
                url = f"https://{REG}/v2/{OWNER}/{repo}/blobs/{dg}"
                r = subprocess.run(
                    ["curl", "-fL", "--http1.1", "--retry", "8",
                     "--retry-all-errors", "--retry-delay", "3",
                     "-H", f"Authorization: Bearer {tok}",
                     "-o", bpath, url],
                    capture_output=True, text=True)
                if r.returncode != 0:
                    sys.exit(f"blob download failed ({r.returncode}): {r.stderr.strip()[:200]}")
                got_sz = os.path.getsize(bpath)
                if got_sz != sz:
                    sys.exit(f"blob {dg} is {got_sz} bytes, manifest says {sz}")
                # Each layer is a tar holding one file. It may be tar+zstd, and
                # Python's tarfile cannot read zstd — "r:*" raises
                #   tarfile.ReadError: file could not be opened successfully
                # So decompress with GNU tar, which handles every compression the
                # registry can emit, and extract the single member to stdout.
                ex = subprocess.run(
                    ["tar", "--to-stdout", "-xf", bpath],
                    capture_output=True)
                if ex.returncode != 0:
                    sys.exit(f"layer {i}: tar failed: {ex.stderr[:200]!r}")
                data = ex.stdout
                if not data:
                    sys.exit(f"layer {i} is empty")
                fh.write(data)
                print(f"        -> {len(data) / 1024 ** 2:.1f} MB extracted", flush=True)
            finally:
                os.unlink(bpath)

    if want:
        got = subprocess.run(["sha256sum", tmp], capture_output=True, text=True).stdout.split()[0]
        if got != want:
            os.unlink(tmp)
            sys.exit(f"sha256 MISMATCH\ngot  {got}\nwant {want}")
        print(f"sha256 ok: {got}")

    os.replace(tmp, out)
    print(f"wrote {out}: {os.path.getsize(out)} bytes")


if __name__ == "__main__":
    main()
