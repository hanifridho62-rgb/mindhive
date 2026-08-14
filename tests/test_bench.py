# tests/test_bench.py
"""A benchmark whose failure modes cannot look like fast results.

Every test drives `bench` through its `transport` seam, so the paths that matter
— unreachable, timed out, 403, 500 — are exercised on a machine with no server
running. The seam's limit is stated in the module and holds here: these tests
prove classification, arithmetic and wiring. They do not prove latency, and the
one figure that cannot be proved against instant fakes — the serialization ratio
— is asserted only for its presence and its honesty about failed members, never
for its value. A test that asserted `ThreadPoolExecutor` works while looking
like it proved GIL serialization is worse than no test.
"""
import pytest
from mindhive import __main__ as cli_mod, bench, paths

ORIGIN = "http://127.0.0.1:54471"
TOKEN = "t0ken-abcdef"

OK_200 = (200, 4096, "")
FORBIDDEN = (403, 32, "")
SERVER_ERROR = (500, 64, "")
REFUSED = (0, 0, "[WinError 10061] No connection could be made")
TIMED_OUT = (0, 0, "The read operation timed out")


def _transport(answers, log=None):
    """A fake HTTP client. Keys are substrings of the URL; the first match wins.
    An unstubbed URL answers as unreachable rather than as a success, so a test
    that forgets a stub fails loudly instead of quietly measuring nothing."""
    def send(url, timeout_s):
        if log is not None:
            log.append({"url": url, "timeout_s": timeout_s})
        for needle, answer in answers.items():
            if needle in url:
                return answer
        return (0, 0, f"unstubbed: {url}")
    return send


def _clock(step=0.01):
    """A fake `perf_counter` that advances a fixed step on every read, so each
    sample measures exactly *step* however the reads interleave."""
    state = {"t": 0.0}

    def read():
        state["t"] += step
        return state["t"]
    return read


def _scripted_clock(elapsed):
    """A fake clock whose successive read PAIRS span the given elapsed seconds,
    one entry per sample, in order. Only valid for strictly sequential sampling
    (i.e. `time_endpoint`, not the concurrent pass)."""
    queue = list(elapsed)
    state = {"t": 0.0, "open": False}

    def read():
        if not state["open"]:
            state["open"] = True
            return state["t"]
        state["open"] = False
        state["t"] += (queue.pop(0) if queue else 0.001)
        return state["t"]
    return read


def _report(answers, **kw):
    kw.setdefault("origin", ORIGIN)
    kw.setdefault("token", TOKEN)
    kw.setdefault("label", "isolated, no browser")
    kw.setdefault("timestamp", "2026-08-14T00:00:00+00:00")
    return bench.run(transport=_transport(answers), clock=_clock(), **kw)


def _row(report, path):
    return next(r for r in report["rows"] if r["path"] == path)


# --- failures must not read as speed -----------------------------------------

def test_one_unreachable_endpoint_still_renders_the_others():
    report = _report({"/api/projects": REFUSED, "/api/overview": OK_200,
                      "/api/oversee": OK_200, "/api/mindnet/cats": OK_200})
    assert len(report["rows"]) == 4
    assert _row(report, "/api/projects")["state"] == bench.UNREACHABLE
    assert _row(report, "/api/projects")["cold_ms"] is None
    assert _row(report, "/api/overview")["state"] == bench.OK
    assert report["measured"] == 3
    text = bench.render(report)
    assert "/api/overview" in text and "/api/projects" in text


def test_a_non_200_is_reported_with_its_status_and_never_averaged():
    report = _report({"/api": SERVER_ERROR})
    row = _row(report, "/api/overview")
    assert row["state"] == bench.HTTP_ERROR
    assert row["statuses"] == [500]
    # The whole point: a 500 answered in microseconds must not become a mean.
    assert row["cold_ms"] is None and row["warm_mean_ms"] is None
    assert row["warm_ok"] == 0
    assert "500" in bench.render(report)


def test_a_403_is_called_out_as_a_probable_token_problem():
    report = _report({"/api": FORBIDDEN})
    row = _row(report, "/api/projects")
    assert row["state"] == bench.HTTP_ERROR
    assert "403" in row["note"] and "token gate" in row["note"]
    assert "403" in bench.render(report)


