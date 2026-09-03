"""指标计算：定位准确率、误报率、错误类型一致性、稳定性。

定义见 docs/method.md §7。两个容易把指标做虚高的地方，这里都按严口径实现：

  · 定位准确率的分母是**全部有误样本**，未检出记为定位失败 —— 而不是只在
    「检出的样本」里算准确率
  · 比例指标一律附 Wilson 95% 置信区间，避免在小样本上宣称不存在的差异
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from ..schema import LabelClass, Sample, Verdict


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson 置信区间。小样本下比正态近似可靠。"""
    if n == 0:
        return (0.0, 0.0)
    p = k / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, centre - half), min(1.0, centre + half))


@dataclass
class Proportion:
    """一个比例指标，带分子分母与置信区间。"""

    numerator: int
    denominator: int

    @property
    def value(self) -> float | None:
        return self.numerator / self.denominator if self.denominator else None

    @property
    def ci(self) -> tuple[float, float]:
        return wilson(self.numerator, self.denominator)

    def to_dict(self) -> dict:
        lo, hi = self.ci
        return {
            "value": self.value,
            "n": self.denominator,
            "k": self.numerator,
            "ci95": [lo, hi] if self.denominator else None,
        }


@dataclass
class LocalizationMetrics:
    detection: Proportion
    exact: Proportion
    tolerant: Proportion

    def to_dict(self) -> dict:
        return {
            "detection_rate": self.detection.to_dict(),
            "exact_localization_acc": self.exact.to_dict(),
            "tolerant_localization_acc_pm1": self.tolerant.to_dict(),
        }


@dataclass
class EvaluationReport:
    n_evaluated: int
    localization: LocalizationMetrics
    false_positive_rate: Proportion
    e9_recall: Proportion
    error_type: dict = field(default_factory=dict)
    by_tier: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "n_evaluated": self.n_evaluated,
            "localization": self.localization.to_dict(),
            "false_positive_rate": self.false_positive_rate.to_dict(),
            "e9_recall": self.e9_recall.to_dict(),
            "error_type": self.error_type,
            "by_tier": self.by_tier,
            "n_missing_verdicts": len(self.missing),
        }


def _dedup_latest(verdicts: list[Verdict]) -> dict[str, Verdict]:
    """同一样本多次评判时取最后一次（稳定性另有专门指标）。"""
    out: dict[str, Verdict] = {}
    for v in verdicts:
        out[v.sample_id] = v
    return out


def compute(
    samples: list[Sample],
    verdicts: list[Verdict],
    *,
    tolerance: int = 1,
) -> EvaluationReport:
    """把评判结果与人工标注对齐，算出全部核心指标。"""
    by_id = {s.id: s for s in samples if s.label is not None}
    latest = _dedup_latest(verdicts)

    det_k = det_n = ex_k = ex_n = 0
    tol_k = 0
    fp_k = fp_n = 0
    e9_k = e9_n = 0
    confusion: Counter[tuple[str, str]] = Counter()
    tier_buckets: dict[str, dict[str, list[int]]] = defaultdict(
        lambda: {"det": [], "exact": [], "fp": []}
    )

    for sid, sample in by_id.items():
        v = latest.get(sid)
        if v is None:
            continue
        label = sample.label
        assert label is not None
        tier = sample.tier.value

        if label.process_correct:
            # 误报率只在「过程无误」的样本上算
            fp_n += 1
            hit = 0 if v.process_valid else 1
            fp_k += hit
            tier_buckets[tier]["fp"].append(hit)
            continue

        # 以下都是「过程有误」的样本
        det_n += 1
        ex_n += 1
        detected = 0 if v.process_valid else 1
        det_k += detected
        tier_buckets[tier]["det"].append(detected)

        exact = int(v.first_error_step == label.first_error_step)
        ex_k += exact
        tier_buckets[tier]["exact"].append(exact)

        if (
            v.first_error_step is not None
            and label.first_error_step is not None
            and abs(v.first_error_step - label.first_error_step) <= tolerance
        ):
            tol_k += 1

        if label.label_class is LabelClass.E9:
            e9_n += 1
            e9_k += detected

        if label.error_type and v.error_type:
            confusion[(label.error_type, v.error_type)] += 1

    correct_types = sum(n for (g, p), n in confusion.items() if g == p)
    total_types = sum(confusion.values())

    return EvaluationReport(
        n_evaluated=len(latest),
        localization=LocalizationMetrics(
            detection=Proportion(det_k, det_n),
            exact=Proportion(ex_k, ex_n),
            tolerant=Proportion(tol_k, ex_n),
        ),
        false_positive_rate=Proportion(fp_k, fp_n),
        e9_recall=Proportion(e9_k, e9_n),
        error_type={
            "accuracy": Proportion(correct_types, total_types).to_dict(),
            "confusion": {f"{g}->{p}": n for (g, p), n in sorted(confusion.items())},
        },
        by_tier={
            tier: {
                "detection_rate": Proportion(sum(b["det"]), len(b["det"])).to_dict(),
                "exact_localization_acc": Proportion(sum(b["exact"]), len(b["exact"])).to_dict(),
                "false_positive_rate": Proportion(sum(b["fp"]), len(b["fp"])).to_dict(),
            }
            for tier, b in sorted(tier_buckets.items())
        },
        missing=sorted(set(by_id) - set(latest)),
    )


def stability(verdicts: list[Verdict]) -> dict:
    """稳定性：同一样本多次评判的众数占比与错误类型一致率。"""
    grouped: dict[str, list[Verdict]] = defaultdict(list)
    for v in verdicts:
        grouped[v.sample_id].append(v)

    repeated = {k: vs for k, vs in grouped.items() if len(vs) > 1}
    if not repeated:
        return {"n_repeated_samples": 0, "note": "没有重复评估的样本"}

    step_ratios, type_ratios = [], []
    for vs in repeated.values():
        steps = Counter(v.first_error_step for v in vs)
        step_ratios.append(steps.most_common(1)[0][1] / len(vs))
        types = Counter(v.error_type for v in vs)
        type_ratios.append(types.most_common(1)[0][1] / len(vs))

    return {
        "n_repeated_samples": len(repeated),
        "mean_repeats": sum(len(v) for v in repeated.values()) / len(repeated),
        "step_mode_ratio": sum(step_ratios) / len(step_ratios),
        "error_type_agreement": sum(type_ratios) / len(type_ratios),
    }
