"""sessions 路由（独立模块，app.include_router 挂载）。"""

from fastapi import APIRouter

from .. import runtime as rout
from ..deps import state

router = APIRouter()


class OpenSessionRequest(rout.BaseModel):
    session_id: str


class BatchDeleteRequest(rout.BaseModel):
    session_ids: list[str]

@router.get("/api/session/current")
async def current_session() -> dict:
    """当前会话 id。"""
    rout._assert_runtime_ready()
    return {"session_id": state.session_id}


@router.get("/api/sessions")
async def list_sessions() -> dict:
    """列出历史会话（最新在前），含 title（首条用户消息）等摘要。

    一条 user_message 都没有的会话**不列**：它们只可能来自各处自动兜底
    （启动自建 / 删除当前会话后重开），界面上只会顶着「未命名会话」的名头
    当噪音（2026-10-12 人反馈）。判据是"开没开过口"，不是 title 是否为空——
    只发图没打字的会话有 user_message 记录，照列。
    """
    rout._assert_runtime_ready()
    sessions = []
    for summary in rout.iter_session_summaries():
        title = rout._first_user_message(summary)
        if title is None:
            continue  # 从没说过话的空会话：不进列表
        sessions.append(
            {
                "session_id": summary.session_id,
                "title": title,
                "created_at": summary.created_at,
                "model": summary.model,
                "is_current": summary.session_id == state.session_id,
            }
        )
    return {"sessions": sessions}


@router.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str) -> dict:
    """读取一个会话的用户/助手消息序列，供前端回灌历史气泡。"""
    rout._assert_runtime_ready()
    rout._safe_session_id(session_id)
    store = rout.SessionStore(rout.session_path(session_id))
    try:
        result = store.read_records()
    except rout.SessionNotFoundError as exc:
        raise rout.HTTPException(status_code=404, detail=str(exc)) from exc
    except rout.SessionCorruptError as exc:
        raise rout.HTTPException(status_code=400, detail=str(exc)) from exc

    messages: list[dict] = []
    for rec in result.records:
        rt = rec.record_type
        if rt == rout.RecordType.USER_MESSAGE:
            messages.append({"role": "user", "text": rec.payload.get("text", "")})
        elif rt == rout.RecordType.ASSISTANT_MESSAGE:
            messages.append({"role": "assistant", "text": rec.payload.get("text", "")})
    replay = store.read_replay()
    return {
        "messages": messages,  # 老字段，形状不变（role/text），现有前端不受影响
        # 新字段：含工具结果。批改面板就靠它恢复——不用重新跑一遍 grade_text。
        # 每条：kind=user/assistant/tool/status/finalize；tool 项带 output（结构化结果）
        # 与 arguments（原文），status 保留取消/失败事实，finalize 是用户定稿区块。
        "replay": [item.model_dump(mode="json") for item in replay.items],
        "skipped_lines": replay.skipped_lines,
        # 会话最后一轮的真实 input_tokens：前端切回历史会话时用量环靠它回显
        "last_input_tokens": replay.last_input_tokens,
        # 本会话逐轮相加的用量（含命中缓存）：前端切回历史会话时状态条靠它回显，
        # 老会话没记过用量时为 null —— 界面整行不显示，不显示假的 0
        "usage": replay.usage.model_dump(mode="json") if replay.usage is not None else None,
    }


@router.get("/api/context/composition")
def context_composition() -> dict:
    """当前会话的上下文构成（环上点开的「上下文检查」用）。

    口径：走 session 层现成的称重（ContextStats.parts 七个桶）+ 保守的字节→token 估算
    （composition_tokens，单点）。**只观测，一个字都不改**——这套东西本来就是为"看窗口
    被谁占着"建的（见 compaction.py 模块说明），只是此前没接到界面上。

    真实总量不在这里给：那是模型回的 context_input_tokens（前端从 result 事件拿）。
    两者配合着看——总量看实测，比例看估算。
    """
    rout._assert_runtime_ready()
    assert state.history is not None
    stats = state.history.context_stats()
    parts = rout.composition_tokens(stats)
    # 工具定义是**另外发**的（不在 messages 里），session 层那次称重量不到它——
    # 而它每次请求都发、且在固定前导里占大头，所以这里单独量一笔补上。
    assert state.registry is not None
    tools_bytes = len(rout.json.dumps(state.registry.schemas(), ensure_ascii=False).encode("utf-8"))
    parts["tools"] = rout.estimate_tokens_from_bytes(tools_bytes)
    return {
        "parts": parts,
        "estimated_total": sum(parts.values()),
        "total_bytes": stats.total_bytes,
        "total_messages": stats.total_messages,
    }