def test_every_endpoint_failing_says_nothing_was_measured():
    report = _report({"/api": REFUSED})
    assert report["measured"] == 0
    assert "NOTHING WAS MEASURED" in bench.render(report)


def test_a_hanging_endpoint_is_classified_as_timed_out_and_the_run_completes():
    # The transport is handed the configured timeout and answers the way a
    # urllib timeout does. This proves the --timeout wiring and the
    # classification; urllib's own enforcement of it is not testable here.
    log = []
    report = bench.run(origin=ORIGIN, token=TOKEN, label="x", timeout_s=7.5,
                       transport=_transport({"/api/oversee": TIMED_OUT,
                                             "/api": OK_200}, log),
                       clock=_clock(), timestamp="2026-08-14T00:00:00+00:00")
    assert _row(report, "/api/oversee")["state"] == bench.TIMEOUT
    assert len(report["rows"]) == 4                  # the run finished
    assert {entry["timeout_s"] for entry in log} == {7.5}


def test_the_default_timeout_is_passed_through_when_none_is_given():
    log = []
    bench.run(origin=ORIGIN, token=TOKEN, label="x",
              transport=_transport({"/api": OK_200}, log), clock=_clock(),
              timestamp="2026-08-14T00:00:00+00:00")
    assert {entry["timeout_s"] for entry in log} == {bench.DEFAULT_TIMEOUT_SECONDS}


# --- the cold sample is the measurement, not a warm-up -----------------------

def test_the_cold_sample_is_reported_apart_from_the_warm_ones():
    row = bench.time_endpoint(ORIGIN, "/api/projects", token=TOKEN, reps=3,
                              transport=_transport({"/api": OK_200}),
                              clock=_scripted_clock([1.8, 0.02, 0.02, 0.02]))
    assert row["cold_ms"] == 1800.0
    assert row["warm_mean_ms"] == 20.0
    assert row["warm_ok"] == 3 and row["warm_total"] == 3


def test_the_sample_counts_add_up_to_one_plus_reps():
    for reps in (0, 1, 5):
        row = bench.time_endpoint(ORIGIN, "/api/overview", token=TOKEN,
                                  reps=reps,
                                  transport=_transport({"/api": OK_200}),
                                  clock=_clock())
        assert row["samples"] == 1 + reps
        assert len(row["warm"]) == reps
        assert row["cold"]["state"] == bench.OK


def test_a_warm_sample_that_missed_is_visible_in_the_counts():
    # A live ledger moves: an auto-harvest append invalidates the fingerprint
    # mid-run, so a nominal hit can be a miss. The counts are what make that
    # visible instead of averaged away.
    answers = iter([OK_200, OK_200, SERVER_ERROR, OK_200])

    def send(url, timeout_s):
        return next(answers)
    row = bench.time_endpoint(ORIGIN, "/api/oversee", token=TOKEN, reps=3,
                              transport=send, clock=_clock())
    assert (row["warm_ok"], row["warm_total"]) == (2, 3)
    assert row["state"] == bench.HTTP_ERROR


# --- conditions ride with the numbers ----------------------------------------

def test_the_report_carries_every_condition_field():
    report = _report({"/api": OK_200}, project="mindhive")
    c = report["conditions"]
    for field in ("url", "project", "label", "method", "timeout_s", "reps",
                  "samples_per_endpoint", "timestamp", "endpoints",
                  "endpoint_set", "token_source", "server_uptime"):
        assert field in c, field
    assert c["url"] == ORIGIN
    assert c["project"] == "mindhive"
    assert "curl -w" in c["method"]              # names the method that changed
    text = bench.render(report)
    for expected in (ORIGIN, "mindhive", "isolated, no browser"):
        assert expected in text


def test_an_unpassed_project_is_named_as_the_server_default_not_guessed():
    report = _report({"/api": OK_200})
    assert "server default" in report["conditions"]["project"]
    assert "server default" in bench.render(report)


