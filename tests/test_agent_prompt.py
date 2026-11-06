"""主系统提示词的离线守护测试。

真模型下的实际效果（自主调 grade_text、第二轮只短引导）由 integration 测试守护；
这里只离线锁定关键策略不被误删：服务对象定位、批改必须走工具、不逐条复述。
"""

from collections import namedtuple

import pytest

from glyph.agent_prompt import (
    _PLAN_ROUTING_LINES,
    _VISION_GRADING_RULE,
    DYNAMIC_SECTION_NAMES,
    STATIC_SECTIONS,
    SYSTEM_PROMPT,
    build_system_prompt,
    prompt_sections,
)
from glyph.tools import (
    DeleteAttemptTool,
    DescribeImageTool,
    ForgetTool,
    GradeTextTool,
    OcrImageTool,
    ReadAttemptsTool,
    ReadMemoryTool,
    RecheckDetectionTool,
    RecordAttemptTool,
    RememberTool,
)
from glyph.tools.memory_tools import RecordReviewResultTool

_ToolSpec = namedtuple("_ToolSpec", "name description")

_ONLY_TWO = [
    _ToolSpec(name="grade_text", description="批改英文文本"),
    _ToolSpec(name="ocr_image", description="图片转写"),
]

_EXPECTED_STATIC = (
    "identity",
    "tone",  # 说话风格：三档共用底线 + 当档口吻；换风格即整段替换（默认自然）
    "trust_boundary",
    "routing",
    "grading_line",
    "speaking",
    "practice",  # T05 五分钟、一个重点：只在用户真要练习时才进这一节
    "mastery",  # ⑥ 错题复习闭环：仅注册 record_review_result 时才出现
    "attempt_records",  # T07 练习尝试记录：仅注册 record_attempt 时才出现
    "study_state",  # T06 只读学习状态：只在被问"学什么"时才有关系
    "reporting",  # 学 CC 的 # Reporting outcomes：说"完成"必须有观测依据
)


def test_system_prompt_serves_all_english_learners():
    """人设一句话说清"你是谁"，**不列举用户身份**。

    人 2026-09-27 定的两条规矩：① 服务所有学习英语的用户；② 不特定指定用户是谁。
    "用户的英语助手"——"用户的"三个字天然涵盖所有人，不再列举"学生、老师和普通爱好者"
    （列举反而是在贴标签）。
    """
    assert "你是 Glyph，用户的英语助手" in SYSTEM_PROMPT
    # 反过来守：不许再冒出列举身份的写法
    assert "学生、老师和普通爱好者" not in SYSTEM_PROMPT
    # 但"同一个功能对不同人意思不一样"这条必须留（不说清模型会默认一种用法）
    assert "同一个功能" in SYSTEM_PROMPT


def test_identity_distinguishes_get_it_done_from_learning():
    """去学生化（第二轮）：同时接住"办事型"和"学习型"，别把谁都当来上课的学生。

    docs/prd/learner-journeys.md 把人分两种：想把英文的事办完（翻译 / 看懂 / 回邮件 /
    拟回复），和想把英语学明白（改错 / 讲解 / 练习）。identity 段必须讲清分流与态度：
    办事就麻利把事办好、不端老师架子硬讲课；学习才讲透、留白让他自己试、不甩答案。
    """
    identity = dict(STATIC_SECTIONS)["identity"]
    assert "想把一件英文的事办完" in identity
    assert "回一封邮件" in identity
    assert "别端老师架子" in identity
    assert "想把英语学明白" in identity
    assert "留白让他自己试" in identity


def test_routing_treats_any_english_writing_as_gradable():
    """作文只是英文写作的一种：邮件 / 消息 / 评论里的英文也要走 grade_text，别只认"作文"。"""
    routing = dict(STATIC_SECTIONS)["routing"]
    assert "邮件" in routing
    assert "消息" in routing


def test_routing_names_ask_user_when_it_says_stop_and_ask():
    """routing 段说"停下来问用户"时**必须指名 ask_user**，不能只说"问一句"。

    真机证据（2026-10-08，scripts/verify_ask_user_real.py）：5 个场景跑下来
    ask_user **一次都没被调用**——模型遇到该问的情况一律用嘴反问一句。
    根因就是这段只写了"问他一句"却没说用什么问，轻量模型照字面理解成自然语言反问。
    工具注册了却从不被用，等于没做。
    """
    routing = dict(STATIC_SECTIONS)["routing"]
    assert "ask_user" in routing


def test_routing_ask_user_must_not_ask_what_user_already_told():
    """ask_user 不许问用户已经说过、或能从他给的信息直接推断出来的事。

    走查反馈：用户明说"我下月考四级"，模型仍弹卡问"按四级还是六级评分"——
    把用户的话当没说过，是最出戏的废话提问。ask_user 只用于"他没给、你也推不
    出来、不问就没法继续"的岔路；已知或可推断的直接照做。
    """
    routing = dict(STATIC_SECTIONS)["routing"]
    assert "已经说过" in routing
    assert "推断" in routing


