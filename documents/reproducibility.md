# Reproducibility manifest (issues.md #69)

**Problem.** `graygoo.asd` declares zero external dependencies, but a
GrayGoo generation is really defined by five things together: the Lisp
tree, the Python harness, the SBCL binary, the OS sandbox posture, and
the model/provider configuration. "Generation X" was not reproducible
from the ASDF system alone.

**Fix.** `manifest.py` (repo root) collects all five into one
JSON-serializable dict:

| Section | Contents |
|---|---|
| `python` | version + implementation |
| `platform` | OS / release / machine |
| `git` | SHA, branch, dirty flag (best effort) |
| `sbcl` | resolved exe + `--version` first line |
| `asdf` | `graygoo.asd` SHA-256, declared deps (none) |
| `source_tree` | `fingerprint.compute_string()` over the tree |
| `deps` | installed `openai` / `python-dotenv` versions |
| `model` | provider, base URL, model id, list prices — **never keys** |
| `sandbox` | declared enforcement posture (honest: OS enforcement `false`) |

Commands:

```text
python manifest.py                # print manifest JSON
python manifest.py --write out.json
python manifest.py --check        # exit 0 iff required keys present
```

`--check` is CI gate 5. Tests in `tests/test_manifest.py` (9 tests)
pin the required keys, JSON round-trip, and — critically — that the
live `CEREBRAS_API_KEY` (when present in the environment) never
appears in the rendered manifest.

**Reproducing a run.** Given a manifest: check out the `git.sha`,
install the pinned SBCL + Python, `pip install openai python-dotenv`,
set `GRAYGOO_SBCL`, and run the five CI gates. Bit-identical model
outputs are NOT promised (provider-side nondeterminism); what is
promised is the identical code + runtime + model configuration, which
is what every experiment report in `documents/` now cites.
