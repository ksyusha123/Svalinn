"""Delay scenario defaults for the shared experiment runner."""

from run_experiment import parser, run
from runner_common import main_guard


def main():
    p = parser()
    p.set_defaults(scenario="SuccessDisaster", workload="Delay", warmup_requests=5)
    a = p.parse_args()
    if a.workload != "Delay":
        p.error("Этот runner предназначен для --workload Delay.")
    if a.scenario == "SuccessDisaster" and a.rates is None:
        a.rates = [5, 10, 20, 40, 60, 80]
    elif a.scenario == "LowFlood" and a.low_rates is None:
        a.low_rates = [5, 10, 20, 40, 60, 80]
    run(a)


if __name__ == "__main__":
    main_guard(main)