def test_routing_plan_asks_before_proposing():
    """计划类请求必须**先问后拟**，不许拿到目标就凭空生成整份计划。

    真机证据（2026-10-13）：人说"想制定一个备考六级的计划"，模型一句没问
    （考哪一次 / 每天能投入多久 / 薄弱项全不知道）就直接 propose_plan 生成了
    一份通用计划。旧行把「→ 调 propose_plan」写成第一步，等于教模型跳过提问。
    规矩：关键事实拿不准先 ask_user（只问最关键的一两项，已知/可推断的别问），
    问清再拟。
    """
    plan_rules = _PLAN_ROUTING_LINES
    assert "先问后拟" in plan_rules
    assert "ask_user" in plan_rules


def test_plan_routing_wires_into_prompt_when_tool_registered():
    """注册了 propose_plan 时，先问后拟的规矩必须真的进到组装后的提示词里。

    只测 _PLAN_ROUTING_LINES 常量不够——万一 {plan_routing} 占位符没被替换
    或 has_plan 门控写错，规矩就静默失效（R8：文档不说谎，提示词同理）。
    """
    full = build_system_prompt(
        tools=[_ToolSpec(name="propose_plan", description="提议学习计划")],
    )
    assert "先问后拟" in full
    # 没注册 propose_plan 时规矩随规划分工一起退场，不该残留半截话
    full_without = build_system_prompt(
        tools=[_ToolSpec(name="grade_text", description="批改英文文本")],
    )
    assert "先问后拟" not in full_without


def test_mastery_review_assumes_no_student_or_exam():
    """复习闭环不挑身份：在职的人写邮件老错时态一样练，不预设对方在上学 / 备考。"""
    mastery = dict(STATIC_SECTIONS)["mastery"]
    assert "不挑身份" in mastery
    assert "考不考试" in mastery


def test_vision_capability_follows_the_main_model():
    """能不能看图是**模型的能力**，不能写死在身份段里。

    换多模态主模型后"你本身看不见图片"会变成假的——
    模型明明看得见却按提示词说看不见，看图功能当场废掉。
    """
    text_plain = build_system_prompt(vision_capable=False)
    assert "不能直接看图" in text_plain

    text_vision = build_system_prompt(vision_capable=True)
    assert "能直接看" in text_vision
    # 多模态下仍要钉死：图上的英文落到文本必须走 ocr_image（忠实抄写 + [?]）
    assert "ocr_image" in text_vision
    assert "不要自己读出来就用" in text_vision


def test_network_capability_follows_the_switch():
    """联网同理：现在写死"当前不可用"，哪天开了联网这句立刻变假。"""
    assert "当前不可用" in build_system_prompt(network_capable=False)
    assert "当前不可用" not in build_system_prompt(network_capable=True)


def test_network_on_line_forbids_claims_without_sources():
    """联网已启用时必须写死：搜索没返回来源 = 没搜到，
    不许称"根据搜索到的信息"、不许在没有来源时编造答案。"""
    prompt = build_system_prompt(network_capable=True)
    assert "没有返回任何来源" in prompt
    assert "根据搜索" in prompt  # 出现在「不许称根据搜索」的禁令里


def test_prompt_has_a_report_truthfully_clause():
    """CC 的 `# Reporting outcomes`：说"完成"必须有观测依据，不许把失败藏起来。

    与已有的"复核不是让你改口"互补：那条管"别因用户驳回就改口"，
    这条管"别把没做成的事说成做成了"。

    ⚠️ 锚定 reporting 段本身：SYSTEM_PROMPT 里 grading_line 也有"如实转达"，
    只断言"如实"二字的话整段删掉都不红（假守护）。
    """
    reporting = dict(STATIC_SECTIONS)["reporting"]
    assert "亲眼看到" in reporting
    assert "没做成" in reporting


def test_tool_calls_require_a_visible_purpose_before_execution():
    """锁定调用前沟通契约；真实输出顺序另由 integration 测试验证。"""
    prompt = build_system_prompt()
    assert "每批工具调用前" in prompt
    assert "先输出" in prompt and "正常回复正文" in prompt
    assert "做什么" in prompt and "为什么要做" in prompt
    assert "同一批" in prompt and "只说明一次" in prompt
    assert "不要" in prompt and "内部推理" in prompt


def test_pre_tool_purpose_in_thinking_does_not_count():
    """招呼必须出现在正文里：思考流里的计划不算数。

    2026-10-14 真机截图：模型把「该调 describe_image」全写在思考块里，正文一个字
    没吐，runtime 只好补发兜底通知 ToolNoticeEvent（前端渲染成两侧发丝线的
    finrow「我先处理一下，再把结果告诉你。」，不是正常正文样式）。思考默认折起、
    用户根本看不到——提示词必须明说"在思考里计划过不等于打过招呼"。
    """
    speaking = dict(STATIC_SECTIONS)["speaking"]
    assert "思考" in speaking
    assert "不算" in speaking


