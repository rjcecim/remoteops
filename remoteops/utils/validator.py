
from PyQt6.QtGui import QValidator


class AffinityValidator(QValidator):
    """
    Validador customizado para máscaras de afinidade de CPU.
    Permite números inteiros separados por vírgula, sem espaços.
    """

    def __init__(self, max_cpu: int, parent=None):
        super().__init__(parent)
        self.max_cpu = max_cpu

    def validate(self, input_text: str, pos: int) -> tuple[QValidator.State, str, int]:
        """
        Valida o texto de entrada para máscara de afinidade.

        Args:
            input_text (str): Texto a ser validado
            pos (int): Posição do cursor

        Returns:
            tuple[QValidator.State, str, int]: (estado, texto, posição)
        """
        # Se vazio, é aceitável (não especificar afinidade)
        if not input_text.strip():
            return QValidator.State.Acceptable, input_text, pos

        # Verificar se contém apenas números, vírgulas e espaços
        allowed_chars = set("0123456789,")
        if not all(c in allowed_chars for c in input_text):
            return QValidator.State.Invalid, input_text, pos

        # Verificar se não há vírgulas consecutivas ou no início/fim
        if input_text.startswith(",") or input_text.endswith(",") or ",," in input_text:
            return QValidator.State.Invalid, input_text, pos

        # Tentar converter para lista de números
        try:
            cpu_list = [int(x.strip()) for x in input_text.split(',') if x.strip()]

            # Verificar se todos os valores estão no range válido
            for cpu in cpu_list:
                if cpu < 1 or cpu > self.max_cpu:
                    return QValidator.State.Invalid, input_text, pos

            # Verificar se não há duplicatas
            if len(cpu_list) != len(set(cpu_list)):
                return QValidator.State.Invalid, input_text, pos

            return QValidator.State.Acceptable, input_text, pos

        except ValueError:
            return QValidator.State.Invalid, input_text, pos

    def fixup(self, input_text: str) -> str:
        """
        Corrige automaticamente o texto de entrada.

        Args:
            input_text (str): Texto a ser corrigido

        Returns:
            str: Texto corrigido
        """
        if not input_text.strip():
            return ""

        try:
            # Converter para lista de números
            cpu_list = [int(x.strip()) for x in input_text.split(',') if x.strip()]

            # Filtrar valores válidos e remover duplicatas
            valid_cpus = []
            for cpu in cpu_list:
                if 1 <= cpu <= self.max_cpu and cpu not in valid_cpus:
                    valid_cpus.append(cpu)

            # Retornar formatado
            return ",".join(map(str, sorted(valid_cpus)))

        except ValueError:
            return ""
