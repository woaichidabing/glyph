"""ask 路由（独立模块，app.include_router 挂载）。"""

from fastapi import APIRouter

from .. import runtime as rout
from ..deps import state

router = APIRouter()


def _sse_data(payload: dict) -> str:
    return f"data: {rout.json.dumps(payload, ensure_ascii=False)}\n\n"


# 网络类错误只用于把提示翻成人话（"能不能重试"的判定仍以 markr/retry.py 为准）。
_NET_HINTS = (
    "timeout",
    "timed out",
    "connection",
    "connect",
    "unreachable",
    "reset by peer",
    "网络",
    "超时",
    "断线",
)


def _humanize_stream_error(exc: Exception) -> str:
    """把 run 生成器里冒出的异常翻成一句给用户看的话，绝不把堆栈 / 英文报错直接糊脸。

    归类与 ``markr/retry.py`` 单点对齐：额度 / 鉴权 / 403 用 ``is_quota_or_auth_error``
    识别（确定性错误，明确引导换模型或查钥匙，而不是让前端误报成"连接中断、状态未确认"）；
    上下文超长引导精简 / 新会话；网络问题引导重试。
    """
    blob = str(exc).lower()
    if rout.is_quota_or_auth_error(exc):
        return (
            "这个模型现在用不了：账号里可能没开通，或者余额/额度已经用完。到模型服务商的控制台开通或充值后再试。"
            "换一个模型再试，或到设置里检查 API Key。"
        )
    if rout.is_context_too_long(exc):
        return "这轮内容太长、超出模型上下文。请新开一个对话，或减少图片 / 材料后再试。"
    if any(h in blob for h in _NET_HINTS):
        return "连接模型超时或网络不稳，可以点「重试」再发一次。"
    detail = str(exc).strip().replace("\n", " ")[:200] or "未知错误"
    return f"出了点问题：{detail}"


async def _sse_run_frames(
    gen: rout.AsyncIterator[rout.Any],
    *,
    busy: bool,
    heartbeat_seconds: float = rout._SSE_HEARTBEAT_SECONDS,
) -> rout.AsyncIterator[str]:
    """把 ``AgentLoop.run`` 的事件流包成 SSE 文本帧，供 /api/ask 统一使用。

    - 前一个任务还在跑（busy）时先发 queued，别让前端误判成卡死；
    - 静默期每 ``heartbeat_seconds`` 秒发一帧注释心跳，保活且喂前端看门狗；
    - 生成器抛任何异常都转成一帧人话 ``error`` 再收尾，绝不让响应裸中断——
      否则前端 reader 只收到"流结束却没终态"，会把额度错误误报成连接中断。

    客户端主动断开抛的是 CancelledError / GeneratorExit（BaseException，不在
    ``Exception`` 内），不在此捕获，交给外层 finally 关生成器、释放 run 锁。
    """
    if busy:
        yield _sse_data({"type": "queued", "message": "前一个任务正在执行，已排队等待…"})

    # 常驻一个"取下一事件"的任务，与心跳超时竞速。
    # 关键：必须用 asyncio.shield 包住它——wait_for 超时只会取消"等待"，不能取消
    # 生成器内部正在跑的那一步（批改 / OCR 可能正在 await），否则一次心跳就把 run 掐死。
    pending: rout.asyncio.Future = rout.asyncio.ensure_future(gen.__anext__())
    try:
        while True:
            try:
                event = await rout.asyncio.wait_for(
                    rout.asyncio.shield(pending), timeout=heartbeat_seconds
                )
            except TimeoutError:
                # 工具在跑、暂时没事件：发心跳保活；pending 仍在后台跑，下一轮继续等同一个
                yield rout._SSE_HEARTBEAT
                continue
            except StopAsyncIteration:
                break
            except Exception as exc:  # noqa: BLE001 - SSE 边界：任何业务异常都要变成人话 error 帧
                yield _sse_data({"type": "error", "message": _humanize_stream_error(exc)})
                return
            # 已拿到这一帧：立刻挂上下一步取数（发帧的同时生成器在后台继续），再吐出本帧
            pending = rout.asyncio.ensure_future(gen.__anext__())
            yield _sse_data(event.model_dump(mode="json"))
    finally:
        if not pending.done():
            pending.cancel()


class AskUserAnswerRequest(rout.BaseModel):
    """用户对"模型问的那句话"的答复。

    ``choice`` 是他点的选项标签，``text`` 是他自己打的字。**两个都给了就按 text 算** ——
    他愿意打字通常是在改需求（"先别改，讲思路"），当成"选了个选项"意思就完全反了。
    """

    choice: str | None = None
    text: str | None = None
    # 他答复的那张卡片上的问题原文。带上就对账：跟当前挂着的那句对不上就当迟到点击
    # 丢掉（防答案进错问题）。前端渲染卡片时本来就有这段话，带回来几乎零成本。
    question: str | None = None


