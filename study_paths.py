"""Resolve local study dependencies without a previous machine's username."""
from __future__ import annotations

import os
from pathlib import Path

BASE = Path(__file__).resolve().parent


def subject_root(subject: str) -> Path:
    variable = "KAOYAN_" + subject.upper() + "_PROJECT_ROOT"
    if subject == "408":
        supplied = (os.environ.get(variable) or os.environ.get("KAOYAN_408_REPO")
                    or os.environ.get("CS408_REPO"))
        sibling = BASE.parent / "kaoyan-408"
        default = sibling if sibling.is_dir() else Path.home() / "Documents/kaoyan-408"
        path = Path(supplied or default).expanduser()
        if not path.is_absolute():
            raise ValueError(variable + "_must_be_absolute")
        return path.resolve()
    if subject == "english":
        supplied = os.environ.get(variable) or os.environ.get("KAOYAN_ENGLISH_ROOT")
        if not supplied:
            start = Path.cwd().resolve()
            for candidate in (start, *start.parents):
                if ((candidate / "scripts/english_learning_pipeline.py").is_file()
                        and (candidate / "english_pipeline/constants.py").is_file()
                        and (candidate / "bank/master_bank.csv").is_file()):
                    return candidate
        sibling = BASE.parent / "kaoyan-english"
        default = sibling if sibling.is_dir() else Path.home() / "Documents/kaoyan-english"
        path = Path(supplied or default).expanduser()
        if not path.is_absolute():
            raise ValueError(variable + "_must_be_absolute")
        return path.resolve()
    default = (BASE.parent / "kaoyan-math" if subject == "math"
               else Path.home() / "Documents" / ("kaoyan-" + subject))
    path = Path(os.environ.get(variable) or default).expanduser()
    if not path.is_absolute():
        raise ValueError(variable + "_must_be_absolute")
    return path.resolve()


def storage_map_root() -> Path:
    path = Path(os.environ.get("STUDY_STORAGE_MAP_ROOT") or BASE.parent / "Study-Storage-Map").expanduser()
    if not path.is_absolute():
        raise ValueError("STUDY_STORAGE_MAP_ROOT_must_be_absolute")
    return path.resolve()


def credential_file() -> Path:
    """Runtime-owned configuration, supplied separately from the review mirror."""
    return Path(os.environ.get("STUDY_OPENAI_CREDENTIAL_FILE", Path.home()/".config/study-pro-bridge/api.json"))