def test_the_default_endpoint_set_is_the_four_before_profile_routes_in_order():
    report = _report({"/api": OK_200})
    assert [r["path"] for r in report["rows"]] == [
        "/api/projects", "/api/overview", "/api/oversee", "/api/mindnet/cats"]
    assert "default" in report["conditions"]["endpoint_set"]


def test_supplied_endpoints_replace_the_default_set_rather_than_appending():
    report = _report({"/api": OK_200}, endpoints=["/api/overview"])
    assert [r["path"] for r in report["rows"]] == ["/api/overview"]
    assert "caller-supplied" in report["conditions"]["endpoint_set"]


def test_the_project_rides_in_the_query_string_of_every_request():
    log = []
    bench.run(origin=ORIGIN, token=TOKEN, label="x", project="fabio",
              transport=_transport({"/api": OK_200}, log), clock=_clock(),
              timestamp="2026-08-14T00:00:00+00:00")
    assert log and all("project=fabio" in entry["url"] for entry in log)


# --- the token: read, never created, never printed ---------------------------

def test_resolve_token_prefers_the_flag_then_the_env_then_the_file(monkeypatch,
                                                                  tmp_path):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("MINDHIVE_DASHBOARD_TOKEN", raising=False)
    (tmp_path / bench.TOKEN_FILENAME).write_text("from-file", encoding="utf-8")
    assert bench.resolve_token("explicit") == ("explicit", "--token")
    monkeypatch.setenv("MINDHIVE_DASHBOARD_TOKEN", "from-env")
    assert bench.resolve_token() == ("from-env", "MINDHIVE_DASHBOARD_TOKEN")
    monkeypatch.delenv("MINDHIVE_DASHBOARD_TOKEN")
    token, source = bench.resolve_token()
    assert token == "from-file" and bench.TOKEN_FILENAME in source


def test_resolve_token_reads_but_never_creates_one(monkeypatch, tmp_path):
    # `dashboard.load_or_create_token` mints and persists one. This must not:
    # the bench mutates nothing, and a minted token is not the token the
    # running server is checking against.
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("MINDHIVE_DASHBOARD_TOKEN", raising=False)
    assert bench.resolve_token() == ("", "")
    assert not (tmp_path / bench.TOKEN_FILENAME).exists()


def test_the_token_value_never_appears_in_the_report_or_the_table():
    report = _report({"/api": OK_200})
    assert TOKEN not in bench.render(report)
    import json as _json
    assert TOKEN not in _json.dumps(report)


# --- serialization: reported, and honest about a failed member ---------------

def test_both_serialization_figures_are_reported():
    report = _report({"/api": OK_200})
    s = report["serialization"]
    assert s["serial_ms"] > 0 and s["concurrent_ms"] > 0
    assert s["ratio"] is not None
    assert len(s["members"]) == 4
    assert not s["incomplete"]
    text = bench.render(report)
    assert "serial total" in text and "concurrent wall" in text


def test_a_failed_member_is_named_rather_than_dropped_from_the_denominator():
    report = _report({"/api/oversee": REFUSED, "/api": OK_200})
    s = report["serialization"]
    assert s["failed"] == ["/api/oversee"]
    assert s["incomplete"] is True
    assert len(s["members"]) == 4                 # still four, not three
    assert "/api/oversee" in bench.render(report)


def test_the_serialization_block_says_which_cache_state_it_measured():
    report = _report({"/api": OK_200})
    assert "before-profile" in report["serialization"]["cache_state"]
    assert "cache state" in bench.render(report)


# --- the command ------------------------------------------------------------

def test_a_missing_label_is_refused_with_a_message_naming_why(capsys):
    assert cli_mod.main(["bench", "dashboard", "--url", ORIGIN]) == 2
    out = capsys.readouterr().out
    assert "--label" in out and "load condition" in out


def test_a_missing_token_is_refused_rather_than_timing_four_403s(monkeypatch,
                                                                capsys):
    monkeypatch.delenv("MINDHIVE_DASHBOARD_TOKEN", raising=False)
    ret = cli_mod.main(["bench", "dashboard", "--url", ORIGIN, "--label", "x"])
    assert ret == 2
    out = capsys.readouterr().out
    assert "token gate" in out and "403" in out