def test_pre_tool_purpose_must_name_a_concrete_action():
    """招呼要说"对什么做什么"，点名禁止"处理一下 / 稍等"这类空壳填充句。

    截图里兜底通知之所以出戏，是因为「我先处理一下」没有任何信息量。
    模型自己说正文时也可能说出同款空壳句，所以提示词要给正例 + 点反例。
    """
    speaking = dict(STATIC_SECTIONS)["speaking"]
    assert "具体" in speaking
    assert "处理一下" in speaking  # 作为反例被点名
    assert "我看一下这张图" in speaking  # 正例：动词 + 具体对象


def test_ability_section_has_no_blank_line_inside():
    """能力段里不许出现空行——空行会把 Markdown 列表切成两张表。

    曾经因为 {network_line} 常量自带结尾换行、模板又补一个，
    渲染出 "…不要编。\\n\\n- 长期记忆…"。
    """
    ability = dict(prompt_sections())["ability"]
    # 标题与正文之间那一个空行是 Markdown 必需的，放行；
    # 守的是正文（列表）内部不再出现空行。
    _header, _sep, body = ability.partition("\n\n")
    assert "\n\n" not in body, body


def test_system_prompt_mandates_grade_text_and_short_guidance():
    assert "grade_text" in SYSTEM_PROMPT
    assert "必须调用" in SYSTEM_PROMPT
    # 纪律：逐条修改交给界面，模型不要重复罗列
    assert "不要重复罗列" in SYSTEM_PROMPT


def test_grade_result_reply_forbids_claiming_personal_history():
    """真机走查（qwen3.5-flash 批改 + qwen3.8-27b 转述）发现：grade_text 返回后，
    主模型顺嘴说"这个错点你之前记过，今天正好到了复习时间"，但它并没有错词本/学习
    历史数据——本轮记忆目录没给"今日到期复习"区块时，这就是编造的个性化话术。
    「结果怎么给」一节必须显式禁止这类说法。
    """
    assert "你之前记过" in SYSTEM_PROMPT
    assert "到了复习时间" in SYSTEM_PROMPT
    # 禁令挂在批改专线静态段：即使没注册复习工具，这条禁令也在，且不泄漏复习能力专属词
    no_review = build_system_prompt(
        tools=[GradeTextTool, OcrImageTool, DescribeImageTool, RecheckDetectionTool]
    )
    assert "你之前记过" in no_review
    assert "今日到期复习" not in no_review


def test_system_prompt_mentions_ocr_and_recheck_tools():
    """模型只会调用它知道名字的工具。

    系统词里不提，哪怕注册表里注册了也永远不会被调用——
    ocr_image 与 recheck_detection 曾因此长期处于"造好了但接不上"的状态。

    断言用工具类的 ``name`` 而非字面量：工具改名时这里必须跟着炸（守 R4 领域模型单点）。
    """
    assert GradeTextTool.name in SYSTEM_PROMPT
    assert OcrImageTool.name in SYSTEM_PROMPT
    assert RecheckDetectionTool.name in SYSTEM_PROMPT
    assert DescribeImageTool.name in SYSTEM_PROMPT


def test_default_prompt_includes_memory_routing():
    """长期记忆是默认能力：提示词里必须有记忆路由与能力声明，

    否则注册表里注册了 remember 模型也永远不会调。
    """
    assert RememberTool.name in SYSTEM_PROMPT
    assert ForgetTool.name in SYSTEM_PROMPT
    assert ReadMemoryTool.name in SYSTEM_PROMPT
    assert "长期记忆" in SYSTEM_PROMPT


def test_prompt_without_memory_tools_drops_memory_routing():
    """装配根若显式不带记忆工具（tools 里没有 remember），记忆文案必须消失，

    否则模型会去调一个不存在的工具（提示词不该制造幻觉）。
    """
    prompt = build_system_prompt(
        tools=[GradeTextTool, OcrImageTool, DescribeImageTool, RecheckDetectionTool]
    )
    assert RememberTool.name not in prompt
    assert ForgetTool.name not in prompt
    assert ReadMemoryTool.name not in prompt
    assert "长期记忆" not in prompt


def test_prompt_adapts_when_memory_tools_registered():
    """记忆三件套注册后，路由规则与能力声明才出现：

    - 记住/忘掉各自有明确路由，不许模型自己"心里记"；
    - 忘掉必须先 read_memory 拿 key，不能凭空传 key；
    - 能力段声明长期记忆已启用、目录行只是钩子。
    """
    prompt = build_system_prompt(
        tools=[
            GradeTextTool,
            OcrImageTool,
            DescribeImageTool,
            RecheckDetectionTool,
            RememberTool,
            ForgetTool,
            ReadMemoryTool,
        ]
    )
    assert RememberTool.name in prompt
    assert ForgetTool.name in prompt
    assert ReadMemoryTool.name in prompt
    assert "原话照存" in prompt
    assert "先用 read_memory" in prompt
    assert "长期记忆" in prompt


def test_system_prompt_ocr_result_must_be_confirmed_before_grading():
    """流程闸的提示词侧：转写出来的文本是草稿，未确认不得进批改。

    这是三道防污染闸里唯一没有代码级保证的一道，提示词是它的第一道防线。
    """
    assert "只转写、不批改" in SYSTEM_PROMPT
    # 纪律本体是"未经确认不得批改"；"同一轮"只是它的一个测试判据，
    # 只写后者会留下"隔一轮再批就行"的绕路
    assert "未经用户确认不得调用 grade_text" in SYSTEM_PROMPT
    assert "不得在同一轮直接拿去批改" in SYSTEM_PROMPT
    assert "以用户确认后的文本为准" in SYSTEM_PROMPT


