# Retraksi memo hasil panen dari ringkasan kompaksi dan baris injeksi

Tanggal: 2026-10-03. Branch: `claude/ai-lokal-mind-hive-ce7f70`. Status: desain, menunggu review
pemilik. **Belum ada yang ditulis ke ledger.** Menerapkan retraksi (`--apply`) menambah event ke
ledger dan hanya dilakukan atas kata pemilik.

## 1. Masalah

`harvest` mengubah teks bertanda `remember:` / `memo:` / `decision:` / `caveat:` menjadi memo
`decision` atau `caveat`, dan setiap prompt menjadi event `user_prompt`. Hanya teks yang diketik
pengguna yang boleh menjadi memo (`harvest.py:452-457`). Dua jenis baris buatan harness lolos dari
aturan itu sebelum diperbaiki:

| Kelas | Baris sumber | Diperbaiki oleh | Masih bocor? |
|---|---|---|---|
| Ringkasan kompaksi | baris `user` dengan `isCompactSummary: true` | 541459f (di-commit 2026-10-03 13:11Z, belum di-merge) | **Ya.** Sejak commit itu masih tercetak 7 memo kompaksi (§2). Berhenti setelah langkah 2 di §6 |
| Badan injeksi (skill, referensi, keluaran perintah lokal) | baris `user` dengan `isMeta: true` | 9c0ea4e (di-commit 2026-09-02 23:43Z) | Tidak terlihat lagi sejak 2026-09-28 06:28Z. Namun 56 dari 69 memo kelas ini tercetak *setelah* commit perbaikannya |

**Perbaikan baru berlaku setelah setiap pemanen menjalankannya.** Harvest berjalan di dua tempat:
- autosync (`autosync.py:73`) — proses baru setiap tick, memakai kode checkout utama;
- loop panen dashboard (`dashboard._auto_harvest_loop`, `dashboard.py:494`) — memakai kode yang
  diimpor saat dashboard mulai, sampai di-restart.

Selisih 25 hari pada kelas `isMeta` menunjukkan jeda itu nyata. Penyebab pastinya tidak dipastikan
di sini. Satu kandidat tercatat di ledger: dashboard di 54461 pernah berupa `dist/MindHive.exe`
beku hasil build 2026-06-24.

Perbaikan juga hanya berlaku ke depan. Memo dan `user_prompt` yang sudah tercetak tetap ada di
ledger dan disajikan di semua permukaan:
- **STATE / MEMORY.md.** Pada 2026-10-03 14:08:30Z, memo kompaksi berukuran 15,9 KB menjadi
  "Last decision" di STATE.md dan MEMORY.md. Ia tetap di sana sampai ada keputusan asli berikutnya.
- **Pencarian.** LEDGER, `hive recall`, ketiga indeks vektor, graph, dan dashboard ikut menyajikannya.
- **Konsumen `user_prompt`.** Jejak skill di Oversee, "Asked" pada roadmap otomatis, dan distiller
  Terion kelak akan membaca ringkasan atau badan skill seolah-olah pemilik yang mengetiknya.

## 2. Bukti terukur (probe read-only, 2026-10-03)

**Cara mengukur.**
- Probe membaca proyeksi `LEDGER/*.md` di semua namespace, ditambah transkrip
  `~/.claude/projects/*/<sesi>.jsonl` milik sesi-sesi tersebut.
- Event mentah tidak dibaca. Mirror pg tidak tersedia (`backend_unavailable`).
- Keluaran probe hanya berupa hitungan dan timestamp. Tidak ada teks memo yang dicetak.
- Aturan pembuktian: memo dihitung *terbukti* hanya bila ekstraksi **pra-perbaikan** atas satu
  baris transkrip menghasilkan persis (ts, type, text) yang sama. Ekstraksi pra-perbaikan =
  `_items_from_obj` dengan gerbang injeksi dimatikan.

**Hasil probe 5 (563 entri LEDGER bertag `harvest,memo`):**

