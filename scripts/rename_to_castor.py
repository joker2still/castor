"""Rename qwe-qwe → castor across the entire codebase.

Run from the repo root:
    python scripts/rename_to_castor.py [--dry-run]

What is NOT touched:
- .venv/, qwe_qwe.egg-info/, __pycache__/, .git/
- "qwen" / "Qwen" — LLM model names, unrelated to the project
- Binary files (images, compiled)
"""

import sys
from pathlib import Path

DRY_RUN = "--dry-run" in sys.argv

ROOT = Path(__file__).resolve().parent.parent

SKIP_DIRS = {".venv", "qwe_qwe.egg-info", "__pycache__", ".git", "castor.egg-info"}
SKIP_FILES = {"rename_to_castor.py"}

TEXT_EXTENSIONS = {
    ".py", ".html", ".md", ".toml", ".json", ".yml", ".yaml",
    ".sh", ".bat", ".txt", ".cfg", ".ini", ".sql", ".rst",
}


REPLACEMENTS = [

    ("qweqwe_yt_",           "castor_yt_"),
    ("qwe-qwe-update-",      "castor-update-"),
    ("qwe-qwe-config-",      "castor-config-"),
    ("qwe-qwe-pricing",      "castor-pricing"),
    ("qwe-qwe/kb",           "castor/kb"),
    ("qwe-auth-v1",          "castor-auth-v1"),
    ("qwe_pytest_",          "castor_pytest_"),
    ("qwe_preset_",          "castor_preset_"),
    ("qwe_turn_ctx",         "castor_turn_ctx"),
    ("qwe_version",          "castor_version"),
    ("qwe_auth",             "castor_auth"),
    ("qwe_rag",              "castor_rag"),

    ("qwe_qwe",              "castor"),
    ("qwe-qwe",              "castor"),

    ("QWE_",                 "CASTOR_"),


    ('"qwe"',                '"castor"'),
    ("'qwe'",                "'castor'"),

    ("QWE-QWE",              "CASTOR"),
]


PROTECTED = [
]


def safe_replace(text: str) -> tuple[str, int]:
    """Apply all REPLACEMENTS to text, skipping PROTECTED substrings."""
    changes = 0
    for old, new in REPLACEMENTS:
        if old not in text:
            continue

        lines_out = []
        for line in text.splitlines(keepends=True):
            if old in line and any(p in line for p in PROTECTED):
                lines_out.append(line)
            elif old in line:
                replaced = line.replace(old, new)
                if replaced != line:
                    changes += 1
                lines_out.append(replaced)
            else:
                lines_out.append(line)
        text = "".join(lines_out)
    return text, changes


def should_skip(path: Path) -> bool:
    parts = set(path.parts)
    if parts & SKIP_DIRS:
        return True
    if path.name in SKIP_FILES:
        return True
    if path.suffix not in TEXT_EXTENSIONS:
        return True
    return False


def process_file(path: Path) -> bool:
    """Return True if file was (or would be) modified."""
    try:
        original = path.read_text(encoding="utf-8")
    except (UnicodeDecodeError, PermissionError):
        return False

    new_text, changes = safe_replace(original)
    if new_text == original:
        return False

    print(f"  {'[DRY]' if DRY_RUN else '[MOD]'} {path.relative_to(ROOT)}  ({changes} change(s))")
    if not DRY_RUN:
        path.write_text(new_text, encoding="utf-8")
    return True


def main():
    print(f"\n{'DRY RUN - ' if DRY_RUN else ''}Renaming qwe-qwe -> castor\n")

    modified = 0
    for path in sorted(ROOT.rglob("*")):
        if not path.is_file():
            continue
        if should_skip(path):
            continue
        if process_file(path):
            modified += 1

    print(f"\n{'Would modify' if DRY_RUN else 'Modified'}: {modified} file(s)")

    if not DRY_RUN:

        old_egg = ROOT / "qwe_qwe.egg-info"
        new_egg = ROOT / "castor.egg-info"
        if old_egg.exists() and not new_egg.exists():
            old_egg.rename(new_egg)
            print(f"  [REN] qwe_qwe.egg-info → castor.egg-info")

    print()


if __name__ == "__main__":
    main()
