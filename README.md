# AutoSpec

Automated Specification Generation for C Programs using LLMs and Frama-C verification.

## Overview

AutoSpec is a tool that automatically generates and verifies ACSL (ANSI/ISO C Specification Language) specifications for C programs. It combines:

- **Static Analysis**: Decompose C programs into verifiable components
- **LLM-based Generation**: Generate ACSL contracts / loop annotations via an LLM
- **Formal Verification**: Verify specifications using Frama-C's WP (Weakest Precondition) plugin
- **Iterative Refinement**: Strengthen or weaken specifications based on verification feedback

_**AutoSpec currently supports verification of the frama-c-problems benchmark suite, with x509-parser support planned for future releases.**_

## Installation

### macOS (Intel + Apple Silicon) Quick Start

> [!NOTE]
> - Docker Desktop must be running (`docker info` should succeed).
> - On Apple Silicon (M1/M2/M3), this project currently runs most reliably with `linux/amd64` images.

1. **Build the Docker image (recommended for local development):**

```bash
docker build --platform linux/amd64 -t autospec:dev .
```

If you prefer the published image:

```bash
docker pull --platform linux/amd64 junjiehu1905/autospec:latest
```

2. **Run the container (macOS-friendly flags):**

```bash
# If you built locally:
docker run -dit --name autospec \
  --platform linux/amd64 \
  -v "$(pwd)":/workspace \
  -p 8000:8000 \
  autospec:dev

# If you pulled published image instead:
docker run -dit --name Spec \
  --platform linux/amd64 \
  -v "$(pwd)":/workspace \
  -p 8000:8000 \
  junjiehu1905/autospec:latest

docker exec -it autospec /bin/bash
```

> [!NOTE]
> - The README assumes the repo is mounted at `/workspace` inside the container.
> - `--network host` is intentionally not used (not supported on Docker Desktop for macOS).
> - `--gpus all` is Linux/NVIDIA specific and is not available on most Macs.

3. **Verify the installation:**

```bash
./scripts/run_frama_c_problems.sh
```

This will run verification on benchmarks in `benchmarks/frama-c-problems/ground-truth` to verify that Frama-C and AutoSpec are working correctly.

## Automated Spec Generation (OpenRouter or vLLM)

This is the core end-to-end workflow: **LLM → insert ACSL → verify**.

### 1) Use OpenRouter (recommended)

Set credentials in a terminal inside Docker:

```bash
export OPENROUTER_API_KEY=sk-or-xxxx
# Optional headers recommended by OpenRouter:
export OPENROUTER_SITE_URL=https://your-site.example
export OPENROUTER_APP_NAME=AutoSpec
```

Run generation + verification using the helper script:

```bash
./scripts/run_openrouter_gen.sh
```

You can override model and other options inline:

```bash
MODEL=anthropic/claude-3.5-sonnet \
INPUT_DIR=benchmarks/frama-c-problems/test-inputs \
OUTPUT_DIR=outputs/annotated-openrouter \
VERIFY_TIMEOUT=120 \
./scripts/run_openrouter_gen.sh
```

### 2) (Optional) Use local vLLM instead of OpenRouter

Start a local OpenAI-compatible server:

```bash
python3 -m vllm.entrypoints.openai.api_server \
  --model Qwen/Qwen3-32B \
  --port 8000 \
  --dtype auto
```

Then call `gen_specs.py` directly:

```bash
PYTHONPATH=. python3 scripts/gen_specs.py \
  --input-dir benchmarks/frama-c-problems/test-inputs \
  --output-dir outputs/annotated \
  --model Qwen/Qwen3-32B \
  --endpoint http://localhost:8000/v1/chat/completions \
  --api-key-env OPENAI_API_KEY \
  --verify
```

> [!NOTE]
> - On macOS, local vLLM in Docker is often CPU-only and can be slow.
> - If your endpoint requires auth, set the matching key env var:
>   - OpenRouter: `OPENROUTER_API_KEY`
>   - Other OpenAI-compatible providers: use `--api-key-env YOUR_ENV_NAME`

Key details for generation:
- The script recursively processes all `.c` files under `--input-dir`.
- It annotates one function/loop at a time by inserting a returned `/*@ ... */` block immediately before the target node.
- With `--verify`, it calls the AutoSpec verifier on each produced file.
- If verification fails, it automatically enters a **final feedback correction loop** (up to 3 attempts) where Frama-C/WP output is fed back to the LLM to repair **ACSL only**.