| Kelompok | Entri | Rincian |
|---|---|---|
| Dari ringkasan kompaksi | **74** | 56 `decision`, 18 `caveat`; 718 KB; 12 sesi; 7 namespace; 70 dari 74 badan lebih dari 2.000 karakter |
| Dari baris `isMeta` | **69** | 11 namespace |
| Dari baris yang diketik | 14 | Semua prompt ≥2.000 karakter dengan penanda lebih dari 400 karakter dari awal; 7 dibuka dengan tag `<` (konten tempelan). **Di luar cakupan** (lihat §8) |
| Tak terjelaskan | 2 | Keduanya bertanggal Agustus 2026 dan terkandung di baris `isMeta` pada detik yang sama, tetapi tanpa kecocokan persis |
| Transkrip sudah terhapus | 404 | 100 sesi, 2026-06-11 … 2026-08-24; transkrip tertua di disk ber-mtime 2026-08-29, sesuai pembersihan otomatis Claude Code. 244 entri di bawah 1 KB. Tidak bisa dibuktikan. |

**Kapan dicetak (probe 6, beberapa jam setelah probe 5):**

| Kelas | Entri | Pertama | Terakhir | Setelah commit perbaikan |
|---|---|---|---|---|
| Kompaksi | 75 | 2026-09-01 00:51Z | 2026-10-03 16:43Z | 7 (541459f, 2026-10-03 13:11Z) |
| `isMeta` | 69 | 2026-08-05 12:38Z | 2026-09-28 06:28Z | 56 (9c0ea4e, 2026-09-02 23:43Z) |

Memo kompaksi bertambah satu antara probe 5 dan probe 6, dari kompaksi sesi ini sendiri. Angkanya
terus naik sampai langkah 2 di §6 selesai. Dry run (§5.6) selalu menghitung ulang.

**Sebaran per namespace** (probe 5; kompaksi / `isMeta`):

| Namespace | Kompaksi | `isMeta` |
|---|---|---|
| `hanif` | 32 | 8 |
| `mindhive` | 21 | 28 |
| `vigilant-lehmann-0ed960` | 9 | 9 |
| `cli-login-status-detection-cfec62` | 6 | 3 |
| `brave-tharp-b4aff7` | 3 | 6 |
| `pc-optimization-mind-hive-16af2e` | 2 | 4 |
| `lintester-mindhive-orchestration-fa4543` | 1 | 1 |
| `mindhive-phase-4-impl-cb3b2f` | — | 5 |
| `lintester-mindhive-orchestration-77909c` | — | 3 |
| `a-56dec8` | — | 1 |
| `gate-kompatibilitas-bahasa-18c4e8` | — | 1 |

**`user_prompt` sampah.** Di 32 transkrip yang dibuka, ekstraksi pra-perbaikan menghasilkan 210
`user_prompt` dari baris kompaksi dan 294 dari baris `isMeta`. Angka ini batas atas: probe tidak
membaca event mentah, jadi berapa yang benar-benar masuk ledger dihitung oleh dry run (§5.6).

## 3. Tujuan dan non-tujuan

**Tujuan:**
1. **Identifikasi eksak lewat asal-usul.** Event ditarik hanya bila baris sumbernya di transkrip
   terbukti baris buatan harness. Isi teks tidak pernah dipakai sebagai heuristik.
2. **Append-only.** Ledger mentah dan rantai hash tidak disentuh; koreksi berupa event baru
   (DATA_MODEL §5.1).
3. **Semua permukaan berhenti menyajikan.** Daftarnya ada di §5.4.
4. **Idempoten dan aman diulang.** Dry run adalah default; `--apply` memerlukan jumlah yang sudah
   direview (`--confirm N`).

**Non-tujuan:**
- Menghapus atau mengedit baris event (terlarang).
- Menarik memo yang diketik pengguna. Ini termasuk ringkasan yang ditempel sendiri dan penanda di
  tengah prosa; lihat §8.