def test_confirmation_gate_covers_both_image_tools():
    """「从图里抄出来的原文必须经人确认才能批改」这道闸必须盖住**两个**看图工具。

    只点名 ocr_image 会留下一条绕路：模型走 describe_image 抄出英文原文 →
    直接喂 grade_text，闸就绕过去了。而 describe 抄出来的英文同样不是真相
    （机器认错的字被当成用户写错，是我们场景里最严重的一类错误），
    凭什么不需要确认？这条是 review 抓出来的真口子。

    #64：铁律文本随视觉工具条件挂载，常量本体在 _VISION_GRADING_RULE；
    默认装配（有视觉工具）仍必须把它拼进系统词。
    """
    assert OcrImageTool.name in _VISION_GRADING_RULE
    assert DescribeImageTool.name in _VISION_GRADING_RULE
    assert "未经用户确认不得调用 grade_text" in _VISION_GRADING_RULE
    assert OcrImageTool.name in SYSTEM_PROMPT
    assert "未经用户确认不得调用 grade_text" in SYSTEM_PROMPT


def test_system_prompt_forbids_describing_images_without_a_tool():
    """主模型看不见图，而且会**编**——这条必须用系统词钉死。

    2026-09-25 实测：把同一张图发给 deepseek-v4-pro 两次，一次它编出一段毫不相干的
    "deadline 流程图"描述（说得有鼻子有眼），一次才老实说看不见。
    同一问题两种答案、其中一种是假的，比稳定报错危险得多 —— 用户会当真。

    2026-09-27：这条从身份段挪进了**能力段**，并随主模型能力变化——
    换多模态主模型后"看不见图片"是假的，写死会让模型明明看得见却说看不见。
    """
    # 默认装配 = 纯文本主模型：必须明确说不能直接看图
    assert "不能直接看图" in SYSTEM_PROMPT
    assert "等于编" in SYSTEM_PROMPT


def test_multimodal_main_model_must_not_claim_blindness():
    """换多模态主模型后不许再写"看不见"——否则它看得见却按提示词说看不见。"""
    vision = build_system_prompt(vision_capable=True)
    assert "不能直接看图" not in vision
    assert "看不见" not in vision


# ---------- #64 自定义纯文本端点：视觉工具整组不注册时，提示词软降级 ----------

_NO_VISION_TOOLS = [GradeTextTool, RecheckDetectionTool]
_WITH_VISION_TOOLS = [
    GradeTextTool,
    OcrImageTool,
    DescribeImageTool,
    RecheckDetectionTool,
]


def test_prompt_without_vision_tools_drops_image_routing_and_gate():
    """一个视觉工具都没注册：图片路由两行与图片确认铁律整段消失，不指引模型调不存在的工具。"""
    prompt = build_system_prompt(tools=_NO_VISION_TOOLS)
    assert OcrImageTool.name not in prompt
    assert DescribeImageTool.name not in prompt
    assert "图片里的英文" not in prompt  # 图片路由行
    assert "未经用户确认不得调用 grade_text" not in prompt  # 图片确认铁律


def test_prompt_without_vision_tools_says_images_unavailable():
    """没视觉工具时能力段明说"看不了图、请贴文字"，不再写"靠 ocr / describe 看"。"""
    prompt = build_system_prompt(tools=_NO_VISION_TOOLS)
    assert "看不了图片" in prompt
    assert "不能直接看图" not in prompt  # 那是"有工具但主模型纯文本"的措辞
    assert "能直接看" not in prompt


def test_prompt_with_vision_tools_keeps_image_routing_gate_and_plain_line():
    """有视觉工具（默认装配）：图片路由 + 确认闸 + "靠工具看图"能力行都在，防条件化误伤。"""
    prompt = build_system_prompt(tools=_WITH_VISION_TOOLS)
    assert OcrImageTool.name in prompt
    assert DescribeImageTool.name in prompt
    assert "图片里的英文" in prompt
    assert "未经用户确认不得调用 grade_text" in prompt
    assert "不能直接看图" in prompt  # 主模型纯文本、靠 ocr / describe 看


def test_confirmation_gate_also_covers_reading_the_image_yourself():
    """确认闸必须按**来源**管，不能只点名两个看图工具。

    多模态下模型能自己照着图把英文读出来，这串字同样是"从图里来的"；
    铁律若只说"ocr_image / describe_image 抄的"，这条就绕过去了。
    """
    assert "你自己照着图" in _VISION_GRADING_RULE
    # 默认装配与多模态装配下，铁律都得在（视觉工具注册时整段拼入，这里锁住别被删）
    assert "草稿不是真相" in _VISION_GRADING_RULE
    assert "草稿不是真相" in SYSTEM_PROMPT


