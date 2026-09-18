import json, sys
import httpx
import time

URL = "http://localhost:8000/optimize-energy"
data = json.load(open("BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"))

ok = 0
for c in data["cases"]:
    time.sleep(6)
    r = httpx.post(URL, json=c["input"], timeout=40)
    if r.status_code != 200:
        print(f'{c["id"]}: HTTP {r.status_code} {r.text[:150]}')
        continue
    got, exp = r.json(), c["expected_output"]
    cost_ok = abs(got["total_cost_bdt"] - exp["total_cost_bdt"]) < 0.5
    kwh_ok = abs(got["total_grid_kwh"] - exp["total_grid_kwh"]) < 0.5
    peak_ok = abs(got["peak_grid_kwh"] - exp["peak_grid_kwh"]) < 0.5
    types_got = [d["directive_type"] for d in got["directive_interpretation"]]
    types_exp = [d["directive_type"] for d in exp["directive_interpretation"]]
    types_ok = types_got == types_exp
    good = cost_ok and kwh_ok and peak_ok and types_ok
    ok += good
    print(f'{c["id"]}: {"PASS" if good else "FAIL"} '
          f'cost {got["total_cost_bdt"]} vs {exp["total_cost_bdt"]} | '
          f'types {types_got} vs {types_exp}')
print(f"\n{ok}/{len(data['cases'])} passed")