- Menarik 404 memo yang transkripnya sudah hilang (§5.1, "Tidak terbukti").
- Memperbaiki harvest. Itu sudah dilakukan 541459f dan 9c0ea4e.

## 4. Pendekatan yang dipertimbangkan

| | Pendekatan | Kekurangan |
|---|---|---|
| **A (dipilih)** | Event deterministik baru `harvest_retracted` + **satu** lipatan tampilan (`retraction.live`) yang dipakai semua konsumen | Banyak konsumen ikut diubah. Itu memang inti pekerjaannya: hari ini tidak ada seam bersama yang dihormati semua permukaan |
| B | Pakai ulang `absorbed` milik `memory_consolidated` | Semantiknya bohong: event itu menyatakan memo dilebur ke penyintas yang sebenarnya tidak ada. Selain itu hanya `briefing`/`sync` yang membacanya (`briefing.py:27-48`, `sync.py:6`). "Last decision", LEDGER, recall, vektor, graph, dan dashboard mengabaikannya, jadi kerja konsumen A tetap diperlukan |
| C | Memo koreksi (`caveat`: "memo X sampah") | Tidak menyembunyikan apa pun, malah menambah derau di permukaan yang hendak dibersihkan |
| D | Menulis ulang segmen | Terlarang: append-only, rantai hash, dan paritas mirror |

**Alasan memilih A:**
- Catatannya jujur: event menyebut apa yang ditarik, alasannya, dan bukti asal-usulnya.
- Eksak dan bisa diaudit (`hive retract --list`).
- Baris aslinya tetap utuh. Kalau suatu hari retraksi perlu dibatalkan, cukup tambahkan event baru.
  Tipe event pembatalan tidak dibuat sekarang (YAGNI).

## 5. Desain

### 5.1 Identifikasi

Sebuah event E (tag memuat `harvest`) di namespace N, dengan `session_id` S, `ts` T, `type` Y, dan
teks X, adalah **target** dengan alasan R bila ketiga syarat ini terpenuhi:

1. transkrip `<projects>/<folder>/S.jsonl` memuat baris B yang membawa penanda R
   (`isCompactSummary: true` untuk `compaction_summary`, `isMeta: true` untuk `injected_meta`);
2. `harvest._normalize_ts(B.timestamp) == T`;
3. ekstraksi pra-perbaikan atas teks B menghasilkan item yang persis sama:
   - memo: (`memo_type`, `value`) = (Y, X);
   - prompt: Y = `user_prompt` dan `value` = X.

**Ekstraksi pra-perbaikan.** Logika penanda (`harvest.py:445-470`) dan logika prompt
(`harvest.py:480-483`) dipisah menjadi dua fungsi murni, `_memo_from_text(text)` dan
`_prompt_from_text(text)`. `_items_from_obj` tetap memanggil keduanya di belakang gerbang
`_is_injected`. Pengidentifikasi memanggilnya **tanpa** gerbang, tetapi hanya untuk baris yang
membawa penanda R. Tidak ada monkeypatch dan tidak ada parameter "matikan gerbang" di jalur panen.

**Ringkasan yang ditempel tetap aman.** Baris yang diketik tidak membawa kedua penanda, jadi tidak
pernah menjadi target. Ini tes kanari yang sama dengan milik 541459f.

**Pencarian transkrip** memakai pemetaan harvest sendiri: berkas tingkat atas berstem
`session_id` di bawah `harvest.claude_projects_root()`. Rencana implementasi memverifikasi bahwa
transkrip subagen tidak pernah dipanen.

**Tidak terbukti.** Memo berikut **tidak pernah** menjadi target:
- memo tanpa transkrip;
- memo yang tidak cocok persis dengan baris mana pun. Kecocokan memakai ekstraksi yang sekarang,
  jadi memo yang dicetak versi harvest lama dengan ekstraksi berbeda (batas kata, pembersihan
  prompt) jatuh ke kelompok ini.

