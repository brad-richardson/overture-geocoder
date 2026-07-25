# Places semantic-digest divergence: root cause

Date: 2026-07-24. Status: SHIPPED — committed; the fix landed the same day in
#141/#142/#143 (#142 exported Rust's lowercase and word-character tables into
the Python baseline; the remaining NFKD and combining-mark properties are item 3
of `docs/plans/2026-07-24-construction-v1-follow-ups.md`). Kept as the
point-in-time root-cause analysis. Evidence produced on the local machine from the real
`2026-06-17.0` projected task parquets in
`benchmarks/places-construction-v1-data/projected/`.

## TL;DR

The Rust↔Python semantic-digest divergence is **not corruption and not a bug in
the Rust data plane**. It is the Python baseline and the Rust transform
disagreeing on three well-defined Unicode string semantics that the contract
never pinned. All observed divergence is byte-for-byte explained by three
mechanism classes (plus one latent fourth); a Python-side emulation of Rust
semantics reproduces the Rust sums **exactly** on every diverging task tested.

Per the frozen contract (`2026-07-22-places-construction-v1-contract.md`):
"**Rust is authoritative** for admission, normalization, tokenization, routing,
ranking, and proof payloads." The baseline is the deviating side.

## The four classes

### Class 1 — Greek Final_Sigma context rule (most rows)

CPython `str.lower()` implements the Unicode `Final_Sigma` conditional mapping:
`Σ` (U+03A3) at the end of a word lowers to `ς` (U+03C2). Rust
`char::to_lowercase` is context-free and always produces `σ` (U+03C3).

Real diverging row (task 73, key = object 12, row group 178, row 7146):

```
brand = 'ΙΡΙΣ-ΚΑΘΑΡΙΣΤΗΡΙΟ ΧΑΛΙΩΝ-ΘΕΣΣΑΛΟΝΙΚΗ'
python token: 'ιρις'   (…03B9 03C2)   ← final sigma ς
rust   token: 'ιρισ'   (…03B9 03C3)   ← plain sigma σ
```

Same token count, different token bytes → different payloads → different
digests in both lanes. Found in Greek names and, surprisingly often, Japanese
rows (Daiwa House "xevoΣ" brand appears in dozens of rows in tasks 86/87).
Explains diverging tasks 01, 73, 85, 86, 87 — and latent rows exist in tasks
15 and 71, which simply were not in the compared role-task set.

### Class 2 — C0 control characters at string edges

CPython `str.strip()` strips everything `str.isspace()` is true for, which
includes U+001C–U+001F (file/group/record/unit separators). Rust `str::trim`
strips only Unicode `White_Space`, which excludes them. Verified exhaustively:
the two sets differ on exactly those four code points.

Real diverging rows: task 73 `'\x1dNUNU Shop'` (Vĩnh Long, VN) and task 76
`'\x1dBất Động Sản ĐẤT VÀNG Quảng Ngãi'` (VN). Tokens are identical (the
control char is a separator either way); the divergence is the **display
payload**: Python's primary is `'NUNU Shop'`, Rust's is `'\x1dNUNU Shop'`.
Every term row of the feature diverges in both lanes.

This class also silently threatens **admission** parity: a value like
`'PERMANENTLY_CLOSED\x1c'` or `'\x1c'`-padded UUID or a primary name that is
only `'\x1c'` would flip rejection counts between the sides. Not observed in
this data, but planet data is bigger.

### Class 3 — word-character class: `Other_Alphabetic` symbols

Rust `char::is_alphanumeric()` uses the derived `Alphabetic` property, which
includes `Other_Alphabetic` — 130 `So` symbols such as circled and negative
squared Latin letters. CPython `str.isalnum()` is category-based (`L*`, `Nd`,
`Nl`, `No`) and excludes them.

Real diverging row (task 76, key = object 13, row group 109, row 12794):

```
primary = '𝓓𝓮𝓹𝓔𝓭 - Roxas National Comprehensive High School 🅛🅘🅑🅡🅐🅡🅨'
python tokens: 8   (🅛🅘🅑🅡🅐🅡🅨 treated as separators → dropped)
rust   tokens: 9   (emits token '🅛🅘🅑🅡🅐🅡🅨')
```

This is the one observed class where **term counts differ** (by one row).
Adding the missing row's digest to the emulated sum reproduced the Rust sum
exactly in both lanes — arithmetic closure proof.

(Ordinary circled letters like Ⓐ don't hit this: NFKD decomposes them to
plain letters first. Negative-squared letters have no decomposition.)

### Class 4 — Unicode version skew (latent, not yet observed)

CPython 3.12 ships Unicode 15.1 tables; the current Rust toolchain ships
Unicode 16.0. Exhaustive comparison over all code points found:

- **55 code points** where lowercase mappings differ (e.g. U+1C89→U+1C8A,
  U+A7CB→U+0264, Garay block U+10D50–U+10D6A) — all Unicode-16 additions.
- **9,843 non-mark code points** that are word characters in Rust but
  unassigned (`Cn`) to CPython 15.1.

None occur in the 12 local tasks (the emulated sums matched without modeling
them beyond the word-char file), but planet data could contain them, and any
future CPython/Rust upgrade shifts the boundary. **Any dual-implementation
digest contract is implicitly pinned to a Unicode version**; the current
contract doesn't say which.

## Proof that the classes are exhaustive (on local data)

