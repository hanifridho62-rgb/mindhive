from mindhive.signature import signature


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
