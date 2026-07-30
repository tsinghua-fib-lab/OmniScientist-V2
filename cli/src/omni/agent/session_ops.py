"""Session branching helpers (P2).

Kept out of :mod:`omni.agent.orchestrator` so the orchestrator stays a lean
coordinator (see the architecture guard in
``tests/agent/test_contract_driven_boundaries.py``). The orchestrator resolves
the source session and owns the principal cache; the transcript copy itself
lives here.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from omni.storage.models import ConversationMessageORM, SessionORM

__all__ = ["copy_session_branch"]


async def copy_session_branch(
    db: Any, project_name: str, src: SessionORM, *,
    up_to_message: str = "", title: str = "",
) -> str:
    """Branch ``src`` into a new session, copying its transcript; return its id.

    Copies the source's conversation messages (chronological) into a fresh
    session so a conversation can *branch* — after which the two sessions evolve
    independently (writing in one never touches the other). ``up_to_message`` (an
    id or unique prefix) truncates the copy at that message inclusive, so a
    caller can rewind then diverge. Original message timestamps are preserved so
    replay order is faithful; ``forked_from`` links the branch back to its source.
    """
    async with db.session() as s:
        rows = list((await s.execute(
            select(ConversationMessageORM)
            .where(ConversationMessageORM.session_id == src.id)
            .order_by(ConversationMessageORM.created_at.asc())
        )).scalars().all())
        if up_to_message:
            cut: list[ConversationMessageORM] = []
            for m in rows:
                cut.append(m)
                if m.id == up_to_message or m.id.startswith(up_to_message):
                    break
            rows = cut
        new = SessionORM(
            project=project_name,
            channel=src.channel,
            external_key=src.external_key,
            title=title or (f"{src.title} (fork)" if src.title else "fork"),
            forked_from=src.id,
        )
        s.add(new)
        await s.flush()  # assign new.id before copying children
        new_id = new.id
        for m in rows:
            s.add(ConversationMessageORM(
                session_id=new_id,
                role=m.role,
                content=m.content,
                content_type=m.content_type,
                name=m.name,
                tool_call_id=m.tool_call_id,
                meta=dict(m.meta or {}),
                created_at=m.created_at,
            ))
        await s.commit()
    return new_id
