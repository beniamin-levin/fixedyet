# Verified backport: the fix applies to 8.4.x and the tests agree

**Issue:** [Inclusion of capteesys in pytest function and run with -s causes doubled prints to stdout](https://github.com/pytest-dev/pytest/issues/13784)

## Verified backport

**Status:** `verified`  
**Result:** the reproduction test fails on 8.4.x before the cherry-pick and passes after; the project's suite has no newly failing tests (3244 passed, 1 failed, 6 error, 124 skipped before, 3245 passed, 1 failed, 6 error, 124 skipped after)  
**Target branch:** `8.4.x` (head `3964af432eb8`)  
**Reported version:** `8.4.1`  
**Fixing commit on main:** [46ea2a3cfc50](https://github.com/pytest-dev/pytest/commit/46ea2a3cfc50ae42310c278387be49d165d71581)

| Reproduction test on `8.4.x` | Run 1 | Run 2 |
|---|---|---|
| Before the cherry-pick | FAIL | FAIL |
| After the cherry-pick | PASS | PASS |

| Project test suite | Before the cherry-pick | After the cherry-pick |
|---|---|---|
| Passed | 3244 | 3245 |
| Failed | 1 | 1 |
| Errors | 6 | 6 |
| Skipped | 124 | 124 |
| Newly failing tests | - | none |

Cherry-picked commit message:

```
Fix capteesys producing doubled output with --capture=no (#14205)

When capteesys is used with -s (--capture=no), output was printed
twice: once by TeeCaptureIO teeing to the original stream in
real-time, and again by pop_outerr_to_orig() writing the buffered
content back via writeorg() on close.

Skip the writeorg() call when tee is enabled, since the output was
already delivered live.

Fixes #13784.

(cherry picked from commit 46ea2a3cfc50ae42310c278387be49d165d71581)
```

Patch (`git format-patch` of the cherry-pick):

```diff
From f727cd0f704befeba0fc0bcaa89441a674a540e2 Mon Sep 17 00:00:00 2001
From: Fridayworks <aifriday700@gmail.com>
Date: Sat, 23 May 2026 11:20:40 -0400
Subject: [PATCH] Fix capteesys producing doubled output with --capture=no
 (#14205)

When capteesys is used with -s (--capture=no), output was printed
twice: once by TeeCaptureIO teeing to the original stream in
real-time, and again by pop_outerr_to_orig() writing the buffered
content back via writeorg() on close.

Skip the writeorg() call when tee is enabled, since the output was
already delivered live.

Fixes #13784.

(cherry picked from commit 46ea2a3cfc50ae42310c278387be49d165d71581)
---
 changelog/13784.bugfix.rst |  1 +
 src/_pytest/capture.py     |  9 ++++++++-
 testing/test_capture.py    | 20 ++++++++++++++++++++
 3 files changed, 29 insertions(+), 1 deletion(-)
 create mode 100644 changelog/13784.bugfix.rst

diff --git a/changelog/13784.bugfix.rst b/changelog/13784.bugfix.rst
new file mode 100644
index 000000000..a2f4cf543
--- /dev/null
+++ b/changelog/13784.bugfix.rst
@@ -0,0 +1 @@
+Fixed ``capteesys`` producing doubled output when used with ``--capture=no`` (``-s``).
diff --git a/src/_pytest/capture.py b/src/_pytest/capture.py
index 6d98676be..f9f8f9e4a 100644
--- a/src/_pytest/capture.py
+++ b/src/_pytest/capture.py
@@ -944,7 +944,14 @@ class CaptureFixture(Generic[AnyStr]):
 
     def close(self) -> None:
         if self._capture is not None:
-            out, err = self._capture.pop_outerr_to_orig()
+            if self._config.get("tee"):
+                # When tee is enabled, output was already written to the
+                # original stream in real-time by TeeCaptureIO.  Using
+                # pop_outerr_to_orig() would write it a second time via
+                # writeorg(), causing doubled output (see #13784).
+                out, err = self._capture.readouterr()
+            else:
+                out, err = self._capture.pop_outerr_to_orig()
             self._captured_out += out
             self._captured_err += err
             self._capture.stop_capturing()
diff --git a/testing/test_capture.py b/testing/test_capture.py
index d9dacebd9..a55c7b250 100644
--- a/testing/test_capture.py
+++ b/testing/test_capture.py
@@ -478,6 +478,26 @@ class TestCaptureFixture:
         result.stdout.fnmatch_lines(["sTdoUt", "sTdeRr"])  # no tee, just reported
         assert not result.stderr.lines
 
+    def test_capteesys_no_double_output_with_capture_no(
+        self, pytester: Pytester
+    ) -> None:
+        """Capteesys with --capture=no should not produce doubled output (#13784)."""
+        p = pytester.makepyfile(
+            """\
+            def test_one(capteesys):
+                print("hello world stdout")
+                out, err = capteesys.readouterr()
+                assert out == "hello world stdout\\n"
+            """
+        )
+        result = pytester.runpytest(p, "--quiet", "--quiet", "-rN", "-s")
+        assert result.ret == ExitCode.OK
+        # "hello world stdout" should appear exactly once, not twice.
+        count = result.stdout.lines.count("hello world stdout")
+        assert count == 1, (
+            f"Expected 'hello world stdout' once, but found {count} times"
+        )
+
     def test_capsyscapfd(self, pytester: Pytester) -> None:
         p = pytester.makepyfile(
             """\
```

_6 sandbox runs, 157 s wall time, $0.0000 model cost._
