"""Diff parsing — turns a unified `git diff` into structured per-file hunks
that the reviewer agents can reason about without re-reading the whole file.

We deliberately keep this dependency-free (no `unidiff` library) so the package
ships small and starts fast.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field


@dataclass
class Hunk:
    """A single hunk inside a file diff."""
    old_start: int
    old_count: int
    new_start: int
    new_count: int
    header: str = ""
    lines: list[str] = field(default_factory=list)

    @property
    def added_lines(self) -> list[tuple[int, str]]:
        """Return (new_line_number, content) for every '+' line in this hunk."""
        out: list[tuple[int, str]] = []
        ln = self.new_start
        for raw in self.lines:
            if raw.startswith("+") and not raw.startswith("+++"):
                out.append((ln, raw[1:]))
                ln += 1
            elif raw.startswith("-") and not raw.startswith("---"):
                # removed line — does not advance new line counter
                continue
            else:
                ln += 1
        return out

    @property
    def removed_lines(self) -> list[tuple[int, str]]:
        out: list[tuple[int, str]] = []
        ln = self.old_start
        for raw in self.lines:
            if raw.startswith("-") and not raw.startswith("---"):
                out.append((ln, raw[1:]))
                ln += 1
            elif raw.startswith("+") and not raw.startswith("+++"):
                continue
            else:
                ln += 1
        return out

    def render_for_review(self) -> str:
        """Render this hunk in a way that's easy for an LLM to cite line numbers from."""
        out = [f"@@ {self.header}".rstrip()]
        ln_old = self.old_start
        ln_new = self.new_start
        for raw in self.lines:
            if raw.startswith("+") and not raw.startswith("+++"):
                out.append(f"+{ln_new:>5}  {raw[1:]}")
                ln_new += 1
            elif raw.startswith("-") and not raw.startswith("---"):
                out.append(f"-{ln_old:>5}  {raw[1:]}")
                ln_old += 1
            else:
                out.append(f" {ln_new:>5}  {raw[1:] if raw.startswith(' ') else raw}")
                ln_old += 1
                ln_new += 1
        return "\n".join(out)


@dataclass
class FileDiff:
    """A diff for a single file."""
    path: str
    old_path: str
    is_new: bool = False
    is_deleted: bool = False
    is_binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)

    @property
    def added_loc(self) -> int:
        return sum(len(h.added_lines) for h in self.hunks)

    @property
    def removed_loc(self) -> int:
        return sum(len(h.removed_lines) for h in self.hunks)

    @property
    def language(self) -> str:
        """Best-effort language tag from path extension."""
        if self.path.endswith((".py",)):
            return "python"
        if self.path.endswith((".ts", ".tsx")):
            return "typescript"
        if self.path.endswith((".js", ".jsx", ".mjs", ".cjs")):
            return "javascript"
        if self.path.endswith((".go",)):
            return "go"
        if self.path.endswith((".rs",)):
            return "rust"
        if self.path.endswith((".java",)):
            return "java"
        if self.path.endswith((".rb",)):
            return "ruby"
        if self.path.endswith((".c", ".h")):
            return "c"
        if self.path.endswith((".cpp", ".cc", ".cxx", ".hpp")):
            return "cpp"
        if self.path.endswith((".sh", ".bash")):
            return "shell"
        if self.path.endswith((".sql",)):
            return "sql"
        if self.path.endswith((".md",)):
            return "markdown"
        if self.path.endswith((".yaml", ".yml")):
            return "yaml"
        if self.path.endswith((".json",)):
            return "json"
        return "text"

    def render_for_review(self) -> str:
        head = f"FILE {self.path} ({self.language}, +{self.added_loc}/-{self.removed_loc} loc)"
        if self.is_new:
            head += " [new file]"
        if self.is_deleted:
            head += " [deleted]"
        if self.is_binary:
            return head + "\n[binary file: skipped]"
        return head + "\n" + "\n\n".join(h.render_for_review() for h in self.hunks)


def _parse_hunk_header(line: str) -> tuple[int, int, int, int, str]:
    # @@ -old_start,old_count +new_start,new_count @@ optional header
    body = line[2:].lstrip()
    end = body.find(" @@")
    if end == -1:
        end = len(body)
    range_part = body[:end].strip()
    header = body[end + 3:].strip() if end < len(body) else ""
    parts = range_part.split(" ")
    old, new = parts[0], parts[1]

    def parse(side: str) -> tuple[int, int]:
        side = side.lstrip("-+")
        if "," in side:
            s, c = side.split(",", 1)
            return int(s), int(c)
        return int(side), 1

    old_start, old_count = parse(old)
    new_start, new_count = parse(new)
    return old_start, old_count, new_start, new_count, header


def parse_diff(diff_text: str) -> list[FileDiff]:
    """Parse a unified diff string into a list of FileDiff objects.

    Handles:
    - new files (`new file mode`)
    - deleted files (`deleted file mode`)
    - binary files (`Binary files differ`)
    - rename hints (best-effort)
    """
    files: list[FileDiff] = []
    current: FileDiff | None = None
    current_hunk: Hunk | None = None

    lines: Iterator[str] = iter(diff_text.splitlines())

    def flush_hunk():
        nonlocal current_hunk
        if current_hunk is not None and current is not None:
            current.hunks.append(current_hunk)
        current_hunk = None

    for line in lines:
        if line.startswith("diff --git "):
            flush_hunk()
            if current is not None:
                files.append(current)
            # diff --git a/path b/path
            parts = line.split(" ")
            old_path = parts[2][2:] if len(parts) > 2 else ""
            new_path = parts[3][2:] if len(parts) > 3 else old_path
            current = FileDiff(path=new_path, old_path=old_path)
            continue
        if current is None:
            continue
        if line.startswith("new file mode"):
            current.is_new = True
            continue
        if line.startswith("deleted file mode"):
            current.is_deleted = True
            continue
        if line.startswith("Binary files"):
            current.is_binary = True
            continue
        if line.startswith("--- ") or line.startswith("+++ "):
            continue
        if line.startswith("@@"):
            flush_hunk()
            old_s, old_c, new_s, new_c, header = _parse_hunk_header(line)
            current_hunk = Hunk(
                old_start=old_s, old_count=old_c,
                new_start=new_s, new_count=new_c,
                header=header,
            )
            continue
        if current_hunk is not None and (
            line.startswith("+") or line.startswith("-") or line.startswith(" ") or line == ""
        ):
            current_hunk.lines.append(line)

    flush_hunk()
    if current is not None:
        files.append(current)
    return files


def render_diff_bundle(files: list[FileDiff], max_chars: int = 18000) -> str:
    """Render multiple file diffs into a single review-friendly bundle, capped."""
    chunks: list[str] = []
    used = 0
    for f in files:
        block = f.render_for_review()
        if used + len(block) + 2 > max_chars:
            chunks.append(f"... ({len(files) - len(chunks)} more files truncated by char cap)")
            break
        chunks.append(block)
        used += len(block) + 2
    return "\n\n".join(chunks)
