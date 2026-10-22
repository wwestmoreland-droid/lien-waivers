from enum import StrEnum


class WaiverType(StrEnum):
    CONDITIONAL_PARTIAL = "CONDITIONAL_PARTIAL"
    UNCONDITIONAL_PARTIAL = "UNCONDITIONAL_PARTIAL"
    CONDITIONAL_FINAL = "CONDITIONAL_FINAL"
    UNCONDITIONAL_FINAL = "UNCONDITIONAL_FINAL"

    @property
    def label(self) -> str:
        return {
            "CONDITIONAL_PARTIAL": "Conditional partial",
            "UNCONDITIONAL_PARTIAL": "Unconditional partial",
            "CONDITIONAL_FINAL": "Conditional final",
            "UNCONDITIONAL_FINAL": "Unconditional final",
        }[self.value]

    @property
    def is_final(self) -> bool:
        return self in (WaiverType.CONDITIONAL_FINAL, WaiverType.UNCONDITIONAL_FINAL)


class WaiverStatus(StrEnum):
    NEEDS_REVIEW = "needs_review"  # blocked: e.g. statutory form missing
    DRAFT = "draft"                # PDF ready, waiting to be sent (Outbox in Phase 3)
    SENT = "sent"
    RECEIVED = "received"          # signed copy in, not yet checked
    ACCEPTED = "accepted"
    REJECTED = "rejected"          # returned copy rejected; waiting on a corrected one
    VOID = "void"


OPEN_STATUSES = {WaiverStatus.NEEDS_REVIEW, WaiverStatus.DRAFT, WaiverStatus.SENT, WaiverStatus.RECEIVED,
                 WaiverStatus.REJECTED}