def test_system_prompt_does_not_promise_unlimited_rejects():
    """复核次数有上限（单条 2 次 / 单篇 5 次），系统词不能无限承诺"再驳一次"。

    否则老师点到底却发现驳不动，会以为工具坏了。
    """
    assert "次数由界面控制" in SYSTEM_PROMPT


def test_system_prompt_recheck_result_is_still_candidate():
    """复核结论是候选不是定论，拍板权在用户手上。"""
    assert "仍然是候选" in SYSTEM_PROMPT
    assert "不要替用户下结论" in SYSTEM_PROMPT


def test_system_prompt_forbids_sycophancy_on_recheck():
    """与复核提示词的第 0 号铁律同源：不许顺着提异议的人改口。

    否则用户一驳回，模型就在聊天里说"你说得对，这条确实不是错误"，
    等于绕过了那道本该由人来拍的闸门。
    """
    assert "不是让你改口" in SYSTEM_PROMPT


# ---------- 分段结构（改编老百姓能看懂的话：从"一坨"变成"活页夹"）----------


def test_prompt_is_split_into_named_sections():
    """系统词不再是单个常量，而是「静态段在前 + 动态段在后」的有名分段。

    静态在前是 prompt cache 命中的前提；分段有名是为了将来逐段打指纹、做 diff。
    """
    sections = prompt_sections()
    names = [name for name, _ in sections]

    assert [name for name, _ in STATIC_SECTIONS] == list(_EXPECTED_STATIC)
    assert names[: len(_EXPECTED_STATIC)] == list(_EXPECTED_STATIC)
    assert names[len(_EXPECTED_STATIC) :] == list(DYNAMIC_SECTION_NAMES)


def test_every_section_starts_with_a_markdown_heading():
    """每段必须以 ``# 小标题`` 开头 —— 这是将来截取 / 替换单段的定位标记。"""
    for name, body in prompt_sections():
        assert body.lstrip().startswith("# "), f"段 {name} 缺少 # 小标题"


def test_system_prompt_is_the_join_of_all_sections():
    """对外仍然只有 ``SYSTEM_PROMPT`` 一个字符串——装配点（web / examples）不用改。"""
    assert SYSTEM_PROMPT == build_system_prompt(prompt_sections())


def test_tool_list_section_reflects_input_tools():
    """工具清单那一段由注册表生成 —— 给什么工具就写什么工具，没有第二处手写。

    ⚠️ 残留风险（已知，本轮不解决）：静态段的规则文字里也提到了工具名
    （比如"未经用户确认不得调用 grade_text"）。那是刻意的 ——
    现有守护测试就指着这些串，模型也靠它们认工具。所以这里是"两处出现"，
    只有工具清单那一段保证跟着注册表变。
    """
    tools_body = dict(prompt_sections(tools=_ONLY_TWO))["tools"]

    assert "grade_text" in tools_body and "ocr_image" in tools_body
    assert RecheckDetectionTool.name not in tools_body, tools_body


def test_tool_list_with_zero_tools_does_not_emit_empty_bullets():
    """一个工具都没注册时，不许拼出悬空的列表项。"""
    tools_body = dict(prompt_sections(tools=[]))["tools"]

    assert "- " not in tools_body
    assert "没有注册任何工具" in tools_body


def test_build_system_prompt_rejects_conflicting_arguments():
    """sections 与 tools 同时给是歧义调用，必须报错，不能悄悄选一个。"""
    with pytest.raises(ValueError):
        build_system_prompt(prompt_sections(), tools=_ONLY_TWO)


def test_default_tool_list_covers_every_registered_tool():
    """默认装配下，注册表里每个工具都必须出现在清单里。

    历史教训：ocr_image 与 recheck_detection 曾长期"造好了但接不上"，
    根因就是那时系统词只提了 grade_text。
    """
    tools_body = dict(prompt_sections())["tools"]

    for tool in (GradeTextTool, OcrImageTool, RecheckDetectionTool, DescribeImageTool):
        assert tool.name in tools_body
        assert tool.description in tools_body


def test_build_system_prompt_is_deterministic():
    """同样的输入必须拼出逐字节相同的结果 —— 否则 cache 前缀无从稳定。"""
    assert build_system_prompt() == build_system_prompt()
    assert SYSTEM_PROMPT == build_system_prompt(prompt_sections())


def test_sections_do_not_leak_draft_annotations():
    """草案里那类 ``（Q1 = A）`` 批注不许混进最终提示词。"""
    assert "Q1" not in SYSTEM_PROMPT
    assert "已瘦身" not in SYSTEM_PROMPT


# ---------- T05 · 短练习的行为约束 ----------

# ⚠️ 这些断言**只锁契约，不证明行为**。真模型下是否真的"一次只出一两个题"、
# 是否"不提前给答案"，由 tests/tools/test_practice_behavior_integration.py
# 的真实样本守护 —— 提示词里写了不等于模型照做。


def test_practice_section_exists_and_is_named():
    """练习是一段有名静态段，将来能单独打指纹 / 替换。"""
    names = [name for name, _ in STATIC_SECTIONS]
    assert "practice" in names


