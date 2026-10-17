"""确定性与幂等：同输入、同配置、同输入修订号得到可重复结果（无随机性）。"""

import json

import pytest

from learning.samples import list_samples, run_sample

SAMPLES = list_samples()


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@pytest.mark.parametrize("name", SAMPLES)
def test_repeatable(name):
    first = run_sample(name)
    second = run_sample(name)
    assert _dump(first["plan"].to_dict()) == _dump(second["plan"].to_dict()), name
    assert _dump([t.to_dict() for t in first["tasks"]]) == _dump(
        [t.to_dict() for t in second["tasks"]]
    ), name
