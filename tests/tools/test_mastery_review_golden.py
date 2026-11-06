"""掌握度复测**金标集的离线自检**（不联网，默认跑）。

第七棒把"出题合不合规、判题对不对"也变成可量化的东西。真模型打分是
``test_mastery_review_integration.py``（``marker=integration``，默认不跑）；
这份自检盯的是**金标数据本身不能错**——数据错了，模型分数再高也是假的：

1. 每道题只考 6 个受控硬语法点之一（``error_code`` 必须落在产品白名单，
   白名单以 ``glyph.memory.mastery.VALID_ERROR_CODES`` 为单一事实源，漂移即红）；
2. 答案是**封闭、客观**的（语法填空/改错，有标准答案＋可接受等价写法），
   不允许开放写作题（那种没有唯一答案，判不动三档）；
3. 题面不能漏答案（挖空题括号给的是原形、改错题给的是错句，正确写法不得
   已经印在题面上）；
4. 每条判题判例必须**自洽**：学生答案对不对（与可接受答案比对）× 有没有给过
   提示，唯一决定应判哪一档；金标不许自相矛盾；
5. 每题三档（independent/hinted/wrong）判例都要齐，六个考点都要覆盖、题量要够。

注意：**首次答错不落账**——那时 agent 的动作是"给个提示让他再试一次"，
既不是 independent 也不是 wrong；只有"首次就对 / 提示后对 / 提示后仍错"
三个判定点才落账。金标里出现"没给提示＋答案错却判 wrong"即为错题。
"""

from __future__ import annotations

import pytest
from mastery_review_eval import (
    _extract_json,
    aggregate_audit,
    aggregate_generation,
    aggregate_judging,
    all_issues,
    answer_is_correct,
    audit_user,
    check_generated_question,
    coverage,
    expected_verdict,
    load_cases,
    parse_audit,
    question_issues,
)

_DATA = load_cases()


def test_金标文件存在且结构合法() -> None:
    assert _DATA, "复测金标集没加载到（检查 tests/fixtures/mastery/review_cases.json）"
    assert _DATA.get("version") == 1, "金标版本号不对，读这份测试的人要确认格式没变"
    assert isinstance(_DATA.get("questions"), list) and _DATA["questions"], "金标里一道题都没有"


def test_每道题都合格且三档判例齐全() -> None:
    bad: list[str] = []
    for q in _DATA["questions"]:
        issues = question_issues(q)
        if issues:
            bad.append(f"{q.get('id', '?')}: " + "；".join(issues))
    assert not bad, "金标题有问题：\n" + "\n".join(bad)


def test_全集覆盖与题量达标() -> None:
    assert all_issues(_DATA) == [], "金标全集问题：\n" + "\n".join(all_issues(_DATA))
    cov = coverage(_DATA)
    assert len(cov) == 6, f"六个考点没覆盖全，只覆盖了 {sorted(cov)}"
    for code, n in cov.items():
        assert n >= 2, f"考点 {code} 只有 {n} 题，每个考点至少 2 题"


def test_题号不重复() -> None:
    ids = [q["id"] for q in _DATA["questions"]]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    assert not dup, f"有重复题号：{dup}"


def test_期望档位由答案对错与是否提示唯一决定() -> None:
    # 首次、没给提示就做对 → independent
    assert expected_verdict(correct=True, hint_given=False) == "independent"
    # 给过提示之后才对 → hinted
    assert expected_verdict(correct=True, hint_given=True) == "hinted"
    # 给过提示仍错 → wrong
    assert expected_verdict(correct=False, hint_given=True) == "wrong"
    # 首次答错：不落账，动作是给提示（金标里不该有这种落档判例）
    assert expected_verdict(correct=False, hint_given=False) == "give_hint"