def test_practice_is_opt_in_not_a_forced_flow():
    """用户只要批改 / 查词时，不许被硬拉进练习流程。"""
    body = dict(STATIC_SECTIONS)["practice"]
    assert "批改" in body and "查词" in body
    assert "不要" in body  # 明确的否定式，而不是柔性建议


def test_practice_limits_batch_size():
    """一次只出 1–2 个短题：直接治"说好五分钟却一次甩四句"。"""
    body = dict(STATIC_SECTIONS)["practice"]
    assert "1–2" in body or "1-2" in body or "两个" in body


def test_practice_forbids_giving_the_answer_early():
    """用户要求独立尝试时，不许提前把答案说了。"""
    body = dict(STATIC_SECTIONS)["practice"]
    assert "提前给答案" in body or "不要给答案" in body


def test_practice_forbids_fabricating_past_sessions():
    """没有记忆依据就不许提"上次"——不虚构学习经历。"""
    body = dict(STATIC_SECTIONS)["practice"]
    assert "上次" in body
    assert "不要" in body or "不" in body


def test_practice_requires_a_honest_wrapup():
    """收尾要分清「独立答对 / 提示后答对 / 仍需练习」，并给出下次可查的一点。"""
    body = dict(STATIC_SECTIONS)["practice"]
    assert "独立答对" in body and "提示后答对" in body and "仍需练习" in body


def test_practice_quotes_the_users_own_sentence():
    """判错要引用用户这次的原句，而不是泛泛讲规则。"""
    body = dict(STATIC_SECTIONS)["practice"]
    assert "原句" in body


def test_practice_has_no_english_specific_state_machine():
    """T05 明确不许写英语专用 runtime 状态机：这一段只能是通用的带练方法。"""
    body = dict(STATIC_SECTIONS)["practice"]
    for forbidden in ("状态机", "必须依次", "按顺序执行步骤"):
        assert forbidden not in body


# ---------- ⑥ 错题复习闭环（掌握度账本驱动的到期复测；条件静态段 mastery）----------

# 与 practice 的区别：practice 是用户随时发起的通用带练；mastery 是记忆目录里
# "今日到期复习"区块驱动、判完必须落账、硬档位由代码算的特定闭环。
# 这些断言只锁提示词契约；真模型是否照做由 ⑧ 的真机端到端验收。


def _mastery_body() -> str:
    return dict(STATIC_SECTIONS)["mastery"]


def test_mastery_section_positioned_right_after_practice():
    """闭环段紧跟通用练习段、排在学习状态段之前，且是有名静态段。"""
    names = [name for name, _ in prompt_sections()]
    assert "mastery" in names
    assert names.index("mastery") == names.index("practice") + 1
    assert names.index("mastery") < names.index("study_state")
    assert _mastery_body().lstrip().startswith("# ")


def test_default_tools_include_record_review_result():
    """record_review_result 在 web 注册表早已启用，默认清单不能再漏它
    （漏了默认装配下闭环段永远不出现，和当年 lookup_word 的"造好了接不上"同病）。"""
    tools_body = dict(prompt_sections())["tools"]
    assert RecordReviewResultTool.name in tools_body


def test_mastery_section_dropped_without_review_tool():
    """没注册 record_review_result 时，整段闭环规则必须消失——
    否则模型会去调一个不存在的工具（提示词不该制造工具幻觉）。"""
    prompt = build_system_prompt(
        tools=[
            GradeTextTool,
            OcrImageTool,
            DescribeImageTool,
            RecheckDetectionTool,
            RememberTool,
            ForgetTool,
            ReadMemoryTool,
        ]
    )
    assert RecordReviewResultTool.name not in prompt
    assert "今日到期复习" not in prompt
    assert "错题复习闭环" not in prompt


def test_mastery_due_reminder_is_restrained():
    """D2/D9：只在用户没派正事时提醒一句；贴作文 / 提问题 / 拒绝就立刻停、
    同会话不重复；逾期或拒绝不惩罚不销账。"""
    body = _mastery_body()
    assert "今日到期复习" in body
    assert "没派正事" in body
    assert "立刻不提" in body
    assert "同一会话" in body and "不重复" in body
    assert "不惩罚" in body and "不销账" in body
    # 没有到期区块时一个字都别提（防模型凭空催复习）
    assert "没有到期区块" in body


def test_mastery_quiz_uses_old_error_as_seed_and_one_question():
    """D4：以本人旧错句为种子换语境出 1 道同考点新题，不原样重问、不一次多题。"""
    body = _mastery_body()
    assert "历史错句" in body
    assert "换主语" in body or "换" in body
    assert "1 道" in body


def test_mastery_quiz_no_trivial_blank_and_correction_fully_correct():
    """⑦评测用真实错题反推的出题护栏，必须写进真机提示词：
    - 括号给的词不能就是答案（不可数名词/拼写点照抄就能填对的"废题"），这类点用改错型；
    - 改错题句子要真有错、改完整句完全正确不引入新错误（many advices→much advice）；
    - 冠词看后词开头发音（honest/hour 用 an）。
    """
    body = _mastery_body()
    assert "废题" in body
    assert "不可数名词" in body and "改错型" in body
    assert "完全正确" in body and "不能引入新错误" in body
    assert "发音" in body and "honest" in body


