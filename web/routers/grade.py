"""grade 路由（独立模块，app.include_router 挂载）。"""

from typing import Literal

from fastapi import APIRouter

from .. import runtime as rout
from ..deps import state

router = APIRouter()


@router.get("/api/recheck/quota")
async def recheck_quota() -> dict:
    """当前会话的复核配额（只读）。前端点「驳回」前先看一眼，超限就降级为剔除。"""
    rout._assert_runtime_ready()
    settings = rout.load_settings()
    used = state.recheck_ledger.get(state.session_id or "", {"per_item": {}, "total": 0})
    return _recheck_quota_payload(settings, used)


def _recheck_quota_payload(settings: rout.Settings, used: dict) -> dict:
    """配额出参单点：quota 端点与 /api/recheck 回包里的 quota 必须长一个样。"""
    per_used: dict[str, int] = dict(used["per_item"])
    return {
        "per_item_max": settings.recheck_max_rounds,
        "total_max": settings.recheck_max_total,
        "total_used": used["total"],
        "total_remaining": max(settings.recheck_max_total - used["total"], 0),
        "per_item_used": per_used,
        "per_item_remaining": {
            k: max(settings.recheck_max_rounds - v, 0) for k, v in per_used.items()
        },
    }


class RecheckApiRequest(rout.BaseModel):
    """批改台「驳回」直连复核：前端把这一条原文对象 + 全文送来，理由可空。"""

    text: str
    detection: dict
    note: str = ""


@router.post("/api/recheck")
async def recheck_one(req: RecheckApiRequest) -> dict:
    """对单条批改建议做一次复核，**不经过对话、不经过主模型转达**。

    前端在批改台点「驳回」直接调这里：round 由服务端按本会话账本自动算
    （配额闸也据此拦超限），复核结果结构化回给界面就地更新卡片。
    复核工具仍走注册表里套了配额闸的 recheck_detection，与对话路径同一套纪律。
    """
    rout._assert_runtime_ready()
    assert state.registry is not None
    if not req.text.strip():
        raise rout.HTTPException(status_code=422, detail="待复核的文本为空，无法复核")
    if state.registry.get("recheck_detection") is None:
        # 自定义纯文本端点等未注册复核工具的装配：界面本不该出现驳回，这是兜底
        raise rout.HTTPException(status_code=503, detail="当前模型没有单条复核能力")

    settings = rout.load_settings()
    # 复核是桌面端「刚批改完立刻驳回」的跟随动作，落在最近活跃会话的账本上；
    # 真正的并发场景（模型在跑时调复核工具）在工具内部按本轮会话 id 记，
    # 见 _QuotaGuardedRecheckTool。
    sid = state.session_id or "unknown"
    used = state.recheck_ledger.setdefault(sid, {"per_item": {}, "total": 0})
    det_id = str(req.detection.get("id") or "")
    if (
        used["total"] >= settings.recheck_max_total
        or used["per_item"].get(det_id, 0) >= settings.recheck_max_rounds
    ):
        raise rout.HTTPException(
            status_code=409,
            detail="这条的复核次数用完了，可以采纳、自己改，或放弃这条。",
        )

    # 单条第几轮由账本决定，不信前端传值（防绕过配额）
    round_ = used["per_item"].get(det_id, 0) + 1
    result = await state.registry.execute(
        "recheck_detection",
        {"text": req.text, "detection": req.detection, "round": round_, "note": req.note.strip()},
    )
    if not result.ok:
        # 工具自身失败（模型超时 / 格式坏）不算次数（配额闸在成功后才计数）
        raise rout.HTTPException(
            status_code=502, detail=str(result.error or "复核没跑完，请稍后再试")
        )

    used_after = state.recheck_ledger.get(sid, {"per_item": {}, "total": 0})
    return {
        "ok": True,
        "recheck": result.output,
        "quota": _recheck_quota_payload(settings, used_after),
    }


@router.post("/api/recheck/reset")
async def recheck_reset() -> dict:
    """清空当前会话的复核账本。UI 在收到新一批 detections（run 2 完成）时调一次。"""
    rout._assert_runtime_ready()
    state.recheck_ledger.pop(state.session_id or "", None)
    return {"ok": True}


# ---------- 定稿端点（MSG-010 ④：直调 review.py 纯函数，不经过模型） ----------


class FinalizeRequest(rout.BaseModel):
    text: str
    detections: list[dict]
    # 对每条的表态：accepted / rejected / edited / cancelled（非法值 422）
    decisions: dict[str, Literal["accepted", "rejected", "edited", "cancelled"]]
    # edited 时人自己的改法（id → 新文本）
    overrides: dict[str, str] | None = None
    # 整体评语（原样附在清单末尾，不经过模型）
    comment: str = ""
    # 手改过的条目 id 列表（清单里标「手动修改」）
    edited_ids: list[str] | None = None
    # 本次定稿属于哪个会话（缺省落到当前活跃会话）与哪次批改（v2.1 #69，§8.6）。
    # 旧前端两者都不带：定稿照常算并返回，只是不落流水（响应 finalize_record 说明原因）。
    session_id: str | None = None
    grade_call_id: str | None = None


