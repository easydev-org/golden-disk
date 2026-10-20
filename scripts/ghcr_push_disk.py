"""Publish a qcow2 to ghcr as an OCI image whose layers are pieces of the file.

    ghcr_push_disk.py <disk.qcow2> <ghcr.io/owner/repo> <tag>

This is the inverse of ghcr_pull_disk.py and produces exactly the layout that
script (and the sandbox image workflows) consume:

  * the file is split into fixed-size chunks, each stored as a tar layer holding
    ONE part file;
  * the manifest carries `disk.sha256` in its annotations, so a consumer can
    verify the reassembled file rather than trusting the transport.

Chunk size is a compromise: 2 GiB keeps the layer count low (17 GiB -> 9 layers)
while staying well under registries' per-blob limits.

Environment: GHCR_TOKEN (a token with write:packages).
"""

import base64
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import urllib.request

REG = "ghcr.io"
CHUNK = 1024 ** 3


def put(url, data, tok, ctype):
    req = urllib.request.Request(
        url, data=data, method="PUT",
        headers={"Authorization": "Bearer " + tok,
                 "Content-Type": ctype,
                 "Content-Length": str(len(data))})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return r.status, r.read()


def main():
    path, ref, tag = sys.argv[1], sys.argv[2], sys.argv[3]
    # ref = ghcr.io/owner/repo
    host, owner, repo = ref.split("/", 2)[0], ref.split("/", 2)[1], ref.split("/", 2)[2]
    pat = os.environ.get("GHCR_TOKEN") or sys.exit("set GHCR_TOKEN")
    user = os.environ.get("GHCR_USER", "easydev-org")

    basic = base64.b64encode(f"{user}:{pat}".encode()).decode()
    scope = f"repository:{owner}/{repo}:pull,push"
    url = f"https://{host}/token?scope={scope}&service={host}"
    tok = json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers={"Authorization": "Basic " + basic}),
        timeout=60))["token"]

    size = os.path.getsize(path)
    print(f"{path}: {size} bytes -> {ref}:{tag}")
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(64 << 20), b""):
            h.update(blk)
    disk_sha = h.hexdigest()
    print(f"sha256 {disk_sha}")

    layers = []
    n = (size + CHUNK - 1) // CHUNK
    with open(path, "rb") as f:
        for i in range(n):
            part = f.read(CHUNK)
            # Wrap the chunk in a tar so the layer is a normal filesystem layer.
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                info = tarfile.TarInfo(name=f"data.part{i:03d}")
                info.size = len(part)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(part))
            blob = buf.getvalue()
            digest = "sha256:" + hashlib.sha256(blob).hexdigest()
            print(f"  layer {i + 1}/{n}: {len(part) / 1024 ** 3:.2f} GiB "
                  f"({len(blob) / 1024 ** 2:.0f} MiB compressed-ish)", flush=True)

            # Blob upload: POST to start, then PUT the bytes to the returned
            # location. A single monolithic PUT is accepted by ghcr.
            start = urllib.request.urlopen(urllib.request.Request(
                f"https://{host}/v2/{owner}/{repo}/blobs/uploads/",
                data=b"", method="POST",
                headers={"Authorization": "Bearer " + tok,
                         "Content-Length": "0"}), timeout=300)
            loc = start.headers["Location"]
            if loc.startswith("/"):
                loc = f"https://{host}{loc}"
            sep = "&" if "?" in loc else "?"
            status, body = put(f"{loc}{sep}digest={digest}", blob, tok,
                               "application/octet-stream")
            print(f"           uploaded: {status}")
            layers.append({
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": digest,
                "size": len(blob),
            })

    config = json.dumps({"architecture": "amd64", "os": "linux",
                         "rootfs": {"type": "layers", "diff_ids": [l["digest"] for l in layers]}}).encode()
    cfg_digest = "sha256:" + hashlib.sha256(config).hexdigest()
    start = urllib.request.urlopen(urllib.request.Request(
        f"https://{host}/v2/{owner}/{repo}/blobs/uploads/", data=b"", method="POST",
        headers={"Authorization": "Bearer " + tok, "Content-Length": "0"}), timeout=300)
    loc = start.headers["Location"]
    if loc.startswith("/"):
        loc = f"https://{host}{loc}"
    sep = "&" if "?" in loc else "?"
    put(f"{loc}{sep}digest={cfg_digest}", config, tok, "application/octet-stream")
    print(f"  config uploaded")

    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": {"mediaType": "application/vnd.oci.image.config.v1+json",
                   "digest": cfg_digest, "size": len(config)},
        "layers": layers,
        "annotations": {
            "disk.sha256": disk_sha,
            "disk.size": str(size),
            "disk.filename": os.path.basename(path),
        },
    }
    mb = json.dumps(manifest).encode()
    status, _ = put(f"https://{host}/v2/{owner}/{repo}/manifests/{tag}", mb, tok,
                    "application/vnd.oci.image.manifest.v1+json")
    print(f"manifest {status}: {ref}:{tag} ({len(layers)} layers)")


if __name__ == "__main__":
    main()