def test_mastery_locks_answer_before_user_answers():
    """质量保险③：出题时先锚定标准答案 / 等价写法 / 考点，再让用户作答；题面不漏答案。"""
    body = _mastery_body()
    assert "标准答案" in body
    assert "等价写法" in body
    assert "考点" in body
    assert "再把题给用户" in body or "再让用户作答" in body or "再让他作答" in body


def test_mastery_judges_three_verdicts_and_must_record():
    """判三档、hard 必须真给过提示、wrong 讲解给答案，判完必须调记账工具落盘。"""
    body = _mastery_body()
    for verdict in ("independent", "hinted", "wrong"):
        assert verdict in body
    assert "给过提示" in body
    assert RecordReviewResultTool.name in body
    assert "key=" in body
    assert "必须" in body and "落盘" in body
    # 档位 / 到期由工具算，模型不能口头宣布已掌握或改期
    assert "不能口头" in body or "无权" in body


def test_mastery_first_wrong_answer_gets_hint_before_any_verdict():
    """⑧真机发现：学生第一次答错时，模型直接判 wrong 落账并公布答案，跳过了"给提示再试"。

    状态机只有 independent/hinted/wrong 三个落档，没有 give_hint——首次答错是一次复测
    内部的**中间态**：只给一个不泄露答案的指向性提示、让他再试一次，本轮不调
    record_review_result、不判 wrong、不直接公布完整答案；等他根据提示第二次作答后，
    才判 hinted（提示后对）或 wrong（仍错）并落盘。
    """
    body = _mastery_body()
    # 首次答错 = 中间态：先提示、再试、不落账
    assert "第一次答错" in body or "首次答错" in body
    assert "再试一次" in body
    assert "先不要" in body and "落盘" in body
    assert RecordReviewResultTool.name in body
    # 首次提示不许直接把完整答案说出来
    assert "不直接公布" in body or "不公布完整答案" in body
    # 最终档位在给过提示后的第二次作答才判
    assert "第二次作答" in body or "再答一次" in body


def test_mastery_user_is_final_judge_and_false_judgement_not_counted():
    """质量保险④：用户可反驳，误判改判，不把争议答案落成 wrong。"""
    body = _mastery_body()
    assert "终审" in body
    assert "误判不计" in body


def test_mastery_after_settling_asks_once_then_remembers_with_code():
    """D16/D3/D17：定稿后先一句话征求，点头才对采纳的硬错批量 remember 并带
    error_code；没点头不偷记；被撤销 / 改判 / 主观建议不进复习账本。"""
    body = _mastery_body()
    assert "定稿" in body
    assert "征求" in body
    assert RememberTool.name in body
    assert "error_code" in body
    assert "没点头" in body or "不要偷偷记" in body
    # 主观 / 被推翻的不进账本
    assert "中式英语" in body


# ---------- T07 练习尝试记录（record/read/delete；条件静态段 attempt_records）----------


def _attempts_body() -> str:
    return dict(STATIC_SECTIONS)["attempt_records"]


def test_attempt_section_positioned_right_after_mastery():
    """尝试记录段紧跟错题复习闭环、在学习状态段之前。"""
    names = [name for name, _ in prompt_sections()]
    assert "attempt_records" in names
    assert names.index("attempt_records") == names.index("mastery") + 1
    assert names.index("attempt_records") < names.index("study_state")
    assert _attempts_body().lstrip().startswith("# ")


def test_default_tools_include_attempt_tools():
    """三工具在 web 注册表启用后，默认清单不能漏（否则"造好了接不上"）。"""
    tools_body = dict(prompt_sections())["tools"]
    for cls in (RecordAttemptTool, ReadAttemptsTool, DeleteAttemptTool):
        assert cls.name in tools_body


def test_attempt_section_dropped_without_record_tool():
    """没注册 record_attempt 时，整段尝试记录规则必须消失，不制造工具幻觉。"""
    prompt = build_system_prompt(
        tools=[
            GradeTextTool,
            OcrImageTool,
            DescribeImageTool,
            RecheckDetectionTool,
            RememberTool,
            ForgetTool,
            ReadMemoryTool,
        ]
    )
    assert RecordAttemptTool.name not in prompt
    assert ReadAttemptsTool.name not in prompt
    assert "练习尝试记录" not in prompt


def test_attempt_record_requires_explicit_user_confirmation():
    """确认闸门：只有用户明确同意才记，confirmation 填原话，模型自总结不能冒充。"""
    body = _attempts_body()
    assert "明确同意保存" in body
    assert "confirmation" in body
    assert "自我总结不能代替" in body


def test_attempt_three_outcomes_are_listed():
    """三档结果逐字写进规则，且 answer 要逐字、不替用户改正。"""
    body = _attempts_body()
    for outcome in ("independent_correct", "correct_after_hint", "needs_practice"):
        assert outcome in body
    assert "逐字" in body and "不要替他改正" in body


