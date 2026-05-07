"""Batch-folder lifecycle: create, locate, list."""

from __future__ import annotations

import shutil
from pathlib import Path

from ..settings import BATCHES_ROOT, batch_dir, is_valid_batch_id, new_batch_id


SUBFOLDERS = ["input", "processed", "export", "logs", "manual_review"]


def create_batch() -> str:
    bid = new_batch_id()
    root = batch_dir(bid)
    for sub in SUBFOLDERS:
        (root / sub).mkdir(parents=True, exist_ok=True)
    return bid


def list_batches() -> list[dict]:
    if not BATCHES_ROOT.is_dir():
        return []
    out: list[dict] = []
    for p in sorted(BATCHES_ROOT.iterdir(), reverse=True):
        if p.is_dir() and is_valid_batch_id(p.name):
            out.append({"batch_id": p.name, "path": str(p)})
    return out


def get_batch_dir(batch_id: str) -> Path:
    d = batch_dir(batch_id)
    if not d.is_dir():
        raise FileNotFoundError(f"Batch not found: {batch_id}")
    return d


def get_input_dir(batch_id: str) -> Path:
    d = get_batch_dir(batch_id) / "input"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_processed_dir(batch_id: str) -> Path:
    d = get_batch_dir(batch_id) / "processed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_export_dir(batch_id: str) -> Path:
    d = get_batch_dir(batch_id) / "export"
    d.mkdir(parents=True, exist_ok=True)
    return d


def list_files_in_batch(batch_id: str) -> list[Path]:
    in_dir = get_input_dir(batch_id)
    return sorted(p for p in in_dir.iterdir() if p.is_file())


def delete_batch(batch_id: str) -> None:
    d = get_batch_dir(batch_id)
    shutil.rmtree(d, ignore_errors=True)