### 3) Check results (verify an outputs directory)

After generation, you can verify (or re-verify) everything under an output directory:

```bash
./scripts/run_frama_c_problems.sh -d outputs/annotated-openrouter #(add -v to get verbose outputs)
./scripts/run_frama_c_problems.sh -d outputs/req2code-openrouter/code #(add -v to get verbose outputs)

```

## Requirement → Spec → Code Pipeline (new, minimal V1)

This pipeline supports a more realistic setup:
**natural-language requirement → ACSL generation → C code generation → Frama-C verification**.

Input dataset (51 requirements aligned with `test-inputs`):
- `benchmarks/frama-c-problems/requirements/requirements_51.json`

Run:

```bash
export OPENROUTER_API_KEY=sk-or-xxxx

PYTHONPATH=. python3 scripts/run_requirement_pipeline.py \
  --requirements-file benchmarks/frama-c-problems/requirements/requirements_51.json \
  --output-dir outputs/req2code \
  --endpoint https://openrouter.ai/api/v1/chat/completions \
  --model deepseek/deepseek-v3.2 \
  --api-key-env OPENROUTER_API_KEY \
  --verify-timeout 120
```

Output layout:
- `outputs/req2code/specs/...` (LLM-generated spec artifacts)
- `outputs/req2code/code/...` (LLM-generated C+ACSL files)
- `outputs/req2code/reports/results.json` (batch summary)

Design and limitations are documented in:
- `docs/requirement_to_code_pipeline.md`

## Results

Below is a side-by-side comparison on **`benchmarks/frama-c-problems/test-inputs` (51 programs)**:

| Metric | AutoSpec (initial) | AutoSpec + final feedback loop |
|---|---:|---:|
| Programs passed | 21 | 24 |
| Programs failed | 30 | 27 |
| Pass rate | 41.2% | 47.1% |

**What is the “final feedback loop”?** After a failed verification run, we feed the Frama-C/WP error output back into the LLM and ask it to **repair ACSL only** (no C code changes), then re-verify.

### Per-program comparison (51 programs)

<details>
<summary>Click to expand</summary>

| Program | AutoSpec | + Final feedback |
|---|:---:|:---:|
| `arrays_and_loops/1.c` | PASS | FAIL |
| `arrays_and_loops/2.c` | FAIL | PASS |
| `arrays_and_loops/3.c` | PASS | PASS |
| `arrays_and_loops/4.c` | FAIL | FAIL |
| `arrays_and_loops/5.c` | FAIL | FAIL |
| `general_wp_problems/absolute_value.c` | PASS | PASS |
| `general_wp_problems/add.c` | PASS | PASS |
| `general_wp_problems/ani.c` | FAIL | FAIL |
| `general_wp_problems/diff.c` | PASS | PASS |
| `general_wp_problems/gcd.c` | PASS | PASS |
| `general_wp_problems/max_of_2.c` | PASS | PASS |
| `general_wp_problems/power.c` | FAIL | FAIL |
| `general_wp_problems/simple_interest.c` | PASS | PASS |
| `general_wp_problems/swap.c` | PASS | PASS |
| `general_wp_problems/triangle_angles.c` | FAIL | FAIL |
| `general_wp_problems/triangle_sides.c` | PASS | PASS |
| `general_wp_problems/wp1.c` | FAIL | FAIL |
| `immutable_arrays/array_sum.c` | FAIL | FAIL |
| `immutable_arrays/binary_search.c` | FAIL | FAIL |
| `immutable_arrays/check_evens_in_array.c` | PASS | PASS |
| `immutable_arrays/max.c` | FAIL | PASS |
| `immutable_arrays/occurences_of_x.c` | FAIL | FAIL |
| `immutable_arrays/sample.c` | FAIL | FAIL |
| `immutable_arrays/search.c` | PASS | PASS |
| `immutable_arrays/search_2.c` | PASS | PASS |
| `loops/1.c` | FAIL | FAIL |
| `loops/2.c` | FAIL | FAIL |
| `loops/3.c` | PASS | FAIL |
| `loops/4.c` | FAIL | PASS |
| `loops/fact.c` | FAIL | FAIL |
| `loops/mult.c` | FAIL | FAIL |
| `loops/sum_digits.c` | FAIL | FAIL |
| `loops/sum_even.c` | FAIL | FAIL |
| `miscellaneous/array_find.c` | PASS | PASS |
| `miscellaneous/array_max_advanced.c` | FAIL | FAIL |
| `miscellaneous/array_swap.c` | PASS | PASS |
| `miscellaneous/increment_arr.c` | FAIL | FAIL |
| `miscellaneous/max_of_2.c` | PASS | PASS |
| `more_arrays/equal_arrays.c` | FAIL | PASS |
| `more_arrays/replace_evens.c` | FAIL | FAIL |
| `more_arrays/reverse_array.c` | FAIL | FAIL |
| `mutable_arrays/array_double.c` | FAIL | FAIL |
| `mutable_arrays/bubble_sort.c` | FAIL | FAIL |
| `pointers/add_pointers.c` | FAIL | FAIL |
| `pointers/add_pointers_3_vars.c` | FAIL | FAIL |
| `pointers/div_rem.c` | PASS | PASS |
| `pointers/incr_a_by_b.c` | FAIL | PASS |
| `pointers/max_pointers.c` | PASS | PASS |
| `pointers/order_3.c` | FAIL | FAIL |
| `pointers/reset_1st.c` | PASS | PASS |
| `pointers/swap.c` | PASS | PASS |

