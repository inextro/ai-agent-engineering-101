"""Re-run ONE harness N times and append to the same results.csv / logs/.

run_ab.py always runs both harnesses. Runs 5 and 6 died on the OpenRouter
free-tier daily cap (50 requests/day), and one plan_exec run costs about 9.5x
the requests of one react run, so topping up the missing plan_exec runs with
run_ab.py would spend most of the budget re-running react for no reason.

Everything that decides a result — the task, the success criterion, the judge,
the row format, the log naming — is imported from run_ab.py so this stays the
same experiment.

Usage: python rerun_one.py plan_exec --runs 2
"""
import argparse
import csv
import time
from pathlib import Path

from harness_plan_execute import run_plan_execute
from harness_react import run_react
from run_ab import HEADER, judge, read_task

HARNESSES = {"react": run_react, "plan_exec": run_plan_execute}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("harness", choices=sorted(HARNESSES))
    ap.add_argument("--runs", type=int, default=1)
    args = ap.parse_args()

    name = args.harness
    fn = HARNESSES[name]
    task, expected = read_task()
    Path("logs").mkdir(exist_ok=True)

    # continue the existing numbering; results.csv must already have its header
    with open("results.csv", encoding="utf-8") as f:
        run_no = sum(1 for _ in f) - 1

    with open("results.csv", "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        for _ in range(args.runs):
            run_no += 1
            lines = []

            def log(msg, _lines=lines):
                print(msg)
                _lines.append(str(msg))

            t0 = time.time()
            note = ""
            try:
                out = fn(task, log=log)
                answer, meter = out[0], out[1]
                if name == "plan_exec":
                    note = f"replans={out[2]}"
            except Exception as e:          # a crash is a failed run, not a lost run
                answer, meter, note = "", None, f"crash: {type(e).__name__}: {e}"
                log(note)
            success = judge(answer, expected)
            log(f"[final] {answer.strip()[:300]}")
            log(f"[judge] expected={expected!r} -> {'O' if success else 'X'} "
                f"({time.time() - t0:.1f}s)")

            Path("logs", f"{name}-{run_no:02d}.txt").write_text(
                "\n".join(lines) + "\n", encoding="utf-8")
            w.writerow([run_no, name, "O" if success else "X",
                        meter.tokens if meter else "",
                        meter.iters if meter else "",
                        meter.interventions if meter else "", note])
            f.flush()


if __name__ == "__main__":
    main()
