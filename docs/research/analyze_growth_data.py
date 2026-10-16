"""Exploratory growth analysis, separate from application implementation.

Requires public OSF files downloaded into the inspect script's temporary cache.
Outputs only aggregate results; never reads the application database.
This is not a replication of any paper and not a validated scoring model.
"""
import hashlib
import json
from pathlib import Path
import tempfile

import numpy as np
import pandas as pd
from openpyxl import load_workbook

CACHE = Path(tempfile.gettempdir()) / "web_app_growth_research_20261007"
OUTPUT = Path(__file__).parent / "growth_analysis_results.json"
DATA = CACHE / "openipf-2021-08-04.csv"


def regression_metrics(y, prediction):
    residual = y - prediction
    sst = np.sum((y - y.mean()) ** 2)
    return {"mae_kg": float(np.abs(residual).mean()),
            "rmse_kg": float(np.sqrt(np.mean(residual ** 2))),
            "r_squared": float(1 - np.sum(residual ** 2) / sst)}


def analyze_powerlifting():
    cols = ["Name", "Sex", "Event", "Equipment", "Division", "Age", "BodyweightKg",
            "Best3SquatKg", "Best3BenchKg", "Best3DeadliftKg", "TotalKg", "Tested", "Date"]
    retained = []
    source_rows = 0
    for chunk in pd.read_csv(DATA, usecols=cols, chunksize=100000, low_memory=False):
        source_rows += len(chunk)
        mask = ((chunk.Event == "SBD") & (chunk.Equipment == "Raw") &
                (chunk.Division == "Open") & (chunk.Tested == "Yes") &
                (chunk.Best3SquatKg > 0) & (chunk.Best3BenchKg > 0) & (chunk.Best3DeadliftKg > 0))
        retained.append(chunk[mask])
    d = pd.concat(retained, ignore_index=True)
    d["id"] = d.Name + "|" + d.Sex
    d["date"] = pd.to_datetime(d.Date)
    # Deduplicate same-date records by competitive total.
    d = d.sort_values(["id", "date", "TotalKg"]).drop_duplicates(["id", "date"], keep="last")
    d = d[d.groupby("id").id.transform("size") >= 3].copy()
    retained_rows = len(d)
    retained_lifters = d.id.nunique()
    d["career_weeks"] = (d.date - d.groupby("id").date.transform("min")).dt.days / 7
    d["next_date"] = d.groupby("id").date.shift(-1)
    d["interval_days"] = (d.next_date - d.date).dt.days
    for lift in ("Squat", "Bench", "Deadlift"):
        d[f"end_{lift}"] = d.groupby("id")[f"Best3{lift}Kg"].shift(-1)
    intervals = d[(d.interval_days >= 28) & (d.interval_days <= 400) &
                  d.Age.between(18, 60) & d.BodyweightKg.between(30, 200) &
                  d.Sex.isin(["M", "F"])].copy()
    train_ids = {key for key in intervals.id.unique()
                 if int(hashlib.sha256(key.encode()).hexdigest()[:8], 16) % 10 < 7}
    train = intervals.id.isin(train_ids).to_numpy()
    report = {"source_url": "https://osf.io/download/ruwda/", "source_bytes": DATA.stat().st_size,
              "source_sha256": hashlib.sha256(DATA.read_bytes()).hexdigest(),
              "source_rows": source_rows, "retained_records": retained_rows,
              "retained_lifters": retained_lifters, "intervals": len(intervals),
              "interval_lifters": intervals.id.nunique(),
              "train_intervals": int(train.sum()), "test_intervals": int((~train).sum()),
              "train_lifters": intervals[train].id.nunique(), "test_lifters": intervals[~train].id.nunique(),
              "one_month_intervals_28_to_45_days": int(intervals.interval_days.between(28, 45).sum()),
              "lifts": {}}
    for lift in ("Squat", "Bench", "Deadlift"):
        baseline = intervals[f"Best3{lift}Kg"].to_numpy()
        end = intervals[f"end_{lift}"].to_numpy()
        bw = intervals.BodyweightKg.to_numpy()
        relative = baseline / bw
        period = np.log1p(intervals.interval_days.to_numpy() / 30.4375)
        career = np.log1p(intervals.career_weeks.to_numpy() / 52)
        age = (intervals.Age.to_numpy() - 30) / 10
        sex = (intervals.Sex.to_numpy() == "F").astype(float)
        x = np.column_stack([np.ones(len(intervals)), np.log(relative), np.log(bw / 75), age, sex,
                             period, career, np.log(relative) * period, age * period,
                             sex * period, career * period])
        log_growth = np.log(end / baseline)
        # Equal total training weight for each lifter, despite repeated intervals.
        weights = 1 / intervals.groupby("id").id.transform("size").to_numpy()
        root_weight = np.sqrt(weights[train])
        coefficient = np.linalg.lstsq(x[train] * root_weight[:, None],
                                     log_growth[train] * root_weight, rcond=None)[0]
        predicted_gain = baseline * np.expm1(x @ coefficient)
        actual_gain = end - baseline
        y = actual_gain[~train]
        p = predicted_gain[~train]
        result = {"no_change_gain_metrics": regression_metrics(y, np.zeros(len(y))),
                  "growth_model_gain_metrics": regression_metrics(y, p),
                  "no_change_end_performance_metrics": regression_metrics(end[~train], baseline[~train]),
                  "growth_model_end_performance_metrics": regression_metrics(end[~train], baseline[~train] + p),
                  "median_test_gain_kg": float(np.median(y)),
                  "test_gain_iqr_kg": [float(v) for v in np.quantile(y, [0.25, 0.75])],
                  "test_median_predicted_gain_kg": float(np.median(p)),
                  "test_nonpositive_expected_gain_fraction": float((p <= 0).mean()),
                  "coefficient_order": ["intercept", "ln_start_per_bw", "ln_bw_per_75", "age_minus_30_per_10",
                                        "female", "ln_1_plus_months", "ln_1_plus_career_years",
                                        "ln_start_per_bw_times_period", "age_times_period", "female_times_period",
                                        "career_times_period"],
                  "exploratory_coefficients": [float(v) for v in coefficient]}
        sample = intervals[intervals.interval_days.between(180, 365)].copy()
        sample["relative"] = sample[f"Best3{lift}Kg"] / sample.BodyweightKg
        sample["gain"] = sample[f"end_{lift}"] - sample[f"Best3{lift}Kg"]
        sample["quartile"] = sample.groupby("Sex").relative.transform(lambda v: pd.qcut(v, 4, labels=False, duplicates="drop"))
        result["six_to_twelve_month_descriptive_quartiles"] = [
            {"sex": str(sex_value), "quartile": int(q) + 1, "intervals": len(group),
             "median_start_per_bw": float(group.relative.median()), "median_gain_kg": float(group.gain.median()),
             "median_interval_days": float(group.interval_days.median())}
            for (sex_value, q), group in sample.groupby(["Sex", "quartile"])]
        if lift == "Bench":
            result["start_below_0_8_bw_intervals"] = int((relative < .8).sum())
            result["start_below_0_6_bw_intervals"] = int((relative < .6).sum())
        report["lifts"][lift] = result
    return report


