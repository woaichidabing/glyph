/**
 * ask_user 答案回传接口。
 * 后端 web/server.py POST /api/ask-user/answer：
 *   body {choice?: string, text?: string, question?: string}，永远回 200 {ok: boolean}。
 * ok=false 不是错误：那是一次迟到点击（重复提交 / 轮次已取消 / 问题对不上），
 * 界面静默收卡即可，绝不弹错误提示。
 */

export type AskAnswerRequest = {
  choice?: string | null;
  text?: string | null;
  question: string;
};

/**
 * 把用户的答复送给挂起中的 ask_user 工具。
 * @returns ok=false 表示后端没有挂着的问题可接（迟到点击），调用方静默收卡；
 *          网络异常（fetch reject）由调用方接住并允许用户重试。
 */
export async function answerAskUser(req: AskAnswerRequest): Promise<boolean> {
  const res = await fetch("/api/ask-user/answer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      choice: req.choice ?? null,
      text: req.text ?? null,
      question: req.question,
    }),
  });
  if (!res.ok) return false;
  const data = (await res.json().catch(() => ({}))) as { ok?: unknown };
  return data?.ok === true;
}
