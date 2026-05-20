"""Tests for the dependency-free unified-diff parser."""
from prcop.diff import parse_diff, render_diff_bundle

SAMPLE_DIFF = """diff --git a/app/server.py b/app/server.py
index 1111111..2222222 100644
--- a/app/server.py
+++ b/app/server.py
@@ -10,6 +10,9 @@ def create_app():
     app = Flask(__name__)
     app.config['SECRET_KEY'] = 'changeme'
+    # NOTE: hardcoded API key — intentionally bad
+    app.config['API_KEY'] = 'sk-test-1234567890abcdef'
+    app.debug = True
     return app

diff --git a/app/new_helper.py b/app/new_helper.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/app/new_helper.py
@@ -0,0 +1,3 @@
+def add(a, b):
+    return a + b
+
diff --git a/app/old.py b/app/old.py
deleted file mode 100644
index 4444444..0000000
--- a/app/old.py
+++ /dev/null
"""


def test_parses_three_files():
    files = parse_diff(SAMPLE_DIFF)
    paths = [f.path for f in files]
    assert paths == ["app/server.py", "app/new_helper.py", "app/old.py"]


def test_new_and_deleted_flags():
    files = {f.path: f for f in parse_diff(SAMPLE_DIFF)}
    assert files["app/new_helper.py"].is_new is True
    assert files["app/old.py"].is_deleted is True
    assert files["app/server.py"].is_new is False


def test_added_lines_have_correct_new_line_numbers():
    files = {f.path: f for f in parse_diff(SAMPLE_DIFF)}
    server = files["app/server.py"]
    added = [(ln, content) for h in server.hunks for ln, content in h.added_lines]
    # Hunk starts at new_start=10, two unchanged lines first → adds at 12, 13, 14
    nums = [ln for ln, _ in added]
    assert nums == [12, 13, 14]
    assert "API_KEY" in added[1][1]


def test_render_bundle_includes_file_headers():
    files = parse_diff(SAMPLE_DIFF)
    bundle = render_diff_bundle(files)
    assert "FILE app/server.py" in bundle
    assert "[new file]" in bundle
    assert "[deleted]" in bundle


def test_render_bundle_respects_char_cap():
    files = parse_diff(SAMPLE_DIFF)
    bundle = render_diff_bundle(files, max_chars=80)
    assert "truncated by char cap" in bundle


def test_language_detection():
    files = parse_diff(SAMPLE_DIFF)
    server = next(f for f in files if f.path == "app/server.py")
    assert server.language == "python"


def test_empty_diff_returns_no_files():
    assert parse_diff("") == []