Dry run hanya melaporkan jumlahnya per bulan dan per ukuran, ditambah satu sinyal informatif:
berapa yang berpasangan dengan `user_prompt` pada sesi dan detik yang sama, yang teksnya dibuka
dengan pembuka ringkasan kompaksi atau `_SKILL_BODY_MARK`.
- Pasangan itu bukan bukti, karena ringkasan yang ditempel sendiri adalah input yang diketik
  (alasan yang sama di 541459f).
- Menerapkannya memerlukan keputusan pemilik tersendiri dan berada di luar cakupan v1.

### 5.2 Kontrak event `harvest_retracted`

**Pendaftaran tipe** (DATA_MODEL §6):
- masuk `events.DETERMINISTIC_TYPES`, karena ini pembukuan mesin, bukan memo yang bisa di-recall;
- `pgschema.EVENT_TYPE_CLASSES` = `"deterministic"`;
- masuk **kedua** daftar pengecualian merge, `consolidate.NEVER_CONSOLIDATE` dan
  `dedupe.NEVER_COLLAPSE` (§3.1).

Karena bukan tipe SEMANTIC, event ini tidak diproyeksikan ke LEDGER dan tidak perlu `ledger_body`.

Bentuk payload. Nilai dalam `<…>` hanya tempat isi; `line` berupa bilangan bulat.

```json
{
  "reason": "compaction_summary",
  "fix": "541459f",
  "targets": [
    {"session_id": "<uuid sesi>", "ts": "<YYYY-MM-DDTHH:MM:SSZ>", "type": "decision",
     "text_sha256": "<64 hex>", "event_id": "<32 hex>",
     "evidence": {"transcript": "<folder>/<sesi>.jsonl", "line": "<int>", "row_uuid": "<uuid baris>"}}
  ],
  "void_consolidations": [{"ts": "<ts konsolidasi>", "type": "decision", "survivor_ts": "<ts target>"}]
}
```

**`retraction.validate_payload`** dijalankan di setiap permukaan append:
- `reason` ∈ {`compaction_summary`, `injected_meta`}; `fix` = string tak kosong;
- `targets` tak kosong dan berisi ≤500 target. Lebih dari itu dipecah menjadi beberapa event;
- `type` ∈ {`decision`, `caveat`, `user_prompt`}, yaitu persis tipe yang dicetak harvest
  (`harvest.py:461-466`, `:575-576`);
- `text_sha256` = 64 hex, `event_id` = 32 hex, `ts` berbentuk `YYYY-MM-DDTHH:MM:SSZ`;
- `evidence.transcript` = string, `evidence.line` = int ≥1.

**Penulis tunggal.** `hive append harvest_retracted` dan setiap jalur append lain menolak tipe ini
sambil menunjuk `hive retract`. Hanya `hive retract --apply` yang menulisnya, setelah memeriksa
bahwa setiap target memang ada di namespace itu dan bertag `harvest`. Akibatnya retraksi tidak bisa
dipakai untuk menyembunyikan memo yang ditambahkan dengan tangan.

**Payload tanpa teks.** Payload hanya memuat hash dan pointer, sehingga isi sampah tidak tersalin
ulang ke ledger.

**Kunci pencocokan:** `(session_id, ts, type, sha256(text))`, dan hanya event bertag `harvest` yang
bisa cocok. Event lain dengan kunci yang sama tidak pernah tersembunyi.
- Python maupun SQL bisa menghitungnya. Mirror pg tidak punya kolom `event_id`
  (`001_schema.sql:14-106`).
- `event_id` (`vectors.event_id`, `vectors.py:464`) ikut dibawa hanya agar indeks vektor event
  bisa membuang id persisnya tanpa menghitung ulang.
- Dua event panen yang sama persis pada keempat unsur kunci adalah duplikat menurut definisi apa
  pun, jadi keduanya ikut tertarik.

### 5.3 Satu lipatan bersama: `retraction.py`

