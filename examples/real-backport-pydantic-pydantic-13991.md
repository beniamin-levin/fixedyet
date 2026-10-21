# Verified backport: the fix applies to v2.14-fixes and the tests agree

**Issue:** [Parametrized generic models no longer resolvable by `__module__`/`__qualname__` (pickle fails) since 2.14.0](https://github.com/pydantic/pydantic/issues/13991)

## Verified backport

**Status:** `verified`  
**Result:** the reproduction test fails on v2.14-fixes before the cherry-pick and passes after; the project's suite has no newly failing tests (6250 passed, 1 error, 1079 skipped before, 6252 passed, 1 error, 1079 skipped after)  
**Target branch:** `v2.14-fixes` (head `b2212c787fb5`)  
**Reported version:** `v2.14.0`  
**Fixing commit on main:** [e1ca33220585](https://github.com/pydantic/pydantic/commit/e1ca33220585e8f745000eff4b067e04fe7d83e9)

| Reproduction test on `v2.14-fixes` | Run 1 | Run 2 |
|---|---|---|
| Before the cherry-pick | FAIL | FAIL |
| After the cherry-pick | PASS | PASS |

| Project test suite | Before the cherry-pick | After the cherry-pick |
|---|---|---|
| Passed | 6250 | 6252 |
| Failed | 0 | 0 |
| Errors | 1 | 1 |
| Skipped | 1079 | 1079 |
| Newly failing tests | - | none |

Cherry-picked commit message:

```
Fix unpickling of generic classes defined in separate module (#13994)

(cherry picked from commit e1ca33220585e8f745000eff4b067e04fe7d83e9)
```

Patch (`git format-patch` of the cherry-pick):

```diff
From fddd15fd7e631c6da6749c9c5523c27230dcbdb4 Mon Sep 17 00:00:00 2001
From: Victorien <65306057+Viicos@users.noreply.github.com>
Date: Wed, 14 Oct 2026 11:27:20 +0200
Subject: [PATCH] Fix unpickling of generic classes defined in separate module
 (#13994)

(cherry picked from commit e1ca33220585e8f745000eff4b067e04fe7d83e9)
---
 pydantic/_internal/_generics.py |  9 +++--
 tests/test_forward_ref.py       | 71 ++++++++++++++++++++++++++++++++-
 tests/test_generics.py          | 49 +++++++++++++++++++++++
 3 files changed, 124 insertions(+), 5 deletions(-)

diff --git a/pydantic/_internal/_generics.py b/pydantic/_internal/_generics.py
index 15c4c56de..618b66ef5 100644
--- a/pydantic/_internal/_generics.py
+++ b/pydantic/_internal/_generics.py
@@ -119,7 +119,11 @@ def create_generic_submodel(
     Returns:
         The created submodel.
     """
-    namespace: dict[str, Any] = {'__module__': origin.__module__}
+    model_module, called_globally = _get_caller_frame_info(depth=3)
+    # If parametrized at the module level, the parametrized class is registered in this module (see below).
+    # Its `__module__` must match. We fall back to the module of the origin for backwards compatibility reasons.
+    module = model_module if called_globally and model_module is not None else origin.__module__
+    namespace: dict[str, Any] = {'__module__': module}
     # As per https://docs.python.org/3/reference/datamodel.html#slots:
     # "The action of a __slots__ declaration is not limited to the class where it is defined.
     # __slots__ declared in parents are available in child classes. However, instances of a
@@ -149,11 +153,10 @@ def create_generic_submodel(
         **kwds,
     )
 
-    model_module, called_globally = _get_caller_frame_info(depth=3)
     if called_globally:  # create global reference and therefore allow pickling
         object_by_reference = None
         reference_name = model_name
-        reference_module_globals = sys.modules[model_module or created_model.__module__].__dict__
+        reference_module_globals = sys.modules[module].__dict__
         while object_by_reference is not created_model:
             object_by_reference = reference_module_globals.setdefault(reference_name, created_model)
             reference_name += '_'
diff --git a/tests/test_forward_ref.py b/tests/test_forward_ref.py
index f0e69b73e..80af7b36b 100644
--- a/tests/test_forward_ref.py
+++ b/tests/test_forward_ref.py
@@ -1145,10 +1145,10 @@ def test_lazy_local_namespace_len() -> None:
 
 
 @pytest.mark.xfail(
-    reason='While `get_cls_type_hints` uses the correct module ns for each base, `collect_model_fields` '
+    reason='While `get_cls_type_hints()` uses the correct module ns for each base, `collect_model_fields()` '
     'will still use the `FieldInfo` instances from each base (see the `parent_fields_lookup` logic). '
     'This means that `f` is still a forward ref in `Foo.model_fields`, and it gets evaluated in '
-    '`GenerateSchema._model_schema`, where only the module of `Foo` is considered.'
+    '`GenerateSchema._model_schema()`, where only the module of `Foo` is considered.'
 )
 def test_uses_the_correct_globals_to_resolve_model_forward_refs(create_module):
     @create_module
@@ -1174,6 +1174,73 @@ class Foo(Bar):
     assert module_2.Foo.model_fields['f'].annotation is int
 
 
+@pytest.mark.xfail(
+    reason='Same as `test_uses_the_correct_globals_to_resolve_model_forward_refs()`. As the generic model is '
+    'parametrized in `module_2`, the `__module__` of the parametrized class is `module_2` and so `f` is '
+    'evaluated in this module when rebuilding.'
+)
+def test_uses_the_correct_globals_to_resolve_parametrized_generic_model_forward_refs(create_module):
+    @create_module
+    def module_1():
+        from typing import Generic, TypeVar
+
+        from pydantic import BaseModel
+
+        T = TypeVar('T')
+
+        class Bar(BaseModel, Generic[T]):
+            t: T
+            f: 'A'
+
+    module_2 = create_module(
+        f"""
+from {module_1.__name__} import Bar
+
+A = str
+
+BarInt = Bar[int]
+        """
+    )
+
+    # Simulates `A` being defined later in `module_1`, after `Bar` got parametrized
+    # (e.g. if `module_1` imports `module_2` before defining `A`):
+    exec('A = int', module_1.__dict__)
+
+    module_2.BarInt.model_rebuild()
+
+    assert module_2.BarInt.model_fields['f'].annotation is int
+
+
+def test_resolve_forward_ref_type_argument_in_parametrizing_module(create_module):
+
+    @create_module
+    def module_1():
+        from typing import Generic, TypeVar
+
+        from pydantic import BaseModel
+
+        T = TypeVar('T')
+
+        class Bar(BaseModel, Generic[T]):
+            t: T
+
+    module_2 = create_module(
+        f"""
+from {module_1.__name__} import Bar
+
+BarA = Bar['A']
+
+A = int
+        """
+    )
+
+    assert not module_2.BarA.__pydantic_complete__
+
+    # `'A'` is evaluated in `module_2`, the module where `Bar` got parametrized:
+    assert module_2.BarA(t='1').t == 1
+    assert module_2.BarA.model_fields['t'].annotation is int
+
+
 @pytest.mark.xfail(
     reason='We should keep a reference to the parent frame, not `f_locals`. '
     "It's probably only reasonable to support this in Python 3.14 with PEP 649."
diff --git a/tests/test_generics.py b/tests/test_generics.py
index 013cb3c30..62b6f985e 100644
--- a/tests/test_generics.py
+++ b/tests/test_generics.py
@@ -1,6 +1,7 @@
 import gc
 import itertools
 import json
+import pickle
 import platform
 import re
 import subprocess
@@ -1039,6 +1040,54 @@ def test_generic_model_from_function_pickle_fail(create_module):
             pickle.dumps(original)
 
 
+def test_generic_model_parametrized_in_other_module_pickle(create_module) -> None:
+    """https://github.com/pydantic/pydantic/issues/13991"""
+
+    @create_module
+    def module_1():
+        from typing import Generic, TypeVar
+
+        from pydantic import BaseModel
+
+        T = TypeVar('T')
+
+        class Page(BaseModel, Generic[T]):
+            items: list[T]
+
+        class Response(BaseModel, Generic[T]):
+            result: T
+
+    module_2 = create_module(
+        f"""
+from pydantic import BaseModel
+
+from {module_1.__name__} import Page, Response
+
+class Item(BaseModel):
+    name: str
+
+ItemResponse = Response[Page[Item]]
+        """
+    )
+
+    ItemResponse = module_2.ItemResponse
+    ItemPage = ItemResponse.model_fields['result'].annotation
+
+    assert ItemResponse.__module__ == ItemPage.__module__ == module_2.__name__
+    assert getattr(module_2, ItemResponse.__qualname__) is ItemResponse
+    assert getattr(module_2, ItemPage.__qualname__) is ItemPage
+    # The module where the generic classes are defined shouldn't be polluted:
+    assert not hasattr(module_1, ItemResponse.__qualname__)
+    assert not hasattr(module_1, ItemPage.__qualname__)
+
+    original = ItemResponse(result={'items': [{'name': 'a'}]})
+    loaded = pickle.loads(pickle.dumps(original))
+    assert type(loaded) is ItemResponse
+    assert type(loaded.result) is ItemPage
+    assert type(loaded.result.items[0]) is module_2.Item
+    assert loaded == original
+
+
 # `cloudpickle` is in the optional `testing-extra` dependency group, so the
 # subprocesses below may not be able to import it.
 @pytest.mark.skipif(
```

_6 sandbox runs, 268 s wall time, $0.0000 model cost._
