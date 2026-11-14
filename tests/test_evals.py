from pathlib import Path

from careflow.evals import runner


async def test_offline_evals_pass_all_gates(tmp_path: Path) -> None:
    results, summary = await runner.run(data_dir=tmp_path)
    assert len(results) == summary["scenarios"] > 15
    assert runner.failed_gates(summary) == [], runner.render(results, summary)


def test_gates_catch_regressions() -> None:
    summary = {
        "red_flag_recall": 0.75,
        "routing_accuracy": 1.0,
        "intake_accuracy": 1.0,
        "unsafe_writes": 1,
    }
    failed = runner.failed_gates(summary)
    assert len(failed) == 2
