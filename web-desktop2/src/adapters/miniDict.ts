import type { DictEntry } from "../types";

/**
 * 内置 mini 词库（词典适配器的第三级兜底）。
 *
 * 方案里定的是 ~2000 词，那是接后端前由脚本从 ECDICT 裁剪后生成的产物；
 * 这里只放一批高频词作为"后端未起时 UI 仍可演示"的样例，
 * 后续用 `scripts/build-mini-dict.mjs` 覆盖本文件即可，接口不变。
 */
type Raw = Omit<DictEntry, "source">;

const RAW: Raw[] = [
  {
    word: "ambiguous",
    phonetic: { uk: "/æmˈbɪɡjuəs/", us: "/æmˈbɪɡjuəs/" },
    senses: [
      {
        pos: "adj.",
        translation: "模棱两可的；含糊不清的；有歧义的",
        examples: [
          { en: "The contract was ambiguous about who would pay the fees.", zh: "这份合同对由谁支付费用规定得含糊不清。" },
          { en: "His reply was deliberately ambiguous.", zh: "他的回答是故意含糊其辞的。" },
        ],
      },
    ],
    forms: ["ambiguity (n.)", "ambiguously (adv.)"],
    synonyms: ["vague", "equivocal"],
    antonyms: ["explicit", "unambiguous"],
    tags: ["CET6", "考研", "雅思"],
  },
  {
    word: "measure",
    phonetic: { uk: "/ˈmeʒə(r)/", us: "/ˈmeʒər/" },
    senses: [
      {
        pos: "n.",
        translation: "措施；办法；尺度；计量单位",
        examples: [
          { en: "The government should take measures to address the issue.", zh: "政府应当采取措施处理这一问题。" },
          { en: "An inch is a measure of length.", zh: "英寸是长度的计量单位。" },
        ],
      },
      {
        pos: "v.",
        translation: "测量；衡量；估量",
        examples: [{ en: "It is hard to measure the damage caused by the storm.", zh: "暴风雨造成的损失难以估量。" }],
      },
    ],
    forms: ["measures (pl.)", "measurable (adj.)"],
    synonyms: ["step", "action"],
    tags: ["CET4", "考研"],
  },
  {
    word: "despite",
    phonetic: { uk: "/dɪˈspaɪt/", us: "/dɪˈspaɪt/" },
    senses: [
      {
        pos: "prep.",
        translation: "尽管；虽然（后接名词或动名词，不接句子）",
        examples: [
          { en: "Despite the rain, they went on with the match.", zh: "尽管下着雨，他们还是继续比赛。" },
          { en: "She smiled despite feeling nervous.", zh: "她虽然紧张，还是笑了。" },
        ],
      },
    ],
    synonyms: ["in spite of"],
    antonyms: ["because of"],
    tags: ["CET4", "考研"],
  },
  {
    word: "affect",
    phonetic: { uk: "/əˈfekt/", us: "/əˈfekt/" },
    senses: [
      {
        pos: "v.",
        translation: "影响；感染；使感动",
        examples: [
          { en: "Lack of sleep affects your concentration.", zh: "睡眠不足会影响你的注意力。" },
          { en: "The whole audience was deeply affected.", zh: "全场观众深受感动。" },
        ],
      },
    ],
    forms: ["effect (n. 结果)", "affected (adj.)"],
    synonyms: ["influence", "impact"],
    tags: ["CET4", "考研"],
  },
  {
    word: "comprise",
    phonetic: { uk: "/kəmˈpraɪz/", us: "/kəmˈpraɪz/" },
    senses: [
      {
        pos: "v.",
        translation: "包含；由……组成（整体包含部分，不用于被动的 be comprised of 争议用法）",
        examples: [
          { en: "The collection comprises 120 short stories.", zh: "这本集子收录了 120 篇短篇小说。" },
          { en: "The committee comprises five members.", zh: "委员会由五名成员组成。" },
        ],
      },
    ],
    synonyms: ["consist of", "be made up of"],
    tags: ["CET6", "考研", "雅思"],
  },
  {
    word: "principle",
    phonetic: { uk: "/ˈprɪnsəpl/", us: "/ˈprɪnsəpl/" },
    senses: [
      {
        pos: "n.",
        translation: "原则；准则；原理",
        examples: [
          { en: "He refused to lie on principle.", zh: "他出于原则拒绝说谎。" },
          { en: "The machine works on the same principle.", zh: "这台机器依同样的原理工作。" },
        ],
      },
    ],
    forms: ["principal (adj. 主要的 / n. 校长)"],
    synonyms: ["rule", "doctrine"],
    tags: ["CET4", "考研"],
  },
  {
    word: "stationary",
    phonetic: { uk: "/ˈsteɪʃənri/", us: "/ˈsteɪʃəneri/" },
    senses: [
      {
        pos: "adj.",
        translation: "静止的；不动的；固定的",
        examples: [
          { en: "The car remained stationary at the lights.", zh: "汽车在信号灯前保持静止。" },
          { en: "A stationary target is easy to hit.", zh: "固定靶很容易击中。" },
        ],
      },
    ],
    forms: ["stationery (n. 文具，注意拼写差别)"],
    antonyms: ["moving", "mobile"],
    tags: ["CET6", "考研"],
  },
  {
    word: "complement",
    phonetic: { uk: "/ˈkɒmplɪment/", us: "/ˈkɑːmplɪment/" },
    senses: [
      {
        pos: "n. / v.",
        translation: "补充（物）；补足；与……相辅相成",
        examples: [
          { en: "The wine is a perfect complement to the dish.", zh: "这款酒与这道菜相得益彰。" },
          { en: "His skills complement hers.", zh: "他的专长正好补上了她的不足。" },
        ],
      },
    ],
    forms: ["compliment (n./v. 赞美，注意区别)"],
    synonyms: ["supplement"],
    tags: ["CET6", "雅思"],
  },
  {
    word: "imply",
    phonetic: { uk: "/ɪmˈplaɪ/", us: "/ɪmˈplaɪ/" },
    senses: [
      {
        pos: "v.",
        translation: "暗示；意味着；含有……之意",
        examples: [
          { en: "Her silence implied disagreement.", zh: "她的沉默意味着不同意。" },
          { en: "Rights imply duties.", zh: "权利意味着义务。" },
        ],
      },
    ],
    forms: ["implication (n.)"],
    synonyms: ["suggest", "hint"],
    tags: ["CET4", "考研"],
  },
  {
    word: "consistent",
    phonetic: { uk: "/kənˈsɪstənt/", us: "/kənˈsɪstənt/" },
    senses: [
      {
        pos: "adj.",
        translation: "一致的；始终如一的；符合的",
        examples: [
          { en: "Her results are consistent with our theory.", zh: "她的结果与我们的理论相符。" },
          { en: "You need a consistent study routine.", zh: "你需要一份稳定持续的学习节奏。" },
        ],
      },
    ],
    forms: ["consistency (n.)", "consistently (adv.)"],
    antonyms: ["inconsistent"],
    tags: ["CET4", "考研"],
  },
  {
    word: "deliberate",
    phonetic: { uk: "/dɪˈlɪbərət/", us: "/dɪˈlɪbərət/" },
    senses: [
      {
        pos: "adj.",
        translation: "故意的；蓄意的；从容不迫的",
        examples: [
          { en: "The omission was deliberate, not accidental.", zh: "这处遗漏是故意的，不是疏忽。" },
          { en: "He spoke in a deliberate manner.", zh: "他不紧不慢地说着。" },
        ],
      },
    ],
    forms: ["deliberately (adv.)"],
    synonyms: ["intentional"],
    antonyms: ["accidental"],
    tags: ["CET6", "考研"],
  },
  {
    word: "elaborate",
    phonetic: { uk: "/ɪˈlæbərət/", us: "/ɪˈlæbərət/" },
    senses: [
      {
        pos: "adj. / v.",
        translation: "精心制作的；详尽的 / 详细阐述",
        examples: [
          { en: "She prepared an elaborate plan for the trip.", zh: "她为这次出行准备了一份周密的计划。" },
          { en: "Could you elaborate on that point?", zh: "你能就这一点展开说说吗？" },
        ],
      },
    ],
    forms: ["elaborately (adv.)", "elaboration (n.)"],
    tags: ["CET6", "雅思"],
  },
  {
    word: "fragile",
    phonetic: { uk: "/ˈfrædʒaɪl/", us: "/ˈfrædʒl/" },
    senses: [
      {
        pos: "adj.",
        translation: "脆弱的；易碎的；不牢固的",
        examples: [
          { en: "The ceasefire remains fragile.", zh: "停火仍然脆弱。" },
          { en: "Handle it with care — it is fragile.", zh: "轻拿轻放——它易碎。" },
        ],
      },
    ],
    forms: ["fragility (n.)"],
    synonyms: ["brittle", "delicate"],
    antonyms: ["sturdy", "robust"],
    tags: ["CET6"],
  },
  {
    word: "generate",
    phonetic: { uk: "/ˈdʒenəreɪt/", us: "/ˈdʒenəreɪt/" },
    senses: [
      {
        pos: "v.",
        translation: "产生；生成；引起",
        examples: [
          { en: "The plan generated a lot of interest.", zh: "这个计划引发了广泛的兴趣。" },
          { en: "Wind can be used to generate electricity.", zh: "风可以用来发电。" },
        ],
      },
    ],
    forms: ["generation (n.)", "generator (n.)"],
    synonyms: ["produce", "create"],
    tags: ["CET4", "考研"],
  },
  {
    word: "hesitate",
    phonetic: { uk: "/ˈhezɪteɪt/", us: "/ˈhezɪteɪt/" },
    senses: [
      {
        pos: "v.",
        translation: "犹豫；迟疑；踌躇",
        examples: [
          { en: "Do not hesitate to ask for help.", zh: "有需要尽管开口，不必迟疑。" },
          { en: "She hesitated before answering.", zh: "她迟疑了一下才回答。" },
        ],
      },
    ],
    forms: ["hesitation (n.)", "hesitant (adj.)"],
    synonyms: ["waver"],
    tags: ["CET4"],
  },
  {
    word: "impose",
    phonetic: { uk: "/ɪmˈpəʊz/", us: "/ɪmˈpoʊz/" },
    senses: [
      {
        pos: "v.",
        translation: "强加；施加；征（税）",
        examples: [
          { en: "The city imposed a ban on night flights.", zh: "该市强制实施了夜间禁飞。" },
          { en: "Do not impose your views on others.", zh: "不要把你的看法强加于人。" },
        ],
      },
    ],
    forms: ["imposition (n.)"],
    tags: ["CET6", "考研"],
  },
  {
    word: "justify",
    phonetic: { uk: "/ˈdʒʌstɪfaɪ/", us: "/ˈdʒʌstɪfaɪ/" },
    senses: [
      {
        pos: "v.",
        translation: "证明……正当；为……辩解",
        examples: [
          { en: "Nothing can justify such behaviour.", zh: "没有什么能为这样的行为开脱。" },
          { en: "You must justify your conclusion with evidence.", zh: "你必须用证据支撑自己的结论。" },
        ],
      },
    ],
    forms: ["justification (n.)", "justified (adj.)"],
    synonyms: ["defend", "warrant"],
    tags: ["CET4", "考研"],
  },
  {
    word: "maintain",
    phonetic: { uk: "/meɪnˈteɪn/", us: "/meɪnˈteɪn/" },
    senses: [
      {
        pos: "v.",
        translation: "维持；保持；保养；坚称",
        examples: [
          { en: "It is hard to maintain a balance between work and study.", zh: "在工作与学习之间保持平衡并不容易。" },
          { en: "He maintains that he is innocent.", zh: "他坚称自己是无辜的。" },
        ],
      },
    ],
    forms: ["maintenance (n.)"],
    synonyms: ["sustain", "preserve"],
    tags: ["CET4", "考研"],
  },
  {
    word: "negotiate",
    phonetic: { uk: "/nɪˈɡəʊʃieɪt/", us: "/nɪˈɡoʊʃieɪt/" },
    senses: [
      {
        pos: "v.",
        translation: "谈判；协商；顺利通过（难关）",
        examples: [
          { en: "The two sides negotiated a new contract.", zh: "双方谈成了一份新合同。" },
          { en: "The driver negotiated the narrow street carefully.", zh: "司机小心翼翼地穿过了那条窄街。" },
        ],
      },
    ],
    forms: ["negotiation (n.)", "negotiable (adj.)"],
    tags: ["CET4", "考研"],
  },
  {
    word: "obstacle",
    phonetic: { uk: "/ˈɒbstəkl/", us: "/ˈɑːbstəkl/" },
    senses: [
      {
        pos: "n.",
        translation: "障碍；阻碍；绊脚石",
        examples: [
          { en: "Lack of time is the main obstacle.", zh: "时间不足是主要的障碍。" },
          { en: "She overcame every obstacle on the way.", zh: "她克服了途中的每一道障碍。" },
        ],
      },
    ],
    synonyms: ["barrier", "hindrance"],
    tags: ["CET4", "考研"],
  },
  {
    word: "persuade",
    phonetic: { uk: "/pəˈsweɪd/", us: "/pərˈsweɪd/" },
    senses: [
      {
        pos: "v.",
        translation: "说服；劝说；使相信",
        examples: [
          { en: "I persuaded him to change his mind.", zh: "我说服他改变了主意。" },
          { en: "She persuaded me that the plan would work.", zh: "她让我相信这个计划行得通。" },
        ],
      },
    ],
    forms: ["persuasion (n.)", "persuasive (adj.)"],
    synonyms: ["convince"],
    tags: ["CET4"],
  },
  {
    word: "reluctant",
    phonetic: { uk: "/rɪˈlʌktənt/", us: "/rɪˈlʌktənt/" },
    senses: [
      {
        pos: "adj.",
        translation: "不情愿的；勉强的",
        examples: [
          { en: "He was reluctant to admit the mistake.", zh: "他不愿承认这个错误。" },
          { en: "She gave a reluctant nod.", zh: "她勉强点了点头。" },
        ],
      },
    ],
    forms: ["reluctance (n.)", "reluctantly (adv.)"],
    antonyms: ["willing", "eager"],
    tags: ["CET6", "考研"],
  },
  {
    word: "significant",
    phonetic: { uk: "/sɪɡˈnɪfɪkənt/", us: "/sɪɡˈnɪfɪkənt/" },
    senses: [
      {
        pos: "adj.",
        translation: "重要的；显著的；有意义的",
        examples: [
          { en: "There has been a significant rise in scores.", zh: "分数有了显著提高。" },
          { en: "This is a significant step forward.", zh: "这是向前迈出的重要一步。" },
        ],
      },
    ],
    forms: ["significance (n.)", "significantly (adv.)"],
    synonyms: ["notable", "considerable"],
    antonyms: ["insignificant"],
    tags: ["CET4", "考研"],
  },
  {
    word: "withdraw",
    phonetic: { uk: "/wɪðˈdrɔː/", us: "/wɪðˈdrɔː/" },
    senses: [
      {
        pos: "v.",
        translation: "撤回；撤退；提取（存款）",
        examples: [
          { en: "He withdrew his application.", zh: "他撤回了申请。" },
          { en: "She withdrew some money from the account.", zh: "她从账户里取了一些钱。" },
        ],
      },
    ],
    forms: ["withdrew (过去式)", "withdrawn (过去分词)", "withdrawal (n.)"],
    antonyms: ["deposit", "submit"],
    tags: ["CET4", "考研"],
  },
];

/** 小写词 → 词条 */
export const MINI: Record<string, DictEntry> = Object.fromEntries(
  RAW.map((r) => [r.word.toLowerCase(), { ...r, source: "mini" as const }]),
);

export const MINI_WORDS: string[] = RAW.map((r) => r.word);

/** 热门词：空态与联想用 */
export const HOT_WORDS: string[] = [
  "ambiguous",
  "measure",
  "significant",
  "justify",
  "reluctant",
  "consistent",
  "comprise",
  "persuade",
];