@router.get("/api/usage/summary")
def usage_summary(days: int = 7) -> dict:
    """跨会话按天的用量汇总（设置页「用量与花费」用）。只读，不改任何东西。

    ``days`` 夹在 1~90：别让一个参数把全部历史都翻出来解析一遍。
    """
    span = max(1, min(days, 90))
    return rout.summarize_sessions(
        rout.sessions_root(), days=span, today=rout.date.today()
    ).to_dict()


@router.post("/api/sessions/new")
async def create_session() -> dict:
    """新建会话并切换过去。若当前有任务在跑则拒绝。"""
    rout._assert_runtime_ready()
    assert state.loop is not None
    if state.loop.busy:
        raise rout.HTTPException(status_code=409, detail="当前有任务正在执行，请稍候再切换会话")
    assert state.settings is not None
    history = await rout.new_session(
        system_prompt=state.system_prompt,
        model=state.settings.model,
        cwd=rout.os.getcwd(),
        context=rout._context_fragments(),
    )
    rout._switch_session(history)
    return {"ok": True, "session_id": history.session_id}


@router.post("/api/sessions/open")
async def open_session_endpoint(req: OpenSessionRequest) -> dict:
    """打开已有会话并切换过去。

    有任务在跑时**不再 409**：前端切会话前已经先取消在途任务，后端这边等它收尾
    再切（最多等几秒）；等不到就如实说忙，绝不让用户点了没反应（2026-10-18 真机：
    刷新后点旧会话没反应，就是这里 409 被前端吞成了一个不明显的 notice）。
    """
    rout._assert_runtime_ready()
    rout._safe_session_id(req.session_id)
    # 已经在池里：直接切过去，绝不再开一份同一个文件的流水。
    # 再开一份会造成三件事：旧句柄泄漏（Windows 上之后删不掉这个文件）、同一个会话出现
    # 两套流水两把闸（"同一会话串行"静默失效）、本轮的进度摘要被重置成空。
    cached = rout._pool_get(req.session_id)
    if cached is not None:
        rout._sync_mirror(cached)
        return {"ok": True, "session_id": cached.session_id}
    if state.loop is not None and state.loop.busy:
        # 前端切会话前已经 abortInFlight 发了取消，这里给一点收尾时间；
        # 真等到超时再如实说忙（用户至少知道"在收尾"，不是点了没反应）。
        for _ in range(rout._OPEN_SESSION_BUSY_WAIT_SECONDS * 10):
            if not state.loop.busy:
                break
            await rout.asyncio.sleep(0.1)
        if state.loop.busy:
            raise rout.HTTPException(status_code=409, detail="上一轮还在收尾，请稍等一秒再切换")
    try:
        history = await rout.open_session(
            req.session_id,
            system_prompt=state.system_prompt,
            context=rout._context_fragments(),
        )
    except rout.SessionNotFoundError as exc:
        raise rout.HTTPException(status_code=404, detail=str(exc)) from exc
    except rout.SessionCorruptError as exc:
        raise rout.HTTPException(status_code=400, detail=str(exc)) from exc
    if history.system_prompt_mismatch:
        sid = req.session_id
        print(f"[markr-web] 会话 {sid} 的 system_prompt 指纹不一致，已用当前提示词覆盖。")
    if history.skipped_lines:
        print(f"[markr-web] 会话 {req.session_id} 打开时跳过了 {history.skipped_lines} 行坏数据。")
    rout._switch_session(history)
    return {"ok": True, "session_id": history.session_id}


