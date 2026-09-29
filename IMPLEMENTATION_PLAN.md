# Rencana Implementasi — Cetak biru MIND HIVE v1.0 (PDF) dan v1.1 (MEB)

Diukur pada commit `0fd14b2` (cabang `claude/blueprint-foundation-v1`) ditambah
pohon kerja P11 yang belum di-commit saat pengukuran — berkas yang dicantumkan di
butir P11 `CHANGELOG.md` dan di-commit tepat sesudahnya sebagai commit P11 — 2026-09-29.

Dokumen penutup cetak biru (RP fase P11; MEB §3 dan §9). Setiap angka di sini
berasal dari keluaran perintah yang benar-benar dijalankan dan dibaca pada commit
itu. Tabel audit (§5) dibangun secara mekanis dari berkas junit XML suite penuh
(`p11/junit.xml di scratch sesi — tidak di-commit; totalnya di §6`) oleh skrip pembangun, bukan ditulis tangan; tabel LOC (§8)
adalah keluaran `python scripts/loc_report.py` apa adanya; matriks ISO (§9) dan
bench (§7) menyalin keluaran `hive bench --json` dan skrip pengukur.
`tests/test_implementation_plan.py` menahan dokumen ini ke pohon kode: setiap
path yang dikutip harus ada, kosakata status tertutup, tabel LOC menyalin baris
MEB dari `scripts/loc_report.py`, dan commit yang disebut harus ada di repositori.

## 0. Cara membaca

- **RTM** (§3, §4): satu baris per butir cetak biru → modul → tes → fase →
  commit yang merealisasikannya. Kolom *Realisasi* diisi dari `git log`
  cabang ini, bukan dari rencana.
- **Tabel audit** (§5): satu baris per butir hasil penggabungan tiga peta
  ketertelusuran (RP, SPEC, MEB; 209 baris digabung menjadi 97 oleh kritik
  independen, ditambah 2 baris yang tidak dipetakan). Kosakata status:
  `LULUS (n tes)` — semua tes berkas yang dikutip lulus di junit; `GAGAL (f dari
  n)`; `DILEWATI` — berkas hanya berisi tes yang dilewati atau tier yang tidak
  diseleksi (`node`, `browser`, `pg`); `TANPA TES` — butir proses/dokumen tanpa berkas tes;
  `DIUKUR` — butir yang buktinya angka pengukuran (§7–§9); `DEVIASI` — butir
  yang sengaja tidak diikuti, tercatat di §11; `MANUAL` — butir yang buktinya di
  luar pytest (git log, ledger).
- Status berkas tes bersifat konservatif: satu tes gagal di berkas yang dikutip
  membuat baris itu `GAGAL` meski tes lain di berkas itu yang lulus adalah yang
  relevan. Tidak ada status yang disimpulkan dari nalar.

## 1. Ringkasan eksekutif

- Enam belas masalah PDF (BAB 2) dan seluruh ID MEB §4 terealisasi dalam 27
  commit fase `feat(blueprint)`/`fix(vectors)` (`9576f15` … `0fd14b2`; 32
  commit di cabang bila 1 `fix(orchestrator)`, 1 `test(runlog)` dan 3 merge
  dihitung) di satu cabang, tanpa push (K22).
- Suite penuh pada commit ini: 11176 kasus uji dikumpulkan ke junit: 11170 lulus, 0 gagal, 0 error, 6 dilewati, 1373 s. Ringkasan status tabel audit:
  DEVIASI: 3, LULUS: 91, MANUAL: 2, TANPA TES: 1.
- Dua bench MEB direproduksi (bukan disalin): startup `import mindhive.__main__` median 168.9 ms (klaim MEB 45 ms); `hive recall --semantic` median hangat 10.576 ms vs leksikal 198.9 ms, rasio 18.81× (klaim spec 14.7×).
- Prakiraan LOC MEB meleset besar ke atas (§8): total baris MEB yang diukur
  24016 baris terhadap target MEB 16515 (dinyatakan 16126), +7501; semua *hard ceiling* (cap 1000, `__main__.py` < 90,
  `orchestrator/lintester.py` = 997, allowlist shrink-only) terpenuhi —
  `python scripts/loc_report.py --check` exit 0.
- Matriks ISO/IEC 25010 (§9) diisi dari pengukuran: keandalan dari `models.py`,
  kinerja dari `hive bench`, kegunaan dari `theme.contrast_ratio`, pemeliharaan
  dari ukuran modul dan siklus impor.
- 33 gap/deviasi hasil tinjauan kritik dipetakan satu per satu (§11):
  12 ditutup, 1 ditutup sebagian, 13 deviasi tercatat, 1 di luar repo, 3 sebagian, 1 terbuka (ρ 0.842), 1 diukur dan tidak terpenuhi (render < 15 ms), 1 dijalankan dengan verdict `inconclusive` (A/B).

## 2. Diagnosis (PDF §10.1) dan matriks probabilitas (PDF §10.2, §10.10; MEB §2)

**Diagnosis.** Enam belas masalah audit PDF BAB 2 dikelompokkan tiga tier
(PDF §10.12): Tier 1 invarian (traversal session id, injeksi SOP, gerbang lolos
hampa), Tier 2 stabilitas (bobot, Laplace, kunci tugas, cache plugin, proses
yatim), Tier 3 struktur (monolit `__main__`, hook, jq, kontras, nav, lisensi,
A/B, fabricate). Realisasi per tier: Tier 1 → P0/P0b/P1d/P2, Tier 2 →
P3/P4/P4b/P1e, Tier 3 → P9/P9b/P5/P7/P10/P11. Urutan eksekusi menyimpang dari
spesifikasi pada satu titik: P9 (Tier 3) mendarat di commit pertama bersama P0
(§11 butir 32).

**Model matematis yang dipakai sebagai dasar prioritas** (`src/mindhive/models.py`,
dipin oleh `tests/test_models.py`, 60 tes):

| Model | Parameter cetak biru | Nilai yang dihitung kode | Dipin oleh |
|---|---|---|---|
| Poisson kegagalan per aksi (MEB §2.1) | λ₀ = 0.05, λ₁ = 0.008 | MTBF 20 → 125 aksi; R(100) 0.67 % → 44.93 %; gain e^4.2 ≈ 66.686 | `test_meb_sensitivity_table_is_the_documents_seven_rows_verbatim` |
| DTMC pemutus integrasi 4-state (MEB §2.2, K1) | ambang 3, cooldown 300 s, 3 trip/3600 s → QUARANTINED | π stasioner dari parameter yang dinyatakan: ketersediaan 95.39 % (baseline 44.27 %); vektor π yang DICETAK MEB memberi 98.03 % — tidak mengikuti parameternya sendiri; reduksi downtime 91.72 % | `test_meb_calibration_what_the_stated_parameters_actually_give` |
| CLT overflow konteks (MEB §2.5, K5) | c = 140 000, b = 160 000, k = 5, μ = 3 000, σ = 800 | P(overflow) ≈ 0.0026 | `tests/test_models.py` |
| Birthday bound SHA-256 (MEB §7.3) | n = 10⁹, 256 bit | ≈ 4.318 × 10⁻⁶⁰ | `test_meb_hash_collision_probability_is_the_birthday_bound` |
| Memori HNSW (MEB §8, K20) | 50 000 vektor × 2 000 B | 100 000 000 B | `test_meb_hnsw_parameters_and_memory_estimate` |
| Spearman ρ kalibrasi bobot (MEB §2.4) | ρ = 0.842 atas 10 modul contoh | **tidak dapat dipin**: MEB tidak memuat data contohnya; hanya kanari monoton | `test_spearman_rho_on_aligned_and_reversed_ranks` |

**Matriks risiko operasional** (MEB §2.6, `models.OPERATIONAL_RISK_MATRIX`,
disalin dari kode pada commit ini):

| # | Mode kegagalan | P | Dampak | Skor | Mitigasi |
|---|---|---:|---:|---:|---|
| 1 | Flaky Test Assertion Manipulation (Agent Cheat) | 0.70 (High) | 4 (High - False Quality) | 2.80 | Immutable Test Gates (penguncian read-only pada berkas uji selama implementasi). |
| 2 | API Rate Limit & Token Runaway | 0.65 (High) | 4 (High - Cost Explosion) | 2.60 | IntegrationBreaker 4-status terpersistensi & pembatasan kuota per lane. |
| 3 | NTFS File Lock Collision (PermissionError pada open) | 0.60 (Medium-High) | 4 (High - Data Loss) | 2.40 | Spin-wait jittered backoff pada os.open di core/locks.py & vectors.py. |
| 4 | Silent Memory Tail Truncation | 0.55 (Medium) | 3 (Medium - Context Loss) | 1.65 | Plafon aman 24.500 bita LF di adaptivetail.py dan retensi 3-tier ke ChromaDB. |
| 5 | Bytecode Sharing Violation (WinError 32 di NTFS) | 0.50 (Medium) | 3 (Medium - Flaky Build) | 1.50 | Penegakan PYTHONDONTWRITEBYTECODE=1 pada seluruh runner subprocess. |
| 6 | Circular Import Deadlock saat Dekomposisi CLI | 0.35 (Low-Medium) | 4 (High - Startup Failure) | 1.40 | Aliran dependensi satu arah (Core → Handlers → Parsers → Main). |
| 7 | Git Worktree Merge Conflict Contention | 0.40 (Medium) | 3 (Medium - Latency) | 1.20 | AST File Ownership Clustering & Dual-hook Gateway Interceptor. |
| 8 | Path Traversal Injection (Windows Reserved Names) | 0.15 (Low) | 5 (Critical - System Breach) | 0.75 | Sanitasi ketat validate_session_id & pemblokiran nama CON, PRN, AUX, NUL. |

## 3. Matriks ketertelusuran v1.0 (RP §3, diperbarui dengan realisasi)

Kolom Aksi seperti di RP: ADA = sudah di master, hanya tes/dokumen; UBAH =
modifikasi; BARU = berkas baru; DOK = dokumentasi/laporan. Kolom Realisasi =
commit cabang ini yang merealisasikan butir itu.

