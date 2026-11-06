"""掌握度复测出题/判题的评测口径（离线纯函数 + 金标自检）。

和 ``golden_eval.py``（管"批改准不准"）分工：本模块管第七棒的两件事——
**出题合不合规**、**判题对不对**。金标集在
``tests/fixtures/mastery/review_cases.json``，只收 6 个受控硬语法点的客观题
（语法填空/改错，答案封闭），不收开放写作题。

档位语义（与系统提示词 mastery 段、record_review_result 工具一致）：
- 首次、没给提示就做对            → ``independent``（独立做对）
- 给过提示之后才做对              → ``hinted``（提示后做对）
- 给过提示仍错                    → ``wrong``（没做对，此时讲解并给答案）
- 首次答错                        → ``give_hint``（**不落账**，动作是给提示让他再试）

所以落账档位由"答案对不对 × 有没有给过提示"唯一决定，金标判例必须与此自洽。
答案对不对用与批改金标同口径的 normalize（小写、抹平所有非字母数字）后，
在"标准答案 + 可接受等价写法"集合里命中即算对。
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any

from golden_eval import normalize as normalize_answer

from glyph.memory.mastery import CODE_LABELS_ZH, VALID_ERROR_CODES

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "mastery" / "review_cases.json"

_VERDICTS = ("independent", "hinted", "wrong")
_QUESTION_TYPES = ("fill", "correct")
# 短于这个长度的答案不做"题面漏答案"子串检查：a/an/the 这类冠词会是大量单词的子串，
# 启发式不可靠；短冠词题的题面挖空位置本身就标明了该填什么。
_MIN_ANS_LEN_FOR_LEAK_CHECK = 3


def load_cases() -> dict[str, Any]:
    """加载复测金标集；文件缺失时返回空 dict（由存在性测试报红）。"""
    if not CASES_PATH.is_file():
        return {}
    data = json.loads(CASES_PATH.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def answer_is_correct(answer: str, acceptable_answers: list[str]) -> bool:
    """学生答案 normalize 后命中标准答案或任一可接受等价写法即算对。"""
    norm = normalize_answer(answer)
    if not norm:
        return False
    return any(norm == normalize_answer(a) for a in acceptable_answers)


def expected_verdict(correct: bool, hint_given: bool) -> str:
    """由"答案对不对 × 给没给过提示"唯一推导应落的档位。

    返回 ``give_hint`` 表示这不是落账判定点（首次答错，应先给提示）。
    """
    if correct and not hint_given:
        return "independent"
    if correct and hint_given:
        return "hinted"
    if not correct and hint_given:
        return "wrong"
    return "give_hint"


_PAREN_RE = re.compile(r"[（(]\s*([^（）()]+?)\s*[）)]")
_PAREN_BLOCK_RE = re.compile(r"[（(][^（）()]*[）)]")


def phrase_in_tokens(needle: list[str], haystack: list[str]) -> bool:
    """答案是否作为**连续整词**序列出现在题面。

    用整词而非裸子串，避免 "bookstore" 含子串 "books"、"announce" 含 "an" 这类误报。
    """
    if not needle:
        return False
    width = len(needle)
    return any(haystack[i : i + width] == needle for i in range(len(haystack) - width + 1))


def _words(text: str) -> list[str]:
    """按字母数字抽词（小写），保留真实词边界。

    不能用 golden_eval.normalize 的结果 split——它把非字母数字（含空格）整个删掉，
    会把 "I went home" 压成一个 token "iwenthome"。
    """
    return re.findall(r"[a-z0-9]+", text.lower())


def fill_leak_issues(prompt: str, acceptable_answers: list[str]) -> list[str]:
    """检查填空题有没有把答案白送给学生（金标题与模型生成题共用同一标准）。

    两种废题：
    - 答案整词出现在挖空**之外**的题面里（题面漏答案）；
    - 括号提示里给的词本身就等于答案——学生照抄括号即可，根本没考到点。
      不可数名词（progress/advice 词形不变）、拼写（答案是正确拼写）这类点
      一旦出成"括号给原形"的填空就是废题，应改成给错句的改错型。
    括号里若用斜杠列了多个候选（a / an / the），那是答题说明、不是漏答案。
    """
    issues: list[str] = []
    parens = _PAREN_RE.findall(prompt)
    outside_words = _words(_PAREN_BLOCK_RE.sub(" ", prompt))
    for a in acceptable_answers:
        ans_words = _words(a)
        if len("".join(ans_words)) >= _MIN_ANS_LEN_FOR_LEAK_CHECK and phrase_in_tokens(
            ans_words, outside_words
        ):
            issues.append(f"答案 {a!r} 作为整词出现在挖空之外的题面里（题面漏答案）")
        for p in parens:
            if "/" not in p and "／" not in p and normalize_answer(p) == normalize_answer(a):
                issues.append(
                    f"括号里给的 {p.strip()!r} 就是答案 {a!r}，照抄括号就能答对（废题）；"
                    "词形不变或考拼写的考点应改用改错型（给含错词的句子让改）"
                )
    return issues


def question_issues(q: dict[str, Any]) -> list[str]:
    """校验单道金标题，返回问题清单（空列表表示合格）。"""
    issues: list[str] = []
    qid = q.get("id", "?")

    required = (
        "id",
        "error_code",
        "type",
        "prompt",
        "acceptable_answers",
        "point_zh",
        "judgements",
    )
    missing_fields = [f for f in required if f not in q]
    if missing_fields:
        return [f"{qid} 缺字段 {missing_fields}"]

    if q["error_code"] not in VALID_ERROR_CODES:
        issues.append(f"{qid} error_code {q['error_code']!r} 不在 6 考点白名单")
    if q["type"] not in _QUESTION_TYPES:
        issues.append(f"{qid} type 必须是 fill/correct，得到 {q['type']!r}")

    acc = q["acceptable_answers"]
    if (
        not isinstance(acc, list)
        or not acc
        or not all(isinstance(a, str) and a.strip() for a in acc)
    ):
        issues.append(f"{qid} acceptable_answers 必须是非空字符串列表")
        acc = []

    prompt = q.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        issues.append(f"{qid} prompt 为空")
        prompt = ""
    # 填空题：必须有挖空，且不能把答案白送给学生。改错题题面本就是错句、常与正确答案
    # 同根（knowledges→knowledge），漏答案检查只针对填空。
    if q["type"] == "fill":
        if "___" not in prompt:
            issues.append(f"{qid} 填空题必须有挖空标记 ___")
        issues.extend(f"{qid} {s}" for s in fill_leak_issues(prompt, acc))

    judges = q["judgements"]
    if not isinstance(judges, list) or not judges:
        issues.append(f"{qid} judgements 为空")
        return issues

    seen: set[str] = set()
    for j in judges:
        if not all(f in j for f in ("answer", "hint_given", "expect")):
            issues.append(f"{qid} 有判例缺 answer/hint_given/expect 字段：{j}")
            continue
        if j["expect"] not in _VERDICTS:
            issues.append(f"{qid} 判例 expect 非法：{j['expect']!r}")
            continue
        correct = answer_is_correct(j["answer"], acc)
        derived = expected_verdict(correct, bool(j["hint_given"]))
        if derived == "give_hint":
            issues.append(
                f"{qid} 判例答案 {j['answer']!r} 是首次答错（未给提示），此时应给提示、"
                f"不落账，不该标成 {j['expect']}"
            )
        elif derived != j["expect"]:
            issues.append(
                f"{qid} 判例自相矛盾：答案 {j['answer']!r}（{'对' if correct else '错'}）、"
                f"hint_given={j['hint_given']} 应判 {derived}，金标写 {j['expect']}"
            )
        else:
            seen.add(j["expect"])

    missing_verdicts = set(_VERDICTS) - seen
    if missing_verdicts:
        issues.append(f"{qid} 三档判例不齐，缺 {sorted(missing_verdicts)}")

    return issues


def coverage(data: dict[str, Any]) -> dict[str, int]:
    """每个考点各有多少题（只计白名单内 code）。"""
    cov: dict[str, int] = {}
    for q in data.get("questions", []):
        code = q.get("error_code")
        if code in VALID_ERROR_CODES:
            cov[code] = cov.get(code, 0) + 1
    return cov


def all_issues(data: dict[str, Any]) -> list[str]:
    """校验整个金标集的跨题约束：题号唯一、题量、六考点覆盖。"""
    issues: list[str] = []
    questions = data.get("questions", [])
    if not isinstance(questions, list):
        return ["questions 不是列表"]

    ids = [q.get("id") for q in questions if isinstance(q, dict)]
    dup = sorted({i for i in ids if ids.count(i) > 1})
    if dup:
        issues.append(f"题号重复：{dup}")

    if len(questions) < 12:
        issues.append(f"题量只有 {len(questions)}，至少 12 题才谈得上量化")

    cov = coverage(data)
    missing_codes = set(VALID_ERROR_CODES) - set(cov)
    if missing_codes:
        issues.append(f"考点未全覆盖，缺 {sorted(missing_codes)}")
    for code in VALID_ERROR_CODES:
        if cov.get(code, 0) < 2:
            issues.append(f"考点 {code} 只有 {cov.get(code, 0)} 题，每个考点至少 2 题")

    return issues


# ---------------------------------------------------------------------------
# 真模型评测（integration，默认不跑）：判题一致率 + 出题结构合规率
#
# 下面三段是确定性的、离线可单测：JSON 抽取、生成题的结构合规校验、结果聚合。
# 真正发网络请求的 evaluate_* 是薄异步壳，放在最后，由 integration 测试驱动。
# ---------------------------------------------------------------------------

_VERDICT_INCLUDING_HINT = ("independent", "hinted", "wrong", "give_hint")
_VERDICT_RE = re.compile(r'"?verdict"?\s*[:：]\s*"?(independent|hinted|wrong|give_hint)')


def _extract_json(text: str) -> dict[str, Any]:
    """从模型输出里抠 JSON，容忍 ```json 围栏和前后废话；失败返回空 dict。"""
    if not text:
        return {}
    t = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.MULTILINE).strip()
    try:
        start, end = t.index("{"), t.rfind("}")
        data = json.loads(t[start : end + 1])
        return data if isinstance(data, dict) else {}
    except (ValueError, json.JSONDecodeError):
        return {}


def check_generated_question(code: str, gen: dict[str, Any]) -> list[str]:
    """对模型**当场生成的新题**做确定性结构合规校验，返回问题清单（空=合规）。

    能自动判定的：自报考点一致、题型合法、题干/封闭答案/讲解齐全、填空题有挖空、
    题面不泄露长答案。**不能**自动判定的：答案本身对不对、新题是否真考该考点
    （需语义理解）——这部分落盘后人工抽查，不在此函数假装覆盖。
    """
    if not isinstance(gen, dict):
        return ["模型没有返回 JSON 对象"]
    issues: list[str] = []
    if gen.get("error_code") != code:
        issues.append(f"自报 error_code {gen.get('error_code')!r} 与指定考点 {code} 不符")

    qtype = gen.get("type")
    if qtype not in _QUESTION_TYPES:
        issues.append(f"type 非法：{qtype!r}（应为 fill/correct）")

    prompt = gen.get("prompt") or ""
    if not isinstance(prompt, str) or not prompt.strip():
        issues.append("prompt 为空")
        prompt = ""

    acc = gen.get("acceptable_answers")
    if (
        not isinstance(acc, list)
        or not acc
        or not all(isinstance(a, str) and a.strip() for a in acc)
    ):
        issues.append("acceptable_answers 必须是非空字符串列表（封闭答案）")
        acc = []

    if not (gen.get("point_zh") or "").strip():
        issues.append("point_zh 为空（必须一句话讲清考点）")

    if qtype == "fill":
        if "___" not in prompt:
            issues.append("填空题没有挖空标记 ___")
        issues.extend(fill_leak_issues(prompt, acc))
    return issues


def aggregate_judging(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合判题结果：总一致率、gold→pred 混淆矩阵、按考点一致率、不一致明细。"""
    total = len(rows)
    correct = sum(1 for r in rows if r["ok"])
    confusion: dict[str, dict[str, int]] = {}
    per_code: dict[str, list[int]] = {}
    mismatches: list[dict[str, Any]] = []
    for r in rows:
        confusion.setdefault(r["gold"], {})
        confusion[r["gold"]][r["pred"]] = confusion[r["gold"]].get(r["pred"], 0) + 1
        bucket = per_code.setdefault(r["code"], [0, 0])
        bucket[0] += int(r["ok"])
        bucket[1] += 1
        if not r["ok"]:
            mismatches.append(
                {
                    "id": r["id"],
                    "code": r["code"],
                    "answer": r["answer"],
                    "hint_given": r["hint_given"],
                    "gold": r["gold"],
                    "pred": r["pred"],
                }
            )
    return {
        "total": total,
        "correct": correct,
        "accuracy": round(correct / total, 4) if total else 0.0,
        "confusion": confusion,
        "per_code": {c: round(ok / n, 4) for c, (ok, n) in per_code.items()},
        "mismatches": mismatches,
    }


def aggregate_generation(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合出题结果：结构合规率、不合规明细、全部生成题（供人工抽查考点正确性）。"""
    total = len(rows)
    compliant = sum(1 for r in rows if r["ok"])
    return {
        "total": total,
        "compliant": compliant,
        "compliance_rate": round(compliant / total, 4) if total else 0.0,
        "failures": [
            {"id": r["id"], "code": r["code"], "issues": r["issues"]} for r in rows if not r["ok"]
        ],
        "generated": [r.get("gen") for r in rows],
    }


# -- 真模型交互（薄异步壳；以下函数在 integration 测试里才真正被调用） -----------

_JUDGE_SYSTEM = (
    "你是英语错题复测的阅卷员。根据题目、标准答案、学生是否已获得提示及其答案，"
    "把这一次作答判成独立做对/提示后做对/没做对。只输出 JSON，不要多余的话。"
)

_GENERATE_SYSTEM = (
    "你是英语错题复测的出题员。根据学生的历史错句，针对指定考点换语境出一道全新的"
    "客观题（语法填空或改错），答案必须封闭、客观。只输出 JSON，不要多余的话。"
)


def _judge_user(q: dict[str, Any], j: dict[str, Any]) -> str:
    hinted = (
        "是（他第一次答错，你给过提示后他才给出这个答案）"
        if j["hint_given"]
        else "否（这是他首次作答，还没给过任何提示）"
    )
    return (
        f"考点：{q['point_zh']}\n"
        f"题目：{q['prompt']}\n"
        f"可接受的正确答案：{'、'.join(q['acceptable_answers'])}\n"
        f"学生是否已获得提示：{hinted}\n"
        f"学生的答案：{j['answer']}\n\n"
        "判定规则：\n"
        "- 学生答案与任一可接受答案等价（忽略大小写、标点，须语法语义正确）："
        "未给提示→independent；给过提示后才对→hinted。\n"
        "- 给过提示仍不正确→wrong。\n"
        "- 未给提示且不正确→give_hint（首次错，应先给提示，不落账）。\n"
        '只输出 JSON：{"verdict": "independent|hinted|wrong|give_hint", "reason": "一句话"}'
    )


def _generate_user(code: str, q: dict[str, Any]) -> str:
    label = CODE_LABELS_ZH.get(code, code)
    return (
        f"学生的历史错句：{q['seed_wrong']}\n"
        f"正确改法：{q['seed_right']}\n"
        f"本轮考点 error_code：{code}（{label}）\n\n"
        "要求：\n"
        "1. 以旧错句为种子，换主语/动词/语境，出 **1 道**同考点新题，不要照抄原句，也不要出多道；\n"
        "2. 题型二选一：\n"
        "   - fill 挖空（用 ___ 标出）：括号里只能给**需要变形的原形**，且正确答案必须和"
        "括号里的词形不同（如 (go)→went、(cat)→cats、(good)→best）；\n"
        "   - correct 改错：给一个含同类错误的完整句子让学生改正。\n"
        "   凡词形不变的点（不可数名词 information/advice/progress/furniture，答案就是原形）"
        "和拼写题（答案是正确拼写），**必须用 correct 改错型**（给 informations、recieve"
        " 这类错词让改）；不许出括号给原形的填空——照抄括号就能答对的题考不到东西；\n"
        "3. acceptable_answers 给封闭、客观的标准答案数组，含可接受的等价写法；"
        "**题面（含括号和语境）任何位置都不得出现可接受答案的原词**；\n"
        "4. point_zh 一句话讲清考点；\n"
        "5. correct 改错题给的句子必须**确实含一个该考点的错误**，不要把本来就正确的句子"
        "当成错句让学生改；按答案改正后的整句必须**完全正确、不得引入新的语法错误**，"
        "若错误与上下文牵连就要一并改对（如 many advices 应改成 much advice，"
        "不能只改成 many advice）；涉及冠词时 a/an 依后词开头**发音**"
        "（honest、hour 用 an；university、UFO 用 a）。\n"
        '只输出 JSON：{"error_code": "'
        + code
        + '", "type": "fill|correct", "prompt": "...", '
        + '"acceptable_answers": ["..."], "point_zh": "..."}'
    )


async def _chat_json(client: Any, model: str, system: str, user: str) -> tuple[dict[str, Any], str]:
    resp = await client.chat.completions.create(
        model=model,
        temperature=0,
        messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
    )
    raw = resp.choices[0].message.content or ""
    return _extract_json(raw), raw


def _verdict_from(data: dict[str, Any], raw: str) -> str:
    cand = data.get("verdict")
    if cand in _VERDICT_INCLUDING_HINT:
        return cand
    m = _VERDICT_RE.search(raw)
    return m.group(1) if m else "unparsed"


async def evaluate_judging(
    client: Any, model: str, data: dict[str, Any], concurrency: int = 4
) -> dict[str, Any]:
    """让强模型对金标每条判例判三档，与金标比对，返回 aggregate_judging 结果。"""
    sem = asyncio.Semaphore(concurrency)

    async def one(q: dict[str, Any], j: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            data_obj, raw = await _chat_json(client, model, _JUDGE_SYSTEM, _judge_user(q, j))
        pred = _verdict_from(data_obj, raw)
        return {
            "id": q["id"],
            "code": q["error_code"],
            "answer": j["answer"],
            "hint_given": j["hint_given"],
            "gold": j["expect"],
            "pred": pred,
            "ok": pred == j["expect"],
        }

    tasks = [one(q, j) for q in data["questions"] for j in q["judgements"]]
    rows = await asyncio.gather(*tasks)
    result = aggregate_judging(list(rows))
    result["model"] = model
    return result


_GEN_MAX_ATTEMPTS = 3


async def evaluate_generation(
    client: Any, model: str, data: dict[str, Any], concurrency: int = 4
) -> dict[str, Any]:
    """让强模型按每道金标的种子当场出新题，做结构合规校验，返回 aggregate_generation 结果。

    没返回有效 JSON 属于网络/格式抖动（不是出题能力），在信号量内重试若干次；
    重试后仍拿不到题才计一次"技术性失败"。
    """
    sem = asyncio.Semaphore(concurrency)

    async def one(q: dict[str, Any]) -> dict[str, Any]:
        code = q["error_code"]
        gen: Any = {}
        async with sem:
            for attempt in range(_GEN_MAX_ATTEMPTS):
                gen, _raw = await _chat_json(
                    client, model, _GENERATE_SYSTEM, _generate_user(code, q)
                )
                if isinstance(gen, dict) and gen:
                    break
                await asyncio.sleep(0.8 * (attempt + 1))
        if not isinstance(gen, dict) or not gen:
            issues = [f"模型在 {_GEN_MAX_ATTEMPTS} 次尝试后仍未返回有效题目 JSON（技术性失败）"]
            gen = {}
        else:
            issues = check_generated_question(code, gen)
        return {"id": q["id"], "code": code, "ok": not issues, "issues": issues, "gen": gen}

    rows = await asyncio.gather(*(one(q) for q in data["questions"]))
    result = aggregate_generation(list(rows))
    result["model"] = model
    return result


# -- 语义审题（LLM-as-judge）：结构检查管不了"答案本身对不对"，交给独立审题员 -------
#
# 结构合规只能保证题型/封闭答案/不漏答案；"标准答案本身是否正确、改错句是否真有错、
# 是否真考该考点"需要语义判断。实测同一强模型在**给足评分规则**后能稳定抓出自己出的
# 错题（挑错比生成容易），故用一次独立审题调用把"语义正确率"也量化、卡阈值。
# 审题只在离线评测跑，不进真机复测链路（省 token；真机靠出题提示词 + 用户终审兜底）。

_AUDIT_SYSTEM = "你是极其严谨的英语命题审题员，只依据客观语言事实判断，不迎合题目、不预设题目没错。"


def audit_user(code: str, qtype: str, prompt: str, answers: list[str]) -> str:
    return (
        f"考点 error_code：{code}\n"
        f"题型：{qtype}（fill=挖空填空；correct=改错，题面应含一个该考点的错误让学生改）\n"
        f"题面：{prompt}\n"
        f"标准答案：{'、'.join(answers)}\n\n"
        "请逐项核查（依据客观语言事实）：\n"
        "1. answer_correct：标准答案本身是否正确？fill 题答案填入后句子是否语法正确、"
        "语义自然？correct 题按答案改正后的句子是否**完全正确、没有残留或新引入的语法错误**"
        "（例如 many advices 只改成 many advice 仍错，many 不能修饰不可数名词）？\n"
        "2. stem_has_target_error：仅 correct 题——题面句子是否**确实包含一个该考点的错误**？"
        "若句子本来就正确、无错可改（被当成错句），判 false；fill 题判 true。\n"
        "3. tests_target_point：这道题是否真的在考指定考点，没有跑题？\n"
        "冠词 a/an 必须看后面单词开头的**发音**而非首字母：honest、hour 元音音素开头用 an；"
        "university、UFO 以 /j/ 辅音音素开头用 a。\n"
        '只输出 JSON：{"answer_correct": true/false, '
        '"stem_has_target_error": true/false, "tests_target_point": true/false, '
        '"reason": "一句话"}'
    )


def parse_audit(data: dict[str, Any]) -> bool:
    """审题三项全为真才算题没问题；裁判没给出结构化结论时保守判不通过（宁可复审）。"""
    if not isinstance(data, dict):
        return False
    return all(
        data.get(k) is True
        for k in ("answer_correct", "stem_has_target_error", "tests_target_point")
    )


def aggregate_audit(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """聚合语义审题：语义正确率（三项全真题占比）与不通过明细。"""
    total = len(rows)
    sound = sum(1 for r in rows if r["ok"])
    return {
        "total": total,
        "sound": sound,
        "sound_rate": round(sound / total, 4) if total else 0.0,
        "failures": [
            {
                "id": r["id"],
                "code": r["code"],
                "reason": (r.get("audit") or {}).get("reason", "裁判无结构化结论"),
                "gen": r.get("gen"),
            }
            for r in rows
            if not r["ok"]
        ],
    }


async def evaluate_answer_correctness(
    client: Any,
    model: str,
    data: dict[str, Any],
    generation: dict[str, Any],
    concurrency: int = 4,
) -> dict[str, Any]:
    """对模型当场生成的题做独立语义审题。generated 与金标 questions 按索引对齐取考点。

    没拿到有效题（技术性失败）的项不参与语义审题——它已在"结构合规"维度计失败，
    不在语义维度二次惩罚；skipped 单独计数，语义正确率分母只含真正审到的题。
    """
    gens = generation.get("generated", [])
    questions = data["questions"]
    sem = asyncio.Semaphore(concurrency)

    async def one(i: int) -> dict[str, Any] | None:
        q = questions[i]
        g = gens[i] if i < len(gens) else None
        if not isinstance(g, dict) or not g:
            return None
        async with sem:
            audit, _raw = await _chat_json(
                client,
                model,
                _AUDIT_SYSTEM,
                audit_user(
                    q["error_code"],
                    g.get("type", ""),
                    g.get("prompt", ""),
                    g.get("acceptable_answers", [])
                    if isinstance(g.get("acceptable_answers"), list)
                    else [],
                ),
            )
        return {
            "id": q["id"],
            "code": q["error_code"],
            "ok": parse_audit(audit),
            "audit": audit,
            "gen": g,
        }

    gathered = await asyncio.gather(*(one(i) for i in range(len(questions))))
    rows = [r for r in gathered if r is not None]
    result = aggregate_audit(list(rows))
    result["skipped"] = len(questions) - len(rows)
    result["model"] = model
    return result
