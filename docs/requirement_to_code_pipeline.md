# Requirement -> Spec -> Code -> Verify 管线说明（含增量增强）

本文档说明基于 AutoSpec 重构后的新流程，并给出可直接用于 ablation 的两种变体：

- `base`：最小可运行基线（单轮）
- `enhanced`：在 `base` 上增加轻量增强模块（多轮但可控）

目标是保持实验可复现、对比清晰，而不是一次性推翻原有管线。

## 1. 任务定义

我们定义端到端任务为：

`Requirement -> Spec -> Code -> Verify`

输入：

- 自然语言需求 `r`

输出：

- 形式化规格 `s`（ACSL）
- 程序 `c`（C）

优化目标是双目标：

1. `c |= s`（形式化验证通过）
2. `s ~= r`（规格与需求语义对齐）

> 核心点：不仅追求 verify pass，也要提升 requirement-spec 的覆盖与对齐质量。

## 2. Base Pipeline（Ablation-0）

当前基线流程：

`r --(LLM)--> s --(LLM)--> c --(Frama-C/WP)--> verify`

特征：

- 无结构化约束抽取
- 无 requirement-spec 对齐检查
- 无 verify 失败后的代码修复循环

这条线保留不动，作为稳定对照组。

## 3. Enhanced Pipeline（增量增强，不替换 base）

增强版在 base 之上增加三段轻量模块：

### 3.1 Requirement -> Constraint Extraction

先把需求拆成结构化约束：

`r -> {preconditions, postconditions, invariants}`

并可给出一个 `function_signature` 候选。输出为 JSON，便于后续评估和复用。

### 3.2 Constraint -> ACSL Mapping

将结构化约束映射为 ACSL（`requires/assigns/ensures`）。

相比“直接从 requirement 一步出 ACSL”，这种两阶段方式更稳健：

- 降低 hallucination
- 提高约束覆盖率
- 便于后续做缺失约束分析

### 3.3 Spec Self-Check + Refine（1~2 轮）

加入轻量对齐检查：

- 输入：`requirement + constraints + spec`
- 输出：`is_aligned / missing_constraints / inconsistent_items / refinement_hints`

若发现缺失或不一致，则自动 refinement（轮数可配）。

形成：

`generate -> check -> refine`

### 3.4 Verification-guided Code Repair（3~5 轮）

`spec -> c0 -> verify`

若验证失败，则把错误信息回灌给 LLM 修复代码：

`c0 -> verify -> c1 -> verify -> ...`

默认设置最大迭代次数，避免无限循环。

## 4. 实现位置

- `autospec/pipeline/requirement_pipeline.py`
  - `RequirementToCodePipeline`：base（保持原逻辑）
  - `EnhancedRequirementToCodePipeline`：enhanced（新增）
- `scripts/run_requirement_pipeline.py`
  - 增加 `--pipeline-variant` 与增强参数
- `scripts/run_openrouter_requirement_pipeline.sh`
  - 增加环境变量控制 enhanced 参数

## 5. 数据格式

需求数据（数组）示例：

```json
[
  {
    "id": 1,
    "path": "pointers/swap.c",
    "requirement_en": "Given pointers to two integers, swap their stored values in place."
  }
]
```

文本字段读取优先级：

1. `requirement_zh`
2. `requirement_en`
3. `requirement`

## 6. 输出目录

当 `--output-dir outputs/req2code` 时：

- `outputs/req2code/specs/<relative_path>.json`
- `outputs/req2code/code/<relative_path>.c`
- `outputs/req2code/reports/results.partial.json`
- `outputs/req2code/reports/results.json`
- `outputs/req2code/reports/<relative_path>.verify.log`

Enhanced 额外信息会写入 `specs/*.json` 与 `results.json`，包括：

- `constraints`
- `alignment_check`
- `repair_attempts` / `repair_history`
- 中间失败日志（`*.repairN.verify.log`）

## 7. 运行方式

### 7.1 运行 base（推荐先跑这个做对照）

```bash
PYTHONPATH=. python3 scripts/run_requirement_pipeline.py \
  --requirements-file benchmarks/frama-c-problems/requirements/requirements_51.json \
  --output-dir outputs/req2code-base \
  --endpoint https://openrouter.ai/api/v1/chat/completions \
  --model deepseek/deepseek-v3.2 \
  --api-key-env OPENROUTER_API_KEY \
  --pipeline-variant base \
  --verify-timeout 120
```

### 7.2 运行 enhanced（增量增强）

```bash
PYTHONPATH=. python3 scripts/run_requirement_pipeline.py \
  --requirements-file benchmarks/frama-c-problems/requirements/requirements_51.json \
  --output-dir outputs/req2code-enhanced \
  --endpoint https://openrouter.ai/api/v1/chat/completions \
  --model deepseek/deepseek-v3.2 \
  --api-key-env OPENROUTER_API_KEY \
  --pipeline-variant enhanced \
  --spec-self-check-rounds 1 \
  --code-repair-max-iter 3 \
  --verify-timeout 120
```

### 7.3 只跑单个任务（调试 prompt 最常用）

```bash
PYTHONPATH=. python3 scripts/run_requirement_pipeline.py \
  --task-id 17 \
  --requirements-file benchmarks/frama-c-problems/requirements/requirements_51.json \
  --output-dir outputs/req2code-debug \
  --pipeline-variant enhanced
```

### 7.4 跳过验证（仅生成）

```bash
PYTHONPATH=. python3 scripts/run_requirement_pipeline.py --skip-verify
```

## 8. OpenRouter 一键脚本

`scripts/run_openrouter_requirement_pipeline.sh` 支持以下变量：

- `PIPELINE_VARIANT=base|enhanced`
- `SPEC_SELF_CHECK_ROUNDS`（enhanced 生效）
- `CODE_REPAIR_MAX_ITER`（enhanced 生效）
- `TASK_ID`、`SKIP_VERIFY`、`MODEL` 等

示例：

```bash
PIPELINE_VARIANT=enhanced \
SPEC_SELF_CHECK_ROUNDS=1 \
CODE_REPAIR_MAX_ITER=3 \
OUTPUT_DIR=outputs/req2code-openrouter-enhanced \
./scripts/run_openrouter_requirement_pipeline.sh
```

## 9. 实验与对比建议（论文友好）

建议至少报告三组：

1. `base`
2. `enhanced`（仅开 spec self-check，不开 code repair）
3. `enhanced`（spec self-check + code repair 全开）

关键指标：

- verify pass rate
- 平均修复轮数
- requirement-spec 缺失约束数（来自 `alignment_check`）
- 每题 token/时延成本（可选）

## 10. 当前边界与后续可选增强

当前增强仍保持“轻量改造”原则，尚未做：

- spec-aware code skeleton（如 `requires` -> 显式检查模板）
- requirement 与 spec 的独立判别器评分
- 更细粒度的约束类型标签（安全性、边界、单调性等）

这些可以作为下一阶段增量，不影响现有 base/enhanced 可比性。
