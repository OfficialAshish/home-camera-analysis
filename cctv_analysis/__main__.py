"""Command line:  python -m cctv_analysis --input /videos --output /results [--config cfg.json] [key=value ...]"""
import argparse
import json

from .config import Config
from .pipeline import run


def main():
    ap = argparse.ArgumentParser(description="CCTV footage analysis")
    ap.add_argument("--input", required=False)
    ap.add_argument("--output", required=False)
    ap.add_argument("--work", default=None)
    ap.add_argument("--config", default=None, help="JSON file with Config fields")
    ap.add_argument("--reports-only", action="store_true", help="rebuild reports from the database")
    ap.add_argument("overrides", nargs="*", help="field=value (value parsed as JSON when possible)")
    a = ap.parse_args()
    d = {}
    if a.config:
        with open(a.config) as fh:
            d.update(json.load(fh))
    for kv in a.overrides:
        k, v = kv.split("=", 1)
        try:
            d[k] = json.loads(v)
        except json.JSONDecodeError:
            d[k] = v
    if a.input:
        d["input_dir"] = a.input
    if a.output:
        d["output_dir"] = a.output
    if a.work:
        d["work_dir"] = a.work
    cfg = Config.from_dict(d)
    if a.reports_only:
        from .pipeline import Analyzer
        from .report import build_reports
        an = Analyzer(cfg)
        print(build_reports(an.store, cfg))
        an.store.commit()
        return
    print(run(cfg))


if __name__ == "__main__":
    main()