@router.post("/api/ask-user/answer")
async def answer_ask_user(req: AskUserAnswerRequest) -> dict:
    """把用户的答复交给挂起中的 ask_user 工具，让它带着答案继续原来的任务。

    问题本身**不走这个接口**：它由 SSE 里那条普通的 ``tool_call`` 事件带出去
    （``name == "ask_user"``，参数里就是 question 和 options），前端照它渲染卡片即可。
    这里只负责"答案回来"这一半。

    ⚠️ **给前端的配对契约**（卡片画上去之前先看这条）：``tool_call`` 是在工具**执行前**
    发出的，所以模型参数填错（只给 1 个选项等）时也会有一帧 ``tool_call``，工具随后
    立刻返回 ``status="steer"`` —— 那一问**根本没发到界面上**。因此卡片必须：
    ① 用 ``call.id`` 与随后的 ``tool_result`` 配对；② 收到配对结果就把卡片收掉。
    否则用户会看到一张点什么都没反应、永远转圈的死卡片。回答时请把卡片上的
    ``question`` 原样带回来（见 :class:`AskUserAnswerRequest`）。

    为什么没有待回答的问题也回 200（ok=False）而不是 4xx：那只是**一次迟到的点击**
    （重复提交 / 手慢了几秒 / 这一轮早就被取消了），报错会让前端弹一个用户看不懂的
    提示；什么都不做、让他把卡片关掉，就是最对的处理。
    """
    ok = state.ask_gateway.answer(choice=req.choice, text=req.text or "", question=req.question)
    return {"ok": ok}


@router.post("/api/cancel")
async def cancel(session_id: str | None = None) -> dict:
    """取消一个正在跑的 run。

    带 session_id 时只掐**它自己**的：池模型下每个会话都有自己的循环，不认会话的话
    别人按停止会把你正在跑的任务掐掉。
    不带 session_id 沿用旧行为（掐最近活跃的那个），兼容测试台与非桌面端调用方。
    """
    target_loop = None
    if session_id:
        session = state.sessions.get(session_id)
        if session is not None:
            target_loop = session.loop
        elif session_id == state.session_id:
            target_loop = state.loop  # 兼容：镜像指向的会话还没进池（老调用方 / 测试）
    else:
        session = rout._latest_session()
        target_loop = session.loop if session is not None else state.loop
    if target_loop is None:
        return {"ok": False, "reason": "not_current_session"}
    target_loop.cancel()
    return {"ok": True}


# ---------- 桌面端入口（multipart：文本 + 多图）与钥匙配置 ----------


