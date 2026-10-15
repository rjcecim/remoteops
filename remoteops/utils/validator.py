from PyQt6.QtGui import QValidator

from remoteops.utils.cpu_topology import classify_affinity_input


class AffinityValidator(QValidator):
    """Números separados por vírgula. Não apaga processadores informados pelo usuário."""

    def __init__(self, max_cpu: int | None = None, parent=None):
        super().__init__(parent)
        self.max_cpu = max_cpu
        self.group_label = ""
        self.last_reason = ""

    def set_max_cpu(self, max_cpu: int | None) -> None:
        self.max_cpu = max_cpu

    def set_group_label(self, group_label: str) -> None:
        self.group_label = group_label or ""

    def _limit(self) -> int | None:
        if self.max_cpu is None or isinstance(self.max_cpu, bool):
            return None
        try:
            value = int(self.max_cpu)
        except (TypeError, ValueError):
            return None
        if value < 1:
            return None
        return value

    def validate(self, input_text: str, pos: int) -> tuple[QValidator.State, str, int]:
        state, reason = classify_affinity_input(
            input_text or "",
            max_cpu=self._limit(),
            group_label=self.group_label,
        )
        self.last_reason = reason
        mapped = {
            "acceptable": QValidator.State.Acceptable,
            "intermediate": QValidator.State.Intermediate,
            "invalid": QValidator.State.Invalid,
        }[state]
        return mapped, input_text, pos

    def fixup(self, input_text: str) -> str:
        """Só completa uma vírgula final. Não troca o valor por outro executável."""
        text = input_text or ""
        stripped = text.strip()
        squashed = stripped.replace(" ", "")
        if (
            stripped.endswith(",")
            and not squashed.endswith(",,")
            and not squashed.startswith(",")
        ):
            return stripped[:-1].rstrip()
        return text
