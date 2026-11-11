# projeto_mistral

Workflows da Mistral implantados como *managed deployment* no Mistral Cloud.

## github-pr-digest

Resume a atividade de pull requests de um repositório do GitHub e envia o resumo por DM no Slack.

**Entradas**

| Campo | Descrição | Exemplo |
|---|---|---|
| `repo` | Repositório no formato `owner/repo` | `mistralai/mistralai-python` |
| `since` | Data inicial (inclusive), formato `YYYY-MM-DD` | `2025-11-01` |
| `slack_user_id` | Opcional. Usuário que recebe a DM (padrão: você mesmo) | `U0CGP0K9CKX` |

**Saída**: mensagem no Slack com duas seções — PRs mesclados desde a data e PRs abertos aguardando revisão — cada PR em uma linha com autor e link. O resultado da execução também contém contagens e o texto da mensagem.

**Exemplo de execução (Python)**

```python
import os
from mistralai.client import Mistral

client = Mistral(api_key=os.environ["MISTRAL_API_KEY"])
execution = client.workflows.execute_workflow(
    workflow_identifier="github-pr-digest",
    input={"repo": "mistralai/mistralai-python", "since": "2025-11-01"},
    wait_for_result=True,
)
print(execution.result)
```

## Desenvolvimento

```bash
uv sync
uv run pytest
```

O worker roda com `uv run python -m worker.entrypoints.worker` (requer `MISTRAL_API_KEY` no `.env`).
