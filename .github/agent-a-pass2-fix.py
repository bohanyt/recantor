from pathlib import Path

path = Path("apps/api/src/recantor/stt_jobs.py")
text = path.read_text()
text = text.replace(
    "from sqlalchemy import and_, func, or_, select",
    "from sqlalchemy import String, and_, cast, func, or_, select",
)
text = text.replace(
    "current = await db.scalar(select(func.now()))",
    "current = await db.scalar(select(func.clock_timestamp()))",
)
old = """    async with get_sessionmaker()() as db, db.begin():
        current = now or await _database_now(db)
        job = await db.scalar(
            select(STTJob).where(STTJob.utterance_id == utterance_id).with_for_update()
        )
        if job is None:
            return None

        if await _canonical_exists(
"""
new = """    async with get_sessionmaker()() as db, db.begin():
        job = await db.scalar(
            select(STTJob).where(STTJob.utterance_id == utterance_id).with_for_update()
        )
        if job is None:
            return None
        current = now or await _database_now(db)

        if await _canonical_exists(
"""
if old not in text:
    raise SystemExit("claim clock insertion point not found")
text = text.replace(old, new, 1)

start = text.index("async def _converge_canonical(batch_size: int) -> int:\n")
end = text.index("\ndef _eligible_expression", start)
replacement = '''async def _converge_canonical(batch_size: int) -> int:
    transcript_key = func.concat("utterance:", cast(STTJob.utterance_id, String))
    async with get_sessionmaker()() as db, db.begin():
        jobs = list(
            (
                await db.scalars(
                    select(STTJob)
                    .join(
                        TranscriptSegment,
                        and_(
                            TranscriptSegment.session_id == STTJob.session_id,
                            TranscriptSegment.producer_key == transcript_key,
                        ),
                    )
                    .where(STTJob.state != STTJobState.SUCCEEDED.value)
                    .order_by(STTJob.updated_at, STTJob.utterance_id)
                    .limit(batch_size)
                    .with_for_update(of=STTJob, skip_locked=True)
                )
            ).all()
        )
        for job in jobs:
            _mark_succeeded(job)
        return len(jobs)

'''
text = text[:start] + replacement + text[end + 1 :]

marker = '''def _eligible_expression(current: datetime, cooldown_before: datetime):
    state_eligible = or_(
        STTJob.state == STTJobState.PENDING.value,
        and_(
            STTJob.state == STTJobState.RETRY_WAIT.value,
            STTJob.next_attempt_at.is_not(None),
            STTJob.next_attempt_at <= current,
        ),
        and_(
            STTJob.state == STTJobState.CLAIMED.value,
            STTJob.claim_expires_at.is_not(None),
            STTJob.claim_expires_at <= current,
        ),
    )
    dispatch_eligible = or_(
        STTJob.last_delivery_attempt_at.is_(None),
        STTJob.last_delivery_attempt_at <= cooldown_before,
    )
    return and_(state_eligible, dispatch_eligible)


'''
if marker not in text:
    raise SystemExit("eligible expression block not found")
extra = marker + '''def _outstanding_expression(current: datetime, cooldown_before: datetime):
    active_claim = and_(
        STTJob.state == STTJobState.CLAIMED.value,
        STTJob.claim_expires_at.is_not(None),
        STTJob.claim_expires_at > current,
    )
    recently_published_unclaimed = and_(
        STTJob.last_delivery_attempt_at.is_not(None),
        STTJob.last_delivery_attempt_at > cooldown_before,
        or_(
            STTJob.state == STTJobState.PENDING.value,
            and_(
                STTJob.state == STTJobState.RETRY_WAIT.value,
                STTJob.next_attempt_at.is_not(None),
                STTJob.next_attempt_at <= current,
            ),
            and_(
                STTJob.state == STTJobState.CLAIMED.value,
                STTJob.claim_expires_at.is_not(None),
                STTJob.claim_expires_at <= current,
            ),
        ),
    )
    return or_(active_claim, recently_published_unclaimed)


'''
text = text.replace(marker, extra, 1)

start = text.index("async def _fair_candidates(\n")
end = text.index("\n\nasync def _mark_delivery_attempt", start)
replacement = '''async def _fair_candidates(
    *,
    now: datetime,
    limit: int,
    per_session_limit: int,
    cooldown_seconds: float,
) -> list[UUID]:
    cooldown_before = now - timedelta(seconds=cooldown_seconds)
    async with get_sessionmaker()() as db:
        outstanding_expression = _outstanding_expression(now, cooldown_before)
        global_outstanding = int(
            await db.scalar(
                select(func.count(STTJob.utterance_id)).where(outstanding_expression)
            )
            or 0
        )
        available_global = max(0, limit - global_outstanding)
        if available_global == 0:
            return []

        outstanding = (
            select(
                STTJob.session_id.label("session_id"),
                func.count(STTJob.utterance_id).label("outstanding_count"),
            )
            .where(outstanding_expression)
            .group_by(STTJob.session_id)
            .subquery()
        )
        session_history = (
            select(
                STTJob.session_id.label("session_id"),
                func.max(STTJob.last_delivery_attempt_at).label("last_delivery_attempt_at"),
            )
            .group_by(STTJob.session_id)
            .subquery()
        )
        session_rank = func.row_number().over(
            partition_by=STTJob.session_id,
            order_by=(
                TranscriptionUtterance.sequence,
                STTJob.created_at,
                STTJob.utterance_id,
            ),
        )
        ranked = (
            select(
                STTJob.utterance_id.label("utterance_id"),
                session_rank.label("session_rank"),
                STTJob.session_id.label("session_id"),
            )
            .join(
                TranscriptionUtterance,
                TranscriptionUtterance.id == STTJob.utterance_id,
            )
            .where(_eligible_expression(now, cooldown_before))
            .subquery()
        )
        outstanding_count = func.coalesce(outstanding.c.outstanding_count, 0)
        rows = (
            await db.execute(
                select(ranked.c.utterance_id)
                .outerjoin(outstanding, outstanding.c.session_id == ranked.c.session_id)
                .outerjoin(
                    session_history,
                    session_history.c.session_id == ranked.c.session_id,
                )
                .where(
                    outstanding_count < per_session_limit,
                    ranked.c.session_rank <= per_session_limit - outstanding_count,
                )
                .order_by(
                    ranked.c.session_rank,
                    session_history.c.last_delivery_attempt_at.asc().nulls_first(),
                    ranked.c.session_id,
                    ranked.c.utterance_id,
                )
                .limit(available_global)
            )
        ).all()
        return [row.utterance_id for row in rows]
'''
text = text[:start] + replacement + text[end:]

old = '''    for utterance_id in candidates:
        try:
            enqueue(utterance_id)
        except Exception:
            enqueue_failures += 1
        else:
            dispatched += 1
        finally:
            # Delivery is only a hint. This timestamp throttles publish storms, then expires;
            # PostgreSQL unfinished state remains eligible for later reconciliation.
            await _mark_delivery_attempt(utterance_id, current)
'''
new = '''    for utterance_id in candidates:
        # Reserve the expiring admission hint before broker publication. A crash or publish
        # failure can delay this work only until the hint TTL expires; PostgreSQL remains truth.
        await _mark_delivery_attempt(utterance_id, current)
        try:
            enqueue(utterance_id)
        except Exception:
            enqueue_failures += 1
        else:
            dispatched += 1
'''
if old not in text:
    raise SystemExit("delivery loop block not found")
text = text.replace(old, new, 1)
path.write_text(text)