Fungsi yang disediakan:
- `key(ev)` — kunci pencocokan §5.2.
- `fold(evs) -> Retracted` — total: payload yang rusak dilewati, tidak pernah raise (DATA_MODEL §5.3).
- `Retracted.__contains__(ev)` — saring dulu dengan dict `(ts, type)` dan tag `harvest`. sha256
  hanya dihitung untuk kandidat, jadi biaya per proyeksi ≈ jumlah target, bukan jumlah event.
- `live(evs, retracted=None)` — mengembalikan event kecuali:
  1. target retraksi;
  2. event `memory_consolidated` yang tercantum di `void_consolidations`.

  Event `harvest_retracted` sendiri tetap diteruskan; konsumen tampilan tidak membacanya.

**Invarian baru, DATA_MODEL §5.8:**
- **Bacaan mentah tidak pernah menyaring:** `events.read_all` / `read_types`, `chain`,
  `hive verify`, pemuat mirror pg, serta kunci dedup harvest (`_existing_event_keys`,
  `_hook_captured_sessions`, `harvest.py:514-556`).
- **Tampilan selalu menyaring.**

Dedup harvest wajib tetap melihat target. Dengan begitu, panen ulang oleh kode lama tidak mencetak
memo yang sama lagi.

### 5.4 Konsumen yang wajib membaca lewat `live()`