async def _append_finalize_to_store(
    store: rout.SessionStore,
    *,
    grade_call_id: str,
    decisions: dict[str, str],
    overrides: dict[str, str],
    edited_ids: list[str],
    kept_ids: list[str],
    comment: str,
    final_text: str,
) -> None:
    """把定稿记录只追加进一个**当前没打开**的旧会话流水（不起 LLM runtime）。

    seq 接文件末尾最大值 +1（与活跃 history 的 seq 口径一致）；锁按文件路径共享，
    不会与别的写者行内穿插。
    """
    read = store.read_records()
    next_seq = max((record.seq for record in read.records), default=0) + 1
    await store.append(
        rout.build_record(
            seq=next_seq,
            ts=rout.local_now().isoformat(),
            payload=rout.FinalizePayload(
                grade_call_id=grade_call_id,
                decisions=decisions,
                overrides=overrides,
                edited_ids=edited_ids,
                kept_ids=kept_ids,
                comment=comment,
                final_text=final_text,
            ),
        )
    )


@router.post("/api/grade/finalize")
async def finalize(req: FinalizeRequest) -> dict:
    """定稿：apply_decisions 算定稿条目（只含采纳/改后采纳，撤销一律剔除），
    finalize_text 把改动套回原文。拍板是人的动作，模型不在场。"""
    try:
        dets = [rout.Detection.model_validate(d) for d in req.detections]
    except rout.ValidationError as exc:
        # 脏数据（字段类型错 / 取值非法）不能变成没有 detail 的 500：
        # 点"定稿"会白屏，之前逐条点的表态全丢。给 422 + 人话。
        raise rout.HTTPException(
            status_code=422, detail=f"批改条目格式不对，定稿没能完成：{exc.errors()[0]['msg']}"
        ) from exc
    warnings: list[str] = []
    kept = rout.apply_decisions(dets, req.decisions, req.overrides, warnings)
    final_text, fin_warnings = rout.finalize_text(req.text, kept)
    marks, mark_warnings = rout.build_marked_segments(req.text, kept)
    edited = set(req.edited_ids or [])
    result_text = rout.build_result_text(req.text, kept, comment=req.comment, edited_ids=edited)
    response: dict = {
        "final_text": final_text,
        "result_text": result_text,
        "marks": [m.model_dump(mode="json") for m in marks],
        "kept": [d.model_dump(mode="json") for d in kept],
        "warnings": warnings + fin_warnings + mark_warnings,
    }

    # 定稿落流水（v2.1 #69 / A1，方案 §8.6）：只追加、不进 messages()。
    response["finalize_record"] = await _persist_finalize(
        req, kept, final_text, response["warnings"]
    )
    return response


async def _persist_finalize(
    req: FinalizeRequest, kept: list[rout.Detection], final_text: str, warnings: list[str]
) -> str:
    """按 §8.6 决定这次定稿要不要落、落到哪。返回给前端的状态标记。

    - 不带 ``grade_call_id``（旧前端）：不落，``skipped_no_call_id``；
    - 没有任何采纳成果（整单撤销 / 全拒绝，kept 为空）：不落，``skipped_nothing_kept``；
    - 活跃会话走内存 history（seq / 缓存一致）；非活跃会话用独立 store 只追加；
    - 落账失败不洗白已定好的成果：返回 skipped 并加一条人话 warning，不把 200 变 500。
    """
    if not req.grade_call_id:
        return "skipped_no_call_id"
    kept_ids = [d.id for d in kept if d.id]
    if not kept_ids:
        return "skipped_nothing_kept"

    sid = req.session_id or state.session_id
    fields = dict(
        grade_call_id=req.grade_call_id,
        decisions=dict(req.decisions),
        overrides=dict(req.overrides or {}),
        edited_ids=list(req.edited_ids or []),
        kept_ids=kept_ids,
        comment=req.comment,
        final_text=final_text,
    )

    # 该会话若还在池里（= 活跃会话）且手里是真 SessionHistory：直接走内存对象
    # （seq 与缓存都一致）。池里查而不是比"当前会话"，是因为现在可以有多个活跃会话。
    session = state.sessions.get(sid) if sid else None
    if session is not None:
        live_history = session.history
    elif sid and sid == state.session_id:
        live_history = state.history  # 兼容路径：并发改造前的老调用方 / 测试替身
    else:
        live_history = None
    if live_history is not None and hasattr(live_history, "record_finalize"):
        await live_history.record_finalize(**fields)
        return "appended"

    # 非活跃会话：校验编号后用独立 store 只追加，不起 runtime。
    if not sid:
        return "skipped_no_session"
    try:
        rout._safe_session_id(sid)
        store = rout.SessionStore(rout.session_path(sid))
    except rout.HTTPException:
        raise
    except Exception as exc:  # 定位会话文件失败不该丢掉定稿成果
        warnings.append(f"定稿没能存入历史记录：{exc}")
        return "skipped_store_error"
    try:
        await _append_finalize_to_store(store, **fields)
    except (rout.SessionNotFoundError, rout.SessionCorruptError, rout.SessionWriteError) as exc:
        warnings.append(f"定稿没能存入历史记录：{exc}")
        return "skipped_store_error"
    finally:
        store.close()
    return "appended"

__all__ = [
    "recheck_quota",
    "_recheck_quota_payload",
    "RecheckApiRequest",
    "recheck_one",
    "recheck_reset",
    "FinalizeRequest",
    "_append_finalize_to_store",
    "finalize",
    "_persist_finalize",
]