def test_attempt_no_single_record_mastery_claim():
    """不得凭一条尝试就声称掌握——掌握度由另一套账本说了算。"""
    body = _attempts_body()
    assert "已经掌握" in body  # 出现在"不要凭一条记录就说他已经掌握"的禁止语境里


def test_attempt_read_only_for_practice_and_never_fabricates():
    """read 只在继续练习 / 复习 / 安排学习时用；批改查词闲聊不读；无记录不编造。"""
    body = _attempts_body()
    assert "继续练习" in body
    assert "普通批改、查词、翻译、闲聊" in body
    assert "不直接泄露旧答案" in body
    assert "不要编造" in body and "上次练过" in body


def test_attempt_delete_requires_request_and_no_inplace_edit():
    """删除须用户明确要求；不提供原地改答案，更正=删旧+确认记新。"""
    body = _attempts_body()
    assert "明确要求删" in body
    assert "不提供原地改答案" in body
    assert "绝不静默改写" in body


# ---------- 收词卡（propose_wordbook_add；routing 段条件行）----------


def test_wordbook_routing_names_proposal_and_forbids_claiming_saved():
    """收词路由必须指名工具，并把“用户自己点、不许说已经收了”写死。"""
    prompt = build_system_prompt(
        tools=[_ToolSpec(name="propose_wordbook_add", description="递一张收词确认卡")]
    )
    assert "propose_wordbook_add" in prompt
    assert "收进生词本" in prompt
    assert "不要替他点" in prompt
    assert "不要说已经收好了" in prompt


def test_prompt_without_wordbook_tool_drops_routing():
    """没注册收词工具时，路由里一个字都不能提，避免模型调不存在的工具。"""
    prompt = build_system_prompt(tools=[GradeTextTool, OcrImageTool])
    assert "propose_wordbook_add" not in prompt
    assert "收词卡" not in prompt


# ---------- 说话风格（tone 段：三档共用底线 + 当档口吻；换风格只换这一段）----------


def test_tone_section_sits_right_after_identity():
    """tone 段必须紧跟 identity：先立"你是谁"，紧接着定"用什么口吻说话"。"""
    names = [name for name, _ in prompt_sections()]
    assert names.index("tone") == names.index("identity") + 1


def test_default_speaking_style_is_natural():
    """不传 style 时默认自然档：口吻标自然，且不出现风趣档专属的"开玩笑"引导。"""
    prompt = build_system_prompt()
    assert "你的口吻：自然" in prompt
    assert "你的口吻：风趣" not in prompt
    assert "开玩笑" not in prompt


def test_every_speaking_style_keeps_the_shared_baseline():
    """三档都必须带同一条说话底线：反奉承 / 先给结果 / 默认Markdown排版 / 风格不改结论。"""
    for style in ("natural", "witty", "calm"):
        tone = dict(prompt_sections(style=style))["tone"]
        assert "诚实比好听重要" in tone
        assert "先给结果或动作" in tone
        # 默认就用 Markdown 组织回复，且同一组列表编号必须连续
        assert "默认用 Markdown" in tone
        assert "编号必须连续" in tone
        assert "风格只改口吻，不改结论" in tone


def test_speaking_style_changes_only_the_tone_section():
    """换风格只能改 tone 段：三档下其余所有业务段逐字节相同（换皮不换料）。"""
    by_style = {
        style: [(n, b) for n, b in prompt_sections(style=style) if n != "tone"]
        for style in ("natural", "witty", "calm")
    }
    assert by_style["natural"] == by_style["witty"] == by_style["calm"]

    tones = {
        style: dict(prompt_sections(style=style))["tone"]
        for style in ("natural", "witty", "calm")
    }
    assert tones["natural"] != tones["witty"]
    assert tones["witty"] != tones["calm"]
    assert "你的口吻：风趣" in tones["witty"]
    assert "你的口吻：冷静" in tones["calm"]
    # 冷静档必须保留"不可省"白名单：关键纠错 / 失败 / 用户要展开不能省。
    assert "不能省" in tones["calm"]
    # 风趣档必须带安全绳：玩笑不冲人、不偏结论。
    assert "绝不冲他这个人" in tones["witty"]


def test_unknown_speaking_style_falls_back_to_natural():
    """脏 style 值不报错，静默回退自然档。"""
    tone = dict(prompt_sections(style="whatever"))["tone"]
    assert tone == dict(prompt_sections(style="natural"))["tone"]


def test_explicit_sections_path_ignores_style():
    """显式给 sections 时 style 无权改动它（只有默认 / 工具装配路径才按 style 选段）。"""
    explicit = prompt_sections()
    assert build_system_prompt(explicit, style="witty") == build_system_prompt(explicit)


def test_multimodal_line_tells_model_to_admit_it_cannot_see() -> None:
    """勾了「图片」但模型其实读不出：不报错、不拦截，让模型自己说看不到（2026-10-31 定）。"""
    from glyph.agent_prompt import _VISION_ABILITY_LINE_MULTIMODAL

    assert "读不出这张图" in _VISION_ABILITY_LINE_MULTIMODAL
    assert "不要编" in _VISION_ABILITY_LINE_MULTIMODAL