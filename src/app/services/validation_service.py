# Copyright (c) 2026 lvyafei · https://toolara.dev
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# 非商业授权：可学习、研究、修改、非商业使用；禁止商业使用。详见 LICENSE.

"""常规线 Step 3 的实现类（nodes/normal_validation_node.py）：本轮结果算不算成功。

两个公式 + 一条运营语义，阈值是同文件顶部的常量：

- field_coverage   = 六个契约字段里「有值」的占比；
- confidence_score = coverage * 0.6 + confidence * 0.4；
- 两项都 >= 0.5 才算通过；
- 覆盖率之外再否决一次：本轮工具全失败不能算成功（部分失败只记 tool_failures）。

覆盖率数的是**原始**抽取结果：先过 Pydantic 会被填上默认值，
intent / requires_tool 恒有值，地板被抬到 2/6，就测不出 LLM 到底给了几个字段。
"""

from __future__ import annotations

from src.app.models.schema import ExtractionSchema

PASS_THRESHOLD = 0.5
COVERAGE_WEIGHT = 0.6
CONFIDENCE_WEIGHT = 0.4
SCHEMA_FIELDS: tuple[str, ...] = tuple(ExtractionSchema.model_fields)

_EMPTY = ("", None, [], {})


def _field_coverage(extracted: dict[str, object]) -> float:
    filled = [name for name in SCHEMA_FIELDS if extracted.get(name) not in _EMPTY]
    return round(len(filled) / len(SCHEMA_FIELDS), 2)


def _score(field_coverage: float, confidence: float) -> float:
    return round(field_coverage * COVERAGE_WEIGHT + confidence * CONFIDENCE_WEIGHT, 2)


class ValidationService:
    def validate(self, extracted: dict[str, object], confidence: float,
                 tool_results: dict | None = None) -> dict[str, object]:
        coverage = _field_coverage(extracted)
        score = _score(coverage, confidence)
        is_valid = coverage >= PASS_THRESHOLD and score >= PASS_THRESHOLD
        validation: dict[str, object] = {
            "field_coverage": coverage,
            "confidence_score": score,
            "is_valid": is_valid,
            "reason": ("Validation passed."
                       if is_valid
                       else "Validation failed because coverage or confidence was too low."),
        }

        results = tool_results or {}
        failed = [name for name, out in results.items()
                  if isinstance(out, dict) and not out.get("ok", True)]
        if results and len(failed) == len(results):
            validation["is_valid"] = False
            validation["reason"] = f"本轮 {len(failed)} 个工具全部失败：{', '.join(failed)}"
        elif failed:
            validation["tool_failures"] = failed
        return validation


validation_service = ValidationService()