| Bab PDF | Butir | Modul / Berkas | Tes | Aksi | Fase | Realisasi |
|---|---|---|---|---|---|---|
| 1.1–1.5 | Prasyarat OS/Python/extras/Claude CLI/hardware | `pyproject.toml`, `README.md` (Quick start, Prerequisites) | `tests/test_pyproject_extras.py`, `tests/test_readme_prerequisites.py` | DOK+tes | P8, P11 | `b274be4`; P11 (bagian Prerequisites README) |
| 1.6 | Registri env var | `README.md` §Environment variables | `tests/test_env_registry.py` | DOK+tes | P8 | `b274be4` |
| 1.7 | Topologi `~/.mindhive/` (+ `state/pending_embed.json`, `chroma/`) | `src/mindhive/paths.py` (`state_dir`), `docs/DATA_MODEL.md` §1 | `tests/test_event_types_v1.py`, `tests/test_store_root_files.py` | UBAH | P0 | `9576f15` |
| 1.8 | Worktree `.worktrees/lane-<N>`, `rollback()` | `src/mindhive/orchestrator/worktree.py` | `tests/test_orchestrator_worktree.py` | ADA | — | pra-cetak biru |
| 2.1 / 10.13.1 | Bug 1 hook portability, `Resolve-MindHivePython` | `hooks/lib.ps1` | `tests/test_hook_shims_ps.py` | UBAH | P5 | `b5e8c0e` |
| 2.2 / 8.1 / 10.13.2 | Bug 2 `SESSION_ID_REGEX`, `validate_session_id`, `strict_session` | `src/mindhive/events.py`, `src/mindhive/cliparse.py` | `tests/test_tier1_invariants.py`, `tests/test_events_sec.py` | UBAH | P0/P0b | `9576f15`, `53989ca` |
| 2.3 / 10.13.3 | Bug 3 `advice.py` gerbang penulis SOP | `src/mindhive/advice.py`, `src/mindhive/commands/registry_sop.py`, `src/mindhive/sopgate.py` | `tests/test_advice_authorship.py`, `tests/test_tier1_invariants.py` | BARU+UBAH | P1d | `99bedec` |
| 2.4 / 10.13.4 | Bug 4 SuiteGate fail-closed + `--allow-untested` + counter | `src/mindhive/orchestrator/gate.py`, `src/mindhive/orchestrator/cli.py`, `src/mindhive/orchestrator/judge.py` | `tests/test_orchestrator_gate.py`, `tests/test_cli_lintester.py`, `tests/test_tier1_invariants.py` | UBAH | P2 | `dee01e3` |
| 2.5 / 10.16 | Bug 5 modularisasi `commands/` + `__main__` < 90 | `src/mindhive/commands/`, `src/mindhive/__main__.py` (60 baris) | `tests/test_commands_reexport.py`, `tests/test_commands_base.py`, `tests/test_file_size_cap.py` | BARU+UBAH | P9/P9b | `9576f15`, `4d615df` |
| 2.6 / 10.14.1 | Bug 6 `compute_calibrated_weight` log-linear, clamp [1, 50] | `src/mindhive/orchestrator/payoff.py`, `src/mindhive/orchestrator/metrics.py`, `src/mindhive/orchestrator/cli.py` (`--weight-model`) | `tests/test_tier2_stability.py`, `tests/test_cli_lintester.py` | UBAH | P4, P8 | `30a1dee`, `b274be4` |
| 2.7 / 10.7 / 10.13.5 | Bug 7 `task_lock` publik, `LockTimeoutError`, jitter, stale 60 s, 12 thread | `src/mindhive/tasktool.py`, `src/mindhive/core/locks.py` | `tests/test_tasktool_lock.py`, `tests/test_core_locks.py` | UBAH | P3, P1j | `c22601d`, `9883ffb` |
| 2.8 / 10.14.2 | Bug 8 Laplace + cache plugin version-hash | `src/mindhive/models.py`, `src/mindhive/core/recommend.py`, `src/mindhive/recommend.py`, `src/mindhive/plugins.py`, `src/mindhive/plugin_cache.py` | `tests/test_tier2_stability.py`, `tests/test_core_recommend.py`, `tests/test_plugins_version_hash.py`, `tests/test_plugin_cache.py` | UBAH | P4, P4b | `30a1dee`, `b7b7531` |
| 2.9 / 11.4 | Bug 9 port `fabricate.py` | `src/mindhive/fabricate.py`, `src/mindhive/commands/orchestration.py`, `src/mindhive/autoresume.py`, `src/mindhive/loop/engine.py` | `tests/test_fabricate.py` | BARU | P10 | `284d064` |
| 2.10 / 10.14.3 | Bug 10 `extract_json_field()` tanpa jq | `hooks/lib.sh` | `tests/test_hook_shims.py` | UBAH | P5 | `b5e8c0e` |
| 2.11 | Bug 11 backlog EventBus (`BACKLOG_HEAD = 50`) | `src/mindhive/orchestrator/board.py` | `tests/test_orchestrator_board.py` | ADA+tes | P5 | `b5e8c0e` |
| 2.12 / TODOS #7 | Bug 12 `#6b7488` → `#9aa3b8`, WCAG | `src/mindhive/theme.py` | `tests/test_theme_contrast.py` | UBAH | P5 | `b5e8c0e` |
| 2.13 / TODOS #2 | Bug 13 nav vs aksi | `src/mindhive/dashboard_page.py`, `src/mindhive/dashboard_css.py` | `tests/test_dashboard_nav_split.py` | UBAH | P5 | `b5e8c0e` |
| 2.14 | Bug 14 LICENSE Apache-2.0, CONTRIBUTING, CHANGELOG, SPDX | `LICENSE`, `CONTRIBUTING.md`, `CHANGELOG.md`, `pyproject.toml`, semua `src/mindhive/**/*.py` | `tests/test_license.py` | BARU | P7 | `cd65458` |
| 2.15 | Bug 15 A/B `hive mindnet ab --execute --record` | `src/mindhive/mindnet/ab.py` | `tests/test_mindnet_ab.py` | ADA; eksekusi | P11 | §12 |
| 2.16 / 10.14.4 | Bug 16 `orchestrator/driver.py`, tree-kill di `verify._stream` | `src/mindhive/orchestrator/driver.py`, `src/mindhive/verify.py` | `tests/test_orchestrator_driver.py`, `tests/test_verify.py` | BARU+UBAH | P1e | `b30d0da` |
| 3.1–3.6 | Poisson/MTBF, DTMC, Spearman, depletion, Bayesian, matriks risiko | `src/mindhive/models.py` | `tests/test_models.py` | BARU | P1a, P1a' | `9576f15`, `a523bc0` |
| 4.1 | Filosofi CQRS/ES | `docs/DATA_MODEL.md` §4.19 | `tests/test_docs_parity.py` (rujukan) | DOK | P8 | `b274be4` (+ `1500444`, `cf2fd9f`, `bc96acd`) |
| 4.2.1–4.2.3 | `canonical_json`, `compute_chain_hash`, `GENESIS_PREV_HASH`, `EventRecord`, lock atomik | `src/mindhive/events.py`, `src/mindhive/core/models.py` | `tests/test_events_chain_helpers.py`, `tests/test_events_canonical.py`, `tests/test_events_append_race.py` | UBAH | P0/P0b | `9576f15`, `53989ca` |
| 4.3.1–4.3.3 | Encoder lokal, template dokumen, metadata back-pointer | `src/mindhive/vectors.py` | `tests/test_vectors_v1.py` | UBAH | P1h | `866fadd` |
| 4.4.1–4.4.2 | Watcher, siklus marker, rename atomik NTFS | `src/mindhive/watcher.py`, `src/mindhive/embedqueue.py` | `tests/test_watcher.py`, `tests/test_event_types_v1.py` | BARU | P1h | `866fadd` |
| 4.5 | Startup auto-recovery | `src/mindhive/watcher.py` (`recover_stale_markers`), `src/mindhive/dashboard.py` | `tests/test_watcher.py` | BARU | P1h | `866fadd` |
| 4.6 | Progress bar vektor, `SKEL_MS=150`, `\r` | `src/mindhive/jobs.py`, `src/mindhive/dashboard_js_actions.py` | `tests/test_jobs.py`, `tests/test_dashboard_theme.py` | UBAH | P1h | `866fadd` |
| 4.7 | Benchmark vektor vs full scan | `src/mindhive/bench_scenarios.py`, `src/mindhive/bench.py` | `tests/test_bench_scenarios.py`, `tests/test_bench.py` | UBAH+DOK | P8, P11 | `b274be4`; angka di §7 |
| 5.1–5.5 / 8.4 | Work Sessions, 4 tag, `ACTION_CATALOG`, Oversee | `src/mindhive/worksessions.py`, `src/mindhive/oversee.py`, `src/mindhive/dashboard_get_v1.py`, `src/mindhive/dashboard_js_oversee.py` | `tests/test_worksessions.py`, `tests/test_oversee.py`, `tests/test_dashboard_get_v1.py` | BARU+UBAH | P1b | `0d75f80` |
| 6.1–6.5 / 8.5 | Adaptive tail, rollover, tier retensi, dreaming 0.88, `hive context --check` | `src/mindhive/adaptivetail.py`, `src/mindhive/core/dreaming.py`, `src/mindhive/sync.py`, `src/mindhive/context.py` | `tests/test_adaptivetail.py`, `tests/test_core_dreaming.py`, `tests/test_sync.py`, `tests/test_context.py` | BARU+UBAH | P1c, P1j | `a3dd07f`, `9883ffb` |
| 7.1–7.6 / 8.6 | Interceptor hybrid, breaker 4-state, `merge_conflict_detected` | `src/mindhive/orchestrator/interceptor.py`, `src/mindhive/orchestrator/breaker.py`, `src/mindhive/orchestrator/quota.py`, `src/mindhive/orchestrator/integrate.py` | `tests/test_orchestrator_interceptor.py`, `tests/test_orchestrator_breaker.py`, `tests/test_orchestrator_quota.py` | BARU+UBAH | P1f | `c73679b` |
| 8.2 / 10.9.1 | vectors: env lock, `VectorStoreManager`, batch 32, `--rebuild` | `src/mindhive/vectors.py`, `src/mindhive/commands/maintenance.py` | `tests/test_vectors_v1.py` | UBAH | P1h | `866fadd` |
| 8.3 | `BackgroundEmbeddingWatcher` | `src/mindhive/watcher.py` | `tests/test_watcher.py` | BARU | P1h | `866fadd` |
| 9.1 | Migrasi 4 fase + kriteria | urutan fase | — | — | — | urutan commit `9576f15` … `30a1dee` |
| 9.2 | Suite TDD wajib | `tests/` | `tests/test_events_append_race.py`, `tests/test_tasktool_lock.py`, `tests/test_vectors_v1.py`, `tests/test_sop_digest.py`, `tests/test_orchestrator_gate.py`, `tests/test_hook_shims.py` | UBAH | P0–P5 | §6 |
| 9.3 | SOP Lima Aturan Emas | `docs/SOP_FIVE_GOLDEN_RULES.md`, `docs/SOP_FIVE_GOLDEN_RULES_COMPACT.md` | `tests/test_sop_golden_rules_doc.py` | DOK | P8, P11 | `b274be4`; registrasi §11 butir 3 |
| 9.4 | Runbook kontinjensi | `docs/RUNBOOK_RECOVERY.md`, `src/mindhive/commands/verification.py` | `tests/test_commands_base.py`, `tests/test_vectors_v1.py`, `tests/test_tasktool_lock.py` | DOK+UBAH | P8 | `b274be4` |
| 10.1–10.3, 10.6, 10.10, 10.15, 11.4 | Diagnosis, matriks probabilitas, benchmark, laporan validasi, kalibrasi | dokumen ini | `tests/test_implementation_plan.py` | DOK | P11 | commit ini |
| 10.4 / 10.5 / 10.11 | Pilar 1 & 4 `BoundedMemo`, `gated_fold`, `static/dashboard.js` | `src/mindhive/dashboard_memo.py`, `src/mindhive/static/dashboard.js`, `src/mindhive/dashboard_get_v1.py` | `tests/test_dashboard_memo_public.py`, `tests/test_static_dashboard_js.py` | UBAH+BARU | P5 | `b5e8c0e` |
| 10.7 | Pilar 2 `task_lock` | (lihat 2.7) | | | P3 | `c22601d` |
| 10.8 | Pilar 3 `paginate_records`, `trim_field_content` | `src/mindhive/pgshape.py`, `src/mindhive/pgtools.py`, `src/mindhive/dashboard_get.py` | `tests/test_pgshape_pagination.py`, `tests/test_dashboard_pagination.py` | UBAH | P5 | `b5e8c0e` |
| 10.9.2 | Preflight `EXIT_ENVIRONMENT_REFUSAL=3` | `src/mindhive/orchestrator/environment.py`, `src/mindhive/orchestrator/cli.py` | `tests/test_orchestrator_environment.py` | UBAH | P5 | `b5e8c0e` |
| 10.12 | Triage 3-tier | urutan fase | — | — | — | deviasi (§11 butir 32) |
| 10.13.6 | `tests/test_tier1_invariants.py` | — | `tests/test_tier1_invariants.py` | BARU | P0/P2 | `9576f15`, `dee01e3` |
| 10.14.5 | `tests/test_tier2_stability.py` | — | `tests/test_tier2_stability.py` | BARU | P4 | `30a1dee` |
| 11.1–11.3 | CI mandiri `hive ci run|status|hook-install`, 4 gate | `src/mindhive/ci/runner.py`, `src/mindhive/ci/cli.py`, `.github/workflows/ci.yml` | `tests/test_ci_runner.py`, `tests/test_ci_cli.py` | BARU | P1g | `16ca7da` |
| 11.5 | Langkah lanjut | dokumen ini §13 | — | DOK | P11 | commit ini |
| TODOS | status #1 #2 #5 #7 #14 #15 #30 #31 #32 … | `TODOS.md` | — | DOK | P8/P9 | `b274be4` (#1 #30 #31), `b5e8c0e` (#2 #7), `284d064` (#15) |

## 4. Matriks ketertelusuran v1.1 (MEB §3, diperbarui)