@pytest.mark.parametrize(
    ("answer", "acceptable", "ok"),
    [
        ("went", ["went"], True),
        (" Went. ", ["went"], True),  # 大小写、空格、标点抹平
        ("are going to visit", ["will visit", "are going to visit", "are visiting"], True),
        ("are visiting", ["will visit", "are going to visit", "are visiting"], True),
        ("visited", ["will visit", "are going to visit", "are visiting"], False),
        ("informations", ["information"], False),
        ("", ["went"], False),
    ],
)
def test_答案判定容忍等价写法但不放过错误(answer: str, acceptable: list[str], ok: bool) -> None:
    assert answer_is_correct(answer, acceptable) is ok


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"verdict": "hinted"}', {"verdict": "hinted"}),
        ('好的，结果是：\n```json\n{"verdict": "wrong"}\n```', {"verdict": "wrong"}),
        ('前缀 {"a": 1} 后缀', {"a": 1}),
        ("没有 JSON 的一段话", {}),
        ("", {}),
    ],
)
def test_从模型输出抠json_容忍围栏与废话(text: str, expected: dict) -> None:
    assert _extract_json(text) == expected


def test_生成题结构校验_合规题通过() -> None:
    gen = {
        "error_code": "VERB:TENSE",
        "type": "fill",
        "prompt": "Now she ___ (dance) in the room.",
        "acceptable_answers": ["is dancing"],
        "point_zh": "now 提示正在进行，用现在进行时",
    }
    assert check_generated_question("VERB:TENSE", gen) == []


def test_生成题结构校验_抓出各类不合规() -> None:
    base = {
        "error_code": "VERB:TENSE",
        "type": "fill",
        "prompt": "Now she ___ (dance) in the room.",
        "acceptable_answers": ["is dancing"],
        "point_zh": "x",
    }
    # 自报考点不符
    bad_code = dict(base, error_code="SPELL")
    assert any("error_code" in s for s in check_generated_question("VERB:TENSE", bad_code))
    # 填空题没有挖空
    no_blank = dict(base, prompt="Now she dances in the room.")
    assert any("挖空" in s for s in check_generated_question("VERB:TENSE", no_blank))
    # 答案泄露在题面
    leak = dict(base, prompt="Yesterday I went home.", acceptable_answers=["went"])
    assert any("泄露" in s or "题面" in s for s in check_generated_question("VERB:TENSE", leak))
    # 封闭答案为空
    no_ans = dict(base, acceptable_answers=[])
    assert any("acceptable_answers" in s for s in check_generated_question("VERB:TENSE", no_ans))
    # 不是 JSON 对象
    assert check_generated_question("VERB:TENSE", "随便一段文字")


def test_填空题括号里直接给答案算废题() -> None:
    # 不可数名词/拼写这类"词形不变"的点，若出成括号给原形的填空，照抄就能对，是废题
    trivial = {
        "error_code": "NOUN:INFL",
        "type": "fill",
        "prompt": "The team has made remarkable ___ (progress) in science.",
        "acceptable_answers": ["progress"],
        "point_zh": "progress 不可数",
    }
    issues = check_generated_question("NOUN:INFL", trivial)
    assert any("废题" in s or "括号" in s for s in issues), issues


def test_题面词恰好包含答案字符串但不是同一个词不算漏答案() -> None:
    # bookstore 里含子串 books，但它是另一个词，不是答案 books，不能误报
    g = {
        "error_code": "NOUN:NUM",
        "type": "fill",
        "prompt": "She bought three ___ (book) from the bookstore yesterday.",
        "acceptable_answers": ["books"],
        "point_zh": "大于 1 的数词后名词用复数",
    }
    issues = check_generated_question("NOUN:NUM", g)
    assert not any("题面" in s or "泄露" in s or "废题" in s for s in issues), issues


