from __future__ import annotations

import math
import random

from meridian.analysts import calcs as c
from meridian.analysts.evidence import stance_for
from meridian.research.llm import extract_json
from meridian.research.workers import SourceModel, validate_sources


def test_rsi_and_efficiency():
    up = [100 + i for i in range(60)]
    assert c.rsi(up) == 100.0
    assert c.efficiency_ratio(up, 48) == 1.0
    rng = random.Random(1)
    chop = [100 + rng.choice([-1, 1]) for _ in range(60)]
    assert c.efficiency_ratio(chop, 48) < 0.3


def test_adf_distinguishes_mean_reversion_from_random_walk():
    rng = random.Random(7)
    mr, rw = [0.0], [0.0]
    for _ in range(400):
        mr.append(0.5 * mr[-1] + rng.gauss(0, 1))
        rw.append(rw[-1] + rng.gauss(0, 1))
    assert c.adf_tstat(mr) < -3.4
    assert c.adf_tstat(rw) > -3.4
    assert c.half_life(mr) is not None and c.half_life(mr) < 5


def test_correlation_and_zscore_and_mtf():
    a = [math.sin(i / 5) for i in range(100)]
    assert c.correlation(a, a) > 0.999
    assert c.correlation(a, [-x for x in a]) < -0.999
    assert c.zscore([1.0] * 19 + [5.0], 20) > 3
    al = c.mtf_alignment([100 + i * 0.5 for i in range(600)])
    assert al == {"1h": 1, "4h": 1, "1d": 1}


def test_stance_mapping():
    assert stance_for("LONG", 0.5, None) == "SUPPORTS"
    assert stance_for("SHORT", 0.5, None) == "OPPOSES"
    assert stance_for("LONG", 0.1, None) == "NEUTRAL"
    assert stance_for("SHORT", None, "OPPOSES") == "OPPOSES"  # direction-agnostic override (e.g. choppy regime)
    assert stance_for("LONG", None, None) == "NEUTRAL"


def test_extract_json_takes_final_block():
    txt = 'thinking... {"x": 1}\nfinal:\n```json\n{"title": "t", "summary": "s"}\n```'
    assert extract_json(txt)["title"] == "t"
    assert extract_json('blah {"a": {"b": 2}} tail')["a"]["b"] == 2


def test_fabricated_citations_are_dropped():
    claimed = [SourceModel(title="Real", url="https://ssrn.com/abstract=1"), SourceModel(title="Made up", url="https://fake.example/x")]
    retrieved = [{"url": "https://ssrn.com/abstract=1/", "title": "Real", "fetched": "true"}]
    out = validate_sources(claimed, retrieved)
    assert [s["title"] for s in out] == ["Real"] and out[0]["verified"] and out[0]["opened"]
