"""Local confidence and persistence gate for focus advice."""

from activity_protocol import Classification
from activity_context import setting
from focus_meter import ActivityState


class DecisionEngine:
    def __init__(self, config):
        self.min_confidence = setting(config, 'activity_min_confidence', .5, .99)
        self.confirmation = setting(config, 'activity_confirmation_seconds', 2, 60)
        self.reset()

    def reset(self, *, keep_correction=False):
        self.first_off = None
        self.confirmations = 0
        self.last = None
        if not keep_correction:
            self.corrected = None

    def correct(self, now, signature):
        self.reset()
        self.corrected = (signature, now + 120)

    def evaluate(self, now, result, result_at, signature):
        if self.corrected and self.corrected[0] == signature and now < self.corrected[1]:
            return ActivityState.UNCERTAIN, 0, False
        if result is None or result.confidence < self.min_confidence:
            self.first_off, self.confirmations, self.last = None, 0, None
            return ActivityState.UNCERTAIN, 0, False
        if result.classification == Classification.OFF_TASK and result.relevance <= .40:
            if self.last != result_at:
                self.confirmations += 1
                self.last = result_at
            if self.first_off is None:
                self.first_off = now
            duration = now - self.first_off
            factor = result.confidence * (1 - result.relevance) * min(1, .6 + duration / 40)
            return ActivityState.OFF_TASK, factor, self.confirmations >= 2 and duration >= self.confirmation
        self.first_off, self.confirmations, self.last = None, 0, None
        if result.classification == Classification.ON_TASK:
            return ActivityState.ON_TASK, result.confidence, False
        if result.classification == Classification.INDIRECTLY_RELATED:
            return ActivityState.INDIRECTLY_RELATED, result.confidence * .5, False
        return ActivityState.UNCERTAIN, 0, False