def test_a_url_without_a_scheme_is_refused(capsys):
    ret = cli_mod.main(["bench", "dashboard", "--url", "127.0.0.1:54471",
                        "--label", "x", "--token", TOKEN])
    assert ret == 2
    assert "--url" in capsys.readouterr().out


def test_the_command_exits_zero_when_an_endpoint_is_slow(monkeypatch, capsys):
    monkeypatch.setattr(bench, "_default_transport",
                        _transport({"/api": OK_200}))
    ret = cli_mod.main(["bench", "dashboard", "--url", ORIGIN, "--label",
                        "live", "--token", TOKEN, "--reps", "1"])
    assert ret == 0
    assert "dashboard bench — live" in capsys.readouterr().out


def test_the_command_exits_one_when_nothing_could_be_measured(monkeypatch,
                                                             capsys):
    monkeypatch.setattr(bench, "_default_transport", _transport({"/api": REFUSED}))
    ret = cli_mod.main(["bench", "dashboard", "--url", ORIGIN, "--label", "x",
                        "--token", TOKEN, "--reps", "1"])
    assert ret == 1
    assert "NOTHING WAS MEASURED" in capsys.readouterr().out


def test_json_output_is_machine_readable_and_carries_the_conditions(monkeypatch,
                                                                   capsys):
    import json
    monkeypatch.setattr(bench, "_default_transport",
                        _transport({"/api": OK_200}))
    ret = cli_mod.main(["bench", "dashboard", "--url", ORIGIN, "--label", "iso",
                        "--token", TOKEN, "--reps", "1", "--endpoint",
                        "/api/overview", "--json"])
    assert ret == 0
    report = json.loads(capsys.readouterr().out)
    assert report["conditions"]["label"] == "iso"
    assert report["conditions"]["endpoints"] == ["/api/overview"]
    assert report["conditions"]["samples_per_endpoint"] == 2
    assert len(report["rows"]) == 1


def test_the_token_file_is_the_default_source(monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.delenv("MINDHIVE_DASHBOARD_TOKEN", raising=False)
    (tmp_path / bench.TOKEN_FILENAME).write_text(TOKEN, encoding="utf-8")
    log = []
    monkeypatch.setattr(bench, "_default_transport",
                        _transport({"/api": OK_200}, log))
    ret = cli_mod.main(["bench", "dashboard", "--url", ORIGIN, "--label", "x",
                        "--reps", "0"])
    assert ret == 0
    out = capsys.readouterr().out
    assert bench.TOKEN_FILENAME in out and TOKEN not in out
    assert log and all(f"token={TOKEN}" in entry["url"] for entry in log)


# --- wiring: the component is correct and the command is reachable -----------

def test_the_bench_group_is_reachable_from_the_real_cli(capsys):
    """`register()` written but never called leaves every test above green and
    the command missing. This is the one that notices."""
    with pytest.raises(SystemExit) as exit_info:
        cli_mod.main(["bench", "dashboard", "--help"])
    assert exit_info.value.code == 0
    out = capsys.readouterr().out
    for flag in ("--label", "--token", "--url", "--project", "--reps",
                 "--timeout", "--endpoint"):
        assert flag in out, flag


def test_bench_dashboard_is_a_group_not_a_flag_on_hive_dashboard(capsys):
    """`python -m mindhive dashboard` must keep working as a bare invocation, so
    the measurement cannot live under that parser."""
    with pytest.raises(SystemExit):
        cli_mod.main(["dashboard", "--help"])
    out = capsys.readouterr().out
    assert "--port" in out
    assert "bench" not in out


def test_the_bench_writes_no_events(monkeypatch):
    from mindhive import events
    monkeypatch.setattr(bench, "_default_transport",
                        _transport({"/api": OK_200}))
    before = len(events.read_all())
    cli_mod.main(["bench", "dashboard", "--url", ORIGIN, "--label", "x",
                  "--token", TOKEN, "--reps", "0"])
    assert len(events.read_all()) == before
