"""Local admission policy for advisory output. Provider messages have no action capability."""
from collections import deque

from activity_protocol import Classification, Recommendation

KINDS = frozenset({'START', 'DEVIATION', 'NEXT_STEP', 'CLARIFY', 'RETURN', 'DIFFICULTY', 'BREAK', 'SUMMARY'})
INSTRUCTION = (
    'Предлагай короткую естественную реплику на русском только при конкретном поводе. '
    'Учитывай цель, текущее наблюдение и недавние изменения. Предложи один посильный шаг. '
    'Без оценок личности, оскорблений, угроз или унижений. Обычно сохраняй тишину. '
    'Фоновые приложения и музыка сами по себе не доказывают отвлечение. '
    'При устойчивом отклонении учитывай его длительность и историю предупреждений. '
)


class CommentaryGate:
    def __init__(self, config, cooldown):
        self.proactive = bool(config.get('live_proactive', False))
        self.cooldown = cooldown
        self.last = float('-inf')
        self.sprint_key = None
        self.warnings = deque(maxlen=5)
        self.started = False
        self.deviations = 0
        try:
            self.max_warnings = 1 + max(0, min(10, int(config.get('voicepack_max_repeats', 2))))
            self.repeat_interval = max(cooldown, float(config.get('voicepack_repeat_interval', 120)))
        except (TypeError, ValueError, OverflowError):
            self.max_warnings, self.repeat_interval = 3, max(120, cooldown)
        self.last_deviation = float('-inf')

    def observe(self, key, classification=None):
        if key != self.sprint_key:
            self.sprint_key = key
            self.warnings.clear()
            self.started = False
            self.deviations = 0
            self.last_deviation = float('-inf')
        if classification in (Classification.ON_TASK, Classification.INDIRECTLY_RELATED):
            self.deviations = 0

    def record(self, now, kind):
        self.warnings.append((now, kind))

    def admit(self, kind, result, now, *, active, current, enabled, busy, confirmed,
              min_confidence, requested=False, returned=False):
        if (kind not in KINDS or not active or not current or not enabled or busy
                or now - self.last < self.cooldown or not (requested or self.proactive) or not result):
            return False
        if not requested and kind == 'START' and self.started:
            return False
        if not requested and kind == 'DEVIATION' and (
            self.deviations >= self.max_warnings or self.deviations and now - self.last_deviation < self.repeat_interval
        ):
            return False
        if kind == 'CLARIFY':
            allowed = (result.classification == Classification.UNCERTAIN
                       and result.recommended_action == Recommendation.REQUEST_CLARIFICATION)
        elif kind == 'DEVIATION':
            allowed = confirmed and result.confidence >= min_confidence
        elif kind in ('BREAK', 'DIFFICULTY'):
            allowed = (requested or confirmed) and result.confidence >= min_confidence
        elif kind == 'RETURN':
            allowed = returned and result.classification == Classification.ON_TASK and result.confidence >= min_confidence
        else:
            allowed = result.confidence >= min_confidence and (requested or kind == 'START')
        if allowed:
            self.last = now
            if kind == 'START':
                self.started = True
            if kind == 'DEVIATION':
                self.deviations += 1
                self.last_deviation = now
        return allowed