def analyze_meta_data():
    workbook = load_workbook(CACHE / "dose_extracted.xlsx", read_only=True, data_only=True)
    rows = iter(workbook["Data"].values)
    headers = next(rows)
    frame = pd.DataFrame(rows, columns=headers)
    strength = frame[frame.outcome.astype(str).str.lower() == "strength"].copy()
    for col in ("pre.mean", "post.mean", "body.mass", "years.exp", "weeks"):
        strength[col] = pd.to_numeric(strength[col], errors="coerce")
    bench = strength[strength.measure == "Bench Press 1RM"].copy()
    bench = bench[(bench["pre.mean"] > 0) & (bench["post.mean"] > 0)]
    bench = bench.drop_duplicates(["number", "group", "pre.mean", "post.mean", "weeks"])
    bench["gain_pct"] = 100 * (bench["post.mean"] / bench["pre.mean"] - 1)
    bench_summary = [{"status": str(status), "group_rows": len(group),
                      "study_ids": int(group.number.nunique()),
                      "median_weeks": float(group.weeks.median()),
                      "median_group_growth_percent": float(group.gain_pct.median())}
                     for status, group in bench.groupby("train.status")]
    return {"source_url": "https://osf.io/download/86g9r/", "rows": len(frame),
            "strength_rows": len(strength), "strength_studies": strength.number.nunique(),
            "strength_measures": sorted(strength.measure.dropna().unique().tolist()),
            "strength_median_weeks": float(strength.weeks.median()),
            "strength_max_weeks": float(strength.weeks.max()),
            "strength_missing_body_mass_fraction": float(strength["body.mass"].isna().mean()),
            "strength_missing_years_exp_fraction": float(strength["years.exp"].isna().mean()),
            "strength_training_status_counts": strength["train.status"].value_counts().to_dict(),
            "bench_group_descriptive_summary": bench_summary,
            "unit_of_data": "Study-group-outcome means, not individual participants; rows are dependent."}


if __name__ == "__main__":
    result = {"analysis_date": "2026-10-07", "powerlifting": analyze_powerlifting(),
              "meta_extracted_data": analyze_meta_data(),
              "limitations": ["Exploratory, no external validation or hyperparameter selection.",
                              "Competition participation is not training experience.",
                              "Selection requires at least three meets and successful SBD; survivorship bias.",
                              "Name and sex are imperfect identifiers; no app users or private data included.",
                              "Intervals are repeated observations; metrics weight test intervals equally.",
                              "No causal interpretation, novice-generalization, or competition fairness claim."]}
    OUTPUT.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(OUTPUT),
                      "powerlifting_intervals": result["powerlifting"]["intervals"],
                      "strength_extraction_rows": result["meta_extracted_data"]["strength_rows"],
                      "bench_groups": result["meta_extracted_data"]["bench_group_descriptive_summary"]},
                     ensure_ascii=True))
