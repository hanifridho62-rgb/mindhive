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


def test_worktree_permission_denied_is_classified_not_raw():
    err = ("Exit code 255\nerror: failed to delete "
           "'C:/Users/hanif/mindhive/.claude/worktrees/upbeat-kare-03fe27': "
           "Permission denied")
    sig = signature(err)
    assert sig == "git:worktree-locked"
    assert not sig.startswith("raw:")           # no longer lost in the junk bucket


# --- the "Exit code <n>" mega-bucket ------------------------------------
# Masking every digit run to N collapsed "Exit code 1" and "Exit code 49" onto
# one hash, and hashing only the FIRST line threw away the line that actually
# named the failure. Together they produced raw:e5637cbd18 — 375 recorded
# occurrences across four projects (49.3% of all captured failures), a bucket
# holding unrelated root causes and therefore unreflectable and unfixable.


def test_distinct_exit_codes_do_not_share_one_signature():
    # Kills the wrong implementation that masks \d+ everywhere: an exit code is
    # the identity of a failure family, not volatile noise like a line number.
    sigs = {signature("Exit code %d" % code) for code in (1, 2, 49, 128, 143, 255)}
    assert len(sigs) == 6


def test_exit_code_header_keeps_the_diagnostic_detail_line():
    # Same exit code, different diagnosis: this isolates the first-line-only
    # defect. Neither detail line matches a named family, so the split can only
    # come from the detail being folded in — not from some other classifier
    # rule catching one of them first.
    store_alias = signature("Exit code 1\nPython was not found; run without "
                            "arguments to install from the Microsoft Store.")
    not_a_repo = signature("Exit code 1\nfatal: not a git repository")
    assert store_alias != not_a_repo


def test_exit_code_detail_still_collapses_volatile_line_numbers_and_paths():
    # The canary for the split above: masking must STILL apply inside the detail
    # line. Without this the fix trades one mega-bucket for unbounded
    # fragmentation, where every traceback line number mints a new signature.
    a = signature('Exit code 2\n  File "C:\\a\\x.py", line 42, in <module>')
    b = signature('Exit code 2\n  File "C:\\b\\y.py", line 99, in <module>')
    assert a == b


def test_bare_exit_code_without_detail_is_stable():
    # Totality: a header with nothing after it must classify, not raise.
    assert signature("Exit code 143") == signature("Exit code 143")
    assert signature("Exit code 143").startswith("raw:")


def test_exit_code_family_stays_an_unclassified_fallback():
    # An exit code plus a detail line is still an unknown root cause, so it keeps
    # the raw: prefix that reflect.py keys its fallback handling off.
    assert signature("Exit code 128\nfatal: not a git repository").startswith("raw:")


def test_unquoted_missing_module_classifies_like_the_quoted_form():
    # Import failures emit the quoted form ("No module named 'tools'"), but
    # `python -m <missing>` emits it UNQUOTED. Matching only the quoted form sent
    # every `python -m` miss into the junk bucket instead of pymodule:*.
    assert signature("C:\\py.exe: No module named pyflakes") == "pymodule:pyflakes"
    assert signature("No module named pyflakes") == signature("No module named 'pyflakes'")


# --- the "Traceback (most recent call last):" mega-bucket -----------------
# The exit-code fix above stopped one level too shallow. For a Python traceback
# the detail line it folded in is `Traceback (most recent call last):` — itself
# a HEADER, not a diagnosis. So every uncaught Python exception that exits 1
# hashed to ONE signature: raw:37c59e9cae, 42 recorded occurrences across 17
# sessions and 7+ unrelated exception classes (AssertionError, KeyError,
# ImportError, AttributeError, FileNotFoundError, JSONDecodeError,
# PermissionError). One bucket, no shared fix, unreflectable and unresolvable.
# The diagnosis is the line that TERMINATES the traceback.


def _tb(exc_line, path="C:/a/x.py", line=42):
    """A realistic harness-reported traceback ending in ``exc_line``."""
    return ('Exit code 1\nTraceback (most recent call last):\n'
            '  File "%s", line %d, in <module>\n    do_it()\n%s'
            % (path, line, exc_line))


def test_distinct_exceptions_do_not_share_one_traceback_signature():
    # The mega-bucket killer: four real corpus samples, four root causes, four
    # fixes. Any implementation keyed off the traceback HEADER fails this.
    sigs = {signature(_tb(exc)) for exc in (
        "KeyError: 'task5'",
        "AssertionError: mutant anchor not found verbatim",
        "json.decoder.JSONDecodeError: Expecting value: line 1 column 1 (char 0)",
        "PermissionError: [WinError 5] Access is denied",
    )}
    assert len(sigs) == 4


def test_traceback_signature_names_the_exception_type_readably():
    # `raw:37c59e9cae` told a human nothing; the type belongs in the id.
    assert signature(_tb("KeyError: 'task5'")).startswith("pyexc:keyerror:")
    # Qualified names collapse to the leaf: json.decoder.JSONDecodeError and a
    # bare JSONDecodeError are the same exception, so they must not split.
    assert signature(_tb("json.decoder.JSONDecodeError: Expecting value")) == \
        signature(_tb("JSONDecodeError: Expecting value"))


