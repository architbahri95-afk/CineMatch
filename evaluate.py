"""Offline evaluation report:  python evaluate.py [repeats]

Holds out 20% of each active user's ratings (over several random splits) and compares rating
predictors (RMSE / MAE) and top-K recommenders (precision, recall, F1, NDCG, hit rate, coverage).
Writes evaluation_report.json and prints markdown tables you can paste into a report."""
import json
import os
import sys
import tempfile

from app import db
from app.recommender import Recommender

repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 5
path = os.path.join(tempfile.mkdtemp(), "eval.db")
db.init_db(path)
ev = Recommender(lambda: db.connect(path)).evaluate(repeats=repeats)
with open("evaluation_report.json", "w") as f:
    json.dump(ev, f, indent=2)

c = ev["config"]
print(f"{c['users']} users per split, {c['repeats']} random 80/20 splits, relevant = held-out rating >= {c['relevant']:g}\n")
print("| Method | RMSE | MAE |\n|---|---|---|")
for k, v in ev["rating_prediction"].items():
    print(f"| {k} | {v['rmse']:.3f} ± {v['rmse_std']:.3f} | {v['mae']:.3f} ± {v['mae_std']:.3f} |")
for k in c["ks"]:
    print(f"\nTop-{k} recommendations\n\n| Algorithm | Precision@{k} | Recall@{k} | F1@{k} | NDCG@{k} | Hit rate | Coverage |\n|---|---|---|---|---|---|---|")
    for a in ev["algorithms"]:
        r = ev["ranking"][str(k)][a["key"]]
        print(f"| {a['label']} | {r['precision']:.3f} | {r['recall']:.3f} | {r['f1']:.3f} | {r['ndcg']:.3f} | {r['hit']:.2f} | {r['coverage']:.2f} |")
print(f"\nHybrid minus each algorithm, Precision@{c['k']} (paired bootstrap, 95% CI):")
for a, v in ev["comparisons"].items():
    print(f"  vs {a:11} {v['mean_diff']:+.3f}  [{v['ci_low']:+.3f}, {v['ci_high']:+.3f}]  {'significant' if v['significant'] else 'not significant'}")