| ID MEB | Butir | Modul / Berkas | Tes | Fase | Realisasi |
|---|---|---|---|---|---|
| SEC-01 | `WINDOWS_RESERVED_NAMES`, 4 pesan `validate_session_id` | `src/mindhive/events.py`, `src/mindhive/contracts.py`, `src/mindhive/core/models.py` | `tests/test_events_sec.py`, `tests/test_contracts_reexport.py` | P0b | `53989ca` |
| SEC-02 | `canonical_json` RFC 8785 (`-0.0`, kedalaman 100, `allow_nan=False`) | `src/mindhive/events.py` | `tests/test_events_canonical.py`, `tests/test_pg_numeric_parity.py` | P0b | `53989ca` |
| CONC-01 | `core/locks.py` | `src/mindhive/core/locks.py` | `tests/test_core_locks.py` | P1j | `9883ffb` |
| CONC-02 | `tasktool.task_lock` verbatim MEB | `src/mindhive/tasktool.py` | `tests/test_tasktool_lock.py` | P3 | `c22601d` |
| VEC-01 | `storage/local_vector.py` + env lock + `chroma_lock` | `src/mindhive/storage/local_vector.py`, `src/mindhive/vectors.py` | `tests/test_vectors_v1.py`, `tests/test_storage_local_vector.py` | P1h, P1i | `866fadd`, `1098559` |
| VEC-02 | `watcher.py` `recover_stale_markers` (> 600 s) | `src/mindhive/watcher.py` | `tests/test_watcher.py` | P1h | `866fadd` |
| CTX-01 | `adaptivetail.py` konstanta + `calculate_budget` + `ROLLOVER_TEMPLATE` | `src/mindhive/adaptivetail.py` | `tests/test_adaptivetail.py` | P1c | `a3dd07f` |
| CTX-02 | `core/dreaming.py` ≥ 0.88 | `src/mindhive/core/dreaming.py` | `tests/test_core_dreaming.py` | P1j | `9883ffb` |
| SES-01 | Work sessions | `src/mindhive/worksessions.py` | `tests/test_worksessions.py` | P1b | `0d75f80` |
| ORCH-01 | SuiteGate fail-closed + Immutable Test Gates | `src/mindhive/orchestrator/gate.py`, `src/mindhive/orchestrator/judge.py`, `src/mindhive/orchestrator/worktree.py` | `tests/test_orchestrator_gate.py`, `tests/test_orchestrator_judge.py` | P2 | `dee01e3` |
| ORCH-02/03 | Interceptor + breaker + `LaneQuota` | `src/mindhive/orchestrator/interceptor.py`, `src/mindhive/orchestrator/breaker.py`, `src/mindhive/orchestrator/quota.py` | `tests/test_orchestrator_interceptor.py`, `tests/test_orchestrator_breaker.py`, `tests/test_orchestrator_quota.py` | P1f | `c73679b` |
| ORCH-04 | `terminate_process_tree` verbatim MEB | `src/mindhive/orchestrator/driver.py`, `src/mindhive/verify.py` | `tests/test_orchestrator_driver.py` | P1e | `b30d0da` |
| CI-01/02 | `NativeCIRunner`, `_cleanup_worktree`, `_gate_env` | `src/mindhive/ci/runner.py`, `src/mindhive/ci/cli.py` | `tests/test_ci_runner.py`, `tests/test_ci_cli.py` | P1g | `16ca7da` |
| CLI-01/02/03 | `commands/base.py`, `verification.py`, shim `main(argv=None)` | `src/mindhive/commands/base.py`, `src/mindhive/commands/verification.py`, `src/mindhive/__main__.py` | `tests/test_commands_base.py`, `tests/test_commands_reexport.py` | P9b | `4d615df` |
| STO-01/02 | `storage/` SPI, driver lokal fsync, Postgres, pgvector | `src/mindhive/storage/interface.py`, `src/mindhive/storage/local_driver.py`, `src/mindhive/storage/postgres_driver.py`, `src/mindhive/storage/pgvector_driver.py` | `tests/test_storage_interface.py`, `tests/test_storage_local_driver.py`, `tests/test_storage_postgres_driver.py`, `tests/test_storage_pgvector_driver.py` | P1i | `1098559` |
| MIG-01 | `EventReplayMigrationEngine`, `hive db migrate|status` | `src/mindhive/storage/migration.py`, `src/mindhive/storage/records.py`, `src/mindhive/storage/cli.py`, `src/mindhive/pgmirror.py`, `src/mindhive/pgmigrations/007_event_embeddings_hashes.sql` | `tests/test_storage_migration.py`, `tests/test_storage_records.py`, `tests/test_storage_cli.py`, `tests/test_storage_pg_live.py` (tier `pg`) | P1i | `1098559` |
| THEME-01 | `PALETTE` MEB + WCAG | `src/mindhive/theme.py` | `tests/test_theme_contrast.py` | P5 | `b5e8c0e` |
| HOOK-01/02 | `extract_json_field`, `Resolve-MindHivePython` | `hooks/lib.sh`, `hooks/lib.ps1` | `tests/test_hook_shims.py`, `tests/test_hook_shims_ps.py` | P5 | `b5e8c0e` |
| TDD-01 | Tier-1 | `tests/test_tier1_invariants.py` | — | P0b/P2 | `53989ca`, `dee01e3` |
| TDD-02 | Tier-2 | `tests/test_tier2_stability.py` | — | P4 | `30a1dee` |
| §1.4 | `mindhive ui` efemeral + `server/` | `src/mindhive/server/lifecycle.py`, `src/mindhive/server/routes.py`, `src/mindhive/server/js.py`, `src/mindhive/server/app.py`, `src/mindhive/server/asgi.py` | `tests/test_server_lifecycle.py`, `tests/test_server_routes.py`, `tests/test_server_app.py`, `tests/test_server_cli.py`, `tests/test_server_asgi.py` | P1k | `cd928f0` |
| §2.1 | Poisson λ₀/λ₁, R(100), gain e^4.2, tabel sensitivitas | `src/mindhive/models.py` | `tests/test_models.py` | P1a' | `a523bc0` |
| §2.4 | Spearman ρ = 0.842 | `src/mindhive/models.py` | `tests/test_models.py` (kanari monoton) | P1a' | `a523bc0` (deviasi: tanpa data) |
| §2.5 | CLT overflow, b = 160 000 | `src/mindhive/models.py`, `src/mindhive/worksessions.py` | `tests/test_models.py`, `tests/test_worksession_usage.py` | P1a' | `a523bc0` |
| §2.6 | Matriks risiko 8 baris | `src/mindhive/models.py` | `tests/test_models.py` | P1a' | `a523bc0` |
| §3 | Prakiraan LOC | `scripts/loc_report.py`, dokumen ini §8 | `tests/test_loc_targets.py` | P11 | commit ini |
| §3.4 | Allowlist shrink-only (baris ditambahkan; tidak ada di peta) | `tests/test_file_size_cap.py` (`ALLOWED`), `scripts/loc_report.py` (`SHRINK_ONLY`) | `tests/test_file_size_cap.py`, `tests/test_loc_targets.py` | P9, P11 | `9576f15`, commit ini |
| §7.3 | Birthday bound | `src/mindhive/models.py` | `tests/test_models.py` | P1a' | `a523bc0` |
| §8 | HNSW param + memori | `src/mindhive/vectors.py`, `src/mindhive/models.py` | `tests/test_vectors_v1.py`, `tests/test_models.py` | P1h/P1a' | `866fadd`, `a523bc0` |
| §9 | Matriks ISO 25010 terukur | dokumen ini §9 | `tests/test_implementation_plan.py` | P11 | commit ini |
| §10.1 | Registri konstanta | `tests/test_constants_registry.py` | `tests/test_constants_registry.py` | P8 | `b274be4` |
| K10.3 | `hive recall --semantic` (baris ditambahkan; tidak ada di peta) | `src/mindhive/commands/parsers/ledger.py`, `src/mindhive/vectors.py`, `src/mindhive/vector_snapshot.py`, `src/mindhive/vector_reader.py` | `tests/test_recall.py`, `tests/test_vectors.py`, `tests/test_vector_snapshot.py`, `tests/test_vector_reader.py` | pasca-P8 | `1500444`, `cf2fd9f`, `bc96acd`, `0fd14b2` |

## 5. Tabel audit

Dibangun dari `p11/junit.xml di scratch sesi — tidak di-commit; totalnya di §6` (suite penuh pada commit `0fd14b2`,
`pytest -q -p no:cacheprovider --tb=short --junitxml=… --ignore=tests/test_implementation_plan.py`
dengan seleksi bawaan `pyproject.toml`: `addopts = -m 'not node and not browser
and not pg'`). Angka di kolom tes adalah `lulus/total` per berkas seperti yang
dicatat junit; berkas tier `node`, `browser` dan `pg` tidak diseleksi oleh
konfigurasi itu dan tampil sebagai *tidak dijalankan*.
`tests/test_implementation_plan.py` dikecualikan karena dokumen ini belum ada
saat suite berjalan; ia dijalankan sendiri sesudahnya (§6).