def test_traceback_signature_still_collapses_volatile_paths_and_line_numbers():
    # The canary for the split above: without masking inside the exception
    # line, this fix trades one mega-bucket for unbounded fragmentation.
    assert signature(_tb("ZeroDivisionError: division by zero", "C:/a/x.py", 42)) == \
        signature(_tb("ZeroDivisionError: division by zero", "C:/b/y.py", 99))
    # Masking reaches inside the message too, not just the frame lines.
    assert signature(_tb("FileNotFoundError: No such file: '/tmp/a.py'")) == \
        signature(_tb("FileNotFoundError: No such file: '/tmp/b.py'"))


def test_traceback_keys_off_the_exception_not_the_exit_code_header():
    # The same uncaught exception reported with and without the harness's
    # exit-code header is ONE root cause with ONE fix. Kills an implementation
    # that folds the exit code into a traceback signature.
    with_header = signature(_tb("KeyError: 'task5'"))
    without = signature('Traceback (most recent call last):\n'
                        '  File "C:/a/x.py", line 42, in <module>\n'
                        "    do_it()\nKeyError: 'task5'")
    assert with_header == without


def test_chained_traceback_keys_off_the_final_exception():
    # CPython prints the CAUSE first and the escaping exception last. Keying
    # off the first traceback — or off the "During handling" connector line —
    # picks the wrong diagnosis.
    chained = ('Exit code 1\nTraceback (most recent call last):\n'
               '  File "a.py", line 1, in <module>\nValueError: inner\n\n'
               'During handling of the above exception, another exception occurred:\n\n'
               'Traceback (most recent call last):\n'
               '  File "a.py", line 3, in <module>\nRuntimeError: outer\n')
    assert signature(chained) == signature(_tb("RuntimeError: outer"))
    assert signature(chained) != signature(_tb("ValueError: inner"))


def test_stdout_around_the_traceback_is_not_mistaken_for_the_diagnosis():
    # Real corpus shape: a probe prints results, then dies. A "last non-empty
    # line" implementation signs on the stdout tail ("MAX_NODES 2000") and
    # mints a fresh signature per run; a "first line after the header"
    # implementation signs on the stdout printed BEFORE it. Both die here.
    noisy = ("MAX_NODES 2000\n" + _tb("KeyError: 'items'") +
             "\nENV_SWITCH: MINDHIVE_SYMBOLS | BUDGET_TOKENS: 1024\n")
    assert signature(noisy) == signature(_tb("KeyError: 'items'"))


def test_truncated_traceback_without_an_exception_line_is_stable():
    # Totality: output cut off mid-traceback must classify, not raise, and
    # must not invent an exception it never saw.
    cut = 'Exit code 1\nTraceback (most recent call last):\n  File "a.py", line 1'
    assert signature(cut) == signature(cut)
    # It lands in the OLD bucket, and that is the honest answer: output cut
    # off before its exception line carries no diagnosis to key off. This is
    # the one remaining path into raw:37c59e9cae - pinned so it stays
    # deliberate rather than drifting back into a mega-bucket.
    assert signature(cut) == "raw:37c59e9cae"


# --- missing symbols: pymodule:'s sibling family --------------------------
# `cannot import name 'X' from 'Y'` and `module 'Y' has no attribute 'X'` are
# one root cause (the name does not exist there — assumed from memory, or read
# from a different checkout) with one fix (check the module's real surface
# first). 10 of the 42 bucketed occurrences were this. Left to pyexc: hashing
# they fragment one-per-symbol and never recur enough to earn a rule.


def test_missing_symbol_is_one_family_not_one_bucket_per_symbol():
    sigs = {
        signature(_tb("ImportError: cannot import name 'sopgate' from 'mindhive'")),
        signature(_tb("ImportError: cannot import name 'theme' from 'mindhive'")),
        signature(_tb("AttributeError: module 'mindhive.paths' has no attribute 'home'")),
        signature(_tb("AttributeError: module 'mindhive.promptgate' has no "
                      "attribute 'is_code_work'")),
    }
    assert sigs == {"pysymbol:mindhive"}


def test_missing_symbol_in_distinct_packages_gets_distinct_signatures():
    assert signature(_tb("ImportError: cannot import name 'x' from 'numpy'")) == \
        "pysymbol:numpy"
    assert signature(_tb("ImportError: cannot import name 'x' from 'numpy'")) != \
        signature(_tb("ImportError: cannot import name 'x' from 'pandas'"))


def test_missing_symbol_is_not_the_same_family_as_a_missing_module():
    # Different root cause, different fix: install the package vs. use a name
    # that actually exists. Guards the ordering of the two classifiers.
    assert signature(_tb("ModuleNotFoundError: No module named 'pypdf'")) == "pymodule:pypdf"
    assert signature(_tb("ImportError: cannot import name 'x' from 'pypdf'")) == "pysymbol:pypdf"
