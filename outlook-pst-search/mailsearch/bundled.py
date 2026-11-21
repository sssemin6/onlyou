"""Restore the bundled Ollama embedding model from split parts on first run."""
import hashlib
from pathlib import Path


def prepare(models_root, bundle=None):
    """Materialize the bundled bge-m3 under models_root and return it.

    The large model blob ships as .partNN chunks so it fits in the repository;
    parts are joined once, verified by digest, and marked complete. Returns
    None when no bundle is present next to the package.
    """
    source = Path(bundle) if bundle else Path(__file__).resolve().parent.parent / "vendor" / "ollama-model" / "bge-m3"
    manifest = source / "manifest.json"
    if not manifest.is_file():
        return None
    models_root = Path(models_root)
    marker = models_root / ".bge-m3-restored"
    if marker.is_file():
        return models_root
    blobs = models_root / "blobs"
    blobs.mkdir(parents=True, exist_ok=True)
    for small in sorted((source / "blobs").iterdir()):
        target = blobs / small.name
        if not target.is_file():
            target.write_bytes(small.read_bytes())
    parts = sorted((source / "parts").glob("*.part*"))
    if parts:
        target_name = parts[0].name[: -len(".part00")]
        target = blobs / target_name
        if not target.is_file():
            digest = hashlib.sha256()
            with target.open("wb") as joined:
                for part in parts:
                    with part.open("rb") as stream:
                        while chunk := stream.read(1 << 20):
                            digest.update(chunk)
                            joined.write(chunk)
            if digest.hexdigest() != target_name[len("sha256-"):]:
                target.unlink(missing_ok=True)
                raise RuntimeError("bundled_model_checksum")
    manifest_target = models_root / "manifests" / "registry.ollama.ai" / "library" / "bge-m3"
    manifest_target.mkdir(parents=True, exist_ok=True)
    (manifest_target / "latest").write_bytes(manifest.read_bytes())
    marker.write_text("ok\n")
    return models_root