`rust_emulated_baseline.py` (scratchpad) = the frozen Python baseline with
three substitutions: per-char lowercase (no Final_Sigma), `White_Space`-only
trim, and Rust's word-character set (exported from the actual Rust toolchain
as a table). Result across every locally available task: **admitted counts,
term-row counts, and both 256-bit lane sums match the Rust candidate report
exactly.** Verified on ALL 12 local tasks (~12M input features, ~90M term
rows): 12/12 exact matches on admitted counts, term-row counts, and both lane
sums (produced by run_pair.sh + rust_emulated_baseline.py).

| task | baseline vs rust | emulated-python vs rust |
|------|------------------|-------------------------|
| 01, 85, 86, 87 | DIVERGES (class 1) | **exact match** |
| 73 | DIVERGES (classes 1+2) | **exact match** |
| 76 | DIVERGES (classes 2+3) | **exact match** |
| 05, 13, 55, 74 | match | **exact match** |
| 15, 71 | match in rehearsal set, but latent class-1 rows confirmed present | **exact match** |

Per-class synthetic fixture (Python baseline vs actual Rust tokenizer harness
vs patched Python), all three observed classes:

```
'ΝΕΟΣ ΚΟΣΜΟΣ'        py: νεος/κοσμος      rust: νεοσ/κοσμοσ      patched: matches rust
'\x1dNUNU Shop'       py strip: 'NUNU…'    rust trim: '\x1dNUNU…'  patched: matches rust
'DepEd 🅛🅘🅑🅡🅐🅡🅨'  py: 1 token          rust: 2 tokens          patched: matches rust
```

## Which side is "right"?

Contractually: Rust (declared authoritative). Practically, Rust is also the
*better* choice for two of three classes:

- **Class 2**: keeping U+001C–1F (i.e., only trimming real whitespace) is
  defensible either way, but the serving-side Worker is Rust — index build and
  query tokenization must share semantics, and both will be Rust.
- **Class 3**: emitting a token for 🅛🅘🅑🅡🅐🅡🅨 is harmless noise.
- **Class 1 (quality caveat)**: context-free `σ` in the *index* is only
  correct if the *query* path folds the same way. A user typing lowercase
  Greek naturally types final `ς` ("ιρις"); Rust lowercase of already-lowercase
  `ς` is a no-op, so the query token would be `ιρις` while the index holds
  `ιρισ` → **miss**. The robust fix for search quality is a explicit
  `ς → σ` fold (this is exactly what Unicode case folding does) applied in
  BOTH index tokenization and the Worker query tokenizer. One match arm in
  Rust; one `str.translate` in Python.

## Recommended fix (small, no architecture change needed)

1. **Amend the tokenizer contract** (pre-genesis, it's explicitly replaceable):
   normalization = trim `White_Space` only; lowercase = Unicode simple+special
   per-char mapping **plus explicit `ς→σ` post-fold**; word chars = Rust
   `is_alphanumeric` ∪ `_`; pin "as implemented by Rust `places_transform_v1`,
   tokenizer_version `nfkd-lower-stripmark-cjk-bigram-v4`". Bump the version
   string; regenerate fixtures.
2. **Rust**: add the `ς→σ` fold (one line in the fold loop). Mirror the same
   fold in the future Worker query tokenizer.
3. **Python baseline**: apply the three substitutions proven in
   `rust_emulated_baseline.py` (≈25 lines: explicit `WHITE_SPACE` strip set,
   per-char lower + `ς→σ`, word-char table exported from Rust into a small
   frozen data file checked in next to the baseline).
4. **Pin the Unicode version** in the contract (16.0, the Rust toolchain's),
   and note that the baseline's word-char/lowercase tables are *exported from
   Rust*, not derived from CPython — that kills class 4 permanently: the
   baseline no longer has an independent Unicode opinion on the two properties
   where independence is spurious.
5. Re-run the role-task rehearsal → binding gates should close for all tasks.

Deliberately NOT recommended: implementing Final_Sigma in Rust (drags
`Cased`/`Case_Ignorable` property tables into the data plane to reproduce a
CPython quirk that harms query consistency anyway).

## Residuals / related observations (non-blocking)

- **Lowercase-before-NFKD quality quirk** (both sides, no divergence):
  math-styled strings like `𝓓𝓮𝓹𝓔𝓭` and `𝐁𝐀𝐀𝐃` produce *mixed-case* tokens
  (`DepEd`, `BAAD`) because styled capitals have no lowercase mapping and NFKD
  runs after lowercasing. A user query "deped" will not match token "DepEd".
  Fix (if desired) is to lowercase again post-NFKD — a semantic change to
  both sides; fold into the same tokenizer-version bump.
- The rehearsal's readiness list named tasks {76, 73, 87, 86, 85, 1}; tasks 15
  and 71 also contain class-1 rows and would have diverged had they been in
  the compared set. No contradiction — the comparison set was 7 role tasks.
- The two remaining Places blockers (dense-task 8 GiB scratch cap; global head
  250k index cap) are unrelated to digests and remain open; separate plan.

## Artifacts

Scratchpad (`/private/tmp/claude-501/…/scratchpad/`): `run_pair.sh`,
`find_rows.py`, `probe_row.py`, `rust_emulated_baseline.py` (prototype fix),
`dump_case.rs` / `dump_alnum.rs` (exhaustive property diffs),
`synthetic_tokens.rs` (Rust tokenizer harness), per-task
`NN/{candidate-report,baseline-report}.json`.