| Bab / Fitur PDF | Modul / Berkas Kode Sumber | Berkas Pengujian (Tests) | Status Verifikasi |
|---|---|---|---|
| **RP-RTM 1.1–1.5 / SPEC-1.2 / SPEC-1.3** — BAB 1.1-1.5 Prasyarat (OS, Python, extras, Claude CLI, hardware) | `pyproject.toml`, `README.md` | `tests/test_pyproject_extras.py` (19/19 lulus), `tests/test_readme_prerequisites.py` (3/3 lulus) | LULUS (22 tes) |
| **SPEC-1.1** — BAB 1 Lingkungan | `hooks/lib.sh`, `hooks/lib.ps1`, `hooks/session_start.sh`, `hooks/session_start.ps1` | `tests/test_hook_shims.py` (350/350 lulus), `tests/test_hook_shims_ps.py` (140/140 lulus) | LULUS (490 tes) |
| **SPEC-1.4** — BAB 1 Lingkungan | `src/mindhive/claude_cli.py`, `src/mindhive/autoresume.py`, `src/mindhive/loop/config.py` | `tests/test_claude_cli.py` (32/32 lulus) | LULUS (32 tes) |
| **RP-RTM 1.6 / SPEC-1.6** — BAB 1.6 Registri env var | `README.md` | `tests/test_env_registry.py` (11/11 lulus), `tests/test_constants_registry.py` (18/18 lulus) | LULUS (29 tes) |
| **RP-RTM 1.7 / SPEC-1.7** — BAB 1.7 Topologi ~/.mindhive/ | `src/mindhive/paths.py`, `src/mindhive/embedqueue.py`, `docs/DATA_MODEL.md` | `tests/test_event_types_v1.py` (49/49 lulus), `tests/test_store_root_files.py` (145/145 lulus) | LULUS (194 tes) |
| **RP-RTM 1.8 / SPEC-1.8** — BAB 1.8 Worktree lane | `src/mindhive/orchestrator/worktree.py` | `tests/test_orchestrator_worktree.py` (48/49 lulus, 1 dilewati) | LULUS (48 tes, 1 dilewati) |
| **RP-RTM 2.1 / SPEC-M1 / HOOK-02** — BAB 2 Masalah 1 / 10.13.1 Bug 1 (hardcoded paths) | `hooks/lib.ps1`, `circle.ps1` | `tests/test_hook_shims_ps.py` (140/140 lulus) | LULUS (140 tes) |
| **RP-RTM 2.2 / SPEC-M2** — BAB 2 Masalah 2 / 8.1 / 10.13.2 Bug 2 (path traversal) | `src/mindhive/events.py`, `src/mindhive/cliparse.py` | `tests/test_tier1_invariants.py` (83/83 lulus), `tests/test_events_sec.py` (67/67 lulus), `tests/test_events_segment_path.py` (168/168 lulus) | LULUS (318 tes) |
| **SEC-01 / K10** — MEB SEC-01 / K10 (BAB 2 Masalah 2) | `src/mindhive/events.py`, `src/mindhive/contracts.py`, `src/mindhive/core/models.py`, `src/mindhive/paths.py` | `tests/test_events_sec.py` (67/67 lulus), `tests/test_contracts_reexport.py` (5/5 lulus), `tests/test_tier1_invariants.py` (83/83 lulus) | LULUS (155 tes) |
| **RP-RTM 2.3 / SPEC-M3** — BAB 2 Masalah 3 / 10.13.3 Bug 3 (SOP injection) | `src/mindhive/advice.py`, `src/mindhive/mindnet/advice.py`, `src/mindhive/commands/registry_sop.py`, `src/mindhive/sopgate.py` | `tests/test_advice_authorship.py` (175/175 lulus), `tests/test_tier1_invariants.py` (83/83 lulus), `tests/test_sop_authorship.py` (70/70 lulus), `tests/test_orchestration_authorship.py` (26/26 lulus) | LULUS (354 tes) |
| **RP-RTM 2.4 / SPEC-M4 / ORCH-01 / K17** — BAB 2 Masalah 4 / 10.13.4 Bug 4 (vacuous pass) / MEB ORCH-01 / K17 | `src/mindhive/orchestrator/gate.py`, `src/mindhive/orchestrator/cli.py`, `src/mindhive/orchestrator/judge.py`, `src/mindhive/orchestrator/worktree.py` | `tests/test_orchestrator_gate.py` (78/78 lulus), `tests/test_cli_lintester.py` (63/63 lulus), `tests/test_tier1_invariants.py` (83/83 lulus), `tests/test_orchestrator_judge.py` (129/129 lulus), `tests/test_orchestrator_worktree.py` (48/49 lulus, 1 dilewati) | LULUS (401 tes, 1 dilewati) |
| **K13** — MEB K13 Immutable Test Gate (BAB 2 Masalah 4) | `src/mindhive/orchestrator/judge.py`, `src/mindhive/orchestrator/gate.py`, `src/mindhive/orchestrator/worktree.py`, `src/mindhive/orchestrator/cli.py` | `tests/test_orchestrator_gate.py` (78/78 lulus), `tests/test_orchestrator_worktree.py` (48/49 lulus, 1 dilewati), `tests/test_tier1_invariants.py` (83/83 lulus) | LULUS (209 tes, 1 dilewati) |
| **RP-RTM 2.5 / SPEC-M5 / SPEC-10.16.4** — BAB 2 Masalah 5 / 10.16 Bug 5 (monolith __main__) / 10.16.4 | `src/mindhive/__main__.py`, `src/mindhive/commands/orchestration.py` | `tests/test_commands_reexport.py` (17/17 lulus), `tests/test_file_size_cap.py` (22/22 lulus), `tests/test_loc_targets.py` (31/31 lulus), `tests/test_commands_base.py` (29/29 lulus) | LULUS (99 tes) |
| **CLI-01 / K7** — MEB CLI-01 / K7 | `src/mindhive/commands/base.py`, `src/mindhive/commands/verification.py`, `src/mindhive/commands/parsers/maintenance.py` | `tests/test_commands_base.py` (29/29 lulus), `tests/test_commands_reexport.py` (17/17 lulus) | LULUS (46 tes) |
| **CLI-02** — MEB §4.2 #8 parsers/ subpaket | `src/mindhive/commands/parsers/__init__.py`, `src/mindhive/commands/parsers/maintenance.py` | `tests/test_commands_base.py` (29/29 lulus) | LULUS (29 tes) |
| **CLI-03 / SPEC-10.16.1** — MEB CLI-03 / BAB 10.16.1 | `src/mindhive/__main__.py`, `src/mindhive/commands/base.py` | `tests/test_commands_reexport.py` (17/17 lulus), `tests/test_commands_base.py` (29/29 lulus) | LULUS (46 tes) |
| **SPEC-10.16.2** — BAB 10 Tier 3 refactor | `src/mindhive/commands/maintenance.py` | `tests/test_commands_reexport.py` (17/17 lulus) | LULUS (17 tes) |
| **SPEC-10.16.3** — BAB 10 Tier 3 refactor | — | — | DEVIASI (tidak ada parser `gate`; gerbang hanya lewat `hive lintester` — §11 butir 7) |
| **RP-RTM 2.6 / SPEC-M6 / K3** — BAB 2 Masalah 6 / 10.14.1 Bug 6 (weight) / MEB K3 | `src/mindhive/orchestrator/payoff.py`, `src/mindhive/orchestrator/metrics.py`, `src/mindhive/models.py`, `src/mindhive/orchestrator/cli.py` | `tests/test_tier2_stability.py` (43/43 lulus), `tests/test_orchestrator_metrics.py` (17/17 lulus), `tests/test_cli_lintester.py` (63/63 lulus) | LULUS (123 tes) |
| **RP-RTM 2.7 / RP-RTM 10.7 / SPEC-M7 / SPEC-Pilar2 / CONC-02 / K4** — BAB 2 Masalah 7 / 10.7 Pilar 2 / 10.13.5 Bug 7 (task lock) / MEB CONC-02 / K4 | `src/mindhive/tasktool.py`, `src/mindhive/core/locks.py` | `tests/test_tasktool_lock.py` (15/15 lulus), `tests/test_tasktool.py` (58/58 lulus), `tests/test_tier2_stability.py` (43/43 lulus), `tests/test_core_locks.py` (15/15 lulus) | LULUS (131 tes) |
| **CONC-01** — MEB §4.2 #3 Primitif lock lintas platform | `src/mindhive/core/locks.py` | `tests/test_core_locks.py` (15/15 lulus) | LULUS (15 tes) |
| **RP-RTM 2.8 / SPEC-M8** — BAB 2 Masalah 8 / 10.14.2 Bug 8 (stale plugin cache) | `src/mindhive/plugins.py`, `src/mindhive/plugin_cache.py`, `hooks/session_start.ps1`, `hooks/session_start.sh`, `src/mindhive/models.py` | `tests/test_plugins_version_hash.py` (14/14 lulus), `tests/test_plugin_cache.py` (36/36 lulus), `tests/test_hook_shims.py` (350/350 lulus), `tests/test_tier2_stability.py` (43/43 lulus), `tests/test_recommend.py` (48/48 lulus), `tests/test_core_recommend.py` (34/34 lulus) | LULUS (525 tes) |
| **K2 / SPEC-3.5** — BAB 3.5 / 2.8 Bug 8 Laplace / MEB K2 | `src/mindhive/models.py`, `src/mindhive/core/recommend.py`, `src/mindhive/recommend.py` | `tests/test_core_recommend.py` (34/34 lulus), `tests/test_tier2_stability.py` (43/43 lulus), `tests/test_models.py` (129/129 lulus) | LULUS (206 tes) |
| **RP-RTM 2.9 / SPEC-M9** — BAB 2 Masalah 9 / 11.4 Bug 9 (fabricate) | `src/mindhive/fabricate.py`, `src/mindhive/events.py`, `src/mindhive/contracts.py`, `src/mindhive/autoresume.py`, `src/mindhive/loop/engine.py`, `src/mindhive/commands/orchestration.py`, `src/mindhive/commands/parsers/orchestration.py`, `src/mindhive/pgschema.py`, `docs/DATA_MODEL.md` | `tests/test_fabricate.py` (46/46 lulus), `tests/test_commands_reexport.py` (17/17 lulus) | LULUS (63 tes) |
| **RP-RTM 2.10 / SPEC-M10 / HOOK-01** — BAB 2 Masalah 10 / 10.14.3 Bug 10 (jq dependency) / MEB HOOK-01 | `hooks/lib.sh` | `tests/test_hook_shims.py` (350/350 lulus) | LULUS (350 tes) |
| **RP-RTM 2.11 / SPEC-M11** — BAB 2 Masalah 11 Bug 11 (TUI / event backlog) | `src/mindhive/orchestrator/board.py`, `src/mindhive/orchestrator/dashboard.py` | `tests/test_orchestrator_board.py` (6/6 lulus), `tests/test_orchestrator_dashboard.py` (140/140 lulus) | LULUS (146 tes) |
| **RP-RTM 2.12 / SPEC-M12 / THEME-01 / K12** — BAB 2 Masalah 12 / TODOS #7 Bug 12 (WCAG contrast) / MEB THEME-01 / K12 | `src/mindhive/theme.py`, `src/mindhive/dashboard_css.py` | `tests/test_theme_contrast.py` (48/48 lulus), `tests/test_dashboard_theme.py` (18/18 lulus) | LULUS (66 tes) |
| **RP-RTM 2.13 / SPEC-M13** — BAB 2 Masalah 13 / TODOS #2 Bug 13 (nav/action split) | `src/mindhive/dashboard_page.py`, `src/mindhive/dashboard_css.py`, `src/mindhive/dashboard_js_core.py` | `tests/test_dashboard_nav_split.py` (23/23 lulus), `tests/test_dashboard_browser.py` (tidak dijalankan) | LULUS (23 tes; 1 berkas tidak dijalankan) |
| **RP-RTM 2.14 / SPEC-M14** — BAB 2 Masalah 14 Bug 14 (license) | `LICENSE`, `CONTRIBUTING.md`, `CHANGELOG.md`, `pyproject.toml`, `README.md` | `tests/test_license.py` (35/35 lulus) | LULUS (35 tes) |
| **RP-RTM 2.15 / SPEC-M15 / SPEC-11.5** — BAB 2 Masalah 15 / BAB 11.5 Bug 15 (A/B unproven) | `src/mindhive/mindnet/ab.py` | `tests/test_mindnet_ab.py` (12/12 lulus) | LULUS (12 tes) |
| **RP-RTM 2.16 / SPEC-M16 / ORCH-04** — BAB 2 Masalah 16 / 10.14.4 Bug 16 (orphan processes) / MEB ORCH-04 | `src/mindhive/orchestrator/driver.py`, `src/mindhive/verify.py`, `src/mindhive/orchestrator/fence.py` | `tests/test_orchestrator_driver.py` (35/35 lulus), `tests/test_verify.py` (25/25 lulus), `tests/test_orchestrator_fence.py` (34/34 lulus) | LULUS (94 tes) |
| **RP-RTM 3.1–3.6 / SPEC-3.1 / SPEC-3.2 / SPEC-3.3 / SPEC-3.4 / SPEC-3.6** — BAB 3.1-3.6 Model matematis | `src/mindhive/models.py`, `src/mindhive/core/models.py`, `src/mindhive/orchestrator/resilience.py`, `src/mindhive/rotation/hook.py` | `tests/test_models.py` (129/129 lulus), `tests/test_orchestrator_breaker.py` (54/54 lulus), `tests/test_tier2_stability.py` (43/43 lulus), `tests/test_rotation_hook.py` (45/45 lulus) | LULUS (271 tes) |
| **§2.1** — MEB §2.1 Poisson / MTBF (BAB 3.1) | `src/mindhive/models.py`, `src/mindhive/core/models.py` | `tests/test_models.py` (129/129 lulus) | LULUS (129 tes) |
| **K1** — MEB §2.2 / K1 DTMC breaker (BAB 3.2) | `src/mindhive/models.py`, `src/mindhive/orchestrator/breaker.py` | `tests/test_models.py` (129/129 lulus), `tests/test_orchestrator_breaker.py` (54/54 lulus) | LULUS (183 tes) |
| **§2.4** — MEB §2.4 Spearman calibration (BAB 3.3) | `src/mindhive/models.py` | `tests/test_models.py` (129/129 lulus) | LULUS (129 tes) |
| **§2.5 / K5** — MEB §2.5 / K5 context budget (BAB 3.4) | `src/mindhive/models.py`, `src/mindhive/worksessions.py`, `src/mindhive/worksession_usage.py` | `tests/test_models.py` (129/129 lulus), `tests/test_worksession_usage.py` (16/16 lulus) | LULUS (145 tes) |
| **§2.6** — MEB §2.6 risk matrix (BAB 3.6) | `src/mindhive/models.py` | `tests/test_models.py` (129/129 lulus) | LULUS (129 tes) |
| **§7.3** — MEB §7.3 birthday bound SHA-256 (BAB 4.2) | `src/mindhive/models.py` | `tests/test_models.py` (129/129 lulus) | LULUS (129 tes) |
| **RP-RTM 4.1 / SPEC-4.1** — BAB 4.1 Filosofi CQRS/ES (vector store as disposable projection) | `docs/DATA_MODEL.md`, `README.md`, `src/mindhive/vectors.py`, `src/mindhive/commands/maintenance.py` | `tests/test_vectors.py` (87/87 lulus), `tests/test_vectors_v1.py` (83/83 lulus) | LULUS (170 tes) |
| **RP-RTM 4.2.1–4.2.3 / SPEC-4.2.1 / SPEC-4.2.2 / SPEC-4.2.3 / K6** — BAB 4.2.1-4.2.3 Rantai hash / MEB K6 | `src/mindhive/events.py`, `src/mindhive/core/models.py`, `src/mindhive/core/__init__.py`, `src/mindhive/chain.py` | `tests/test_events_chain_helpers.py` (27/27 lulus), `tests/test_events_append_race.py` (5/5 lulus), `tests/test_events_canonical.py` (13/13 lulus), `tests/test_core_package.py` (11/11 lulus) | LULUS (56 tes) |
| **SEC-02 / K9 / K24** — MEB SEC-02 / K9 / K24 (BAB 4.2.1) | `src/mindhive/events.py` | `tests/test_events_canonical.py` (13/13 lulus), `tests/test_pg_numeric_parity.py` (11/11 lulus), `tests/test_pg_live.py` (tidak dijalankan) | LULUS (24 tes; 1 berkas tidak dijalankan) |
| **K23** — MEB K23 events.py cap | `src/mindhive/contracts.py`, `src/mindhive/events.py` | `tests/test_contracts_reexport.py` (5/5 lulus), `tests/test_file_size_cap.py` (22/22 lulus) | LULUS (27 tes) |
| **RP-RTM 4.3.1–4.3.3 / SPEC-4.3.1 / SPEC-4.3.2 / SPEC-4.3.3** — BAB 4.3.1-4.3.3 Encoder & dokumen embedding | `src/mindhive/vectors.py` | `tests/test_vectors_v1.py` (83/83 lulus), `tests/test_vectors.py` (87/87 lulus) | LULUS (170 tes) |
| **§8 / K20** — MEB §8 / K20 HNSW (BAB 4.3.1) | `src/mindhive/vectors.py`, `src/mindhive/models.py`, `src/mindhive/storage/local_vector.py` | `tests/test_vectors_v1.py` (83/83 lulus), `tests/test_models.py` (129/129 lulus), `tests/test_storage_local_vector.py` (49/49 lulus) | LULUS (261 tes) |
| **RP-RTM 4.4.1–4.4.2 / SPEC-4.4.1 / SPEC-4.4.2 / SPEC-8.1 / K8** — BAB 4.4.1-4.4.2 / 8.1 Watcher & pending-embed marker / MEB K8 | `src/mindhive/watcher.py`, `src/mindhive/embedqueue.py`, `src/mindhive/events.py` | `tests/test_watcher.py` (56/56 lulus), `tests/test_event_types_v1.py` (49/49 lulus) | LULUS (105 tes) |
| **RP-RTM 4.5 / SPEC-4.5 / VEC-02** — BAB 4.5 Startup auto-recovery / MEB VEC-02 | `src/mindhive/watcher.py`, `src/mindhive/dashboard.py`, `src/mindhive/embedqueue.py`, `src/mindhive/commands/verification.py` | `tests/test_watcher.py` (56/56 lulus) | LULUS (56 tes) |
| **RP-RTM 8.3 / SPEC-8.3** — BAB 8.3 BackgroundEmbeddingWatcher | `src/mindhive/watcher.py`, `src/mindhive/commands/verification.py` | `tests/test_watcher.py` (56/56 lulus) | LULUS (56 tes) |
| **RP-RTM 8.2 / 10.9.1 / SPEC-8.2 / SPEC-Pilar5 / SPEC-1.5 / VEC-01** — BAB 1.5 / 8.2 / 10.9.1 Pilar 5 vectors & thread env lock / MEB VEC-01 | `src/mindhive/vectors.py`, `src/mindhive/commands/maintenance.py`, `src/mindhive/commands/parsers/maintenance.py`, `src/mindhive/storage/local_vector.py` | `tests/test_vectors_v1.py` (83/83 lulus), `tests/test_storage_local_vector.py` (49/49 lulus) | LULUS (132 tes) |
| **RP-RTM 4.6 / SPEC-4.6** — BAB 4.6 Progress bar vektor | `src/mindhive/jobs.py`, `src/mindhive/dashboard_js_actions.py`, `src/mindhive/dashboard_js_core.py`, `src/mindhive/dashboard.py`, `src/mindhive/dashboard_css.py` | `tests/test_jobs.py` (47/47 lulus), `tests/test_dashboard_theme.py` (18/18 lulus), `tests/test_watcher.py` (56/56 lulus) | LULUS (121 tes) |
| **RP-RTM 4.7 / SPEC-4.7 / SPEC-10.6** — BAB 4.7 / 10.6 Benchmark vektor | `src/mindhive/bench.py`, `src/mindhive/bench_scenarios.py` | `tests/test_bench_scenarios.py` (38/38 lulus), `tests/test_bench.py` (31/31 lulus) | LULUS (69 tes) |
| **K15** — MEB §1.5 / K15 CLI startup | `src/mindhive/bench_scenarios.py`, `src/mindhive/bench.py` | `tests/test_cli_startup.py` (4/4 lulus), `tests/test_bench_scenarios.py` (38/38 lulus), `tests/test_bench.py` (31/31 lulus) | LULUS (73 tes) |
| **RP-RTM 5.1–5.5 / 8.4 / SES-01 / SPEC-8.4 / SPEC-5.2** — BAB 5.1-5.5 / 8.4 Work Sessions / MEB SES-01 | `src/mindhive/worksessions.py`, `src/mindhive/worksession_migrate.py`, `src/mindhive/worksession_lock.py`, `src/mindhive/worksession_usage.py`, `src/mindhive/initiatives.py`, `src/mindhive/oversee.py`, `src/mindhive/dashboard_get_v1.py`, `src/mindhive/dashboard_js_oversee.py`, `src/mindhive/projector.py` | `tests/test_worksessions.py` (83/83 lulus), `tests/test_worksession_migrate.py` (16/16 lulus), `tests/test_oversee.py` (53/53 lulus), `tests/test_worksessions_node.py` (tidak dijalankan), `tests/test_initiatives.py` (25/25 lulus), `tests/test_worksession_usage.py` (16/16 lulus), `tests/test_dashboard_get_v1.py` (16/16 lulus), `tests/test_worksession_pagination.py` (21/21 lulus) | LULUS (230 tes; 1 berkas tidak dijalankan) |
| **SPEC-5.3.1** — BAB 5 Work Sessions | `src/mindhive/worksessions.py` | `tests/test_worksessions.py` (83/83 lulus) | LULUS (83 tes) |
| **SPEC-5.3.2** — BAB 5 Work Sessions | `src/mindhive/worksessions.py`, `src/mindhive/initiatives.py` | `tests/test_worksessions.py` (83/83 lulus), `tests/test_initiatives.py` (25/25 lulus) | LULUS (108 tes) |
| **SPEC-5.3.3** — BAB 5 Work Sessions | `src/mindhive/worksessions.py`, `src/mindhive/worksession_usage.py` | `tests/test_worksessions.py` (83/83 lulus), `tests/test_worksession_usage.py` (16/16 lulus) | LULUS (99 tes) |
| **SPEC-5.3.4** — BAB 5 Work Sessions | `src/mindhive/worksessions.py` | `tests/test_worksessions.py` (83/83 lulus) | LULUS (83 tes) |
| **SPEC-5.4** — BAB 5 Work Sessions | `src/mindhive/worksessions.py`, `src/mindhive/worksession_lock.py` | `tests/test_worksessions.py` (83/83 lulus) | LULUS (83 tes) |
| **SPEC-5.5** — BAB 5 Work Sessions | `src/mindhive/dashboard_js_oversee.py`, `src/mindhive/dashboard_css.py`, `src/mindhive/dashboard_get_v1.py`, `src/mindhive/worksession_migrate.py` | `tests/test_worksessions_node.py` (tidak dijalankan), `tests/test_dashboard_get_v1.py` (16/16 lulus), `tests/test_worksession_migrate.py` (16/16 lulus), `tests/test_worksession_pagination.py` (21/21 lulus) | LULUS (53 tes; 1 berkas tidak dijalankan) |
| **RP-RTM 6.1–6.5 / 8.5 / SPEC-8.5** — BAB 6.1-6.5 / 8.5 Adaptive tail | `src/mindhive/adaptivetail.py`, `src/mindhive/sync.py`, `src/mindhive/projector.py`, `src/mindhive/context.py`, `src/mindhive/core/dreaming.py`, `src/mindhive/vectors.py` | `tests/test_adaptivetail.py` (68/68 lulus), `tests/test_context.py` (103/103 lulus), `tests/test_sync.py` (31/31 lulus), `tests/test_sop_digest.py` (11/11 lulus), `tests/test_core_dreaming.py` (15/15 lulus) | LULUS (228 tes) |
| **CTX-01 / K16 / SPEC-6.2 / SPEC-6.4** — MEB CTX-01 / K16 / BAB 6.2 / 6.4 | `src/mindhive/adaptivetail.py`, `src/mindhive/projector.py`, `src/mindhive/context.py` | `tests/test_adaptivetail.py` (68/68 lulus), `tests/test_sync.py` (31/31 lulus), `tests/test_context.py` (103/103 lulus) | LULUS (202 tes) |
| **SPEC-6.1** — BAB 6 Adaptive tail | `src/mindhive/adaptivetail.py`, `src/mindhive/context.py` | `tests/test_adaptivetail.py` (68/68 lulus), `tests/test_context.py` (103/103 lulus) | LULUS (171 tes) |
| **SPEC-6.3** — BAB 6 Adaptive tail | `src/mindhive/adaptivetail.py` | `tests/test_adaptivetail.py` (68/68 lulus) | LULUS (68 tes) |
| **CTX-02 / SPEC-6.5** — MEB CTX-02 / BAB 6.5 Dreaming | `src/mindhive/core/dreaming.py`, `src/mindhive/adaptivetail.py`, `src/mindhive/consolidate.py` | `tests/test_core_dreaming.py` (15/15 lulus), `tests/test_adaptivetail.py` (68/68 lulus), `tests/test_consolidate.py` (11/11 lulus) | LULUS (94 tes) |
| **RP-RTM 7.1–7.6 / 8.6 / SPEC-8.6** — BAB 7.1-7.6 / 8.6 Interceptor & integration breaker | `src/mindhive/orchestrator/interceptor.py`, `src/mindhive/orchestrator/breaker.py`, `src/mindhive/orchestrator/gateway.py`, `src/mindhive/orchestrator/quota.py`, `src/mindhive/repo.py`, `src/mindhive/mindnet/worktree.py`, `src/mindhive/orchestrator/integrate.py`, `src/mindhive/commands/ledger.py` | `tests/test_orchestrator_interceptor.py` (101/101 lulus), `tests/test_orchestrator_breaker.py` (54/54 lulus), `tests/test_repo.py` (42/42 lulus), `tests/test_mindnet_worktree.py` (89/89 lulus), `tests/test_orchestrator_quota.py` (25/25 lulus) | LULUS (311 tes) |
| **ORCH-02 / SPEC-7.1** — MEB ORCH-02 / BAB 7.1 | `src/mindhive/orchestrator/interceptor.py` | `tests/test_orchestrator_interceptor.py` (101/101 lulus) | LULUS (101 tes) |
| **SPEC-7.2** — BAB 7 Gateway interceptor | `src/mindhive/orchestrator/interceptor.py` | `tests/test_orchestrator_interceptor.py` (101/101 lulus) | LULUS (101 tes) |
| **SPEC-7.3** — BAB 7 Gateway interceptor | `src/mindhive/orchestrator/interceptor.py`, `src/mindhive/orchestrator/gateway.py`, `src/mindhive/repo.py` | `tests/test_orchestrator_interceptor.py` (101/101 lulus) | LULUS (101 tes) |
| **SPEC-7.4** — BAB 7 Gateway interceptor | `src/mindhive/orchestrator/interceptor.py` | `tests/test_orchestrator_interceptor.py` (101/101 lulus) | LULUS (101 tes) |
| **SPEC-7.5** — BAB 7 Gateway interceptor | `src/mindhive/orchestrator/worktree.py`, `src/mindhive/mindnet/worktree.py` | `tests/test_orchestrator_worktree.py` (48/49 lulus, 1 dilewati) | LULUS (48 tes, 1 dilewati) |
| **ORCH-03 / K21 / SPEC-7.6** — MEB ORCH-03 / K21 / BAB 7.6 | `src/mindhive/orchestrator/breaker.py`, `src/mindhive/orchestrator/quota.py`, `src/mindhive/orchestrator/integrate.py`, `src/mindhive/core/models.py`, `src/mindhive/orchestrator/cli.py` | `tests/test_orchestrator_breaker.py` (54/54 lulus), `tests/test_orchestrator_quota.py` (25/25 lulus), `tests/test_orchestrator_integrate.py` (25/25 lulus) | LULUS (104 tes) |
| **RP-RTM 9.1 / SPEC-9.1-F1 / SPEC-9.1-F2 / SPEC-9.1-F3 / SPEC-9.1-F4** — BAB 9.1 Migrasi 4 fase (F1-F4) | `src/mindhive/events.py`, `src/mindhive/tasktool.py`, `src/mindhive/bench_scenarios.py`, `src/mindhive/vectors.py`, `src/mindhive/worksession_migrate.py`, `src/mindhive/adaptivetail.py`, `src/mindhive/orchestrator/integrate.py`, `src/mindhive/orchestrator/breaker.py` | `tests/test_events_append_race.py` (5/5 lulus), `tests/test_tasktool_lock.py` (15/15 lulus), `tests/test_bench_scenarios.py` (38/38 lulus), `tests/test_worksession_migrate.py` (16/16 lulus), `tests/test_adaptivetail.py` (68/68 lulus), `tests/test_orchestrator_integrate.py` (25/25 lulus), `tests/test_orchestrator_breaker.py` (54/54 lulus) | LULUS (221 tes) |
| **RP-RTM 9.2 / SPEC-9.2** — BAB 9.2 Suite TDD wajib | — | `tests/test_events_append_race.py` (5/5 lulus), `tests/test_tasktool_lock.py` (15/15 lulus), `tests/test_tasktool.py` (58/58 lulus), `tests/test_vectors_v1.py` (83/83 lulus), `tests/test_vectors.py` (87/87 lulus), `tests/test_sop_digest.py` (11/11 lulus), `tests/test_sopgate.py` (63/63 lulus), `tests/test_orchestrator_gate.py` (78/78 lulus), `tests/test_hook_shims.py` (350/350 lulus) | LULUS (750 tes) |
| **RP-RTM 9.3 / SPEC-9.3** — BAB 9.3 SOP Lima Aturan Emas | `docs/SOP_FIVE_GOLDEN_RULES.md`, `src/mindhive/changelog.py`, `docs/SOP_FIVE_GOLDEN_RULES_COMPACT.md` | `tests/test_docs_parity.py` (11/11 lulus), `tests/test_changelog.py` (66/66 lulus), `tests/test_sop_golden_rules_doc.py` (4/4 lulus) | LULUS (81 tes) |
| **RP-RTM 9.4 / SPEC-9.4a / SPEC-9.4b / SPEC-9.4c** — BAB 9.4 Runbook kontinjensi | `docs/RUNBOOK_RECOVERY.md`, `README.md`, `src/mindhive/commands/verification.py`, `src/mindhive/commands/parsers/maintenance.py`, `src/mindhive/tasktool.py` | `tests/test_commands_base.py` (29/29 lulus), `tests/test_vectors_v1.py` (83/83 lulus), `tests/test_tasktool_lock.py` (15/15 lulus) | LULUS (127 tes) |
| **RP-RTM 10.1–10.3, 10.6, 10.10, 10.15, 11.4 / §9** — BAB 10.1-10.3 / 10.6 / 10.10 / 10.15 / 11.4 IMPLEMENTATION_PLAN + MEB §9 ISO 25010 | `src/mindhive/bench.py`, `src/mindhive/bench_scenarios.py`, `IMPLEMENTATION_PLAN.md`, `scripts/loc_report.py` | `tests/test_implementation_plan.py` | LULUS (7 tes; `tests/test_implementation_plan.py` dijalankan sendiri setelah dokumen dibangun, di luar junit — 7 passed in 0.13s) |
| **RP-RTM 10.4 / 10.5.1–10.5.4 / 10.11 L1–L2 / SPEC-Pilar1 / SPEC-Pilar4 / SPEC-10.5.2 / SPEC-10.5.4** — BAB 10.4 / 10.5.1-10.5.4 / 10.11 L1-L2 Pilar 1 & 4 | `src/mindhive/dashboard_memo.py`, `src/mindhive/static/dashboard.js`, `src/mindhive/dashboard_get_v1.py`, `src/mindhive/dashboard_page.py`, `pyproject.toml`, `src/mindhive/dashboard_js_actions.py`, `src/mindhive/dashboard_js_core.py`, `src/mindhive/dashboard.py` | `tests/test_dashboard_memo_public.py` (21/21 lulus), `tests/test_static_dashboard_js.py` (20/20 lulus), `tests/test_paint_guard_node.py` (tidak dijalankan), `tests/test_dashboard_hivemind_gate.py` (18/18 lulus), `tests/test_dashboard_locks.py` (95/95 lulus), `tests/test_dashboard_overview_memo.py` (9/9 lulus) | LULUS (163 tes; 1 berkas tidak dijalankan) |
| **RP-RTM 10.8 / SPEC-Pilar3** — BAB 10.8 Pilar 3 paginasi | `src/mindhive/pgshape.py`, `src/mindhive/pgtools.py`, `src/mindhive/dashboard_query.py`, `src/mindhive/dashboard_get.py` | `tests/test_pgshape_pagination.py` (44/44 lulus), `tests/test_dashboard_pagination.py` (56/56 lulus), `tests/test_worksession_pagination.py` (21/21 lulus) | LULUS (121 tes) |
| **RP-RTM 10.9.2 / SPEC-10.9.2** — BAB 10.9.2 Preflight | `src/mindhive/orchestrator/environment.py`, `src/mindhive/orchestrator/cli.py` | `tests/test_orchestrator_environment.py` (65/65 lulus), `tests/test_cli_lintester.py` (63/63 lulus) | LULUS (128 tes) |
| **SPEC-10.11** — BAB 10 Runbook L1-L6 | `src/mindhive/dashboard_memo.py`, `src/mindhive/tasktool.py`, `src/mindhive/pgshape.py`, `src/mindhive/static/dashboard.js`, `src/mindhive/vectors.py`, `src/mindhive/orchestrator/environment.py` | `tests/test_dashboard_memo_public.py` (21/21 lulus), `tests/test_tasktool_lock.py` (15/15 lulus), `tests/test_pgshape_pagination.py` (44/44 lulus), `tests/test_static_dashboard_js.py` (20/20 lulus), `tests/test_vectors_v1.py` (83/83 lulus), `tests/test_orchestrator_environment.py` (65/65 lulus) | LULUS (248 tes) |
| **RP-RTM 10.12 / SPEC-10.12** — BAB 10.12 Triage 3-tier | — | — | DEVIASI (urutan tier 1 → 2 → 3 tidak diikuti: P9 Tier 3 mendarat di commit pertama bersama P0 — §11 butir 32) |
| **RP-RTM 10.13.6 / SPEC-10.13.6 / TDD-01** — BAB 10.13.6 Tier-1 invariants / MEB TDD-01 | `tests/test_tier1_invariants.py`, `src/mindhive/events.py`, `src/mindhive/advice.py`, `src/mindhive/orchestrator/gate.py` | `tests/test_tier1_invariants.py` (83/83 lulus), `tests/test_events_chain_helpers.py` (27/27 lulus), `tests/test_events_canonical.py` (13/13 lulus) | LULUS (123 tes) |
| **RP-RTM 10.14.5 / SPEC-10.14.5 / TDD-02** — BAB 10.14.5 Tier-2 stability / MEB TDD-02 | `tests/test_tier2_stability.py`, `src/mindhive/orchestrator/payoff.py`, `src/mindhive/orchestrator/resilience.py`, `src/mindhive/recommend.py`, `src/mindhive/plugins.py`, `src/mindhive/plugin_cache.py` | `tests/test_tier2_stability.py` (43/43 lulus), `tests/test_plugins_version_hash.py` (14/14 lulus), `tests/test_plugin_cache.py` (36/36 lulus) | LULUS (93 tes) |
| **SPEC-10.15** — BAB 10 Laporan gabungan | — | `tests/test_tier1_invariants.py` (83/83 lulus), `tests/test_tier2_stability.py` (43/43 lulus), `tests/test_orchestrator_driver.py` (35/35 lulus), `tests/test_hook_shims.py` (350/350 lulus), `tests/test_tasktool_lock.py` (15/15 lulus) | LULUS (526 tes) |
| **RP-RTM 11.1–11.3 / CI-01 / CI-02 / SPEC-11.2.1a / SPEC-11.2.1b / SPEC-11.2.1c / SPEC-11.3.1 / SPEC-11.3.2** — BAB 11.1-11.3 CI mandiri / MEB CI-01 / CI-02 | `src/mindhive/ci/runner.py`, `src/mindhive/ci/cli.py`, `src/mindhive/ci/__init__.py`, `src/mindhive/oversee.py`, `src/mindhive/dashboard_js_oversee.py`, `.github/workflows/ci.yml` | `tests/test_ci_runner.py` (51/51 lulus), `tests/test_ci_cli.py` (35/35 lulus), `tests/test_oversee.py` (53/53 lulus) | LULUS (139 tes) |
| **SPEC-11.2.1d** — BAB 11 Native CI | `src/mindhive/orchestrator/integrate.py` | — | DEVIASI (`src/mindhive/orchestrator/integrate.py` tidak memicu runner CI native — §11 butir 9) |
| **RP-RTM 11.5** — BAB 11.5 Langkah lanjut | `IMPLEMENTATION_PLAN.md` | — | TANPA TES |
| **RP-RTM TODOS / SPEC-11.4** — TODOS status / BAB 11.4 residual risks | `TODOS.md` | — | MANUAL (`TODOS.md` direkonsiliasi: #1 #2 #5 #7 #15 #30 #31 DONE, #32 diberi catatan, #14 terbuka — §11 butir 31) |
| **STO-01** — MEB STO-01 Storage SPI | `src/mindhive/storage/interface.py`, `src/mindhive/storage/postgres_driver.py`, `src/mindhive/storage/pgvector_driver.py` | `tests/test_storage_interface.py` (20/20 lulus), `tests/test_storage_postgres_driver.py` (40/40 lulus) | LULUS (60 tes) |
| **STO-02** — MEB §4.2 #9 Local JSONL driver + fsync | `src/mindhive/storage/local_driver.py`, `src/mindhive/events.py`, `src/mindhive/storage/records.py` | `tests/test_storage_local_driver.py` (39/39 lulus) | LULUS (39 tes) |
| **MIG-01 / K18** — MEB MIG-01 / K18 | `src/mindhive/storage/migration.py`, `src/mindhive/storage/records.py`, `src/mindhive/storage/cli.py`, `src/mindhive/pgmirror.py`, `src/mindhive/pgmigrations/007_event_embeddings_hashes.sql`, `pyproject.toml`, `README.md` | `tests/test_storage_migration.py` (33/33 lulus), `tests/test_storage_records.py` (20/20 lulus), `tests/test_storage_cli.py` (55/55 lulus), `tests/test_storage_pg_live.py` (tidak dijalankan) | LULUS (108 tes; 1 berkas tidak dijalankan) |
| **K19** — MEB pgvector_driver (K19) | `src/mindhive/storage/pgvector_driver.py`, `src/mindhive/pgmirror.py` | `tests/test_storage_pgvector_driver.py` (37/37 lulus), `tests/test_storage_pg_live.py` (tidak dijalankan) | LULUS (37 tes; 1 berkas tidak dijalankan) |
| **K25** — MEB postgres_driver vs repo gate (K25) | `src/mindhive/pgmirror.py`, `src/mindhive/storage/postgres_driver.py` | `tests/test_pg_repo_gates.py` (22/22 lulus), `tests/test_pgmirror.py` (25/25 lulus), `tests/test_storage_postgres_driver.py` (40/40 lulus), `tests/test_storage_migration.py` (33/33 lulus), `tests/test_storage_cli.py` (55/55 lulus) | LULUS (175 tes) |
| **K22** — MEB K22 commit policy | — | — | MANUAL (git log: 27 commit `feat(blueprint)`/`fix(vectors)` di `claude/blueprint-foundation-v1`, tanpa ref remote — §11 butir 25) |
| **§1.4 / K11** — MEB §1.4 / K11 UI efemeral | `src/mindhive/server/lifecycle.py`, `src/mindhive/server/routes.py`, `src/mindhive/server/js.py`, `src/mindhive/server/app.py`, `src/mindhive/server/asgi.py`, `src/mindhive/commands/dashboard.py`, `src/mindhive/commands/parsers/dashboard.py`, `src/mindhive/dashboard.py`, `src/mindhive/dashboard_page.py`, `pyproject.toml` | `tests/test_server_lifecycle.py` (69/69 lulus), `tests/test_server_routes.py` (21/21 lulus), `tests/test_server_app.py` (14/14 lulus), `tests/test_server_cli.py` (36/36 lulus), `tests/test_server_asgi.py` (21/23 lulus, 2 dilewati), `tests/test_pyproject_extras.py` (19/19 lulus) | LULUS (180 tes, 2 dilewati) |
| **K14** — MEB risiko #7 PYTHONDONTWRITEBYTECODE (K14) | `src/mindhive/paths.py`, `src/mindhive/selftest.py`, `src/mindhive/orchestrator/driver.py`, `src/mindhive/ci/runner.py` | `tests/test_no_bytecode_env.py` (9/9 lulus), `tests/test_ci_runner.py` (51/51 lulus), `tests/test_orchestrator_driver.py` (35/35 lulus) | LULUS (95 tes) |
| **§3** — MEB §3 Prakiraan LOC | `scripts/loc_report.py` | `tests/test_loc_targets.py` (31/31 lulus), `tests/test_file_size_cap.py` (22/22 lulus) | LULUS (53 tes) |
| **§10.1** — MEB §10.1 Registri konstanta | `src/mindhive/tasktool.py`, `src/mindhive/adaptivetail.py`, `src/mindhive/core/dreaming.py`, `src/mindhive/orchestrator/metrics.py`, `src/mindhive/orchestrator/payoff.py`, `src/mindhive/events.py` | `tests/test_constants_registry.py` (18/18 lulus), `tests/test_env_registry.py` (11/11 lulus) | LULUS (29 tes) |

## 6. Laporan validasi gabungan (PDF §10.15, §9.2)

- **Suite penuh pada commit ini** (junit): 11176 kasus uji dikumpulkan ke junit: 11170 lulus, 0 gagal, 0 error, 6 dilewati, 1373 s.
- **Ringkasan status tabel audit**: DEVIASI: 3, LULUS: 91, MANUAL: 2, TANPA TES: 1.
- **Suite penuh pada pohon kontainmen** (sebelum commit `0fd14b2`,
  dijalankan di worktree sekali pakai pada `6d4af2c` + diff kontainmen):
  11130 lulus, 6 dilewati, 360 dideselect, 2 gagal (`tests/test_advice_authorship.py` — registri spawn-site belum memuat `vector_reader.py`; diklasifikasikan sebagai TOOL spawner satu situs tanpa scrub, berkas diulang 175/175 lulus), 1441 s (24:01).
- **Suite TDD wajib PDF §9.2** — semua ada dan dijalankan di suite di atas:
  `tests/test_events_append_race.py` (4 proses × 40 = 160 baris, nol hilang),
  `tests/test_tasktool_lock.py` (12 thread × 25 = 300 record),
  `tests/test_vectors_v1.py` (idempoten per namespace),
  `tests/test_sop_digest.py` dan `tests/test_sopgate.py` (≤ 24 500 B),
  `tests/test_orchestrator_gate.py` (fail-closed), `tests/test_hook_shims.py` +
  `tests/test_hook_shims_ps.py` (sembilan pasang shim, exit 0).
- **Kriteria kelulusan PDF §9.1**: F1 — tes literal "100 putaran × 12 proses"
  tidak ada; yang ada 4 proses × 40 baris (events) dan 12 thread × 25 record
  (tasktool), keduanya nol kehilangan. F2 — paritas replay: `hive db migrate
  --dry-run` memverifikasi rantai hash saat replay (`tests/test_storage_migration.py`);
  *recall semantik < 85 ms*: 10.576 ms median hangat, 12.69 ms maks (< 85 ms; klaim, bukan gerbang). F3 — `MEMORY.md` ≤ 24 500 B
  (`tests/test_adaptivetail.py`, `tests/test_context.py`). F4 — interceptor +
  breaker menjaga cabang (`tests/test_orchestrator_integrate.py`).
- **Tier `pg`** (`tests/test_storage_pg_live.py`, `tests/test_pg_live.py`, …)
  tidak diseleksi: tidak ada server Postgres di mesin ini. Cabang `vector(dim)`
  pgvector (K19) hanya tercakup fake (`tests/test_storage_pgvector_driver.py`).
- **Tier `node` dan `browser`** juga tidak diseleksi oleh `addopts` (bersama
  `pg`: 360 kasus uji dideselect menurut ringkasan pytest); tes JS dasbor dapat
  dijalankan dengan `pytest -m node` — tidak dijalankan untuk dokumen ini.
- **Tes yang diketahui rapuh**: `tests/test_orchestrator_driver.py`
  (batas 8 s pada tree-kill cucu yatim) pernah gagal sekali (9.04 s) saat
  pengukuran lain berjalan bersamaan, lulus 4.24 s sendirian; dicatat, tidak
  dilonggarkan.

## 7. Benchmark (PDF §4.7, §10.3, §10.6; MEB K15) — direproduksi, bukan disalin

Semua bench dijalankan pada commit ini dengan `PYTHONPATH=src` dan, untuk yang
membaca store, `MINDHIVE_HOME` menunjuk ke **salinan** store nyata di scratch
(3 434 berkas; indeks chroma 9.76 MB, 654 event terindeks). Store nyata di
`~/.mindhive` tidak pernah dibuka oleh chromadb atau sqlite untuk pengukuran.

### 7.1 `hive bench cli-startup` (MEB K15)

Kondisi: 3.14.3 pada `win32`, 10 rep per skenario, `PYTHONPATH` = `C:/Users/hanif/mindhive/.claude/worktrees/blueprint-v1/src`, `PYTHONDONTWRITEBYTECODE` = `1`; klaim MEB 45.0 ms (MEB section 1.5 "42 ms" (K15 measures against < 45 ms) -- printed for comparison, not a gate).

| Skenario | Median | Min | Maks | Gagal | Klaim MEB |
|---|---:|---:|---:|---:|---:|
| `import` | 168.9 ms | 162.3 ms | 174.1 ms | 0 | 45.0 ms |
| `help` | 348.9 ms | 338.1 ms | 481.5 ms | 0 | 45.0 ms |

### 7.2 `hive bench recall-semantic` (PDF §4.7)

Kondisi: 7 rep × 3 kueri (default (one query per memo kind)), limit 10, ledger 1400 berkas / 2.20 MB, indeks 654 event (9.76 MB disalin dalam 46.9 ms), model `all-MiniLM-L6-v2`. Klaim spec §4.7: pemindaian penuh 1090 ms vs vektor 74 ms (rasio 14.7) atas 22835 event / 12.54 MB.

| Jalur | Cold | Median hangat | Min | Maks | ok | Catatan |
|---|---:|---:|---:|---:|---:|---|
| `hive recall` (leksikal, LEDGER/*.md) | 565.9 ms | 198.933 ms | 184.887 ms | 218.752 ms | 21/21 | pemindaian penuh |
| `hive recall --semantic`, baca in-process (sebelum kontainmen) | 158.7 ms | 9.787 ms | 9.229 ms | 13.282 ms | 21/21 | open 10537 ms (muat model + chroma) |
| `hive recall --semantic`, baca lewat proses pembaca (`ISOLATE_READS = True`) | 243.2 ms | 10.576 ms | 9.606 ms | 12.688 ms | 21/21 | open 11925 ms; rasio leksikal/semantik 18.81× |

Biaya isolasi pada median hangat: +0.789 ms (9.787 → 10.576 ms), di dalam sebaran antar-run (min–maks 9.61–12.69 ms). Rasio leksikal/semantik 29.34× → 18.81×; klaim spec 14.7× direproduksi dalam orde besaran yang sama.

### 7.3 `hive bench dashboard` (PDF §10.5.4: render < 15 ms, hit ratio ≥ 98 %)

Kondisi: `preview 54471 on a scratch copy of the store, idle machine`, url `http://127.0.0.1:54471`, proyek `mindhive`, 5 sampel hangat per endpoint setelah satu sampel dingin, timeout 30.0 s.

| Endpoint | Cold/miss | Warm/hit (rata-rata) | hits | state |
|---|---:|---:|---:|---|
| `/api/projects` | 31.6 ms | 16.4 ms | 5/5 | ok |
| `/api/overview` | 3643.2 ms | 654.2 ms | 5/5 | ok |
| `/api/oversee` | 1458.1 ms | 14.0 ms | 5/5 | ok |
| `/api/mindnet/cats` | 1261.4 ms | 25.9 ms | 5/5 | ok |

Serialisasi: serial 1091.1 ms vs konkuren 30.6 ms, rasio 35.66× (≈ 1.0 berarti server menjawab satu per satu).

### 7.4 Yang tidak diukur

Token per sesi (PDF §4.7, §10.6) tidak diukur oleh bench mana pun; tidak ada
angka yang dicatat. RAM diukur sekali, hanya untuk extra `vectors`
(`K32GetProcessMemoryInfo`, Windows 11, 2026-09-29): working set 14 MiB
interpreter kosong → 74 MiB setelah `import chromadb` → 433 MiB setelah
`sentence_transformers` → 455 MiB dengan `all-MiniLM-L6-v2` dimuat → 500 MiB
puncak setelah satu query; RAM proses dasbor dan CLI tanpa extra tidak diukur.
Latensi di atas adalah klaim terukur, bukan gerbang: tidak ada tes yang gagal
bila angkanya berubah.

## 8. Prakiraan MEB vs Aktual (MEB §3)

Keluaran `python scripts/loc_report.py --root .` pada commit ini, verbatim
(baris, baseline dan target MEB disalin dari MEB §3.1 oleh
`scripts/loc_report.py`; `tests/test_loc_targets.py` memin salinannya):

| MEB row | MEB baseline | MEB target | Measured | vs target | Modules (MEB) | Verdict |
|---|---:|---:|---:|---:|---|---|
| src/mindhive/__main__.py | 4237 | < 90 | 60 | -30 | 1 | met |
| src/mindhive/lintester.py | 997 | 997 | 997 | +0 | 1 | met (the MEB's path is wrong: the module is orchestrator/lintester.py) |
| src/mindhive/replenish.py | 2781 | 2781 | 2781 | +0 | 1 | met |
| src/mindhive/workspace.py | 1462 | 1462 | 1462 | +0 | 1 | met |
| src/mindhive/research.py | 1353 | 1353 | 1349 | -4 | 1 | shrank |
| src/mindhive/mindnet/controller.py | 1016 | 1016 | 1016 | +0 | 1 | met |
| src/mindhive/events.py | 684 | 745 | 996 | +251 | 1 | over |
| src/mindhive/paths.py | 118 | 135 | 638 | +503 | 1 | over |
| src/mindhive/cliparse.py | 95 | 108 | 157 | +49 | 1 | over |
| src/mindhive/vectors.py | 380 | 475 | 931 | +456 | 1 | over |
| src/mindhive/tasktool.py | 462 | 498 | 579 | +81 | 1 | over |
| src/mindhive/recommend.py | 285 | 340 | 470 | +130 | 1 | over |
| Modul-Modul Baru (14 Modul) | 0 | 2885 | 6678 | +3793 | 20 (14) | over (the MEB names ten components for fourteen modules; ci/ and storage/ are counted whole) |
| Subpaket commands/ (8 Modul) | 0 | 2450 | 4145 | +1695 | 16 (8) | over; over 400: commands/orchestration.py 466 |
| Subpaket commands/parsers/ (6 Modul) | 0 | 1180 | 1757 | +577 | 7 (6) | over; over 250: commands/parsers/dashboard.py 275, commands/parsers/maintenance.py 295, commands/parsers/registry.py 394 |

MEB totals: stated 14771 -> 16126; its rows sum to 13870 -> 16515; measured over the same rows: 24016.

Bacaan:

- Prakiraan MEB meleset ke atas pada setiap baris yang berubah. Tiga penyebab
  yang terukur: (1) MEB menghitung modul baru sebagai *sepuluh komponen* untuk
  empat belas modul, sedangkan `ci/` dan `storage/` masing-masing menjadi paket
  (20 modul terukur); (2) setiap modul di repositori ini membawa docstring
  desain yang panjang (kebijakan proyek, dipin oleh `tests/test_license.py`
  dan `tests/test_file_size_cap.py`), yang tidak ada dalam prakiraan; (3)
  `events.py` menampung SEC-01/SEC-02, `fsync`, tipe `fabrication` dan
  re-export carve-out — 996 dari 1 000 baris, sisa ruang 4 baris.
- Yang mengikat adalah *hard ceiling*, bukan prakiraan: `python
  scripts/loc_report.py --check` exit 0 pada commit ini (`hard_breaches: []`),
  `__main__.py` 60 < 90, `orchestrator/lintester.py` tepat 997 (P7 menukar
  komentar path baris 1 dengan SPDX, net 0 — deviasi §11 butir 23).
- `commands/orchestration.py` (466) melampaui batas per-modul MEB 400 tetapi di
  bawah cap repositori 500 (`tests/test_commands_reexport.py`); tiga parser
  melampaui 250 (dashboard 275, maintenance 295, registry 394) — semuanya di
  bawah 500.

## 9. Matriks ISO/IEC 25010 terukur (MEB §9)

Setiap baris menyebut sumber angkanya. "Terukur" berarti dari keluaran
perintah pada commit ini; "dipin" berarti tes memaksa nilainya.

| Karakteristik ISO/IEC 25010 | Ukuran | Nilai terukur pada commit ini | Sumber |
|---|---|---|---|
| Kesesuaian fungsional | Cakupan butir cetak biru oleh tes | tabel audit §5: lihat §6 | junit suite penuh |
| Efisiensi kinerja | Startup CLI (`import mindhive.__main__`) | 168.9 ms median (klaim MEB 45 ms) | `hive bench cli-startup` |
| Efisiensi kinerja | Startup CLI (`python -m mindhive --help`) | 348.9 ms median | `hive bench cli-startup` |
| Efisiensi kinerja | `hive recall --semantic` median hangat | 10.576 ms (leksikal 198.9 ms; rasio 18.81×; klaim spec 74 ms / 14.7×) | `hive bench recall-semantic` |
| Efisiensi kinerja | Dasbor: warm/hit terlambat di antara endpoint default | 654.2 ms (target spec 10.5.4: < 15 ms render) | `hive bench dashboard` |
| Keandalan | Ketersediaan lane, DTMC pemutus (MEB K1) | dihitung dari parameter yang dinyatakan: 95.39 % (baseline 44.27 %); vektor π yang dicetak MEB: 98.03 %; reduksi downtime 91.72 % (MEB mencetak 91.75 %) | `models.integration_breaker_stationary`, dipin `tests/test_models.py` |
| Keandalan | MTBF aksi (λ₁ = 0.008) dan R(100) | 125 aksi; R(100) = 44.93 % | `models.poisson_reliability` |
| Keandalan | Kegagalan proses pembaca chroma | segfault/hang → `SemanticQueryError`; 9 percobaan tanpa proses induk mati (DATA_MODEL §4.19) | `tests/test_vector_snapshot.py`, `tests/test_vector_reader.py` |
| Kegunaan | Kontras WCAG token teks (terburuk) | 4.12:1 (`dimmest` di atas `raised`); semua token teks ≥ 4.5: tidak — `dimmest` (teks paling redup) sengaja di bawah AA untuk teks normal, ≥ 3.0 untuk non-teks | `theme.contrast_ratio`, `tests/test_theme_contrast.py` |
| Keamanan | Traversal session id, injeksi SOP, gerbang fail-closed | dipin Tier-1 (42 tes) — lihat §5 baris SEC-01/SEC-02/ORCH-01 | `tests/test_tier1_invariants.py`, `tests/test_events_sec.py` |
| Kemampuan pemeliharaan | Modul `src/mindhive`; terbesar; > 1000; > 500 | 237 modul; terbesar `replenish.py` 2781; > 1000: 4 (allowlist shrink-only); > 500: 67 | `scripts/loc_report.py`, `tests/test_file_size_cap.py` |
| Kemampuan pemeliharaan | `__main__.py` / `orchestrator/lintester.py` | 60 baris (< 90) / 997 baris (= 997, zero-edit) | `tests/test_commands_reexport.py`, `scripts/loc_report.py --check` |
| Kemampuan pemeliharaan | Modul baru MEB: rata-rata dan > 500 | 6678 baris / 20 modul = 333.9 rata-rata; > 500: models.py, watcher.py | `scripts/loc_report.py` |
| Kemampuan pemeliharaan | Siklus impor level modul | tidak ada | `iso_measure.py` (Tarjan atas impor level modul) |
| Kemampuan pemeliharaan | Pelanggaran hard ceiling | tidak ada | `scripts/loc_report.py --check` |
| Portabilitas | OS dan Python yang diuji CI | ubuntu-latest, windows-latest × 3.11, 3.12, 3.14 (`.github/workflows/ci.yml`); suite ini dijalankan di Windows 11 / Python 3.14.3 | `.github/workflows/ci.yml` |
| Kompatibilitas | Ekstensi pgvector `vector(dim)` | tidak dapat diverifikasi di mesin ini (tanpa server Postgres); hanya fake | `tests/test_storage_pgvector_driver.py` |

## 10. Kalibrasi (PDF §10.10; MEB §2.4, K1, K2)

| Konstanta | Cetak biru | Kode | Keputusan |
|---|---|---|---|
| Ketersediaan lane (K1) | π dicetak 71.64 % → A = 98.03 % | π dari parameter yang dinyatakan → A = 95.39 %; π yang dicetak MEB baru didapat dengan p_f = 0.0375, p_q = 0.03 | Keduanya dipin di `tests/test_models.py`; angka MEB dilaporkan sebagai *klaim yang tidak mengikuti parameternya* |
| Ambang promosi skill (K2) | `AUTO_MIN_RATE = 0.75` (PDF) | `recommend.AUTO_MIN_RATE = 0.8 == core.recommend.QUALIFIED_MIN_SCORE`; `models.AUTO_MIN_RATE = 0.75` (PDF) dengan tes divergensi | Dua ambang berbeda, keduanya dipin (`tests/test_core_recommend.py`, `tests/test_models.py`) |
| Cooldown pemutus alat | 10 s (PDF) | `resilience.DEFAULT_COOLDOWN_S = 30.0` | Dipin sebagai divergensi (`tests/test_tier2_stability.py::test_the_three_breakers_keep_their_own_numbers`) |
| Spearman ρ (MEB §2.4) | 0.842 atas 10 modul | tidak ada data contoh di `meb.txt` | Tidak dapat dikalibrasi; kanari monoton saja |
| Bobot log-linear (K3) | α = 1.0, β_CC = 0.65, β_churn = 0.45, clamp [1, 50] | sama; rasio maks/min atas grid K3 ≤ 18.4 | Dipin (`tests/test_tier2_stability.py`); auto-kalibrasi dari `elapsed_ns` tidak diimplementasikan (§11 butir 12) |
| Jitter kunci tugas (K4) | 15–200 ms, 20 spin, stale 60 s, timeout 15 s | verbatim | Dipin (`tests/test_tasktool_lock.py`); `_exclusive` lama tetap 5 spin (§11 butir 13) |

## 11. Register deviasi dan gap (33 butir tinjauan kritik, status pada commit ini)

| # | Butir (ID) | Status pada commit `0fd14b2` |
|---|---|---|
| 1 | `IMPLEMENTATION_PLAN.md` (RP-RTM 10.1–10.3/10.6/10.10/10.15/11.4, MEB §3, §9, K15) | DITUTUP — dokumen ini; `tests/test_implementation_plan.py` |
| 2 | A/B nyata `hive mindnet ab --execute --record` (RP-RTM 2.15, SPEC-11.5) | DIJALANKAN — hasil riil `inconclusive` tercatat (`experiment_result` `de169eea`, 2026-09-29T08:22:48Z): kedua lengan `stalled`, `a` ditolak gerbang executor (`no validation_script`), tidak ada proses anak yang dibuat; kuota/auth CLI `claude` tidak teruji oleh run ini — §12 |
| 3 | `docs/SOP_FIVE_GOLDEN_RULES.md` commit + registrasi `hive sop add` (RP-RTM 9.3) | DITUTUP DENGAN KOREKSI — dokumen di-commit `b274be4`; perintah registrasi yang ditulis dokumen itu **ditolak** saat pertama kali dijalankan (`sop body exceeds 4000 chars`, `sop.MAX_TEXT_CHARS`); bentuk ringkas `docs/SOP_FIVE_GOLDEN_RULES_COMPACT.md` dibuat, dokumen panjang dikoreksi, `tests/test_sop_golden_rules_doc.py` memin keduanya; registrasi: `hive sop add five-golden-rules --title "Five golden rules (spec 9.3)" --file docs/SOP_FIVE_GOLDEN_RULES_COMPACT.md --tags sop,golden-rules --no-mandatory` @ 2026-09-29T08:03:01Z, 12 SOP aktif |
| 4 | Runbook commit + mekanisme yang hilang (RP-RTM 9.4) | DITUTUP SEBAGIAN — `docs/RUNBOOK_RECOVERY.md` di-commit `b274be4`; tujuh mekanisme yang disebut spec §9.4 tetapi tidak ada di kode dicatat di tabel akhir runbook itu (`verify --json` tidak menyebut baris pertama yang berubah; tidak ada rekonsiliasi rantai; `append.lock` tanpa aturan basi) — DEVIASI TERCATAT |
| 5 | `docs/DATA_MODEL.md` §4.19 Chroma/CQRS (RP-RTM 4.1) | DITUTUP — `b274be4`, diperluas oleh `1500444`, `cf2fd9f`, `bc96acd`, `0fd14b2` |
| 6 | README prasyarat OS / Claude CLI / hardware (RP-RTM 1.1–1.5) | DITUTUP — README `### Prerequisites` ditambahkan (OS dari matriks CI `ubuntu-latest`/`windows-latest`, Python ≥ 3.11 dari `pyproject.toml`, Claude Code CLI 2.1.198 untuk perintah yang memanggil agen, RAM extra `vectors` 500 MB terukur, indeks 9.76 MB, suite 11 138 tes / 24 menit); `tests/test_readme_prerequisites.py` 3 lulus — membaca jawabannya dari `pyproject.toml` dan `.github/workflows/ci.yml`, bukan dari README |
| 7 | Subperintah mandiri `hive gate` (SPEC-10.16.3) | DEVIASI — gerbang hanya lewat `hive lintester` (`--allow-untested`, `--allow-test-edits`); tidak ada parser `gate` |
| 8 | `commands/vectors.py` dan paket `src/mindhive/cli/` (SPEC-10.16.1/2, M5) | DEVIASI — `embed` tinggal di `src/mindhive/commands/maintenance.py`; nama paket `commands/` dipertahankan |
| 9 | Langkah integrasi memicu CI native otomatis (SPEC-11.2.1d) | DEVIASI — `src/mindhive/orchestrator/integrate.py` tidak merujuk runner CI; CI berjalan lewat `hive ci run` atau hook pre-push |
| 10 | Skrip autoplan tanpa jq (SPEC-M10) | DI LUAR REPO — skill eksternal; TODOS #14 tetap terbuka |
| 11 | Backlog EventBus menyimpan 50 event pertama (SPEC-M11) | DITUTUP — `src/mindhive/orchestrator/board.py` `BACKLOG_HEAD = 50` (P5 `b5e8c0e`) |
| 12 | Koefisien bobot auto-kalibrasi dari `elapsed_ns` (SPEC-M6, K3) | DEVIASI — koefisien K3 tetap; `--weight-model` memilih model (P8) |
| 13 | `_LOCK_DENIED_SPINS = 10` (SPEC-Pilar2) | DEVIASI — `tasktool._exclusive` tetap 5; `task_lock` memakai 20 spin K4 |
| 14 | Plafon baris 200 (SPEC-Pilar3, 10.4) | DEVIASI — `pgshape.MAX_ROWS_CEILING = 500` dipertahankan dengan sengaja |
| 15 | PreToolUse menolak saat kontradiksi; field payload (SPEC-7.2/7.4/8.6) | DEVIASI — verdict selalu `allow`; payload tanpa `event_id`/`stream_snippet`/`worktree_lane`/`latency_ms` |
| 16 | Notifikasi merah dasbor untuk `merge_conflict_detected` (SPEC-7.6) | DEVIASI — tipe event tercatat (`contracts.py`, `events.py`, `mindnet/worktree.py`, `pgschema.py`); tidak ada permukaan dasbor |
| 17 | Retensi tier skill/inisiatif/tautan kerja (SPEC-6.3) | DEVIASI — Tier 2 = sesi kerja aktif saja; tidak diuji |
| 18 | 0.5·Levenshtein + 0.5·cosine dan "Global Brain" (SPEC-6.5, CTX-02) | DEVIASI — cosine dengan fallback Jaccard; tidak ada modul Global Brain |
| 19 | Pemulihan marker basi saat hook sesi mulai (SPEC-4.5) | DEVIASI — hanya saat boot dasbor dan `hive watcher recover` |
| 20 | Metrik token dan RAM (SPEC-4.7, 10.6) | SEBAGIAN — latensi diukur (§7); RAM extra `vectors` diukur sekali (§7.4, puncak 500 MiB); token per sesi tidak |
| 21 | F1 100 × 12 proses; F2 paritas + recall < 85 ms; F3 (SPEC-9.1) | SEBAGIAN — F1 literal tidak ada (4 × 40 dan 12 × 25 sebagai gantinya); F2 recall: 10.576 ms median hangat, 12.69 ms maks (< 85 ms; klaim, bukan gerbang); F3 tes unit + pemeriksaan manual |
| 22 | Render < 15 ms dan hit ratio ≥ 98 % (SPEC-10.5.4) | TIDAK TERPENUHI (diukur) — warm/hit 14.0 ms (`/api/oversee`) sampai 654.2 ms (`/api/overview`) pada salinan store; `/api/overview` jauh di atas 15 ms bahkan hangat; hit ratio tidak dilaporkan bench — §7.3; tidak ada tes yang memin keduanya |
| 23 | Nol edit `orchestrator/lintester.py` (MEB §3.3) | DEVIASI TERCATAT — P7 menukar komentar path baris 1 dengan SPDX, net 0 baris; `scripts/loc_report.py` `LINTESTER_CEILING = 997` menahan panjangnya |
| 24 | Pin ρ = 0.842 (MEB §2.4) | TERBUKA — butuh data contoh yang tidak dimuat MEB |
| 25 | Commit per fase, tanpa push (K22) | DITUTUP — 27 commit `feat(blueprint)`/`fix(vectors)` (32 dengan 1 `fix(orchestrator)`, 1 `test(runlog)` dan 3 merge) dari `9576f15` sampai `0fd14b2` di `claude/blueprint-foundation-v1`; tidak ada ref remote untuk cabang ini |
| 26 | Baris RTM untuk `hive recall --semantic` | DITUTUP — baris K10.3 di §4 |
| 27 | Baris RTM allowlist shrink-only (MEB §3.4) | DITUTUP — baris §3.4 di §4 |
| 28 | `docs/session-legend.md` memuat setiap modul baru | DITUTUP — baris `fabricate.py` ditambahkan; setiap modul yang ditambahkan cabang ini kini bernama di `docs/session-legend.md` (pemeriksaan mekanis atas `src/mindhive/**/*.py`: 8 modul yang tidak bernama semuanya pra-cetak biru — `autosync.py`, `clusterscli.py`, `incidentfile.py`, `injectors.py`, `mindnet/brief.py`, `pane.py`, `selftest.py`, `symbols.py` — dicatat, di luar lingkup P11) |
| 29 | Pin topologi store lengkap (SPEC-1.7) | SEBAGIAN — `chain.json`, `index.json`, `graph.json`, `cluster_db.json`, `LEDGER/` diuji tersebar di `tests/test_chain.py`, `tests/test_projector.py`, `tests/test_graph.py`, `tests/test_paths.py`, `tests/test_clusters.py`; tidak ada satu tes topologi |
| 30 | Merge WT/P8/P10 tanpa kehilangan kerja P5 (MERGE-ORDER) | DITUTUP — semua di satu cabang; `theme.dim = #9aa3b8`, split nav, `static/dashboard.js` ada; cap terpenuhi (`loc_report --check` exit 0) |
| 31 | Satu `TODOS.md` yang direkonsiliasi | DITUTUP — `TODOS.md` cabang ini: #1, #2, #5, #7, #15, #30, #31 DONE; #32 diberi catatan 2026-09-28 (fail-closed tertutup, sisanya terbuka); #14 terbuka |
| 32 | Urutan triage tier 1 → 2 → 3 (SPEC-10.12) | DEVIASI TERCATAT — P9 (Tier 3) mendarat di commit pertama bersama P0 |
| 33 | Satu laporan validasi gabungan (SPEC-10.15) | DITUTUP — §6 |

## 12. Eksperimen A/B (PDF §2.15, §11.5)

Dijalankan sekali, 2026-09-29T08:22:40Z, di worktree sekali pakai (`git worktree
add --detach … 0fd14b2`, bukan di pohon kerja P11, karena lengan yang dieksekusi
memberi agen hak tulis ke checkout): `python -m mindhive mindnet ab --execute
--record --limit 1 --json`, exit 0 dalam ~8 s. Grafik MindNetwork nyata proyek
`mindhive` saat itu: lima tugas `pending` (`a` root, `b`, `c`, `d`,
`faculty-panel-rows`; semuanya fase brainstorm, epoch 0). Hasil (keluaran
`--json`, disalin apa adanya):

```
{
 "metric": "validated completions",
 "verdict": "inconclusive",
 "reason": "no task passed the completion gate in EITHER arm, so validated completions could not have told them apart. On the agent path `ran-unverified` routes to `skipped` with no status row — a fully successful run scores zero here. Grade this with a metric that can move, or run it against a graph whose tasks reach validation",
 "delta": 0,
 "notes": [
  "one wave only — see this module's docstring on why a successful agent wave never runs a second"
 ],
 "arms": {
  "off": {
   "brief_on": false,
   "outcome": "stalled",
   "score": 0,
   "completed": [],
   "blocked": [],
   "skipped": [
    "a"
   ]
  },
  "on": {
   "brief_on": true,
   "outcome": "stalled",
   "score": 0,
   "completed": [],
   "blocked": [],
   "skipped": [
    "a"
   ]
  }
 }
}
```

Bacaan:

- Kedua lengan `stalled`; tugas `a` `skipped` di keduanya — dan **tidak ada
  proses anak yang pernah dibuat**. Runner bawaan lengan yang dieksekusi adalah
  `mindnet/executor.py` (menjalankan `validation_script` tugas di balik gerbang
  fail-closed), dan kelima tugas grafik nyata dideklarasikan tanpa
  `validation_script` (bentuk bawaan `hive mindnet declare`), sehingga gerbang
  menjawab `refused` (`'a' has no validation_script, so there is nothing for
  'brainstorm' to verify`);
  `orchestrator.NON_EXECUTING_RESULTS` memetakan `refused` ke `skipped` tanpa
  baris status. Buktinya: tidak ada artefak baru di `~/.mindhive/mindhive/runs/`
  (yang terbaru 2026-09-08), tidak ada berkas yang berubah di worktree, tugas `a`
  tetap `pending` tanpa bukti (`hive mindnet show a`), seluruh run ~8 s.
- Karena itu run ini **tidak menguji** kuota atau autentikasi CLI `claude`: tidak
  ada agen yang dipanggil. Teks alasan `--json` yang menyebut `ran-unverified`
  adalah penjelasan umum harness tentang mengapa metriknya tidak bisa bergerak,
  bukan catatan bahwa agen berjalan.
- Verdict `inconclusive` bukan kegagalan harness: metrik *validated completions*
  tidak dapat bergerak pada grafik yang tugasnya tidak mencapai validasi. Harness,
  perekaman (`experiment_result` `de169eea`, 2026-09-29T08:22:48Z, di ledger
  proyek), dan jalur eksekusi terbukti berjalan ujung ke ujung; yang belum ada
  adalah grafik yang tugasnya membawa `validation_script` (§13 butir 3).

## 13. Langkah lanjut (PDF §11.5)

1. **Cabang hook**: keluarga hook POSIX + PowerShell sudah di master sebelum
   cetak biru (`5b5cdf9`, `677e0ac`); P5 menambah `extract_json_field` dan
   alias `Resolve-MindHivePython` di cabang ini. Tidak ada verb
   `hive hooks install`: setelah merge, pada mesin yang memakai
   `~/.claude/settings.json` lama, daftarkan shim hook (`session_start.ps1`
   dan kawan-kawannya) sesuai README "Installing the hooks on a new machine",
   dan hook `Stop` lewat `hive session install-hook <settings.json>`, yang
   mencadangkan settings.json lebih dulu.
2. **CI**: `.github/workflows/ci.yml` hanya `workflow_dispatch` + tag `v*`;
   `hive ci run --branch claude/blueprint-foundation-v1` adalah gerbang lokal.
   Pemicu otomatis dari langkah integrasi (§11 butir 9) belum ada.
3. **A/B**: §12. Verdict membutuhkan kedua lengan mencetak skor, dan skor hanya
   bergerak bila tugas melewati validasi: deklarasikan tugas dengan
   `validation_script` (lihat `hive mindnet declare -h`) lalu ulangi
   `hive mindnet ab --execute --record`; kuota CLI `claude` (habis sampai
   3 Oktober 2026 saat penulisan) baru relevan bila lengan menjalankan agen.
4. **Gap yang tersisa** (§11): butir 16, 19, 20 (token), 21-F1, 22, 24 adalah
   pekerjaan nyata; butir 7–9, 12–15, 17–18, 23, 32 adalah keputusan yang
   dicatat dan tidak dijadwalkan; butir 6 dan 28 ditutup oleh commit P11.
5. **Reinstall checkout utama** setelah merge agar skrip konsol `mindhive`
   (P1k) dan `hive bench cli-startup|recall-semantic` (P8) tersedia dari
   `hive.exe` tanpa `PYTHONPATH`.

## 14. Reproduksi

    git -C <checkout> checkout claude/blueprint-foundation-v1
    set PYTHONPATH=src
    python -m pytest -q -p no:cacheprovider --tb=short --junitxml=junit.xml --ignore=tests/test_implementation_plan.py
    python -m pytest -q -p no:cacheprovider tests/test_implementation_plan.py
    python scripts/loc_report.py --check
    python -m mindhive bench cli-startup --json
    set MINDHIVE_HOME=<salinan store>  &  python -m mindhive bench recall-semantic --reps 7 --json
    python -m mindhive dashboard --port 54471 --no-browser   (di terminal lain)
    python -m mindhive bench dashboard --url http://127.0.0.1:54471 --label "preview, salinan store" --json
    python -m mindhive mindnet ab --execute --record --limit 1

Skrip pengukur ISO (`iso_measure.py`) dan pembangun tabel audit (`build_plan.py`)
membaca berkas-berkas di atas; keduanya berada di scratch sesi, bukan di
repositori, karena bergantung pada berkas junit yang tidak di-commit.