</details>

## How to Run Verification Manually (Frama-C / WP)

### Running Ground Truth Benchmarks

```bash
# Run all benchmark categories
./scripts/run_frama_c_problems.sh
```

> [!NOTE]
> If you are not already inside the container shell, enter it first:
>
> ```bash
> docker exec -it autospec /bin/bash
> ```
> 
> If `frama-c` is not found inside the container, run:
>
> ```bash
> opam init
> eval $(opam env)
> ```
>
> AutoSpec now includes compatibility fallback for older Frama-C releases (e.g. 26.x):
> if `-generated-spec-custom terminates:skip` is unsupported, verification is retried
> automatically without that option.

Verify a single file (ground truth or your own C file):

```bash
python3 -m autospec.cli.main verify benchmarks/frama-c-problems/ground-truth/loops/1.c --verbose
```

### Custom Timeout

```bash
python3 -m autospec.cli.main verify file.c --timeout 120
```

### CLI Help

```bash
python3 -m autospec.cli.main --help
python3 -m autospec.cli.main verify --help
```

**Benchmark Suites:**

AutoSpec includes comprehensive benchmark suites for evaluation:

```bash
# Run all benchmarks (frama-c-problems + x509-parser)
./scripts/run_all_benchmarks.sh

# Run only frama-c-problems (~51 programs)
./scripts/run_all_benchmarks.sh -o frama-c

# Skip x509-parser for faster testing
./scripts/run_all_benchmarks.sh -s

# Test specific category
./scripts/run_frama_c_problems.sh loops
./scripts/run_frama_c_problems.sh arrays_and_loops -v

# Test x509-parser only
./scripts/run_x509_parser.sh
```

See [benchmarks/README.md](benchmarks/README.md) for detailed documentation.


### Adding New C Programs

1. Create a C file under `benchmarks/frama-c-problems/` (or anywhere you like).
2. Add ACSL annotations (preconditions, postconditions, loop invariants).
3. Verify with AutoSpec:

```bash
python3 -m autospec.cli.main verify benchmarks/frama-c-problems/your_file.c
```

### Example ACSL Annotation

```c
/*@
  @ requires n > 0;
  @ requires \valid_read(arr + (0..n-1));
  @ ensures \result >= arr[0];
  @ ensures \forall integer i; 0 <= i < n ==> \result >= arr[i];
  @*/
int array_max(int *arr, int n) {
    // ... implementation
}
```

## Configuration

Edit `autospec/config.py` to customize:

- `FRAMA_C_COMMAND`: Path to Frama-C executable
- `FRAMA_C_TIMEOUT`: Overall verification timeout (default: 10s)
- `FRAMA_C_WP_TIMEOUT`: Per-proof timeout (default: 10s)
- `LOG_LEVEL`: Logging verbosity

Or use environment variables:

```bash
export FRAMA_C_TIMEOUT=120
export FRAMA_C_WP_TIMEOUT=20
export VERBOSE=true
```