@router.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str) -> dict:
    """删除一个会话；若删的是当前会话，自动新建一个并切换过去。"""
    rout._assert_runtime_ready()
    rout._safe_session_id(session_id)
    # 忙检查必须查**要删的这个会话**：池模型下别的会话在跑时镜像可能指向一个空闲会话，
    # 只读 `state.loop.busy` 会答"不忙"，然后把正在写的本子关掉再删文件。
    if rout._session_busy(session_id):
        raise rout.HTTPException(status_code=409, detail="当前有任务正在执行，请稍候再删除会话")
    is_current = state.session_id == session_id
    try:
        # 池里若还持有这个会话，必须先关掉流水句柄才能删文件（Windows 上更明显）
        rout._pool_evict(session_id)
        path = rout.session_path(session_id)
        if not path.exists():
            raise rout.HTTPException(status_code=404, detail=f"会话不存在：{session_id}")
        try:
            path.unlink()
        except OSError as exc:
            raise rout.HTTPException(status_code=500, detail=f"删除失败：{exc}") from exc
        state.title_cache.pop(session_id, None)
        state.recheck_ledger.pop(session_id, None)  # 删会话顺手清账本，不残留
        if is_current:
            return {"deleted": session_id, "new_session_id": await rout._open_fresh_session()}
        return {"deleted": session_id, "new_session_id": None}
    except rout.HTTPException:
        raise  # 业务上的拒绝（404 / 500 / 409）照原样抛，不用兜
    except Exception as exc:
        # 文件已经删了，但新会话没开出来：先尽力退回一个还存在的会话，
        # 实在不行也要把原因说清楚，不能只留一句 503 让人干瞪眼。
        if not await rout._salvage_runtime():
            raise rout.HTTPException(
                status_code=500,
                detail=f"会话已删除，但没能重建运行时（{exc}）。请重启本地后端。",
            ) from exc
        print(f"[markr-web] 删除后重建会话失败（{exc}），已退回现有会话 {state.session_id}。")
        return {"deleted": session_id, "new_session_id": state.session_id}
    finally:
        # 中途岔了（包括 unlink 抛出非 OSError 的意外）会让 _history 停在 None，
        # 那样所有接口都会变成 503「运行时未就绪」，只有重启才救得回来。
        if state.history is None:
            if await rout._salvage_runtime():
                print(f"[markr-web] 删除后运行时已兜回会话 {state.session_id}。")
            else:
                print("[markr-web] 删除后运行时没能恢复，需要重启本地后端。")


@router.post("/api/sessions/batch-delete")
async def batch_delete_sessions(req: BatchDeleteRequest) -> dict:
    """批量删除会话；若当前会话在列表中，自动新建一个并切换过去。"""
    rout._assert_runtime_ready()
    ids = list(dict.fromkeys(req.session_ids))  # 去重保序
    if not ids:
        return {"deleted": [], "new_session_id": None}
    # 同单删：逐条查**它自己**在不在用，有任何一个在跑就整体拒绝
    if any(rout._session_busy(sid) for sid in ids):
        raise rout.HTTPException(status_code=409, detail="当前有任务正在执行，请稍候再删除会话")

    is_current = state.session_id in ids
    deleted: list[str] = []
    new_session_id: str | None = None
    try:
        # 池里若还持有这些会话，先把流水句柄关掉再删文件
        for sid in ids:
            rout._pool_evict(sid)

        for sid in ids:
            if not rout.is_valid_session_id(sid):
                continue  # 形状不对的编号直接忽略，不去碰磁盘
            path = rout.session_path(sid)
            if not path.exists():
                continue
            try:
                path.unlink()
                deleted.append(sid)
                state.title_cache.pop(sid, None)
                state.recheck_ledger.pop(sid, None)  # 删会话顺手清账本，不残留
            except OSError:
                continue

        if is_current:
            new_session_id = await rout._open_fresh_session()
    except Exception as exc:
        if not await rout._salvage_runtime():
            raise rout.HTTPException(
                status_code=500,
                detail=(
                    f"已删除 {len(deleted)} 个会话，但没能重建运行时（{exc}）。请重启本地后端。"
                ),
            ) from exc
        print(f"[markr-web] 批量删除中途出错（{exc}），已退回现有会话 {state.session_id}。")
        new_session_id = state.session_id if is_current else None
    finally:
        # 上面任何一步岔了（包括 unlink 抛出非 OSError 的意外），_history 都可能是 None。
        # 那会让**所有**接口变成 503「运行时未就绪」，只有重启进程才救得回来 ——
        # 一次删除失败不该让整个后端变死，所以这里无条件兜一次。
        if state.history is None:
            if await rout._salvage_runtime():
                print(f"[markr-web] 删除后运行时已兜回会话 {state.session_id}。")
            else:
                print("[markr-web] 删除后运行时没能恢复，需要重启本地后端。")

    return {"deleted": deleted, "new_session_id": new_session_id}


__all__ = [
    "OpenSessionRequest",
    "BatchDeleteRequest",
    "current_session",
    "list_sessions",
    "get_session_messages",
    "context_composition",
    "usage_summary",
    "create_session",
    "open_session_endpoint",
    "delete_session_endpoint",
    "batch_delete_sessions",
]