def test_填空多选说明括号不等于给答案() -> None:
    # 冠词题题面说明里列了 a / an / the 三个选项（含斜杠），不是把答案直接给学生
    article = {
        "error_code": "DET",
        "type": "fill",
        "prompt": "在空格处填恰当冠词（a / an / the，不需要则填 /）：She is ___ honest girl.",
        "acceptable_answers": ["an"],
        "point_zh": "honest 元音音素开头用 an",
    }
    assert check_generated_question("DET", article) == []


def test_改错题题面含同根错词不算漏答案() -> None:
    # knowledges（错）与 knowledge（对）同根：改错题题面本就含错词，不能误报泄露
    gen = {
        "error_code": "NOUN:INFL",
        "type": "correct",
        "prompt": "I have many knowledges.",
        "acceptable_answers": ["knowledge"],
        "point_zh": "knowledge 不可数",
    }
    assert check_generated_question("NOUN:INFL", gen) == []


def test_判题聚合算一致率混淆矩阵与按考点() -> None:
    rows = [
        {
            "id": "q1",
            "code": "A",
            "answer": "went",
            "hint_given": False,
            "gold": "independent",
            "pred": "independent",
            "ok": True,
        },
        {
            "id": "q1",
            "code": "A",
            "answer": "went",
            "hint_given": True,
            "gold": "hinted",
            "pred": "wrong",
            "ok": False,
        },
        {
            "id": "q2",
            "code": "B",
            "answer": "cats",
            "hint_given": True,
            "gold": "wrong",
            "pred": "wrong",
            "ok": True,
        },
    ]
    agg = aggregate_judging(rows)
    assert agg["total"] == 3 and agg["correct"] == 2
    assert agg["accuracy"] == round(2 / 3, 4)
    assert agg["per_code"] == {"A": 0.5, "B": 1.0}
    assert agg["confusion"]["hinted"] == {"wrong": 1}
    assert len(agg["mismatches"]) == 1 and agg["mismatches"][0]["gold"] == "hinted"


def test_出题聚合算合规率与失败明细() -> None:
    rows = [
        {"id": "q1", "code": "A", "ok": True, "issues": [], "gen": {"prompt": "x"}},
        {"id": "q2", "code": "B", "ok": False, "issues": ["答案泄露"], "gen": {}},
    ]
    agg = aggregate_generation(rows)
    assert agg["total"] == 2 and agg["compliant"] == 1
    assert agg["compliance_rate"] == 0.5
    assert agg["failures"] == [{"id": "q2", "code": "B", "issues": ["答案泄露"]}]


@pytest.mark.parametrize(
    ("data", "ok"),
    [
        ({"answer_correct": True, "stem_has_target_error": True,
          "tests_target_point": True}, True),
        ({"answer_correct": False, "stem_has_target_error": True,
          "tests_target_point": True}, False),
        ({"answer_correct": True, "stem_has_target_error": False,
          "tests_target_point": True}, False),
        ({"answer_correct": True, "stem_has_target_error": True,
          "tests_target_point": False}, False),
        ({}, False),  # 裁判没返回结构化结论：保守判不通过，宁可复审
    ],
)
def test_审题裁判结论解析(data: dict, ok: bool) -> None:
    assert parse_audit(data) is ok


def test_审题提示词含关键命题规则() -> None:
    user = audit_user("DET", "correct", "He is an honest man.", ["a honest"])
    # 改错型必须真有错、冠词看发音（honest/hour 用 an），这两点是抓"把对的改错"的关键
    assert "honest" in user and "发音" in user
    assert "无错可改" in user or "本来就正确" in user


def test_审题聚合算语义正确率() -> None:
    rows = [
        {"id": "q1", "code": "A", "ok": True, "audit": {}, "gen": {}},
        {"id": "q2", "code": "B", "ok": False,
         "audit": {"reason": "题面本就正确"}, "gen": {}},
    ]
    agg = aggregate_audit(rows)
    assert agg["total"] == 2 and agg["sound"] == 1
    assert agg["sound_rate"] == 0.5
    assert agg["failures"][0]["id"] == "q2"
