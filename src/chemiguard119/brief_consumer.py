"""Backend/Front를 수정하지 않는 참조 소비자. revision 저장 책임은 실제 Backend에 있다."""

from chemiguard119.action_models import BriefResponse


class BriefConsumer:
    def __init__(self) -> None:
        self.active: tuple[str, int, str] | None = None
        self.snapshot: BriefResponse | None = None

    def activate(self, incident_id: str, revision: int, request_id: str) -> None:
        """새 요청을 보내기 전에 호출한다. 기존 카드를 즉시 제거하며 재사용하지 않는다."""
        self.active = (incident_id, revision, request_id)
        self.snapshot = None

    def accept(self, payload: dict) -> bool:
        incoming = BriefResponse.model_validate(payload)
        if (
            incoming.incident_id,
            incoming.revision,
            incoming.request_id,
        ) != self.active:
            return False
        if self.snapshot:
            if self.snapshot.state_fingerprint != incoming.state_fingerprint:
                return False
            if self.snapshot.phase == "final" or incoming.phase == "initial":
                return False
        self.snapshot = incoming  # 전체 snapshot 교체. 카드 배열 append 금지.
        return True
