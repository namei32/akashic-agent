from __future__ import annotations

import ast
import importlib
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from typing import Any

from .schema import digest, file_digest, write_json

HARNESS_REF = "4a172d3e339ff806b5b2641426fe50e78c0130c5"


def harness(root: Path) -> SimpleNamespace:
    """Reuse the frozen experimental harness without importing its production tree."""
    import eval

    sys.dont_write_bytecode = True

    names = ("__init__.py", "schema.py", "embeddings.py", "retrieval.py", "client.py")
    hashes = {}
    for name in names:
        relative = "eval/association/" + name
        expected = subprocess.check_output(
            ["git", "-C", str(root), "show", f"{HARNESS_REF}:{relative}"]
        )
        path = root / relative
        if path.read_bytes() != expected:
            raise ValueError(
                f"Experimental harness differs from pinned source: {relative}"
            )
        hashes[relative] = file_digest(path)
    eval.__path__.append(str(root / "eval"))
    modules = {
        name: importlib.import_module("eval.association." + name)
        for name in ("schema", "embeddings", "retrieval", "client")
    }
    retrieval_file = modules["retrieval"].__file__
    if (
        not isinstance(retrieval_file, str)
        or Path(retrieval_file).resolve().parent
        != (root / "eval/association").resolve()
    ):
        raise ValueError("Another evaluation harness was already imported")
    native = importlib.import_module("plugins.akasha.application.cycle")
    project = Path(__file__).resolve().parents[2]
    native_file = native.__file__
    if (
        not isinstance(native_file, str)
        or Path(native_file).resolve()
        != project / "plugins/akasha/application/cycle.py"
    ):
        raise ValueError("Evaluation loaded another checkout's memory engine")
    return SimpleNamespace(**modules, identity={"commit": HARNESS_REF, "files": hashes})


def functions(path: Path, names: tuple[str, ...]) -> dict[str, Any]:
    """Load only reviewed pure official prompt/metric functions, without model imports."""
    tree = ast.parse(path.read_text())
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    if {node.name for node in selected} != set(names):
        raise ValueError("Official scoring functions changed")
    namespace: dict[str, Any] = {"json": json}
    module = ast.Module(body=[], type_ignores=[])
    module.body.extend(selected)
    exec(compile(module, str(path), "exec"), namespace)
    return namespace


def source_identity(h: SimpleNamespace) -> dict:
    import importlib.metadata
    import numpy as np

    project = Path(__file__).resolve().parents[2]
    paths = [
        p
        for directory in (project / "eval/episodic", project / "plugins/akasha")
        for p in directory.rglob("*.py")
    ]
    provider = {}
    import os

    if root := os.environ.get("AKASHIC_EVAL_RUN_ROOT"):
        base = Path(root)
        provider = {
            name: file_digest(base / name)
            for name in (
                "provider_server.py",
                "refresh_desktop_auth.py",
                "provider-source-path.txt",
                "python-path.txt",
            )
        }
        provider_source = Path((base / "provider-source-path.txt").read_text().strip())
        provider["source_head"] = subprocess.check_output(
            ["git", "-C", str(provider_source), "rev-parse", "HEAD"], text=True
        ).strip()
        provider["artifact_python"] = digest(
            {
                str(p.relative_to(base / "model-plugin-home")): file_digest(p)
                for p in sorted((base / "model-plugin-home").rglob("*.py"))
            }
        )
    return {
        "head": subprocess.check_output(
            ["git", "-C", str(project), "rev-parse", "HEAD"], text=True
        ).strip(),
        "files": {str(p.relative_to(project)): file_digest(p) for p in sorted(paths)},
        "harness": h.identity,
        "provider": provider,
        "python": sys.version,
        "packages": {
            name: importlib.metadata.version(name)
            for name in ("numpy", "pydantic", "jieba")
        },
        "blas": np.__config__.show(mode="dicts"),
        "threads": {
            key: os.environ.get(key)
            for key in ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
        },
    }


def begin(output: Path, manifest: dict) -> None:
    if (output / "manifest.json").exists():
        if json.loads((output / "manifest.json").read_text()) != json.loads(
            json.dumps(manifest)
        ):
            raise ValueError(
                "Resume source, data, configuration or parent artifacts changed"
            )
    else:
        output.mkdir(parents=True, exist_ok=False)
        write_json(output / "manifest.json", manifest)


def parent(directory: Path, filename: str, suite, settings) -> list[dict]:
    from .schema import jsonl

    manifest = json.loads((directory / "manifest.json").read_text())
    if manifest["suite_sha256"] != digest(suite) or manifest[
        "settings"
    ] != settings.model_dump(mode="json"):
        raise ValueError("Input stage belongs to another dataset or configuration")
    receipt = json.loads((directory / "completion.json").read_text())
    if receipt["sha256"] != file_digest(directory / filename):
        raise ValueError("Completed stage records changed")
    return jsonl(directory / filename)
