from learning.review.knowledge import _reference_only


def test_exercise_numbers_do_not_claim_to_contain_the_question_body():
    assert _reference_only('第一章的练习题1、3、5、6、9、10')
    assert _reference_only('第四章习题第1、2和3题')
    assert _reference_only('本周的作业是第四章习题第1、2和3题，共3题，请同学们按时完成作业')
    assert not _reference_only('计算电子自旋磁矩，并分析塞曼效应')