@router.post("/api/ask")
async def ask(
    text: rout.Annotated[str, rout.Form()] = "",
    images: rout.Annotated[list[rout.UploadFile] | None, rout.File()] = None,
    study_context: rout.Annotated[str | None, rout.Form()] = None,
    # 这一轮必须写在哪个会话里。桌面端每个标签页各持一个 id，服务端据此对齐——
    # 不传就沿用当前会话（兼容测试台），传了就一定是它，打不开会开新的并回传新 id。
    session_id: rout.Annotated[str | None, rout.Form()] = None,
) -> rout.StreamingResponse:
    """桌面端问答入口：一次收下文本 + 多张图片 + 只读学习状态摘要。

    图片进内存仓换 image_id，再以一句事实陈述拼进消息（与测试台 app.js 同一约定，
    不替用户下命令）；随后交给 AgentLoop，由模型自主决定调看图 / 转写 / 批改等工具，
    SSE 把全过程回传。前端只负责递东西和显示。

    ``study_context`` 是 T06 的只读学习状态摘要（JSON 串），由桌面端随请求带上：
    后端不独立读浏览器本地存储，只把它渲染成每轮上下文片段。
    没带 / 解析不出来就是没有这段，Agent 按提示词要求说不知道，不自己编进度。
    """
    assert state.settings is not None
    settings = state.settings
    image_list = images or []

    # 会话 id 形状不对就在入口拒掉：进了 SSE 再抛，前端只能看到"流断了却没终态"。
    if session_id:
        rout._safe_session_id(session_id)
    # 先把这一轮的会话取出来（池里有就拿、没有就打开）。
    try:
        session = await rout._ensure_session(session_id)
    except rout.HTTPException:
        raise
    except Exception as exc:
        raise rout.HTTPException(status_code=503, detail=f"会话准备失败：{exc}") from exc
    # 摘要先解析出来，**等拿到闸才写进会话**：排队中的请求若先写，上一轮后续的模型调用
    # 会读到"别人那一轮"的进度与附件（这就是改造前两个标签页串台的翻版）。
    parsed_study = rout._parse_study_context(study_context)

    attachments: list[dict[str, str]] = []
    for img in image_list:
        raw = await img.read()
        ext = rout.Path(img.filename or "").suffix.lower()
        is_image = ext in rout._IMAGE_EXTS or (img.content_type or "").startswith("image/")
        if not is_image:
            raise rout.HTTPException(
                status_code=415,
                detail=f"暂不支持 {ext or '该类型'} 文件，请直接发图片",
            )
        if len(raw) > settings.ocr_max_bytes:
            limit_mb = settings.ocr_max_bytes // 1024 // 1024
            raise rout.HTTPException(
                status_code=413, detail=f"图片过大，请压缩到 {limit_mb}MB 以内"
            )
        mime = rout._IMAGE_MIME.get(ext) or img.content_type or "application/octet-stream"
        image_id = state.image_store.put(raw, mime)
        attachments.append({"filename": img.filename or "未命名", "image_id": image_id})

    body = text.strip()
    if body:
        message = body
    elif attachments:
        # 用户只发图没打字：给一条最小事实坐进 user message，不替他说"请批改"。
        message = f"（附图 {len(attachments)} 张）"
    else:
        raise rout.HTTPException(status_code=400, detail="请输入问题或添加图片")

    async def event_stream() -> rout.AsyncIterator[str]:
        # 整段（绑会话 + 跑）都在**本条会话自己的闸**内：同一个会话的两个请求必须排队
        # （否则同一个本子被两条流同时写），不同会话各用各的闸，互不阻塞。
        #
        # 绑定放在等闸之前：排队的这一段也已经属于这个会话了，工具/上下文回调
        # 若在排队期间被问到"当前是谁"，答案必须是它而不是"最后被激活的那个"。
        token = rout._activate(session)
        try:
            # 取闸带超时（BUG-03）：闸被上一轮占着时，旧请求要等下一帧写失败（最长
            # _SSE_HEARTBEAT_SECONDS）才被回收；这段等待里若一帧不发，用户会以为卡死。
            # 故"超时就先吐一帧排队提示"，等待期间始终有可见反馈；拿到闸后不再重复发。
            while True:
                try:
                    await rout.asyncio.wait_for(
                        session.gate.acquire(), timeout=rout._QUEUE_NOTICE_SECONDS
                    )
                    break
                except TimeoutError:
                    yield _sse_data({"type": "queued", "message": "上一轮正在收尾，已排队等待…"})
            try:
                # 拿到闸才把这一轮的摘要 / 附件写进会话：此刻这个会话归这一轮独占，
                # 排队期间不会覆盖上一轮正在用的进度与图片。
                session.study_context = parsed_study
                session.pending_attachments = attachments
                # 第一帧先把"这一轮写在哪个会话"告诉前端：id 可能因打不开而被换成新的
                yield _sse_data({"type": "session_bound", "session_id": session.session_id})
                gen = session.loop.run(message)
                try:
                    # 排队告知 / 静默期心跳保活 / 异常转人话 error 帧，统一在公共包装里
                    # 顺带记录"这一轮用户有没有打字"：纯工具轮（只传图、只点按钮）不值得提炼（D27）
                    had_prose = bool(message and message.strip())
                    async for frame in _sse_run_frames(gen, busy=session.loop.busy):
                        yield frame
                    # 一轮结束：问调度器要不要跑后台提炼。不阻塞——差事丢给后台任务。
                    rout.logger.info("记忆钩子：轮结束，准备问调度器（had_prose=%s）", had_prose)
                    rout._tick_memory_scheduler(had_prose=had_prose)
                finally:
                    # 客户端断开 / 取消：显式关掉 run 生成器，让 _run_lock 立即释放；否则锁要等
                    # 异步生成器被 GC 才释放，期间切换 / 删除会话全部 409（锁泄漏）
                    try:
                        await gen.aclose()
                    except Exception:
                        pass
            finally:
                # 闸必须还回去：取消（GeneratorExit）/ 异常 / 正常收尾都走这条
                session.gate.release()
        finally:
            # 解绑 + 记账：请求结束后这个会话不再被"在飞"占用（淘汰 / 删除才敢动它）
            session.in_flight = max(session.in_flight - 1, 0)
            rout._deactivate(token)

    session.in_flight += 1  # 从这一刻起到这个流结束，这个会话都算"有人在用"
    return rout.StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


__all__ = [
    "_sse_data",
    "_NET_HINTS",
    "_humanize_stream_error",
    "_sse_run_frames",
    "AskUserAnswerRequest",
    "answer_ask_user",
    "cancel",
    "ask",
]