| Permukaan | Seam | Perubahan |
|---|---|---|
| "Last decision" di STATE.md / STATE_BRIEF.md | `projector._latest` `:53-62` | membaca `live(evs)` |
| LEDGER/*.md + index.json | `projector.build_ledger_entries` `:395-432`, prune di `_project` `:539-543` | entri dari `live(evs)`, sehingga prune menghapus berkas target. `chain.write_chain` tetap atas `evs` mentah |
| Briefing (Key decisions, timeline) | `briefing._collapsed_entries` `:51-72`, `:144` | `live(evs)`; kunci absorbed dihitung dari `live(evs)` agar konsolidasi yang dibatalkan berhenti menyembunyikan |
| MEMORY.md | `sync.py` `:83-130`, `:187` | sama |
| `hive recall` leksikal | `recall.py:99` membaca LEDGER | tercakup oleh prune LEDGER, tanpa perubahan kode |
| `hive recall --semantic`, indeks event | `vectors.embed_events` `:494`, `semantic_recall_events` `:553` | embed melewati target. Jalur baca membuang hit yang `event_id`-nya ditarik (pertahanan berlapis, tanpa menulis indeks dari jalur baca, §4.19) |
| `hive recall --semantic`, indeks berkas | `vectors.embed` `:366-391` | hari ini hanya menghapus id berkas yang masih ada (`:386`), sehingga vektor berkas LEDGER yang sudah di-prune tertinggal selamanya. Ditambah sapuan: id koleksi yang tidak lagi punya berkas ikut dihapus |
| Konsolidasi | `consolidate.consolidate` `:163` | `plan_consolidation(live(evs))`, sehingga target tak pernah lagi menjadi penyintas |
| graph.json | `graph.py` | `live(evs)` |
| Dashboard, Memory view | `dashboard_data.py:68,221`, `dashboard_get.py:156,292` | `live(evs)` |
| Oversee, jejak skill | `oversee.py:213,313` | target `user_prompt` tidak dibaca |
| Roadmap otomatis ("Asked") | `roadmap_auto.py:207` | sama |
| Prompt gate | `promptgate.py:327` | sama |
| Distiller Terion (kelak) | sumber `distill_source` | membaca lewat `live()`; dicatat sebagai aturan sumber di spec Terion |
| Alat mirror pg | migrasi 008 (baru): view `retracted_targets` dan `live_events`; kueri alat memakai `live_events` | **PR terpisah**: butuh klaster pg, leg CI pg tidak bisa dijalankan di sini |
| Embedding event pg | `hive pg embed` | lewati dan hapus target; PR yang sama dengan baris di atas |

**Audit konsumen.** Rencana implementasi menjalankan grep atas setiap pembaca `SEMANTIC_TYPES`,
`MEMORY_VIEW_TYPES`, `"decision"`, `"caveat"`, dan `"user_prompt"` di luar infrastruktur mentah.
Setiap temuan baru ditambahkan ke tabel ini. Setiap baris tabel mendapat satu tes perilaku.

### 5.5 Konsolidasi yang penyintasnya ditarik

- **Saat `--apply`.** CLI mencari, di event mentah, setiap `memory_consolidated` yang penyintasnya
  adalah target, yaitu yang memenuhi ketiganya:
  - `type` sama;
  - `survivor_ts` = `ts` target;
  - `text` = `consolidate.dedupe_headline(teks target)`.

  Kunci konsolidasi itu dicatat di `void_consolidations`.
- **Saat dibaca.** `live()` membuang konsolidasi tersebut, sehingga memo yang dulu di-absorb
  muncul kembali. Bila memo itu sendiri juga sampah, ia tetap tersembunyi oleh retraksinya sendiri.
- **Ke depan.** Konsolidasi berjalan atas `live()`, jadi kasus ini tidak bisa terjadi lagi.

### 5.6 CLI `hive retract` (mendaftar sendiri, seperti `spendcli`)

```
hive retract [--reason compaction_summary|injected_meta|all] [--project P | --all-namespaces]
             [--claude-dir DIR] [--json] [--show-text] [--apply --confirm N] [--force]
hive retract --list [--project P | --all-namespaces] [--json]
```

**Dry run (default).** Dicetak per namespace dan per alasan:
- jumlah target baru dan target yang sudah ditarik;
- konsolidasi yang akan dibatalkan;
- `user_prompt` yang ikut tertarik;
- kelompok di luar cakupan (diketik, tak terjelaskan, transkrip hilang) beserta sinyal pasangannya;
- satu baris per target: ts, type, byte, awalan sesi, `transkrip:baris`.

Teks memo tidak dicetak. `--show-text` menambahkan 80 karakter pertama. `--json` memuat hash,
bukan teks.

**LIVE_LOCK.** Dry run dan apply sama-sama memindai semua event dan transkrip, jadi keduanya
memanggil `guard.ensure_safe(force)` (`guard.py:10`) lebih dulu. Penolakannya satu baris `error:`
dan exit 2, sama seperti `hive embed` dan `hive gc --prune` (`commands/maintenance.py:73-77`,
`:118-123`).

**`--apply --confirm N`:**
1. Hitung ulang daftar target. Tolak (exit 2, tidak ada yang ditulis) bila jumlah target baru ≠ N.
   Ini pengaman terhadap review yang sudah basi.
2. Tambahkan paling banyak satu event per namespace per alasan, dipecah per 500 target.
3. Jalankan `projector.project(ns)` untuk setiap namespace yang tersentuh.
4. Bersihkan vektor bila indeks ada dan dependensinya terpasang: hapus id event target, lalu
   sapu id berkas. Bila tidak bisa, cetak perintah yang harus dijalankan.

**Exit code:**
- 0 — laporan tercetak, retraksi diterapkan, atau tidak ada yang perlu ditarik;
- 2 — salah pakai atau penolakan: alasan tak dikenal; `--apply` tanpa `--confirm`; N tidak cocok;
  `--claude-dir` eksplisit yang bukan direktori; LIVE_LOCK aktif tanpa `--force`.

**Biaya baca.** Event setiap namespace dibaca sekali. Ini bacaan pemeliharaan eksplisit, sama
seperti konsolidasi. Setiap transkrip sesi kandidat dibaca sekali secara streaming, hanya pada
baris yang memuat `isCompactSummary` atau `isMeta`.

### 5.7 Idempotensi dan laju

- Target yang sudah ada di `fold(evs)` dilewati. Bila tidak ada target baru: "tidak ada yang perlu
  ditarik" dan tidak ada event yang ditulis.
- Sampah kompaksi masih terus masuk sampai langkah 2 di §6 selesai: sejak 541459f di-commit, 7 memo
  kompaksi baru tercetak (probe 6). Setelah langkah 2, dry run diulang; jumlah barunya harus 0 dan
  tetap 0.

### 5.8 Uji

Semua uji berjalan di store sintetis (`conftest.isolate_store`), tidak pernah di store asli.

1. **Kontrak.** Alasan dan tipe yang sah dan yang ditolak; batas 500 target; hash dan ts salah
   bentuk; `hive append harvest_retracted` ditolak.
2. **Lipatan.** Total atas payload rusak. Dua kanari:
   - event dengan ts dan type sama tetapi teks berbeda tidak ikut tersembunyi;
   - event tanpa tag `harvest` dengan kunci yang sama persis tidak ikut tersembunyi.
3. **Identifikasi.** Transkrip sintetis berisi:
   - baris kompaksi;
   - baris `isMeta` yang dibuka dengan `Caveat:`;
   - baris ketikan berteks identik dengan ringkasan (kanari ringkasan tempelan);
   - sesi tanpa transkrip.

   Hasil yang diharapkan:
   - hanya dua yang pertama menjadi target;
   - kanari tetap hidup;
   - sesi tanpa transkrip masuk "tidak terbukti".
4. **Konsumen.** Satu tes per baris §5.4:
   - "Last decision" melompati target;
   - berkas LEDGER di-prune;
   - `hive recall` tidak menemukan frasa unik dari target;
   - `embed_events` tidak mengindeks target dan jalur baca membuangnya;
   - sapuan indeks berkas bekerja;
   - Memory view, Oversee, roadmap_auto, prompt gate, dan graph bersih.
5. **Mentah tetap mentah.**
   - `events.read_all` masih mengembalikan target;
   - kepala rantai hanya bertambah oleh event retraksi; `hive verify` lolos;
   - panen ulang dengan kode lama tidak menambah ulang target.
6. **Konsolidasi.** Penyintas yang ditarik membatalkan klusternya. Memo asli yang dulu di-absorb
   muncul kembali.
7. **CLI.**
   - Dry run tidak menulis apa pun; ini dibuktikan dengan kepala rantai yang tidak berubah.
   - `--apply` tanpa `--confirm` atau dengan N yang salah: exit 2, tidak ada yang tertulis.
   - `--apply` kedua kali: tidak ada event baru.
   - LIVE_LOCK menolak dry run dan apply (exit 2); `--force` melewatinya.
   - `--json` stabil.
8. **Penanda arsitektur.** Uji drift `NEVER_CONSOLIDATE`/`NEVER_COLLAPSE`, drift
   `EVENT_TYPE_CLASSES`, paritas docs/surface, dan `test_commands_*`.

### 5.9 Dokumentasi

- DATA_MODEL: §3 entri `harvest_retracted`, §5 invarian 8 (lihat §5.3 spec ini), §4.19 sapuan
  indeks berkas.
- README: entri `hive retract`.
- `docs/session-legend.md`: satu baris `retraction.py` dan `retractcli.py`.

## 6. Urutan rollout dan gerbang pemilik

| # | Langkah | Gerbang |
|---|---|---|
| 1 | Pemilik menyetujui desain ini | pemilik |
| 2 | Merge PR yang membawa 541459f; pull checkout utama; restart dashboard di 54461 dengan `python -m mindhive dashboard`. Autosync memuat kode baru pada tick berikutnya, loop panen dashboard baru setelah restart. Ini menghentikan sampah kompaksi baru | pemilik (merge, pull, restart) |
| 3 | `superpowers:writing-plans`, lalu satu PR: `retraction.py`, konsumen §5.4 (tanpa pg), `retractcli.py`, uji, dokumen. `hive log`; suite hijau; `hive ci premerge`. Setelah merge: pull lagi dan restart dashboard agar Memory view memakai `live()` | pemilik (merge, restart) |
| 4 | `hive retract --all-namespaces` (dry run); pemilik membaca daftarnya | — |
| 5 | `hive retract --reason compaction_summary --all-namespaces --apply --confirm N` | **kata pemilik** |
| 6 | Sama untuk `--reason injected_meta` | **kata pemilik**, terpisah |
| 7 | Verifikasi (§7) | — |
| 8 | PR pg (migrasi 008 + `hive pg embed`) saat klaster pg tersedia | pemilik |

## 7. Verifikasi setelah apply

- `hive state`: "Last decision" adalah keputusan asli.
- Berkas LEDGER target sudah hilang. Jumlah berkas LEDGER turun tepat sebanyak target memo.
- `hive recall "<frasa unik dari salah satu target>"` (leksikal dan `--semantic`) tidak
  mengembalikan target.
- Jumlah di Memory view dashboard turun. Pratinjau pada port lain, bukan 54461.
- `hive verify` dan kepala `chain.json` konsisten. `events.read_all` masih memuat target.
- `hive retract --all-namespaces` melaporkan 0 target baru.
- Sehari kemudian, dry run masih melaporkan 0 target baru. Ini memastikan tidak ada pemanen yang
  masih memakai kode lama.

## 8. Temuan sampingan (dilaporkan, tidak diperbaiki di sini)

1. **Penanda di tengah prosa.**
   - Ke-14 memo dari baris ketikan semuanya berasal dari prompt ≥2.000 karakter, dengan penanda
     lebih dari 400 karakter dari awal. Tujuh di antaranya konten tempelan (dibuka `<`).
   - Kemungkinan besar ini teks yang *membahas* sintaks penanda, misalnya prompt tugas atau dokumen
     tempelan, bukan memo yang disengaja.
   - Opsi: hormati penanda hanya di awal baris. Ini keputusan terpisah, dan memo yang sudah ada
     tetap input yang diketik.
2. **Vektor berkas basi.** `vectors.embed` tidak pernah membuang vektor berkas LEDGER yang sudah
   di-prune. Sapuannya ikut dikerjakan di §5.4 karena retraksi bergantung padanya.
3. **404 memo tanpa transkrip** (Juni–Agustus) tidak bisa dibuktikan. Sinyal pasangan dari dry run
   bersifat informatif, dan menerapkannya memerlukan keputusan pemilik tersendiri.
4. **Jeda perbaikan `isMeta`.** 56 memo `isMeta` tercetak sampai 25 hari setelah 9c0ea4e di-commit
   (§1). Ke depan, sebuah perbaikan harvest baru dianggap berlaku setelah semua pemanen terbukti
   menjalankannya.

## 9. Risiko

| Risiko | Mitigasi |
|---|---|
| Memo asli ikut tersembunyi | Hanya bukti asal-usul eksak; kanari ringkasan tempelan; hanya event bertag `harvest` yang bisa cocok; dry run + `--confirm N` |
| Sampah lolos di permukaan yang tidak terdaftar | Audit grep konsumen di rencana; satu tes per konsumen |
| Perbaikan belum berjalan di semua pemanen | Langkah 2 di §6 mencakup restart dashboard; dry run diulang sehari setelah apply. Data `isMeta` menunjukkan jeda ini nyata (25 hari) |
| Biaya proyeksi per tick autosync | Saring dulu dengan `(ts, type)`; sha256 hanya untuk kandidat; satu lipatan per proyeksi |
| Panen ulang mencetak ulang target | Dedup harvest membaca event mentah, jadi target tetap terlihat; ada tesnya |
| Transkrip terhapus setelah apply | Pointer bukti dicatat saat apply; retraksi tidak membutuhkan transkrip lagi |
| Mirror pg menyajikan target sebelum PR pg | Mirror menyalin baris mentah (paritas tidak terganggu); alatnya disaring di PR pg, dan sampai saat itu jawaban mirror tentang memo dianggap belum bersih |
