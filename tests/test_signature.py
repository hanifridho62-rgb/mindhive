from mindhive.signature import signature, slug_id


def test_slug_id_distinct_texts_never_collide():
    # Two texts that slug to the same 40-char prefix must NOT share a component
    # (the content hash disambiguates them).
    a = slug_id("commit to master " + "x" * 60)
    b = slug_id("commit to master " + "y" * 60)
    assert a != b
    assert a.startswith("commit-to-master")


def test_slug_id_non_latin_gets_unique_hash_not_shared_bucket():
    # Non-latin text slugs to empty; slug() would collapse every such text onto
    # the shared "unknown" bucket. slug_id gives each its own hash.
    a = slug_id("提交到主分支")
    b = slug_id("强制推送")
    assert a and b and a != b
    assert "unknown" not in (a, b)


def test_slug_id_is_deterministic():
    assert slug_id("git push --force") == slug_id("git push --force")


def test_python_missing_module_collapses_to_top_level_package():
    # All submodules of the same package share ONE signature — same root cause,
    # same fix (package not importable from here).
    assert signature("ModuleNotFoundError: No module named 'mindhive'") == "pymodule:mindhive"
    assert signature("ModuleNotFoundError: No module named 'mindhive.paths'") == "pymodule:mindhive"
    assert signature("ModuleNotFoundError: No module named 'scripts.core.event_cli'") == "pymodule:scripts"


def test_distinct_packages_get_distinct_signatures():
    assert signature("ModuleNotFoundError: No module named 'pypdf'") == "pymodule:pypdf"
    assert signature("No module named 'hive_pipeline'") == "pymodule:hive_pipeline"
    assert signature("ModuleNotFoundError: No module named 'pypdf'") != signature("No module named 'tools'")


def test_powershell_unknown_command():
    err = "head : The term 'head' is not recognized as the name of a cmdlet, function, ..."
    assert signature(err) == "pscmd:head"
    assert signature("'hive' is not recognized as an internal or external command") == "pscmd:hive"


def test_python_unterminated_string():
    assert signature("SyntaxError: unterminated string literal (detected at line 4)") == "pysyntax:unterminated-string"


def test_fallback_masks_line_numbers_and_paths_so_variants_collapse():
    a = signature("Traceback: error at C:\\Users\\hanif\\a.py line 42: boom")
    b = signature("Traceback: error at C:\\Users\\hanif\\b.py line 99: boom")
    assert a == b
    assert a.startswith("raw:")


def test_empty_error_is_stable():
    assert signature("") == signature(None)
    assert signature("").startswith("raw:")


def test_real_behavioral_families():
    assert signature("<tool_use_error>File has not been read yet. Read it first "
                     "before writing to it.</tool_use_error>") == "edit:not-read"
    assert signature("<tool_use_error>String to replace not found in file. "
                     "String: empty = L1OBI(...)</tool_use_error>") == "edit:string-not-found"
    assert signature("Ripgrep search timed out after 20 seconds. The search may "
                     "have matched") == "tool:ripgrep-timeout"
    assert signature("File content (1.5MB) exceeds maximum allowed size (256KB). "
                     "Use offset and limit") == "read:file-too-large"


def test_behavioral_families_take_priority_over_fallback():
    for err in ("File has not been read yet", "String to replace not found",
                "Ripgrep search timed out", "exceeds maximum allowed size"):
        assert not signature(err).startswith("raw:")
